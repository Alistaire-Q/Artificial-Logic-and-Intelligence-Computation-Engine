"""Ana ajan döngüsü (Claude Code / Codex tarzı).

user mesajı -> model (araçlarla) -> tool_calls varsa izin kontrolü + çalıştır ->
sonuçları geri ver -> model artık araç çağırmayınca tur biter.
"""

from __future__ import annotations

import json
import re
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Protocol

from alice.config import Config
from alice.decider import Decider
from alice.permissions import Permissions
from alice.prompts import COMPACT_PROMPT, build_system_prompt
from alice.providers.openai_compat import ChatResult, StreamCancelled
from alice.providers.router import AllProvidersFailed, Router, estimate_tokens, strip_images
from alice.tools.base import Tool, ToolContext, ToolError, ToolResult, truncate


class UI(Protocol):
    """Arayüzün (terminal ya da masaüstü uygulaması) ajan olaylarını gösterme sözleşmesi."""

    def stream_start(self) -> None: ...  # model cevabı başlıyor (ilk parça gelene kadar "düşünüyor")
    def stream_delta(self, text: str) -> None: ...
    # Opsiyonel: def thinking_delta(self, text: str) -> None  (modelin akıl yürütme metni)
    def stream_reset(self) -> None: ...  # model akış ortasında düştü, yedek model baştan yazacak
    def stream_end(self, text: str, source: str) -> None: ...
    def tool_start(self, call_id: str, name: str, summary: str) -> None: ...
    def tool_end(self, call_id: str, name: str, result: ToolResult) -> None: ...
    def ask_permission(self, name: str, summary: str, risk: str) -> str: ...  # y | n | a
    def info(self, msg: str) -> None: ...


class NullUI:
    def __init__(self, approve: str = "y"):
        self.approve = approve
        self.events: list[tuple[str, Any]] = []

    def stream_start(self):
        self.events.append(("start", None))

    def stream_delta(self, text):
        self.events.append(("delta", text))

    def stream_reset(self):
        self.events.append(("reset", None))

    def stream_end(self, text, source):
        if text:
            self.events.append(("text", text))

    def tool_start(self, call_id, name, summary):
        self.events.append(("tool", name))

    def tool_end(self, call_id, name, result):
        self.events.append(("result", result.text))

    def ask_permission(self, name, summary, risk):
        self.events.append(("ask", name))
        return self.approve

    def info(self, msg):
        self.events.append(("info", msg))


class Cancelled(Exception):
    pass


class _Sink:
    def __init__(self, ui: UI, cancel: threading.Event):
        self.ui = ui
        self.cancel = cancel
        self.dead = False  # tur durdurulduysa arkada süren model çağrısı artık arayüze yazmasın
        self.parts: list[str] = []

    def on_delta(self, text: str) -> None:
        if self.dead:
            return
        self.parts.append(text)
        self.ui.stream_delta(text)

    def on_reset(self) -> None:
        if self.dead:
            return
        self.parts.clear()
        self.ui.stream_reset()

    def on_reasoning(self, text: str) -> None:
        fn = getattr(self.ui, "thinking_delta", None)
        if fn is not None and not self.dead:
            fn(text)

    def cancelled(self) -> bool:
        return self.dead or self.cancel.is_set()


def _call_key(call: dict[str, Any]) -> str:
    return call["function"]["name"] + ":" + (call["function"].get("arguments") or "").strip()


REPEAT_FAIL_NOTE = ("\n\n[ALICE notu] Bu çağrı aynı argümanlarla {n}. kez başarısız oldu. Aynısını tekrarlama: hatayı oku, "
                    "kök nedeni düşün, farklı bir araç ya da yaklaşım dene; gerçekten yapılamıyorsa kullanıcıya sor.")
ERROR_STREAK_NOTE = ("\n\n[ALICE notu] Son {n} araç çağrısı hata verdi. Devam etmeden dur ve düşün: hata mesajları ne "
                     "söylüyor, varsayımların (ortam, kabuk sözdizimi, dosya yolu, programın kurulu olup olmadığı) doğru mu? "
                     "Yeni bir yol seç ve neden işe yarayacağını kısaca yaz.")
