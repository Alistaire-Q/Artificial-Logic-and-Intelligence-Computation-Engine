"""Uçuş aracı (tek araç, context'e az yük): canlı durum, son değerlendirme, kontrol listesi, kneeboard."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from alice.tools.base import READ, WRITE, Tool, ToolContext, ToolError, ToolResult, obj

ACTIONS = ["status", "alert_home", "alert_alt", "alert_fuel", "alerts_clear", "last_debrief", "checklist_list", "checklist_start", "checklist_next", "checklist_prev",
           "checklist_stop", "kneeboard_build", "kneeboard_note", "kneeboard_clear", "kneeboard_show"]


class Flight(Tool):
    name = "flight"
    description = ("DCS / War Thunder uçuşu: canlı durum, sesli uyarılar, kontrol listeleri, kneeboard.\n"
                   "- Hız, irtifa, yakıt, yön, üsse mesafe, silah sorulunca HER SEFER status çağır; eski cevabı "
                   "tekrarlama.\n"
                   "- status'ta olmayan ya da BİLİNMİYOR yazan bilgiyi ASLA uydurma ve genel bilginden tahmin etme; "
                   "'oyun bu bilgiyi vermiyor' de.\n"
                   "- Sadece alert_home/alt/fuel ile kurulabilen uyarıları söz ver ('X mile gelince söyle' -> "
                   "alert_home).\n"
                   "- Kontrol listesinde sesliyken sadece dönen adımı oku; 'tamam/devam' gelince checklist_next.")
    parameters = obj({
        "action": {"type": "string", "enum": ACTIONS,
                   "description": "status: canlı yakıt, kalan dakika, üssün yönü/mesafesi, kaç derece dönmeli | "
                   "alert_home: üsse nm mil kalınca söyle | alert_alt: ft irtifasına gelince söyle | alert_fuel: yakıt "
                   "kg'a inince söyle | alerts_clear | last_debrief: son uçuşun değerlendirmesi | checklist_list | "
                   "checklist_start: name listesini başlat | checklist_next/prev/stop | kneeboard_note: text'i not "
                   "et | kneeboard_build: görevden kneeboard yap | kneeboard_show | kneeboard_clear."},
        "name": {"type": "string", "description": "checklist_start için liste adı: kalkış, kalkış sonrası, iniş, iniş "
                 "sonrası, motor çalıştırma, motor arızası, savaş girişi, savaş çıkışı."},
        "text": {"type": "string", "description": "kneeboard_note için not."},
        "mission": {"type": "string", "description": "kneeboard_build için .miz yolu. Boş: son görev."},
        "nm": {"type": "number", "description": "alert_home için deniz mili."},
        "ft": {"type": "number", "description": "alert_alt için feet."},
        "kg": {"type": "number", "description": "alert_fuel için kilogram."},
    }, ["action"])
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
            value = _number(value)
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


def _number(value: Any) -> float | None:
    """Model sayıyı bazen yazı olarak gönderir ("10", "12,5", "10 mil"); sayıya çevir, olmazsa None."""
    if value is None or isinstance(value, (int, float)):
        return value
    text = str(value)
    m = re.search(r"-?\d{1,3}(?:([.,])\d{3})(?:\1\d{3})*(?![.,]?\d)", text)  # "5.000", "12,000"
    if m:
        return float(re.sub(r"[.,]", "", m.group()))
    m = re.search(r"-?\d+(?:[.,]\d+)?", text)
    return float(m.group().replace(",", ".")) if m else None
