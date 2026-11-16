"""ALICE'in kendi git çağrıları için koruma ve sandbox'ın .git'e dokunduğunu fark etme.

Sandbox'taki komut proje klasörüne yazabilir; .git de onun içinde. Kandırılmış bir komut .git/hooks/pre-commit
koyar ya da .git/config'e core.fsmonitor / core.hooksPath yazarsa, ALICE sonradan sandbox DIŞINDA git çalıştırdığında
(commit, status, ekip PR'ı, inceleme) o program tam yetkiyle çalışırdı. Klasörü kilitlemek işe yaramadı (sandbox
yeniden adlandırıp baştan oluşturabiliyor). Bu yüzden:

  * ALICE'in git çağrıları hook'ları kapalı (core.hooksPath sandbox'ın yazamadığı, var olmayan bir klasör),
    fsmonitor kapalı, ext:: protokolü yasak ve fark komutlarında dış diff/textconv programları kapalı çalışır.
  * Sandbox'taki komuttan önce/sonra .git/hooks ve .git/config'in izi alınır; değiştiyse araç sonucunda uyarılır.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

_DIFF_CMDS = {"diff", "show", "log"}


def no_hooks_dir() -> Path:
    from alice.config import alice_home

    return alice_home() / "git-hooks-off"  # oluşturulmaz; ~/.alice'e sandbox yazamaz


def config_args() -> list[str]:
    return ["-c", f"core.hooksPath={no_hooks_dir().as_posix()}", "-c", "core.fsmonitor=false",
            "-c", "protocol.ext.allow=never"]


def harden(args: list[str] | tuple[str, ...]) -> list[str]:
    """git'e verilecek argümanların (alt komut dahil, `git`ten sonrası) korumalı hali."""
    args = list(args)
    i = 0
    while i < len(args):  # alt komutu bul: "-c a=b" / "-C yol" ikişer, diğer seçenekler birer argüman
        if args[i] in ("-c", "-C"):
            i += 2
        elif args[i].startswith("-"):
            i += 1
        else:
            break
    if i < len(args) and args[i] in _DIFF_CMDS:
        args[i + 1:i + 1] = ["--no-ext-diff", "--no-textconv"]
    return config_args() + args


# ------------------------------------------------------------------------------------------- iz
def _git_dirs(cwd: Path) -> list[Path]:
    """cwd'yi içeren deponun git klasörü (worktree'de ortak klasör de). Depo değilse []."""
    try:
        cur = Path(cwd).resolve()
    except OSError:
        return []
    for d in (cur, *cur.parents):
        dot = d / ".git"
        if dot.is_dir():
            return [dot]
        if dot.is_file():
            try:
                line = dot.read_text(encoding="utf-8", errors="replace").strip()
            except OSError:
                return []
            if not line.startswith("gitdir:"):
                return []
            gd = Path(line[7:].strip())
            gd = gd if gd.is_absolute() else (d / gd)
            out = [gd]
            try:
                common = (gd / "commondir").read_text(encoding="utf-8").strip()
                cd = Path(common) if Path(common).is_absolute() else gd / common
                out.append(cd.resolve())
            except OSError:
                pass
            return out
    return []


def _digest(p: Path) -> str:
    try:
        return hashlib.sha256(p.read_bytes()).hexdigest()
    except OSError:
        return "?"


def snapshot(cwd: Path) -> dict[str, str]:
    """{yol: içerik özeti} — .git/config ve .git/hooks altındaki dosyalar."""
    out: dict[str, str] = {}
    for gd in _git_dirs(cwd):
        cfg = gd / "config"
        if cfg.is_file():
            out[str(cfg)] = _digest(cfg)
        hooks = gd / "hooks"
        try:
            files = [h for h in hooks.iterdir() if h.is_file()] if hooks.is_dir() else []
        except OSError:
            files = []
        for h in files:
            out[str(h)] = _digest(h)
    return out


def changes(before: dict[str, str], after: dict[str, str]) -> list[str]:
    return sorted(k for k in set(before) | set(after) if before.get(k) != after.get(k))


def warning(changed: list[str]) -> str:
    if not changed:
        return ""
    names = ", ".join(changed[:8]) + (" ..." if len(changed) > 8 else "")
    return ("\n[GÜVENLİK UYARISI] Sandbox'taki komut deponun git ayarlarını/hook'larını değiştirdi: " + names +
            ". Bunlar git her çalıştığında (commit, status, pull) çalışabilir. ALICE'in kendi git çağrıları hook'ları "
            "kapalı çalışır, ama kullanıcının kendi git'i ve editörü çalıştırır. Kullanıcıya bu değişikliği açıkça "
            "bildir; o istemedikçe bu dosyaları kullanma, gerekiyorsa kullanıcı onayıyla geri al.]")
