"""Pencere: çekirdeğe bağlı pencere çekirdek kapanınca kapanır, yeniden başlayınca yeni adrese geçer."""

from __future__ import annotations

import threading
import time

from alice.app import window


def test_app_modu_yeni_adresle_yeniden_acilir(monkeypatch, tmp_path):
    opened = []

    class Proc:
        def __init__(self, args, **kw):
            opened.append(next(a for a in args if a.startswith("--app=")))
            self.done = threading.Event()

        def terminate(self):
            self.done.set()

        def wait(self):
            self.done.wait(5)

    monkeypatch.setattr(window, "_browser_candidates", lambda: [str(tmp_path / "edge.exe")])
    monkeypatch.setattr(window.subprocess, "Popen", Proc)
    real_sleep = time.sleep
    monkeypatch.setattr(window.time, "sleep", lambda s: real_sleep(0.01))
    seq = iter(["", "http://b/?t=2", "", None])
    assert window.open_window("http://a/?t=1", prefer="app", watch=lambda: next(seq, None)) == "app"
    assert opened == ["--app=http://a/?t=1", "--app=http://b/?t=2"]


def test_sahibi_olmayan_sunucu_ortak_durumu_kapatmaz():
    from alice.app.server import AppServer

    closed = []

    class State:
        token = "x"

        def shutdown(self):
            closed.append(1)

    AppServer(State(), owns_state=False).start().stop()
    assert closed == []
    AppServer(State()).start().stop()
    assert closed == [1]
