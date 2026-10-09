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
        "process_output", "process_stop", "web_fetch", "web_search", "windows", "todo_write", "memory", "task",
        "load_tools"}

# grup -> (araçlar, kısa açıklama, anahtar kelime regex'i)
GROUPS: dict[str, tuple[set[str], str, str]] = {
    "desktop": ({"computer", "ui_elements", "ui_act", "clipboard", "reflex"},
                "ekran görüntüsü, fare/klavye, masaüstü uygulamalarında tıklama, pano",
                r"tıkla|ekran|pencere|fare|mouse|klavye|screenshot|buton|düğme|menü|click|screen|pano|kopyala|"
                r"yapıştır|clipboard|oyun|ayarlar|uygulamada|arayüz|simge|ikon"),
    "browser": ({"browser"}, "web sitelerinde gezinme, form doldurma, tıklama (Playwright)",
                r"site|tarayıcı|browser|chrome|edge|firefox|youtube|google|sayfa|link|https?://|www\.|\.com|"
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


def groups_for_text(text: str) -> set[str]:
    return {g for g, rx in _RX.items() if rx.search(text or "")}


def visible_tools(tools: dict[str, Tool], enabled: set[str]) -> list[Tool]:
    """Temel araçlar + açık gruplar + gruba ait olmayanlar (MCP vb.)."""
    out = []
    for name, t in tools.items():
        g = TOOL_TO_GROUP.get(name)
        if name in CORE or g is None or g in enabled:
            out.append(t)
    return out


class LoadTools(Tool):
    name = "load_tools"
    parallel_safe = True
    description = ("Ek araç grubu aç (sonraki adımdan itibaren kullanılabilir). Gruplar: "
                   + "; ".join(f"{g}: {desc}" for g, (_, desc, _) in GROUPS.items())
                   + ". Hepsi için 'all'.")
    parameters = obj({"groups": {"type": "array", "items": {"type": "string"}}}, ["groups"])
    risk = READ

    def run(self, ctx: ToolContext, groups: list[str] | str) -> ToolResult:
        if isinstance(groups, str):
            groups = [g.strip() for g in groups.split(",")]
        wanted = set(GROUPS) if "all" in groups else set(groups)
        unknown = wanted - set(GROUPS)
        if unknown:
            raise ToolError(f"bilinmeyen grup: {', '.join(sorted(unknown))}. Gruplar: {', '.join(GROUPS)}")
        enabled: set[str] = ctx.state.setdefault("tool_groups", set())
        enabled |= wanted
        names = sorted(n for g in wanted for n in GROUPS[g][0])
        return ToolResult(f"açıldı: {', '.join(names)}")


TOOLS: list[Any] = [LoadTools()]
