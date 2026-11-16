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
import re
import subprocess
import sys
from pathlib import Path

_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0

_DIFF_CMDS = {"diff", "show", "log"}


def no_hooks_dir() -> Path:
    from alice.config import alice_home

    return alice_home() / "git-hooks-off"  # oluşturulmaz; ~/.alice'e sandbox yazamaz


def config_args() -> list[str]:
    return ["-c", f"core.hooksPath={no_hooks_dir().as_posix()}", "-c", "core.fsmonitor=false",
            "-c", "protocol.ext.allow=never"]


# Deponun kendi config'inde (include'lar dahil) program çalıştıran ayarlar. Sandbox'taki komut .git/config'e
# filter.x.process, core.sshCommand, credential.helper... yazarsa ALICE'in sandbox dışındaki git'i onu tam yetkiyle
# çalıştırırdı. ALICE'in çağrılarında bunlar zararsız değerle ezilir (boş komut = filtre/sürücü yok).
_SAFE_DEFAULTS = {"core.sshcommand": "ssh", "core.gitproxy": "", "core.askpass": "", "core.alternaterefscommand": "",
                  "core.editor": "true", "sequence.editor": "true", "gpg.program": "gpg", "gpg.openpgp.program": "gpg",
                  "gpg.x509.program": "gpgsm", "gpg.ssh.program": "ssh-keygen",
                  "gpg.ssh.defaultkeycommand": ""}
_SUB_RX = re.compile(r"^(filter|diff|merge)\.(.+)\.(clean|smudge|process|command|textconv|driver)$")
_REMOTE_RX = re.compile(r"^remote\.(.+)\.(uploadpack|receivepack)$")
_cache: dict[str, tuple[float, list[str]]] = {}


def _local_config(cwd: Path) -> list[tuple[str, str]]:
    """Deponun yerel config'i (include'lar dahil). `git config` hiçbir program çalıştırmaz."""
    try:
        r = subprocess.run(["git", "-C", str(cwd), "config", "--local", "--includes", "--list", "-z"],
                           capture_output=True, timeout=10, stdin=subprocess.DEVNULL, creationflags=_NO_WINDOW)
    except (OSError, subprocess.SubprocessError):
        return []
    if r.returncode != 0:
        return []
    out = []
    for item in r.stdout.decode("utf-8", errors="replace").split("\0"):
        if item:
            key, _, val = item.partition("\n")
            out.append((key.lower(), val))
    return out


def _global_helpers() -> list[str]:
    try:
        r = subprocess.run(["git", "config", "--global", "--get-all", "credential.helper"], capture_output=True,
                           timeout=10, stdin=subprocess.DEVNULL, creationflags=_NO_WINDOW)
        sysr = subprocess.run(["git", "config", "--system", "--get-all", "credential.helper"], capture_output=True,
                              timeout=10, stdin=subprocess.DEVNULL, creationflags=_NO_WINDOW)
    except (OSError, subprocess.SubprocessError):
        return []
    lines = sysr.stdout.decode("utf-8", errors="replace").splitlines() + \
        r.stdout.decode("utf-8", errors="replace").splitlines()
    return [x for x in lines if x.strip()]


def repo_overrides(cwd: Path | str | None) -> list[str]:
    """Deponun program çalıştıran yerel ayarlarını ezen `-c` argümanları. git-lfs filtresi korunur."""
    if cwd is None:
        return []
    dirs = _git_dirs(Path(cwd))
    if not dirs:
        return []
    cfg = dirs[-1] / "config" if len(dirs) > 1 else dirs[0] / "config"
    try:
        mtime = cfg.stat().st_mtime
    except OSError:
        return []
    hit = _cache.get(str(cfg))
    if hit and hit[0] == mtime:
        return list(hit[1])
    out: list[str] = []
    helpers = False
    for key, val in _local_config(Path(cwd)):
        if key in _SAFE_DEFAULTS:
            out += ["-c", f"{key}={_SAFE_DEFAULTS[key]}"]
        elif key == "credential.helper" or (key.startswith("credential.") and key.endswith(".helper")):
            helpers = True
            out += ["-c", f"{key}="]
        elif (m := _SUB_RX.match(key)):
            if m.group(1) == "filter" and val.strip().startswith("git-lfs "):
                continue
            out += ["-c", f"{key}="]
        elif (m := _REMOTE_RX.match(key)):
            out += ["-c", f"{key}=git-{'upload-pack' if m.group(2) == 'uploadpack' else 'receive-pack'}"]
    if helpers:  # boş değer listeyi sıfırlar: kullanıcının genel (gh, manager) yardımcıları geri eklenir
        for h in _global_helpers():
            out += ["-c", f"credential.helper={h}"]
    out = list(dict.fromkeys(_pairs(out)))
    flat = [x for pair in out for x in ("-c", pair)]
    _cache[str(cfg)] = (mtime, flat)
    return list(flat)


def _pairs(args: list[str]) -> list[str]:
    return [args[i + 1] for i in range(0, len(args), 2)]


def harden(args: list[str] | tuple[str, ...], cwd: Path | str | None = None) -> list[str]:
    """git'e verilecek argümanların (alt komut dahil, `git`ten sonrası) korumalı hali. cwd verilirse deponun program
    çalıştıran yerel ayarları da ezilir (`-C yol` argümanlarda varsa o kullanılır)."""
    args = list(args)
    if cwd is None and "-C" in args[:-1]:
        cwd = args[args.index("-C") + 1]
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
    return config_args() + repo_overrides(cwd) + args


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
