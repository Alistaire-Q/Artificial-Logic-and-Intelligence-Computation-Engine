"""Araç grupları: her istekte 33 aracın tanımını (~5K token) göndermek yerine sadece gerekenleri gönder.

Temel araçlar her zaman gider. Diğer gruplar kullanıcının mesajındaki anahtar kelimelerle, konuşmada
daha önce kullanılmışlarsa ya da model `load_tools` ile istediğinde eklenir ve o oturum boyunca kalır.
Daha küçük istek = daha fazla ücretsiz modele sığar, daha hızlı ve daha az kafa karıştırıcı.
"""

from __future__ import annotations

import re
from typing import Any

from alice.tools.base import READ, Tool, ToolContext, ToolError, ToolResult, obj

CORE = {"read_file", "write_file", "edit_file", "multi_edit", "list_dir", "glob", "grep", "run_command",
        "process_output", "process_stop", "web_fetch", "web_search", "windows", "todo_write", "memory", "task", "weather",
        "load_tools"}

# grup -> (araçlar, kısa açıklama, anahtar kelime regex'i)
GROUPS: dict[str, tuple[set[str], str, str]] = {
    "desktop": ({"computer", "ui_elements", "ui_act", "clipboard", "reflex"},
                "ekran görüntüsü, fare/klavye, masaüstü uygulamalarında tıklama, pano",
                r"tıkla|ekran|pencere|fare|mouse|klavye|screenshot|buton|düğme|menü|click|screen|pano|kopyala|"
                r"yapıştır|clipboard|oyun|ayarlar|uygulamada|arayüz|simge|ikon"),
    "browser": ({"browser"}, "web sitelerinde gezinme, form doldurma, tıklama (Playwright)",
                r"site|tarayıcı|browser|chrome|edge|firefox|opera|youtube|google|sayfa|link|https?://|www\.|\.com|"
                r"giriş yap|login|online|internetten|instagram|twitter|github|e-?posta|mail"),
    "system": ({"system_info", "process_list", "process_kill", "metrics"},
               "CPU/RAM/GPU/disk durumu, çalışan işlemler, geçmiş ölçümler",
               r"cpu|ram|gpu|işlemci|ekran kartı|sıcaklık|derece|bellek|disk|pil|batarya|performans|işlem|süreç|"
               r"process|görev yöneticisi|kasıyor|yavaş|fps|metrik"),
    "automation": ({"rules", "schedule"}, "uyarı kuralları ve zamanlanmış görevler",
                   r"kural|uyar|hatırlat|zamanla|her gün|her sabah|her akşam|haftada|alarm|schedule|rule|cron|"
                   r"saat \d|otomatik"),
    "blender": ({"blender"}, "Blender'da 3B sahne (bpy betiği)",
                r"blender|3d|3b|üç boyut|modelle|sahne|render|mesh"),
    "flight": ({"flight"}, "DCS / War Thunder uçuş verisi ve kneeboard",
               r"dcs|war ?thunder|uçuş|uçak|kneeboard|pist|iniş|kalkış|simülasyon|sim\b"),
    "personal": ({"notes", "report", "media"}, "notlar, sabah/PC raporu, müzik ve ses seviyesi",
                 r"\bnot\b|notlar|not al|rapor|özet|sabah|müzik|şarkı|ses seviyesi|sesi aç|sesi kıs|spotify|"
                 r"kapat .*dakika|dakika sonra"),
}

TOOL_TO_GROUP = {name: g for g, (names, _, _) in GROUPS.items() for name in names}
_RX = {g: re.compile(rx, re.I) for g, (_, _, rx) in GROUPS.items()}


# MCP sunucularının araçları (mcp__<sunucu>__<araç>) "mcp:<sunucu>" grubunda; sunucu adı geçince ya da
# aşağıdaki ek kelimelerle açılır. Bilinen sunucular için ek anahtar kelimeler:
MCP_KEYWORDS = {"github": r"git ?hub|\brepo|issue|pull request|\bpr\b|commit|release|branch"}


def group_of(name: str) -> str | None:
    if name in TOOL_TO_GROUP:
        return TOOL_TO_GROUP[name]
    if name.startswith("mcp__"):
        return "mcp:" + name.split("__")[1]
    return None


def mcp_servers(tool_names) -> set[str]:
    return {n.split("__")[1] for n in tool_names if n.startswith("mcp__") and n.count("__") >= 2}


def groups_for_text(text: str, tool_names=()) -> set[str]:
    text = text or ""
    out = {g for g, rx in _RX.items() if rx.search(text)}
    for srv in mcp_servers(tool_names):
        rx = re.escape(srv) + ("|" + MCP_KEYWORDS[srv] if srv in MCP_KEYWORDS else "")
        if re.search(rx, text, re.I):
            out.add("mcp:" + srv)
    return out


def visible_tools(tools: dict[str, Tool], enabled: set[str]) -> list[Tool]:
    """Temel araçlar + açık gruplar + gruba ait olmayanlar (MCP vb.)."""
    out = []
    for name, t in tools.items():
        g = group_of(name)
        if name in CORE or g is None or g in enabled:
            out.append(t)
    return out


class LoadTools(Tool):
    name = "load_tools"
    parallel_safe = True
    description = ("Ek araç grubu aç (sonraki adımdan itibaren kullanılabilir). Gruplar: "
                   + "; ".join(f"{g}: {desc}" for g, (_, desc, _) in GROUPS.items())
                   + "; MCP sunucuları için 'mcp:<sunucu>' (ör. mcp:github). Hepsi için 'all'.")
    parameters = obj({"groups": {"type": "array", "items": {"type": "string"}}}, ["groups"])
    risk = READ

    def run(self, ctx: ToolContext, groups: list[str] | str) -> ToolResult:
        if isinstance(groups, str):
            groups = [g.strip() for g in groups.split(",")]
        servers = mcp_servers(ctx.state.get("tool_names", ()))
        known = set(GROUPS) | {"mcp:" + s for s in servers}
        groups = ["mcp:" + g if g in servers else g for g in groups]
        wanted = known if "all" in groups else set(groups)
        unknown = wanted - known
        if unknown:
            raise ToolError(f"bilinmeyen grup: {', '.join(sorted(unknown))}. Gruplar: {', '.join(GROUPS)}")
        enabled: set[str] = ctx.state.setdefault("tool_groups", set())
        enabled |= wanted
        names = sorted(n for g in wanted if g in GROUPS for n in GROUPS[g][0])
        names += sorted(g for g in wanted if g.startswith("mcp:"))
        return ToolResult(f"açıldı: {', '.join(names)}")


TOOLS: list[Any] = [LoadTools()]
