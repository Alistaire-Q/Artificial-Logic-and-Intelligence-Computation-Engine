"""ALICE'in tek Python'u.

Aynı makinede ALICE iki ortamdan başlıyordu: Windows açılışındaki kısayol .venv'den, elle/uygulamadan başlatınca
sistem Python'undan. Paketler ortamdan ortama değiştiği için bir özellik bazen var bazen yok oluyordu (OCR). Burada
çekirdeğin çalışacağı Python bir kez kaydedilir (~/.alice/python.json); başka bir Python'dan başlatılan çekirdek
kendini kayıtlı olanla yeniden başlatır, açılış kısayolu ve arka plan başlatmaları da hep onu kullanır.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

REEXEC_ENV = "ALICE_REEXEC"  # yeniden başlatılan süreç bir daha yönlendirmesin (döngü olmasın)


def _path() -> Path:
    from alice.config import alice_home

    return alice_home() / "python.json"


def gui_python(exe: str | Path) -> Path:
    """Konsol penceresi açmayan karşılığı (Windows'ta pythonw.exe)."""
    exe = Path(exe)
    if sys.platform == "win32":
        w = exe.with_name("pythonw.exe")
        if w.exists():
            return w
    return exe


def canonical() -> Path | None:
    """Kayıtlı Python (hâlâ duruyorsa)."""
    try:
        p = Path(json.loads(_path().read_text(encoding="utf-8"))["python"])
    except (OSError, ValueError, KeyError, TypeError):
        return None
    return p if p.exists() else None


def remember(exe: str | Path | None = None) -> Path:
    p = gui_python(exe or sys.executable)
    path = _path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"python": str(p)}, ensure_ascii=False), encoding="utf-8")
    return p


def python() -> Path:
    """Çekirdek ve yardımcı süreçler bununla başlatılır: kayıtlı olan, yoksa şu anki."""
    return canonical() or gui_python(sys.executable)


def same(a: str | Path, b: str | Path) -> bool:
    """Aynı ortam mı (python.exe ile pythonw.exe aynı klasörde: aynı)."""
    try:
        return Path(a).resolve().parent == Path(b).resolve().parent
    except OSError:
        return False


def redirect_core(argv: list[str], popen=subprocess.Popen) -> bool:
    """Çekirdek kayıtlı Python'dan başka biriyle başladıysa kayıtlıyla yeniden başlat; başlattıysa True (bu süreç
    çıkmalı). İlk çalışmada kayıt yoksa şu anki Python kaydedilir."""
    if os.environ.get(REEXEC_ENV):
        return False
    want = canonical()
    if want is None:
        remember()
        return False
    if same(want, sys.executable):
        return False
    kwargs: dict = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL,
                    "cwd": str(Path.home()), "env": {**os.environ, REEXEC_ENV: "1"}}
    if sys.platform == "win32":
        kwargs["creationflags"] = 0x00000008 | 0x00000200  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True
    popen([str(want), "-m", "alice", "core", *argv], **kwargs)
    return True
