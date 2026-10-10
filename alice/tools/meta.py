"""Ajanın kendini yönetmesi için araçlar: todo listesi, kalıcı hafıza, alt ajan, reflex."""

from __future__ import annotations

from typing import Any

from alice.tools.base import COMPUTER, READ, WRITE, Tool, ToolContext, ToolError, ToolResult, obj


class TodoWrite(Tool):
    name = "todo_write"
    description = ("Çok adımlı işlerde plan/ilerleme listesi tut. Her çağrıda listenin TAMAMINI gönder. "
                   "status: pending | in_progress | done. Aynı anda tek bir in_progress olsun.")
    parameters = obj({"todos": {"type": "array", "items": obj({
        "content": {"type": "string"}, "status": {"type": "string", "enum": ["pending", "in_progress", "done"]}},
        ["content", "status"])}}, ["todos"])

    def summary(self, args: dict[str, Any]) -> str:
        todos = args.get("todos") or []
        done = sum(1 for t in todos if t.get("status") == "done")
        return f"{len(todos)} görev, {done} tamam"

    def run(self, ctx: ToolContext, todos: list[dict[str, Any]]) -> ToolResult:
        ctx.state["todos"] = todos
        mark = {"pending": "[ ]", "in_progress": "[~]", "done": "[x]"}
        text = "\n".join(f"{mark.get(t.get('status'), '[ ]')} {t.get('content')}" for t in todos)
        return ToolResult(text or "liste boş")


class Memory(Tool):
    name = "memory"
    description = ("Uzun süreli, yerel hafıza (oturumlar arası). Kullanıcının kalıcı tercihlerini, kişisel bilgilerini, "
                   "çevresindeki kişileri, alışkanlıklarını ve ALICE'ten nasıl davranmasını istediğini kaydet (action=add, "
                   "kind: preference|fact|person|routine|note; tek kısa Türkçe cümle). Bilgi değişince eskisini güncelle "
                   "(action=update id text) ya da sil (action=forget, id ya da query). Geçmiş konuşmaları ve bilgileri "
                   "aramak için action=search query (\"geçen hafta ne konuşmuştuk\", \"en sevdiğim dizi\"). "
                   "action=read hepsini listeler. Profil zaten sistem isteminde: sadece okumak için çağırma.")
    parameters = obj({"action": {"type": "string", "enum": ["read", "add", "update", "forget", "search"]},
                      "note": {"type": "string", "description": "add/update için bilgi cümlesi"},
                      "kind": {"type": "string", "enum": ["preference", "fact", "person", "routine", "note"]},
                      "id": {"type": "integer"}, "query": {"type": "string"}}, ["action"])
    risk = WRITE

    def risk_for(self, args: dict[str, Any]) -> str:
        return WRITE if args.get("action") == "forget" else READ

    def summary(self, args: dict[str, Any]) -> str:
        return f"{args.get('action')} {args.get('note') or args.get('query') or args.get('id') or ''}".strip()

    def run(self, ctx: ToolContext, action: str, note: str | None = None, kind: str | None = None,
            id: int | None = None, query: str | None = None, text: str | None = None) -> ToolResult:
        from alice.memory import Fact, shared_memory

        mem = ctx.state.get("memory") or shared_memory(ctx.config, ctx.router)
        if mem is None:
            raise ToolError("hafıza kapalı (config.toml [memory] enabled = false)")
        note = note or text
        if action == "read":
            return ToolResult(mem.export_markdown())
        if action == "search":
            if not (query or note):
                raise ToolError("search için query gerekli")
            hits = mem.search(query or note or "", limit=8, min_score=0.2)
            return ToolResult("\n".join(h.line() for h in hits) or "hafızada ilgili bir şey yok")
        if action == "add":
            if not note:
                raise ToolError("add için note gerekli")
            fid, new = mem.remember(note, kind or "fact", source="araç")
            return ToolResult(f"kaydedildi #{fid}" if new else f"benzer bilgi güncellendi #{fid}")
        if action == "update":
            if id is None or not note:
                raise ToolError("update için id ve note gerekli")
            return ToolResult(f"güncellendi #{id}" if mem.update(id, note) else f"#{id} bulunamadı")
        if action == "forget":
            if id is not None:
                gone = mem.forget(id)
                return ToolResult(f"silindi: {gone.text}" if gone else f"#{id} bulunamadı")
            if not (query or note):
                raise ToolError("forget için id ya da query gerekli")
            gone, hits = mem.forget_matching(query or note or "")
            if gone:
                return ToolResult(f"silindi: {gone[0].text}")
            cands = [h.item.line() for h in hits if isinstance(h.item, Fact)]
            return ToolResult("emin olamadım, hiçbirini silmedim. Adaylar (id ile sil):\n" + "\n".join(cands)
                              if cands else "eşleşen bilgi yok")
        raise ToolError(f"bilinmeyen action: {action}")


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
        "Hızlı reflex modu (System One / Jev): ekranda görünen bir şeye tıklama, arama kutusuna yazma gibi kısa ve net "
        "bir arayüz hedefini (ör. 'ekrandaki Impossible şarkısına tıkla', 'Ayarlar > Gizlilik sayfasını aç', "
        "'arama kutusuna X yazıp ilk sonuca tıkla') büyük modele her adımı sormadan, adım başına ~100-500 ms kararla yürütür. "
        "Sayfayı görüntüyle değil öğe listesiyle okur, her sitede/uygulamada aynı çalışır. "
        "target=browser (kullanıcının açık tarayıcısında o an öndeki sekme; url verilirse o siteye gider) ya da desktop (Windows UI Automation, "
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
