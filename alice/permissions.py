"""İzin modları.

plan : sadece okuma/araştırma; dosya değiştiren, komut çalıştıran, ekrana dokunan her şey reddedilir
ask  : okuma dışındaki her şey onay ister
auto : tehlikeli komutlar (rm -r, format, registry, shutdown...) ve sistem klasörlerine
       yazma onay ister; geri kalan her şey (dosya düzenleme, komut, fare/klavye) serbest
yolo : hiçbir şey sormaz
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

from alice.tools.base import DANGER, NETWORK, READ, WRITE, Tool, ToolContext

MODES = ("plan", "ask", "auto", "yolo")

SYSTEM_DIRS_WIN = [r"c:\windows", r"c:\program files", r"c:\program files (x86)", r"c:\programdata\microsoft"]
SYSTEM_DIRS_POSIX = ["/etc", "/usr", "/bin", "/sbin", "/boot", "/lib", "/lib64", "/var/lib", "/System", "/Library"]


def is_system_path(p: Path) -> bool:
    s = str(p)
    if sys.platform == "win32":
        low = s.lower()
        return any(low == d or low.startswith(d + "\\") for d in SYSTEM_DIRS_WIN)
    return any(s == d or s.startswith(d + "/") for d in SYSTEM_DIRS_POSIX)


class Permissions:
    def __init__(self, mode: str = "auto"):
        if mode not in MODES:
            raise ValueError(f"geçersiz izin modu: {mode}")
        self.mode = mode
        self.always_allow: set[str] = set()

    def decide(self, tool: Tool, args: dict[str, Any], ctx: ToolContext) -> str:
        """'allow', 'ask' ya da 'deny' döndürür."""
        if self.mode == "yolo":
            return "allow"
        risk = tool.risk_for(args)
        if risk == READ:
            return "allow"
        if self.mode == "plan":
            return "allow" if risk == NETWORK else "deny"
        if tool.name in self.always_allow and risk != DANGER:
            return "allow"
        if self.mode == "ask":
            return "ask"
        # auto
        if risk == DANGER:
            return "ask"
        if risk == WRITE:
            try:
                if any(is_system_path(ctx.resolve(p)) for p in _write_targets(args)):
                    return "ask"
            except (OSError, ValueError):
                return "ask"
        return "allow"


def _write_targets(args: dict[str, Any]) -> list[str]:
    """Yazma aracının dokunacağı yollar: "path" ve yama metnindeki dosyalar (apply_patch sistem klasörüne
    yazarken önceden hiç sorulmuyordu)."""
    out = [str(args["path"])] if "path" in args else []
    patch = args.get("patch")
    if isinstance(patch, str) and patch:
        from alice.tools.base import ToolError
        from alice.tools.patch import parse_patch

        try:
            ops = parse_patch(patch)
        except ToolError:
            return out  # bozuk yama: araç zaten hata verip hiçbir şey yazmaz
        for op in ops:
            out += [p for p in (op.path, op.move_to) if p]
    return out
