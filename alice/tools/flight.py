"""Uçuş aracı (tek araç, context'e az yük): canlı durum, son değerlendirme, kontrol listesi, kneeboard."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from alice.tools.base import READ, WRITE, Tool, ToolContext, ToolError, ToolResult, obj

ACTIONS = ["status", "alert_home", "alert_alt", "alert_fuel", "alerts_clear", "last_debrief", "checklist_list", "checklist_start", "checklist_next", "checklist_prev",
           "checklist_stop", "kneeboard_build", "kneeboard_note", "kneeboard_clear", "kneeboard_show"]


class Flight(Tool):
    name = "flight"
    description = ("DCS/War Thunder. status: canlı yakıt/kalan dk/eve dönüş, üssün yönü ve mesafesi, sağa/sola kaç "
                   "derece dönmeli (yön/mesafe/yakıt/silah sorulunca HER SEFER status çağır). status'ta olmayan ya da "
                   "BİLİNMİYOR yazan bir bilgiyi (silah sayısı, mesafe, yön...) ASLA uydurma ve genel bilginden "
                   "tahmin etme: 'oyun bu bilgiyi vermiyor' de. alert_home (nm): "
                   "üsse nm mil kalınca otomatik sesli uyarı kurar; 'X mile gelince söyle' denince bunu çağır, "
                   "alert_alt (ft): o irtifaya gelince söyler; alert_fuel (kg): yakıt o kiloya inince söyler; "
                   "alerts_clear: kurulu uyarıları siler. Bunlar dışında uyarı söz verme. checklist_start (name: iniş, kalkış...): "
                   "sesliyken sadece dönen adımı oku, 'tamam/devam' gelince checklist_next. kneeboard_note (text): "
                   "uçuşta not. kneeboard_build (mission: .miz, boşsa son görev).")
    parameters = obj({"action": {"type": "string", "enum": ACTIONS}, "name": {"type": "string"},
                      "text": {"type": "string"}, "mission": {"type": "string"},
                      "nm": {"type": "number", "description": "alert_home için mil"},
                      "ft": {"type": "number", "description": "alert_alt için feet"},
                      "kg": {"type": "number", "description": "alert_fuel için kilo"}}, ["action"])
    risk = READ

    def risk_for(self, args: dict[str, Any]) -> str:
        a = args.get("action", "")
        return WRITE if a in ("kneeboard_build", "kneeboard_note", "kneeboard_clear") else READ

    def run(self, ctx: ToolContext, action: str, name: str = "", text: str = "", mission: str = "",
            nm: float | None = None, ft: float | None = None, kg: float | None = None) -> ToolResult:
        if action.startswith("alert"):
            from alice.sims import hub

            if hub.ACTIVE is None:
                return ToolResult("uçuş takibi bu süreçte çalışmıyor (alice core run ile başlar)")
            if action == "alerts_clear":
                return ToolResult(hub.ACTIVE.clear_alerts())
            name, value, call = {"alert_home": ("nm", nm, hub.ACTIVE.alert_home),
                                 "alert_alt": ("ft", ft, hub.ACTIVE.alert_altitude),
                                 "alert_fuel": ("kg", kg, hub.ACTIVE.alert_fuel)}[action]
            if not value or value <= 0:
                raise ToolError(f"{name} gerekli")
            return ToolResult(call(value))
        if action.startswith("checklist_"):
            return self._checklist(ctx, action[len("checklist_"):], name)
        if action.startswith("kneeboard_"):
            return self._kneeboard(action[len("kneeboard_"):], mission, text)
        return self._status(action)

    # ----------------------------------------------------------- durum
    @staticmethod
    def _status(action: str) -> ToolResult:
        from alice.sims import hub
        from alice.sims.flight import Debrief, flights_dir

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
        d = Debrief(**json.loads(files[-1].read_text(encoding="utf-8")))
        return ToolResult(f"{d.summary()}\nRapor: {files[-1].with_suffix('.html')}")

    # -------------------------------------------------- kontrol listesi
    @staticmethod
    def _checklist(ctx: ToolContext, action: str, name: str) -> ToolResult:
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
            raise ToolError("açık kontrol listesi yok (önce checklist_start)")
        if action == "stop":
            ctx.state.pop("checklist")
            return ToolResult("kontrol listesi kapatıldı")
        text = {"next": run.next, "prev": run.prev}[action]()
        if run.done:
            ctx.state.pop("checklist")
        return ToolResult(text)

    # -------------------------------------------------------- kneeboard
    @staticmethod
    def _kneeboard(action: str, mission: str, text: str) -> ToolResult:
        from alice.sims import kneeboard as kb
        from alice.sims.dcs import latest_mission, read_mission, saved_games_dirs

        if action == "show":
            return ToolResult("\n".join(f"{n['time']} {n['text']}" for n in kb.load_notes()) or "not yok")
        if action == "note":
            if not text:
                raise ToolError("text gerekli")
            kb.add_note(text)
        elif action == "clear":
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
        return ToolResult(f"kneeboard güncellendi ({len(written)} sayfa; {what}).")


TOOLS = [Flight()]
