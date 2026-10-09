"""Uçuş araçları: kontrol listesi, kneeboard, uçuş durumu ve son değerlendirme."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from alice.tools.base import READ, WRITE, Tool, ToolContext, ToolError, ToolResult, obj


class Checklist(Tool):
    name = "checklist"
    description = (
        "Uçuş kontrol listeleri (DCS). action: list | start (name: ör. 'iniş', 'kalkış', 'iniş sonrası') | next | "
        "prev | stop | current. Sesli kullanımda SADECE dönen adımı oku ve kullanıcının 'tamam/devam' demesini "
        "bekle; sonra next çağır.")
    parameters = obj({"action": {"type": "string", "enum": ["list", "start", "next", "prev", "stop", "current"]},
                      "name": {"type": "string"}}, ["action"])

    def run(self, ctx: ToolContext, action: str, name: str = "") -> ToolResult:
        from alice.sims.checklists import Runner, find, load_all

        if action == "list":
            return ToolResult("\n".join(f"{cl.key}: {cl.title} ({len(cl.steps)} adım)" for cl in load_all().values())
                              or "kontrol listesi yok")
        if action == "start":
            cl = find(name)
            if cl is None:
                raise ToolError(f"liste bulunamadı: {name}")
            ctx.state["checklist"] = Runner(cl)
            return ToolResult(f"{cl.title}. {ctx.state['checklist'].current()}")
        run = ctx.state.get("checklist")
        if run is None:
            raise ToolError("açık kontrol listesi yok (önce start)")
        if action == "stop":
            ctx.state.pop("checklist")
            return ToolResult("kontrol listesi kapatıldı")
        text = {"next": run.next, "prev": run.prev, "current": run.current}[action]()
        if run.done:
            ctx.state.pop("checklist")
        return ToolResult(text)


class Kneeboard(Tool):
    name = "kneeboard"
    description = (
        "DCS kneeboard sayfaları. action: build (görev dosyasından frekans ve waypoint sayfası; mission = .miz yolu, "
        "boşsa en son görev) | note (text: sesle not ekle, sayfa güncellenir) | clear_notes | show (notları listele).")
    parameters = obj({"action": {"type": "string", "enum": ["build", "note", "clear_notes", "show"]},
                      "mission": {"type": "string"}, "text": {"type": "string"}}, ["action"])
    risk = WRITE

    def risk_for(self, args: dict[str, Any]) -> str:
        return READ if args.get("action") == "show" else WRITE

    def run(self, ctx: ToolContext, action: str, mission: str = "", text: str = "") -> ToolResult:
        from alice.sims import kneeboard as kb
        from alice.sims.dcs import latest_mission, read_mission, saved_games_dirs

        if action == "show":
            return ToolResult("\n".join(f"{n['time']} {n['text']}" for n in kb.load_notes()) or "not yok")
        if action == "note":
            if not text:
                raise ToolError("text gerekli")
            kb.add_note(text)
        elif action == "clear_notes":
            kb.clear_notes()
        dirs = saved_games_dirs()
        if not dirs:
            raise ToolError("DCS Saved Games klasörü bulunamadı")
        info = None
        path = Path(mission).expanduser() if mission else latest_mission(dirs)
        if path is not None and path.exists():
            try:
                info = read_mission(path)
            except (ValueError, KeyError, OSError) as e:
                if action == "build":
                    raise ToolError(f"görev okunamadı: {e}") from e
        written = kb.write(kb.render(info, kb.load_notes()), kb.kneeboard_dirs(dirs))
        what = f"görev: {info.name}, {len(info.waypoints)} waypoint" if info else "görev yok"
        return ToolResult(f"kneeboard güncellendi ({len(written)} sayfa; {what}). Oyunda kneeboard'u açınca görünür.")


class Flight(Tool):
    name = "flight"
    description = ("Canlı uçuş durumu (DCS/War Thunder): irtifa, hız, yakıt, kalan dakika, eve dönüş payı, chaff/flare. "
                   "action: status | last_debrief (son uçuşun değerlendirmesi).")
    parameters = obj({"action": {"type": "string", "enum": ["status", "last_debrief"]}}, ["action"])

    def run(self, ctx: ToolContext, action: str) -> ToolResult:
        from alice.sims import hub
        from alice.sims.flight import flights_dir

        if action == "status":
            if hub.ACTIVE is None:
                return ToolResult("uçuş takibi bu süreçte çalışmıyor (alice core run ile başlar)")
            return ToolResult(hub.ACTIVE.status())
        if hub.ACTIVE is not None and hub.ACTIVE.last_debrief:
            d, page = hub.ACTIVE.last_debrief
            return ToolResult(f"{d.summary()}\nRapor: {page}")
        files = sorted(flights_dir().glob("*.json")) if flights_dir().is_dir() else []
        if not files:
            return ToolResult("kayıtlı uçuş yok")
        from alice.sims.flight import Debrief

        d = Debrief(**json.loads(files[-1].read_text(encoding="utf-8")))
        return ToolResult(f"{d.summary()}\nRapor: {files[-1].with_suffix('.html')}")


TOOLS = [Checklist(), Kneeboard(), Flight()]
