"""Ajanın kendini yönetmesi için araçlar: todo listesi, kalıcı hafıza, alt ajan, reflex."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from alice.config import alice_home
from alice.tools.base import COMPUTER, READ, WRITE, Tool, ToolContext, ToolError, ToolResult, obj


class TodoWrite(Tool):
    name = "todo_write"
    description = ("Çok adımlı işlerde plan/ilerleme listesi tut. Her çağrıda listenin TAMAMINI gönder. "
                   "status: pending | in_progress | done. Aynı anda tek bir in_progress olsun.")
    parameters = obj({"todos": {"type": "array", "items": obj({
        "content": {"type": "string"}, "status": {"type": "string", "enum": ["pending", "in_progress", "done"]}},
        ["content", "status"])}}, ["todos"])

    def run(self, ctx: ToolContext, todos: list[dict[str, Any]]) -> ToolResult:
        ctx.state["todos"] = todos
        mark = {"pending": "[ ]", "in_progress": "[~]", "done": "[x]"}
        text = "\n".join(f"{mark.get(t.get('status'), '[ ]')} {t.get('content')}" for t in todos)
        ctx.emit(f"plan:\n{text}")
        return ToolResult(text or "liste boş")


class Memory(Tool):
    name = "memory"
    description = ("Oturumlar arası kalıcı hafıza (~/.alice/memory.md). Kullanıcı hakkında öğrendiğin kalıcı "
                   "bilgileri, tercihleri ve tekrar lazım olacak yolları kaydet. action: read | add.")
    parameters = obj({"action": {"type": "string", "enum": ["read", "add"]}, "note": {"type": "string"}}, ["action"])
    risk = WRITE

    def risk_for(self, args: dict[str, Any]) -> str:
        return READ

    def run(self, ctx: ToolContext, action: str, note: str | None = None) -> ToolResult:
        path = alice_home() / "memory.md"
        if action == "read":
            return ToolResult(path.read_text(encoding="utf-8") if path.exists() else "(hafıza boş)")
        if not note:
            raise ToolError("add için note gerekli")
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(f"* {note.strip()}  _({datetime.now():%Y-%m-%d})_\n")
        return ToolResult("kaydedildi")


class Task(Tool):
    name = "task"
    description = ("Alt ajan başlat: kendi temiz context'iyle bağımsız bir işi (araştırma, büyük bir kod tabanında arama, "
                   "uzun bir alt görev) yapar ve sadece sonucu döndürür. Ana context'i şişirmemek için kullan.")
    parameters = obj({"prompt": {"type": "string", "description": "alt ajana eksiksiz görev tanımı"},
                      "max_steps": {"type": "integer"}}, ["prompt"])
    risk = READ  # alt ajanın kendi araç çağrıları ayrı ayrı izin kontrolünden geçer

    def run(self, ctx: ToolContext, prompt: str, max_steps: int = 40) -> ToolResult:
        parent = ctx.state.get("agent")
        if parent is None:
            raise ToolError("alt ajan bu bağlamda kullanılamıyor")
        sub = parent.spawn_child()
        ctx.emit("alt ajan başladı")
        result = sub.run_turn(prompt, max_steps=max_steps)
        ctx.emit("alt ajan bitti")
        return ToolResult(result or "(alt ajan boş cevap döndürdü)")


class Reflex(Tool):
    name = "reflex"
    description = (
        "Hızlı reflex modu (System One / Jev): kısa ve net bir arayüz hedefini (ör. 'Ayarlar > Gizlilik sayfasını aç', "
        "'arama kutusuna X yazıp ilk sonuca tıkla') büyük modele her adımı sormadan, adım başına ~100-500 ms kararla yürütür. "
        "target=browser (Playwright sayfası; url verilirse önce oraya gider) ya da desktop (Windows UI Automation, "
        "title ile pencere seçilir). inputs: alan adı parçası -> yazılacak metin. Sonuç 'needs_help' ise kalan kısmı sen yap."
    )
    parameters = obj({
        "goal": {"type": "string"},
        "target": {"type": "string", "enum": ["browser", "desktop"]},
        "url": {"type": "string"},
        "title": {"type": "string"},
        "inputs": {"type": "object", "additionalProperties": {"type": "string"}},
        "max_steps": {"type": "integer"},
    }, ["goal", "target"])
    risk = COMPUTER

    def run(self, ctx: ToolContext, goal: str, target: str, url: str | None = None, title: str | None = None,
            inputs: dict[str, str] | None = None, max_steps: int = 20) -> ToolResult:
        from alice.reflex.loop import ReflexLoop, make_text_writer
        from alice.reflex.observers import BrowserObserver, DesktopObserver

        if ctx.decider is None or ctx.router is None:
            raise ToolError("karar motoru yapılandırılmamış")
        observer = BrowserObserver(ctx, url) if target == "browser" else DesktopObserver(ctx, title)
        loop = ReflexLoop(observer, ctx.decider, make_text_writer(ctx.router),
                          min_confidence=float(ctx.config.decider.get("min_confidence", 0.55)), emit=ctx.emit)
        return ToolResult(loop.run(goal, max_steps=max_steps, inputs=inputs).report())


TOOLS = [TodoWrite(), Memory(), Task(), Reflex()]
