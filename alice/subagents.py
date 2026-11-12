"""Alt ajanlar: `task` aracının başlattığı, kendi temiz bağlamıyla çalışan ajanlar (Claude Code'daki Agent aracı gibi).

Eskiden alt ajan sohbeti kilitleyerek tek tek çalışıyor, ana sohbetin ekranına yazıyor ve bitince unutuluyordu:
- Paralel: aynı cevaptaki birden çok `task` çağrısı aynı anda çalışır.
- Arka plan: background=true ile başlar, sohbet sürer; bitince sonucu "[Alt ajan ...]" mesajıyla sohbete düşer.
- Devam: biten alt ajana id ile yeni mesaj yazılabilir; konuşması (okudukları, bulduklarını) korunur.
- Görünürlük: her alt ajanın durumu, adımı ve son aracı yan paneldeki "Ajanlar" sekmesinde canlı görünür; oradan
  durdurulabilir, devam mesajı yazılabilir.

Her alt ajanın kendi araç bağlamı vardır (durum sözlüğü ana sohbetinkinin sığ kopyası): ana sohbette Durdur'a
basmak arka plandaki alt ajanın araçlarını durdurmasın, alt ajanın Durdur'u da ana sohbeti etkilemesin. İzin
soruları alt ajanın adıyla ana sohbetin arayüzüne gider (birer birer).
"""

from __future__ import annotations

import itertools
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from alice.tools.base import ToolContext

KEEP = 30  # sohbet başına hatırlanan alt ajan
LIVE_EVERY = 0.4  # sn: canlı durum en sık bu aralıkla yayınlanır
_perm_lock = threading.Lock()  # paralel alt ajanların izin soruları üst üste binmesin


@dataclass
class Sub:
    id: str
    label: str
    prompt: str
    kind: str = ""  # özel ajan tanımının adı; boş: genel
    background: bool = False
    max_steps: int = 40
    status: str = "running"  # running | done | failed | stopped
    result: str = ""
    steps: int = 0
    last: str = ""
    turns: int = 1
    started: float = field(default_factory=time.time)
    ended: float = 0.0
    child: Any = None
    thread: threading.Thread | None = None
    history: list[dict[str, str]] = field(default_factory=list)  # [{"q": istek, "a": sonuç}]

    def snapshot(self) -> dict[str, Any]:
        now = self.ended or time.time()
        return {"id": self.id, "label": self.label, "kind": self.kind, "background": self.background,
                "status": self.status, "steps": self.steps, "last": self.last, "turns": self.turns,
                "prompt": self.prompt[:600], "result": self.result[:4000], "secs": int(now - self.started),
                "history": [{"q": h["q"][:300], "a": h["a"][:600]} for h in self.history[-5:]]}


class SubUI:
    """Alt ajanın arayüzü: model ve araç olaylarını ana ekrana yazmaz, yalnız durumu günceller. İzin soruları ana
    sohbetin arayüzüne alt ajanın adıyla gider."""

    def __init__(self, sub: Sub, parent_ui: Any, publish: Callable[[dict[str, Any]], Any] | None):
        self.sub, self.parent_ui, self.publish = sub, parent_ui, publish
        self._last = 0.0

    def __getattr__(self, name: str):
        return lambda *a, **k: None

    def changed(self, force: bool = False) -> None:
        now = time.monotonic()
        if self.publish is None or (not force and now - self._last < LIVE_EVERY):
            return
        self._last = now
        try:
            self.publish({"type": "subagent", **self.sub.snapshot()})
        except Exception:  # noqa: BLE001 - arayüz yoksa iş sürsün
            pass

    def tool_start(self, call_id: str, name: str, summary: str) -> None:
        self.sub.steps += 1
        self.sub.last = f"{name} {summary}".strip()[:160]
        self.changed()

    def info(self, msg: str) -> None:
        if "adım limiti" in msg or "hiçbir model" in msg:
            self.sub.last = msg.splitlines()[0][:160]
            self.changed()

    def ask_permission(self, name: str, summary: str, risk: str) -> str:
        ask = getattr(self.parent_ui, "ask_permission", None)
        if ask is None:
            return "n"
        with _perm_lock:
            ans = ask(name, f"[alt ajan {self.sub.id} · {self.sub.label}] {summary}", risk)
        return "y" if ans == "a" else ans  # "her zaman" ana oturuma ait kalsın


def registry(ctx: ToolContext) -> dict[str, Sub]:
    return ctx.state.setdefault("subagents", {})


def _next_id(ctx: ToolContext) -> str:
    counter = ctx.state.setdefault("subagent_ids", itertools.count(1))
    return f"a{next(counter)}"


