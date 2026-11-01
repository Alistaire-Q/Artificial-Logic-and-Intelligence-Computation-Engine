"""Çekirdek HUD sürecini başlatır, çökerse yeniden başlatır. HUD çökse de ALICE konuşmaya devam eder."""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any, Callable

CHECK_EVERY = 1.0       # çöken HUD en geç bu kadar + yeniden başlama süresinde döner (hedef 5 sn)
BURST, BURST_WINDOW, BURST_WAIT = 5, 60.0, 30.0   # dakikada 5 çökmeden sonra 30 sn bekle (döngüye girmesin)
CREATE_NO_WINDOW = 0x08000000


def python_exe() -> str:
    """Konsol penceresi açmayan Python (pythonw.exe), yoksa çalışan Python."""
    exe = Path(sys.executable)
    if sys.platform == "win32" and exe.name.lower() == "python.exe":
        w = exe.with_name("pythonw.exe")
        if w.exists():
            return str(w)
    return str(exe)


class HudSupervisor:
    def __init__(self, port: int, spawn: Callable[[list[str]], Any] | None = None, check_every: float = CHECK_EVERY):
        self.port = port
        self.spawn = spawn or self._spawn
        self.check_every = check_every
        self.proc: Any = None
        self.restarts = 0
        self._starts: deque[float] = deque()
        self._wait_until = 0.0
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True, name="alice-hud-supervisor")

    def argv(self) -> list[str]:
        return [python_exe(), "-m", "alice", "hud", "--port", str(self.port), "--parent", str(os.getpid())]

    @staticmethod
    def _spawn(argv: list[str]):
        root = Path(__file__).resolve().parent.parent.parent  # `-m alice` çekirdeğin kodunu bulsun
        flags = CREATE_NO_WINDOW if sys.platform == "win32" else 0
        return subprocess.Popen(argv, cwd=str(root), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL, creationflags=flags)

    def start(self) -> "HudSupervisor":
        self.check()
        self._thread.start()
        return self

    def check(self, now: float | None = None) -> None:
        """HUD çalışmıyorsa başlat (çökme seline girdiyse biraz bekleyerek)."""
        now = time.monotonic() if now is None else now
        if self.proc is not None and self.proc.poll() is None:
            return
        if now < self._wait_until:
            return
        while self._starts and now - self._starts[0] > BURST_WINDOW:
            self._starts.popleft()
        if len(self._starts) >= BURST:
            self._starts.clear()
            self._wait_until = now + BURST_WAIT
            return
        if self.proc is not None:
            self.restarts += 1
        self._starts.append(now)
        try:
            self.proc = self.spawn(self.argv())
        except OSError:
            self.proc = None

    def _run(self) -> None:
        while not self._stop.wait(self.check_every):
            self.check()

    def stop(self) -> None:
        self._stop.set()
        p, self.proc = self.proc, None
        if p is not None and p.poll() is None:
            try:
                p.terminate()
                p.wait(3)
            except Exception:  # noqa: BLE001 - kapanırken HUD'un inadı çekirdeği bekletmesin
                pass
