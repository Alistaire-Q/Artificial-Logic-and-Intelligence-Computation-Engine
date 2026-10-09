"""Eklemelerin araçları: notlar, sabah özeti, haftalık rapor, medya ve kapatma zamanlayıcısı."""

from __future__ import annotations

from typing import Any

from alice.tools.base import COMPUTER, DANGER, READ, WRITE, Tool, ToolContext, ToolError, ToolResult, obj


class Notes(Tool):
    name = "notes"
    description = ("Kullanıcının notları ('not al'). search: query ve/veya since/until ('7d', 'dün', ISO). "
                   "Uçuştaki notlar için flight kneeboard_note.")
    parameters = obj({"action": {"type": "string", "enum": ["add", "search", "list", "undo"]},
                      "text": {"type": "string"}, "query": {"type": "string"},
                      "since": {"type": "string"}, "until": {"type": "string"}}, ["action"])
    risk = WRITE

    def risk_for(self, args: dict[str, Any]) -> str:
        return READ

    def run(self, ctx: ToolContext, action: str, text: str = "", query: str = "", since: str | None = None,
            until: str | None = None) -> ToolResult:
        from alice.core.store import parse_time
        from alice.extras import notes

        if action == "add":
            if not text.strip():
                raise ToolError("text gerekli")
            notes.add(text)
            return ToolResult("not alındı")
        if action == "undo":
            n = notes.remove_last()
            return ToolResult(f"silindi: {n.text}" if n else "silinecek not yok")
        try:
            t0, t1 = parse_time(since), parse_time(until)
        except ValueError as e:
            raise ToolError(str(e)) from e
        found = notes.search(query if action == "search" else "", t0, t1)
        if action == "list":
            found = found[-20:]
        return ToolResult("\n".join(n.line() for n in found[-50:]) or "not bulunamadı")


class Report(Tool):
    name = "report"
    description = ("kind=morning: sabah özeti verisi (hava, bugün okul/staj, gece görevler ve uyarılar, dünden "
                   "kalanlar); kısa ve sıcak anlat. kind=pc: son days günün bilgisayar raporu (+HTML).")
    parameters = obj({"kind": {"type": "string", "enum": ["morning", "pc"]}, "days": {"type": "integer"}}, ["kind"])
    risk = READ

    def run(self, ctx: ToolContext, kind: str, days: int = 7) -> ToolResult:
        from alice.core.schedule import TaskStore
        from alice.extras import brief, report
        from alice.tools.core import _store

        if kind == "morning":
            return ToolResult(brief.render(brief.gather(ctx.config.data.get("brief", {}), _store(ctx), TaskStore())))
        r = report.build(_store(ctx), days)
        return ToolResult(f"{report.text(r)}\nRapor: {report.save_html(r)}")


class Media(Tool):
    name = "media"
    description = ("Müzik/ses. volume_up/down: times basış (~%2). pause_in: müziği minutes sonra durdur. "
                   "shutdown_in: bilgisayarı minutes sonra kapat.")
    parameters = obj({"action": {"type": "string", "enum": ["play_pause", "next", "previous", "stop", "mute",
                                                             "volume_up", "volume_down", "set_volume", "pause_in",
                                                             "shutdown_in", "cancel_shutdown"]},
                      "times": {"type": "integer"}, "percent": {"type": "integer"}, "minutes": {"type": "number"}},
                     ["action"])
    risk = COMPUTER

    def risk_for(self, args: dict[str, Any]) -> str:
        return DANGER if args.get("action") == "shutdown_in" else COMPUTER

    def run(self, ctx: ToolContext, action: str, times: int = 5, percent: int | None = None,
            minutes: float | None = None) -> ToolResult:
        from alice.extras import media

        if action == "set_volume":
            if percent is None:
                raise ToolError("percent gerekli")
            return ToolResult(media.set_volume(percent))
        if action in ("pause_in", "shutdown_in"):
            if not minutes or minutes <= 0:
                raise ToolError("minutes gerekli")
            return ToolResult(media.pause_in(minutes) if action == "pause_in" else media.shutdown_in(minutes))
        if action == "cancel_shutdown":
            return ToolResult(media.cancel_shutdown())
        return ToolResult(media.key(action, times if action.startswith("volume") else 1))


TOOLS = [Notes(), Report(), Media()]
