from __future__ import annotations

import json
from datetime import datetime
from types import SimpleNamespace

from alice import night


def ev(ts, kind="flight", title="Uçuş", body="", severity="info"):
    return SimpleNamespace(ts=ts, kind=kind, title=title, body=body, severity=severity, topic="")


class Store:
    def __init__(self, events):
        self._events = events
        self.added = []

    def events(self, since=None, kind=None, limit=50):
        return [e for e in self._events if e.ts >= (since or 0)]

    def add_event(self, *a, **k):
        self.added.append(a)


def ts(day: int, hour: int, minute: int = 0) -> float:
    return datetime(2026, 10, day, hour, minute).timestamp()


def test_noisy_callouts_found_by_pattern_not_exact_text():
    evs = [ev(1000 + i * 30, body=f"Arkanda düşman, saat {4 + i % 3}, {300 + i * 10} metre!") for i in range(6)]
    evs += [ev(1000 + i * 400, body="Düşman saat 2, 3,0 kilometre.") for i in range(4)]  # seyrek: sorun değil
    found = night.noisy_callouts(evs)
    assert len(found) == 1 and found[0].count == 6 and "Arkanda düşman" in found[0].title


def test_repeated_errors_ignore_critical_flight_callouts():
    evs = [ev(i, kind="error", title=f"takvim takibi hata verdi {i}") for i in range(3)]
    evs += [ev(i, severity="critical", body="Yakıt 3 dakika yeter.") for i in range(5)]
    found = night.repeated_errors(evs)
    assert [(f.kind, f.count) for f in found] == [("error", 3)]


def test_false_wakes_from_voice_log():
    lines = [f"2026-11-02 03:0{i}:00 uyandı [base 0.5sn]: Eşek eşek {i}" for i in range(5)]
    lines += ["2026-11-02 03:09:00 uyandı [base 0.5sn]: Alice saat kaç", "2026-11-01 23:00:00 uyandı: dün"]
    f = night.false_wakes(lines, "2026-11-02")
    assert f is not None and f.count == 5 and "Eşek eşek" in f.detail
    assert night.false_wakes(lines[:4], "2026-11-02") is None


def test_crashes_group_traceback_last_lines():
    tb = "Traceback (most recent call last):\n  File \"x.py\", line 1, in <module>\n    f()\nValueError: kötü\n"
    found = night.crashes(tb * 3 + tb.replace("ValueError: kötü", "KeyError: 'a'"))
    assert [(f.title, f.count) for f in found] == [("Tekrarlayan çökme: ValueError: kötü", 3)]


def test_insights_short_flights_after_late_nights():
    flights = []
    for day in range(1, 21):
        late = day % 2 == 0
        if late:  # o gece 02:00'de oynandı (önceki güne sayılır)
            flights.append({"start_wall": ts(day + 1 if day < 20 else day, 2), "duration_s": 300})
        for k in range(3):
            # geç gecenin ertesi akşamı kısa uçuşlar
            prev_late = (day - 1) % 2 == 0 and day > 1
            flights.append({"start_wall": ts(day, 19, k * 10), "duration_s": 120 if prev_late else 360})
    out = night.insights(flights)
    assert out and out[0].startswith("Gece 1'den sonra oynadığın günlerin ertesinde uçuşların ortalama")
    assert night.insights(flights[:5]) == []  # az veri: bağlantı uydurulmaz


def test_run_writes_report_and_morning_flow(tmp_path, monkeypatch):
    now = ts(30, 4)
    evs = [ev(now - 3000 + i * 20, body=f"Yere yaklaşıyorsun, burnu kaldır! {i}") for i in range(5)]
    st = Store(evs)
    kw = dict(voice_log=tmp_path / "yok.log", core_log=tmp_path / "yok.log")
    assert night.maybe_run(st, None, now=ts(30, 2), flights=[], **kw) is None  # gece 3'ten önce değil
    r = night.maybe_run(st, None, now=now, flights=[], **kw)
    assert r is not None and r["findings"][0]["kind"] == "noisy"
    assert night.maybe_run(st, None, now=now + 600, flights=[], **kw) is None  # gecede bir kez
    md = (night.reports_dir() / "2026-10-30.md").read_text(encoding="utf-8")
    assert "# Gece raporu: 2026-10-30" in md and "Öneri:" in md
    assert st.added[0][1] == "Gece raporu hazır"

    said = []
    assert not night.maybe_tell(lambda t, b: said.append(b) or True, now=ts(30, 7))  # sabah 8'den önce değil
    assert not night.maybe_tell(lambda t, b: False, now=ts(30, 9))  # uçuş modunda susturuldu: söylenmiş sayılmaz
    assert night.maybe_tell(lambda t, b: said.append(b) or True, now=ts(30, 9, 10))
    assert not night.maybe_tell(lambda t, b: said.append(b) or True, now=ts(30, 10))
    assert len(said) == 1 and said[0].startswith("Gece 1 şey buldum") and "Düzeltmeyi deneyeyim mi?" in said[0]
    assert json.loads((night.reports_dir() / "2026-10-30.json").read_text(encoding="utf-8"))["date"] == "2026-10-30"


def test_render_when_all_is_well():
    assert "Her şey yolunda" in night.render({"date": "x", "findings": [], "insights": [], "tomorrow": []})
    assert night.morning_text({"findings": [], "insights": [], "tomorrow": []}) == ""


def test_core_runs_night_step(monkeypatch):
    from alice.core import service

    calls = []
    monkeypatch.setattr(night, "maybe_run", lambda store, mem: calls.append("run"))
    monkeypatch.setattr(night, "maybe_tell", lambda send: calls.append("tell"))
    core = SimpleNamespace(cfg=SimpleNamespace(data={}), store=None, notifier=None)
    service.Core._night(core, None)
    assert calls == ["run", "tell"]
    core.cfg.data = {"night": {"enabled": False}}
    service.Core._night(core, None)
    assert calls == ["run", "tell"]
