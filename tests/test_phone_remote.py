"""Telefon uygulamasının bilgisayar sekmesi: ölçüm geçmişi (küçük grafikler), açık pencereler ve öne getirme."""

from __future__ import annotations

import time

import pytest

from alice.app import remote
from alice.core.store import Store


def test_series_buckets_and_carries_last_value(tmp_path):
    with Store(tmp_path / "s.db") as st:
        now = time.time()
        st.add("pc.cpu.percent", 10, ts=now - 590)
        st.add("pc.cpu.percent", 30, ts=now - 300)
        st.add("pc.cpu.percent", 50, ts=now - 5)
        out = remote.series(st, minutes=10, points=10)
    cpu = out["cpu"]
    assert len(cpu) == 10
    assert cpu[0] == 10 and cpu[-1] == 50
    assert 30 in cpu  # ortadaki örnek kendi aralığında
    assert all(a is not None for a in cpu)
    assert out["ram"] == []  # hiç örnek yoksa boş


def test_windows_without_window_tools_is_empty(monkeypatch):
    monkeypatch.setattr(remote, "IS_WIN", False)
    import alice.tools.desktop as desktop

    def boom():
        raise RuntimeError("wmctrl yok")

    monkeypatch.setattr(desktop, "list_windows", boom)
    assert remote.windows() == []


def test_windows_lists_titles_with_app_name(monkeypatch):
    monkeypatch.setattr(remote, "IS_WIN", False)
    import alice.tools.desktop as desktop

    monkeypatch.setattr(desktop, "list_windows", lambda: ["notlar.txt - Not Defteri", "Discord"])
    rows = remote.windows()
    assert [r["app"] for r in rows] == ["Not Defteri", "Discord"]


def test_focus_action(monkeypatch):
    import alice.tools.desktop as desktop

    seen = {}
    monkeypatch.setattr(desktop, "focus_window", lambda t: seen.setdefault("t", t) or t)
    assert remote.action({"action": "focus", "title": "Discord"}) == "Discord öne getirildi"
    with pytest.raises(ValueError):
        remote.action({"action": "focus", "title": ""})