REPEAT_OK_NOTE = ("\n\n[ALICE notu] Aynı çağrıyı {n} kez yaptın ve sonuç ilerlemiyor gibi. Döngüye girme: farklı bir şey "
                  "dene ya da elindeki bilgiyle işi bitir.")


def parse_args(raw: str | dict | None) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if not raw or not raw.strip():
        return {}
    try:
        val = json.loads(raw)
    except json.JSONDecodeError:
        # Bazı modeller sona fazladan metin ya da code fence ekliyor.
        cleaned = re.sub(r"^```(?:json)?|```$", "", raw.strip()).strip()
        m = re.search(r"\{.*\}", cleaned, re.S)
        if not m:
            raise
        val = json.loads(m.group(0))
    if not isinstance(val, dict):
        raise json.JSONDecodeError("argümanlar obje değil", str(raw), 0)
    return val


PLAN_NOTE = ("[PLAN MODU: Şu an sadece okuyup araştırabilirsin; dosya değiştiren, komut çalıştıran ya da ekrana "
             "dokunan araçlar reddedilecek. İşi incele, sonra adım adım net bir uygulama planı yaz ve kullanıcının "
             "onayını bekle.]\n\n")


class Agent:
    def __init__(
        self,
        config: Config,
        router: Router,
        tools: list[Tool],
        ctx: ToolContext,
        permissions: Permissions,
        ui: UI | None = None,
        decider: Decider | None = None,
        system_prompt: str | None = None,
        is_child: bool = False,
    ):
        self.config = config
        self.router = router
        self.tools = {t.name: t for t in tools if not (is_child and t.name == "task")}
        self.ctx = ctx
        self.permissions = permissions
        self.ui = ui or NullUI()
        self.decider = decider
        self.is_child = is_child
        self.role = "brain"  # sesli asistan "voice" kullanır: ilk kelimesi en çabuk gelen modeller
        self.messages: list[dict[str, Any]] = [{"role": "system", "content": system_prompt or build_system_prompt(ctx.cwd)}]
        a = config.agent
        self.max_steps = int(a.get("max_steps", 80))
        self.compact_at = int(a.get("compact_at_tokens", 48000))
        self.output_limit = int(a.get("tool_output_chars", 12000))
        self.keep_images = int(a.get("keep_images", 2))
        self.stream = bool(a.get("stream", True))
        self.cancel = threading.Event()
        self.busy = False
        self._call_counts: dict[str, int] = {}
        self._fail_counts: dict[str, int] = {}
        self._error_streak = 0
        # Her araç adımından sonra çağrılır (oturumu kaydetmek için): tur takılır ya da çökerse iz kaybolmasın.
        self.on_step: Any = None
        # Geri sarma için: kullanıcı mesajının indeksi -> o turda ilk değişen dosyaların eski hali.
        self.checkpoints: dict[int, dict[str, bytes | None]] = {}
        if not is_child:
            ctx.state["agent"] = self

    # ------------------------------------------------------------------ public
    def tool_specs(self) -> list[dict[str, Any]]:
        """Modele gönderilen araçlar: temel araçlar + bu oturumda açılmış gruplar (bkz. tools/groups.py)."""
        if str(self.config.agent.get("tool_groups", "auto")) == "all":
            return [t.spec() for t in self.tools.values()]
        from alice.tools.groups import visible_tools

        return [t.spec() for t in visible_tools(self.tools, self.ctx.state.setdefault("tool_groups", set()))]

    def _enable_groups_for(self, text: str = "", tool_names: tuple[str, ...] = ()) -> None:
        from alice.tools.groups import group_of, groups_for_text

        enabled: set[str] = self.ctx.state.setdefault("tool_groups", set())
        self.ctx.state["tool_names"] = tuple(self.tools)
        enabled |= groups_for_text(text, self.tools)
        enabled |= {g for g in map(group_of, tool_names) if g}

    def spawn_child(self) -> "Agent":
        return Agent(self.config, self.router, list(self.tools.values()), self.ctx, self.permissions, self.ui,
                     self.decider, system_prompt=self.messages[0]["content"], is_child=True)

    def run_turn(self, user_text: str, max_steps: int | None = None, images: list[str] | None = None) -> str:
        from alice.mentions import expand_mentions

        content_text = expand_mentions(user_text, self.ctx.cwd)
        if self.permissions.mode == "plan" and not self.is_child:
            content_text = PLAN_NOTE + content_text
        content: Any = content_text
        if images:
            content = [{"type": "text", "text": content_text}] + [
                {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b}"}} for b in images]
        if not self.is_child:
            self.checkpoints[len(self.messages)] = {}
            self.ctx.state["checkpoint"] = self.checkpoints[len(self.messages)]
        msg: dict[str, Any] = {"role": "user", "content": content}
        if content_text != user_text:
            msg["_display"] = user_text
        if images:
            msg["_user_images"] = len(images)
        self.messages.append(msg)
        self._enable_groups_for(content_text)
        final = ""
        steps = max_steps or self.max_steps
        self.cancel.clear()
        self.busy = True
        self._call_counts.clear()
        self._fail_counts.clear()
        self._error_streak = 0
        sink = _Sink(self.ui, self.cancel)
        try:
            for _ in range(steps):
                if self.cancel.is_set():
                    raise Cancelled
                self._maybe_compact()
                self._prune_images()
                sink.parts.clear()
                self.ui.stream_start()
                res = self._interruptible(self.router.chat, self.role, list(self.messages), tools=self.tool_specs(),
                                          stream=sink if self.stream else None)
                msg = res.message
                if not self.stream and msg.get("content"):
                    self.ui.stream_delta(msg["content"])
                self.messages.append(msg)
                self.ui.stream_end(msg.get("content") or "", f"{res.provider}:{res.model}")
                if msg.get("content"):
                    final = msg["content"]
                if not msg.get("tool_calls"):
                    return final
                self._run_tools(msg["tool_calls"])
                if self.on_step is not None:
                    try:
                        self.on_step()
                    except Exception:  # noqa: BLE001 - kayıt hatası işi durdurmasın
                        pass
            self.ui.info(f"adım limiti ({steps}) doldu; devam etmek için yeni mesaj yaz")
            return final
        except (KeyboardInterrupt, Cancelled, StreamCancelled):
            sink.dead = True
            partial = "".join(sink.parts)
            if partial and not (self.messages[-1]["role"] == "assistant" and self.messages[-1].get("content") == partial):
                self.messages.append({"role": "assistant", "content": partial + " [durduruldu]"})
                self.ui.stream_end(partial, "")
            self._repair_after_interrupt()
            self.ui.info("durduruldu")
            return final
        except AllProvidersFailed as e:
            self.ui.stream_end("", "")
            self._repair_after_interrupt()
            self.ui.info(str(e))
            return final
        finally:
            self.busy = False

    def stop(self) -> None:
        """Başka bir thread'den (ör. uygulamadaki Durdur butonu) mevcut turu durdur. Model ya da araç cevabı
        beklenmez; çalışan komutlar da sonlandırılır."""
        self.cancel.set()
        from alice.tools.shell import kill_foreground

        kill_foreground(self.ctx)

    def _interruptible(self, fn, *args: Any, **kwargs: Any) -> Any:
        """fn'i ayrı bir thread'de çalıştır ve bitmesini beklerken durdurma isteğine bak. Durdurulursa beklemeden
        Cancelled fırlatır; arkadaki çağrı kendi başına biter ve sonucu atılır."""
        box: dict[str, Any] = {}
        done = threading.Event()

        def work() -> None:
            try:
                box["res"] = fn(*args, **kwargs)
            except BaseException as e:  # noqa: BLE001 - çağıran thread'de yeniden fırlatılır
                box["err"] = e
            finally:
                done.set()

        threading.Thread(target=work, daemon=True, name="alice-call").start()
        while not done.wait(0.1):
            if self.cancel.is_set():
                raise Cancelled
        if "err" in box:
            raise box["err"]
        return box["res"]

    def rewind(self, msg_index: int) -> tuple[str, list[str]]:
        """Konuşmayı msg_index'teki kullanıcı mesajından öncesine geri sar ve o andan beri
        ALICE'in dosya araçlarıyla değiştirdiği dosyaları eski haline getir.
        (Shell komutlarının yaptığı değişiklikler izlenemez.) Mesajın metnini ve geri yüklenen dosyaları döndürür."""
        if self.busy:
            raise RuntimeError("çalışırken geri sarılamaz")
        if not 0 < msg_index < len(self.messages) or self.messages[msg_index].get("role") != "user":
            raise ValueError("geçersiz mesaj")
        original: dict[str, bytes | None] = {}
        for idx in sorted(k for k in self.checkpoints if k >= msg_index):
            for path, data in self.checkpoints[idx].items():
                original.setdefault(path, data)
        restored = []
        for path, data in original.items():
            p = Path(path)
            try:
                if data is None:
                    if p.exists():
                        p.unlink()
                else:
                    p.parent.mkdir(parents=True, exist_ok=True)
                    p.write_bytes(data)
                restored.append(path)
            except OSError as e:
                self.ui.info(f"geri yüklenemedi: {path} ({e})")
        m = self.messages[msg_index]
        text = m.get("_display") or (m["content"] if isinstance(m["content"], str) else
                                     next((p.get("text", "") for p in m["content"] if p.get("type") == "text"), ""))
        if text.startswith(PLAN_NOTE):
            text = text[len(PLAN_NOTE):]
        self.messages = self.messages[:msg_index]
        for k in [k for k in self.checkpoints if k >= msg_index]:
            del self.checkpoints[k]
        return text, restored

    # ------------------------------------------------------------------- tools
    def _run_tools(self, calls: list[dict[str, Any]]) -> None:
        # Model gönderilmemiş bir grubun aracını çağırdıysa (sistem isteminden biliyor) grubu açık tut.
        self._enable_groups_for(tool_names=tuple(c["function"]["name"] for c in calls))
        results = self._execute_calls(calls)
        pending_images: list[tuple[str, list[str]]] = []
        for call, result in zip(calls, results, strict=True):
            name = call["function"]["name"]
            text = truncate(result.text, self.output_limit)
            if result.is_error:
                text = f"HATA: {text}"
            text += self._loop_note(call, result)
            if result.images:
                if self._brain_sees_images():
                    pending_images.append((name, result.images))
                else:
                    text += "\n\n[görüntü açıklaması - vision modeli]\n" + self._describe_images(result.images)
            self.messages.append({"role": "tool", "tool_call_id": call["id"], "name": name, "content": text})
        for name, images in pending_images:
            parts: list[dict[str, Any]] = [{"type": "text", "text": f"[{name} aracının döndürdüğü görüntü]"}]
            parts += [{"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b}"}} for b in images]
            self.messages.append({"role": "user", "content": parts, "_image": True})

    def _loop_note(self, call: dict[str, Any], result: ToolResult) -> str:
        """Zayıf modeller aynı hatalı çağrıyı tekrar tekrar yapıp takılıyor; bunu fark edip modele söyle."""
        if self.cancel.is_set():
            return ""
        key = _call_key(call)
        self._call_counts[key] = self._call_counts.get(key, 0) + 1
        if result.is_error:
            self._error_streak += 1
            self._fail_counts[key] = self._fail_counts.get(key, 0) + 1
            if self._fail_counts[key] >= 2:
                return REPEAT_FAIL_NOTE.format(n=self._fail_counts[key])
            if self._error_streak >= 3 and self._error_streak % 3 == 0:
                return ERROR_STREAK_NOTE.format(n=self._error_streak)
            return ""
        self._error_streak = 0
        n = self._call_counts[key]
        if n >= 3 and call["function"]["name"] not in ("todo_write",):
            return REPEAT_OK_NOTE.format(n=n)
        return ""

    def _execute_calls(self, calls: list[dict[str, Any]]) -> list[ToolResult]:
        """Çağrıları sırayla işle; art arda gelen, izin gerektirmeyen ve paralel güvenli (salt okuma)
        çağrıları aynı anda çalıştır. Arayüz olayları hep bu thread'den ve çağrı sırasıyla gider."""
        results: list[ToolResult | None] = [None] * len(calls)
        i = 0
        while i < len(calls):
            if self.cancel.is_set():
                results[i] = ToolResult("durduruldu (kullanıcı kesti)", is_error=True)
                i += 1
                continue
            prep = self._prepare(calls[i])
            if isinstance(prep, ToolResult):
                results[i] = prep
                i += 1
                continue
            batch = [(i, prep)]
            j = i + 1
            if self._parallel_ok(prep):
                while j < len(calls) and not self.cancel.is_set():
                    nxt = self._prepare(calls[j], quiet=True)
                    if isinstance(nxt, ToolResult) or not self._parallel_ok(nxt):
                        break
                    batch.append((j, nxt))
                    j += 1
            if len(batch) == 1:
                results[i] = self._execute(calls[i], *prep)
            else:
                for idx, (tool, _args, summary) in batch:
                    self.ui.tool_start(calls[idx]["id"], tool.name, summary)
                with ThreadPoolExecutor(max_workers=min(8, len(batch))) as pool:
                    futures = {idx: pool.submit(self._run_tool, tool, args) for idx, (tool, args, _) in batch}
                    for idx, (tool, _, _) in batch:
                        results[idx] = futures[idx].result()
                        self.ui.tool_end(calls[idx]["id"], tool.name, results[idx])
            i = j
        return [r if r is not None else ToolResult("çalıştırılmadı", is_error=True) for r in results]

    def _parallel_ok(self, prep: tuple[Tool, dict[str, Any], str]) -> bool:
        tool, args, _ = prep
        return bool(getattr(tool, "parallel_safe", False)) and self.permissions.decide(tool, args, self.ctx) == "allow"

    def _prepare(self, call: dict[str, Any], quiet: bool = False) -> ToolResult | tuple[Tool, dict[str, Any], str]:
        """Aracı bul, argümanları çöz, izni al. Hata ya da ret varsa ToolResult döner.
        quiet=True iken izin sorulmaz (paralel gruba alınabilir mi diye bakarken)."""
        name = call["function"]["name"]
        tool = self.tools.get(name)
        if tool is None:
            return ToolResult(f"bilinmeyen araç '{name}'. Mevcut araçlar: {', '.join(self.tools)}", is_error=True)
        try:
            args = parse_args(call["function"].get("arguments"))
        except json.JSONDecodeError as e:
            return ToolResult(f"argümanlar geçerli JSON değil ({e}). Tekrar dene.", is_error=True)
        summary = tool.summary(args)
        if quiet:
            return tool, args, summary
        verdict = self.permissions.decide(tool, args, self.ctx)
        if verdict == "deny":
            res = ToolResult("Plan modundasın: bu araç değişiklik yapıyor ve şu an kapalı. Önce planını yaz; kullanıcı "
                             "onaylayıp modu değiştirince uygularsın.", is_error=True)
            self.ui.tool_start(call["id"], name, summary)
            self.ui.tool_end(call["id"], name, res)
            return res
        if verdict == "ask":
            ans = self.ui.ask_permission(name, summary, tool.risk_for(args))
            if ans == "a":
                self.permissions.always_allow.add(name)
            elif ans != "y":
                return ToolResult("Kullanıcı bu çağrıyı reddetti. Aynı şeyi tekrar deneme; başka yol bul ya da kullanıcıya sor.",
                                  is_error=True)
        return tool, args, summary

    def _execute(self, call: dict[str, Any], tool: Tool, args: dict[str, Any], summary: str) -> ToolResult:
        self.ui.tool_start(call["id"], tool.name, summary)
        result = self._run_tool(tool, args)
        self.ui.tool_end(call["id"], tool.name, result)
        return result

    def _run_tool(self, tool: Tool, args: dict[str, Any]) -> ToolResult:
        # Araçlar bu thread'de çalışır (Playwright ve UI Automation nesneleri thread'e bağlı). Durdurulunca
        # stop() araçların başlattığı komutları öldürür, böylece bekleyen araç da hemen döner.
        try:
            return tool.run(self.ctx, **args)
        except ToolError as e:
            return ToolResult(str(e), is_error=True)
        except TypeError as e:
            return ToolResult(f"geçersiz parametre: {e}", is_error=True)
        except KeyboardInterrupt:
            raise
        except Exception as e:  # noqa: BLE001 - araç hatası ajanı düşürmesin, modele dönsün
            return ToolResult(f"{type(e).__name__}: {e}", is_error=True)

    # ----------------------------------------------------------------- vision
    def _brain_sees_images(self) -> bool:
        ref = self.router.first_available(self.role)
        return bool(ref and self.router.supports_vision(ref))

    def _describe_images(self, images: list[str]) -> str:
        task = next((m["content"] for m in reversed(self.messages)
                     if m["role"] == "user" and isinstance(m.get("content"), str)), "")
        parts: list[dict[str, Any]] = [{"type": "text", "text": (
            "You are the eyes of a computer-use agent. Describe this screenshot precisely for the task below: "
            "visible windows and their titles, focused element, all buttons/links/fields/menus with their exact text "
            "and approximate pixel coordinates (x,y of center in this image), dialogs, errors, and anything relevant. "
            f"Task: {task[:800]}")}]
        parts += [{"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b}"}} for b in images]
        try:
            res = self.router.chat("vision", [{"role": "user", "content": parts}], max_tokens=1500)
            return res.text
        except (AllProvidersFailed, KeyError) as e:
            return f"(görüntü açıklanamadı: {str(e)[:200]})"

    def _prune_images(self) -> None:
        idx = [i for i, m in enumerate(self.messages) if m.get("_image")]
        for i in idx[:-self.keep_images] if self.keep_images else idx:
            self.messages[i] = {"role": "user", "content": "[eski ekran görüntüsü yer kazanmak için kaldırıldı]", "_image": False}

    # -------------------------------------------------------------- context
    def _maybe_compact(self, force: bool = False) -> bool:
        total = estimate_tokens(self.messages, self.tool_specs())
        if not force and total < self.compact_at:
            return False
        budget = int(self.compact_at * 0.35)
        users = [i for i, m in enumerate(self.messages) if i > 0 and m["role"] == "user" and not m.get("_image")]
        if not users:
            return False
        cut = users[-1]
        for i in reversed(users):
            if estimate_tokens(self.messages[i:]) <= budget:
                cut = i
            else:
                break
        old = self.messages[1:cut]
        if not old:
            # Tek turda bile çok büyümüş: büyük araç çıktılarını kısalt.
            for m in self.messages[cut:]:
                if m["role"] == "tool" and len(m["content"]) > 3000:
                    m["content"] = truncate(m["content"], 3000)
            return False
        transcript = []
        for m in strip_images(old):
            c = m.get("content") or ""
            if m.get("tool_calls"):
                c += "\n" + "\n".join(f"-> {tc['function']['name']}({tc['function']['arguments'][:400]})" for tc in m["tool_calls"])
            transcript.append(f"[{m['role']}] {truncate(c, 2500)}")
        self.ui.info("context büyüdü, özetleniyor...")
        try:
            res: ChatResult = self.router.chat("brain", [
                {"role": "system", "content": COMPACT_PROMPT},
                {"role": "user", "content": "\n\n".join(transcript)[-120000:]},
            ], max_tokens=2000)
        except AllProvidersFailed as e:
            self.ui.info(f"özetleme başarısız: {e}")
            return False
        self.messages = [self.messages[0],
                         {"role": "user", "content": f"[Önceki konuşmanın özeti]\n{res.text}"},
                         {"role": "assistant", "content": "Özeti aldım, kaldığım yerden devam ediyorum."},
                         *self.messages[cut:]]
        self.checkpoints.clear()  # mesaj indeksleri değişti, eski geri sarma noktaları geçersiz
        return True

    def compact(self) -> bool:
        return self._maybe_compact(force=True)

    def _repair_after_interrupt(self) -> None:
        """Yarım kalan tool_calls için sahte sonuç ekle, yoksa sonraki istek 400 alır."""
        for i in range(len(self.messages) - 1, 0, -1):
            m = self.messages[i]
            if m["role"] == "assistant" and m.get("tool_calls"):
                answered = {x.get("tool_call_id") for x in self.messages[i + 1:] if x["role"] == "tool"}
                for tc in m["tool_calls"]:
                    if tc["id"] not in answered:
                        self.messages.append({"role": "tool", "tool_call_id": tc["id"], "name": tc["function"]["name"],
                                              "content": "durduruldu (kullanıcı kesti)"})
                break

    def reset(self) -> None:
        self.messages = self.messages[:1]