def child_context(parent_ctx: ToolContext) -> ToolContext:
    """Alt ajanın araç bağlamı: aynı klasör ve yapılandırma, durumun sığ kopyası (tarayıcı, süreçler, yayın kancaları
    paylaşılır), ama `agent` alt ajanın kendisi olur ki Durdur ikisini birbirine karıştırmasın."""
    state = dict(parent_ctx.state)
    # Kendi kayıtları: ön plandaki komutları (Durdur'da öldürülenler) ve tarayıcı meşguliyeti ayrı tutulur.
    for k in ("subagents", "subagent_ids", "fg_procs", "browser_busy"):
        state.pop(k, None)
    return ToolContext(cwd=parent_ctx.cwd, config=parent_ctx.config, router=parent_ctx.router,
                       decider=parent_ctx.decider, state=state, emit=lambda s: None)


def build_child(parent: Any, kind: str, ui: Any, ctx: ToolContext) -> tuple[Any, int]:
    """(alt ajan, varsayılan adım sınırı). kind: özel ajan tanımının adı; boş: ana ajanın kopyası."""
    from alice.agent import Agent

    if kind:
        from alice.skills import load_agents, spawn

        adef = load_agents(parent.ctx.cwd).get(kind.strip().lower())
        if adef is None:
            from alice.tools.base import ToolError

            names = ", ".join(load_agents(parent.ctx.cwd)) or "(yok)"
            raise ToolError(f"'{kind}' adında alt ajan yok. Tanımlı olanlar: {names}")
        child = spawn(parent, adef, ctx=ctx, ui=ui)
        steps = adef.max_steps
    else:
        child = Agent(parent.config, parent.router, list(parent.tools.values()), ctx, parent.permissions, ui,
                      getattr(parent, "decider", None), system_prompt=parent.messages[0]["content"], is_child=True)
        steps = 40
    ctx.state["agent"] = child
    return child, steps


def start(parent: Any, ctx: ToolContext, prompt: str, kind: str = "", label: str = "", background: bool = False,
          max_steps: int | None = None) -> Sub:
    subs = registry(ctx)
    sub = Sub(id=_next_id(ctx), label=(label or kind or prompt.split("\n")[0])[:48].strip() or "alt ajan",
              prompt=prompt, kind=kind, background=background)
    ui = SubUI(sub, getattr(parent, "ui", None), getattr(getattr(parent, "ui", None), "publish", None))
    sub.child, steps = build_child(parent, kind, ui, child_context(ctx))
    sub.max_steps = int(max_steps or steps)
    subs[sub.id] = sub
    for old in [k for k, s in subs.items() if s.status != "running"][:-KEEP]:
        subs.pop(old, None)
    _go(ctx, sub, prompt)
    return sub


def resume(ctx: ToolContext, sub_id: str, text: str, background: bool = False) -> Sub:
    from alice.tools.base import ToolError

    sub = registry(ctx).get(sub_id.strip())
    if sub is None:
        raise ToolError(f"böyle bir alt ajan yok: {sub_id}. Bu sohbettekiler: {', '.join(registry(ctx)) or '(yok)'}")
    if sub.status == "running":
        raise ToolError(f"{sub.id} hâlâ çalışıyor; bitmesini bekle ya da durdur")
    sub.turns += 1
    sub.status, sub.result, sub.ended, sub.background = "running", "", 0.0, background
    sub.started = time.time()
    _go(ctx, sub, text)
    return sub


def _go(ctx: ToolContext, sub: Sub, text: str) -> None:
    ui: SubUI = sub.child.ui
    ui.changed(force=True)
    deliver = ctx.state.get("deliver")

    def run() -> None:
        try:
            out = sub.child.run_turn(text, max_steps=sub.max_steps)
            sub.result = out or "(alt ajan boş cevap döndürdü)"
            sub.status = "stopped" if sub.child.cancel.is_set() else "done"
        except Exception as e:  # noqa: BLE001 - alt ajanın hatası sohbeti düşürmesin
            sub.result, sub.status = f"alt ajan hata verdi: {e}", "failed"
        sub.ended = time.time()
        sub.history.append({"q": text, "a": sub.result})
        ui.changed(force=True)
        if sub.background and sub.status != "stopped" and callable(deliver):
            deliver(f"[Alt ajan {sub.id} · {sub.label} {'bitti' if sub.status == 'done' else 'başarısız'}]\n"
                    f"{sub.result[:6000]}\n\n(Devam ettirmek için: task id={sub.id} prompt=...)")

    sub.thread = threading.Thread(target=run, daemon=True, name=f"alice-sub-{sub.id}")
    sub.thread.start()


def wait(sub: Sub, cancelled: Callable[[], bool]) -> None:
    """Ön planda çalışan alt ajanı bekle; ana sohbette Durdur'a basılırsa alt ajanı da durdur."""
    while sub.thread is not None and sub.thread.is_alive():
        if cancelled():
            stop(sub)
        sub.thread.join(0.25)


def stop(sub: Sub) -> bool:
    if sub.status != "running" or sub.child is None:
        return False
    sub.child.stop()
    return True


def snapshots(ctx: ToolContext) -> list[dict[str, Any]]:
    return [s.snapshot() for s in reversed(list(registry(ctx).values()))]
