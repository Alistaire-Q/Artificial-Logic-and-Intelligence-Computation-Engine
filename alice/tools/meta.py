"""Ajanın kendini yönetmesi için araçlar: todo listesi, kalıcı hafıza, alt ajan, reflex."""

from __future__ import annotations

from typing import Any

from alice.tools.base import COMPUTER, READ, WRITE, Tool, ToolContext, ToolError, ToolResult, obj


class TodoWrite(Tool):
    name = "todo_write"
    description = ("Çok adımlı (3+ adım) bir işte planı ve ilerlemeyi listede tutar; kullanıcı da listeyi görür. "
                   "Her çağrıda listenin TAMAMINI gönder; aynı anda tek bir in_progress olsun, bir adım biter bitmez "
                   "done yap. Tek adımlık ya da sohbet işlerinde kullanma.")
    parameters = obj({"todos": {"type": "array", "description": "Güncel listenin tamamı.", "items": obj({
        "content": {"type": "string", "description": "Adım, emir kipinde kısa cümle, ör. 'Testleri çalıştır'."},
        "status": {"type": "string", "enum": ["pending", "in_progress", "done"],
                   "description": "pending: başlamadı | in_progress: şu an yapılan | done: bitti."}},
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
    description = ("Oturumlar arası kalıcı hafıza. Kullanıcı hakkında ileride işe yarayacak kalıcı bilgiyi kaydet: "
                   "tercihleri, kişisel bilgileri, çevresindeki kişiler, alışkanlıkları, ALICE'ten nasıl davranmasını "
                   "istediği ('bunu hatırla', 'bundan sonra hep...').\n"
                   "- Bilgi değişince yenisini eklemek yerine eskisini update ile güncelle; yanlışsa forget.\n"
                   "- Geçmiş konuşmalarda ya da kayıtlarda bir şey aramak için search ('geçen hafta ne konuşmuştuk').\n"
                   "- Profil zaten sistem isteminde; sadece okumak için çağırma. Geçici şeyleri (o anki iş) kaydetme; "
                   "not için notes aracı.")
    parameters = obj({
        "action": {"type": "string", "enum": ["read", "add", "update", "forget", "search"],
                   "description": "read: hepsini listele | add: note'u kaydet | update: id'li bilgiyi note ile değiştir "
                   "| forget: id'li ya da query'ye uyan bilgiyi sil | search: query ile ara."},
        "note": {"type": "string", "description": "add/update için tek, kısa Türkçe cümle, ör. 'Kullanıcı kahveyi "
                 "şekersiz içer.'"},
        "kind": {"type": "string", "enum": ["preference", "fact", "person", "routine", "note"],
                 "description": "add için tür: preference tercih, fact bilgi, person kişi, routine alışkanlık. "
                 "Varsayılan fact."},
        "id": {"type": "integer", "description": "update/forget için bilginin numarası (read/search çıktısındaki #)."},
        "query": {"type": "string", "description": "search ya da forget için arama ifadesi."},
    }, ["action"])
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
    description = ("Kendi temiz bağlamıyla çalışan bir alt ajan başlatır; bağımsız bir işi (araştırma, büyük bir "
                   "klasörde arama, uzun bir alt görev) yapar ve sadece sonucunu döndürür. Konuşmayı çok sayıda ara "
                   "çıktıyla doldurmamak için. Alt ajan bu konuşmayı görmez: prompt'a gereken her şeyi yaz. Bir iki "
                   "araç çağrısıyla bitecek işlerde kullanma.")
    parameters = obj({
        "prompt": {"type": "string", "description": "Alt ajana eksiksiz görev: ne yapacak, nerede, ne döndürecek."},
        "max_steps": {"type": "integer", "description": "En fazla adım. Varsayılan 40."},
    }, ["prompt"])
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
        "Ekranda görünen bir şeye tıklama, bir alana yazma gibi kısa ve net bir arayüz hedefini hızlıca yapar "
        "(adım başına ~0,1-0,5 sn). Sayfayı öğe listesiyle okur; her sitede ve uygulamada aynı çalışır.\n"
        "- Kullan: 'ekrandaki Impossible şarkısına tıkla', 'Ayarlar > Gizlilik'i aç', 'arama kutusuna X yazıp ilk "
        "sonuca tıkla'.\n"
        "- Kullanma: birçok adımlı, ekranı izleyerek yapılacak işler (computer_task).\n"
        "- Sonuç 'needs_help' ise kalan kısmı browser snapshot/click ya da ui_elements/ui_act ile sen bitir."
    )
    parameters = obj({
        "goal": {"type": "string", "description": "Hedef, ekranda göründüğü adlarla, ör. 'ekrandaki Impossible "
                 "şarkısına tıkla'."},
        "target": {"type": "string", "enum": ["browser", "desktop"],
                   "description": "browser: kullanıcının tarayıcısında öndeki sekme | desktop: masaüstü penceresi."},
        "url": {"type": "string", "description": "browser için: önce bu adrese git. Boş: öndeki sekmede kal."},
        "title": {"type": "string", "description": "desktop için pencere başlığından bir parça. Boş: öndeki pencere."},
        "inputs": {"type": "object", "additionalProperties": {"type": "string"},
                   "description": "Doldurulacak alanlar: alan adından parça -> yazılacak metin, ör. {\"ara\": \"lofi\"}."},
        "max_steps": {"type": "integer", "description": "En fazla adım. Varsayılan 20."},
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
