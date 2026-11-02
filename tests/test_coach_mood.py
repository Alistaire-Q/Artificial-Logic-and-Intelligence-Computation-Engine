from __future__ import annotations

from types import SimpleNamespace

from alice import mood
from alice.sims import coach


def flight(end: float, low=True, turning=True, landed=False, dur=200.0):
    n = 30
    wall = [end - n + i + 1 for i in range(n)] + [end + 1, end + 2]  # son iki örnek: ölümden sonra donmuş
    return {"start_wall": end - dur, "end_wall": end + 2, "duration_s": dur, "aircraft": "su_30mkk",
            "touchdown_vv_ms": -2.0 if landed else None,
            "series": {"wall": wall, "alt_ft": [1500 if low else 9000] * n + [0, 0],
                       "g": [5.5 if turning else 1.2] * n + [0, 0], "ias_kt": [300] * n + [0, 0]}}


def ev(ts, body):
    return SimpleNamespace(ts=ts, kind="flight", title="Uçuş", body=body)


def test_death_context_reads_last_alive_moment():
    f = flight(1000.0)
    events = [ev(970.0, "Arkanda düşman, saat 6, 400 metre!"), ev(950.0, "SAM menzilindesin: saat 2, 9 km."),
              ev(995.0, "Yanıyorsun! F-16C vurdu.")]
    c = coach.death_context(f, events)
    assert (c["low"], c["turning"], c["six"], c["sam"], c["killer"]) == (True, True, True, True, "F-16C")
    assert coach.death_context(flight(1000.0, landed=True), events) is None
    assert coach.death_context({"touchdown_vv_ms": None, "series": {}}, events) is None


def test_patterns_need_share_and_count():
    deaths, events = [], []
    for i in range(10):
        end = 1000.0 * (i + 1)
        deaths.append(flight(end, low=i < 7, turning=i < 7))
        if i < 7:
            events.append(ev(end - 20, "Arkanda düşman, saat 5, 300 metre!"))
    p = coach.patterns(coach.contexts(deaths, events))
    assert p == {"low_turn_six": "Son 10 ölümünün 7'si alçakta dönerken arkadan geldi. Alçakta uzun dönme; enerjini "
                                 "koru, irtifa kazanıp dön."}
    assert coach.patterns(coach.contexts(deaths[:2], events)) == {}  # az ölüm: desen yok
    few = coach.contexts(deaths, events[:2])
    assert "six" not in coach.patterns(few) and "low_turn_six" not in coach.patterns(few)


def test_turkish_number_suffix():
    assert [coach.ek(n) for n in (1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 20, 40)] == [
        "1'i", "2'si", "3'ü", "4'ü", "5'i", "6'sı", "7'si", "8'i", "9'u", "10'u", "20'si", "40'ı"]


def test_mood_levels():
    now = 10_000.0
    quick = [{"end_wall": now - 60 * i, "duration_s": 70, "touchdown_vv_ms": None} for i in range(3)]
    assert mood.read([], [], None, now).level == "sakin"
    assert mood.read(quick, [], None, now).level == "gergin"
    m = mood.read(quick, ["amk ya yine mi", "siktir", "lan bu ne"], 125.0, now)
    assert m.level == "tilt" and m.swears == 3 and "nabız 125" in m.reasons
    old = [{"end_wall": now - 3600, "duration_s": 70, "touchdown_vv_ms": None}] * 5
    assert mood.read(old, [], None, now).quick_deaths == 0  # 15 dakikadan eski


def test_heard_lines_only_recent(tmp_path):
    p = tmp_path / "voice.log"
    p.write_text("2026-11-02 03:00:00 uyandırma yok [base 1sn]: eski laf\n"
                 "2026-11-02 03:20:00 live [m] duyulan: amk yine düştüm | cevap: üzgünüm\n"
                 "2026-11-02 03:21:00 uyandırma yok [base 1sn]: lan ne biçim oyun\n", encoding="utf-8")
    from datetime import datetime

    since = datetime(2026, 11, 2, 3, 10).timestamp()
    assert [x.strip() for x in mood.heard_lines(p, since)] == ["amk yine düştüm", "lan ne biçim oyun"]


def test_hint_once_per_session_and_calm_mode():
    from tests.test_sims import Recorder, st

    from alice.sims.flight import FlightCompanion, TouchdownTracker

    r = Recorder()
    c = FlightCompanion(r.say)
    c.coach_flags = {"sam"}
    tr = TouchdownTracker(c)
    tr.feed(st(0, ias=0, agl=0.5, alt=50))
    tr.feed(st(1, ias=80, agl=30))
    sam = {"id": "", "clock": 2, "dist_m": 9000.0, "kind": "sam", "name": "SAM", "range_m": 18000.0}
    tr.feed(st(2, extra={"contacts": [sam]}))
    assert r.texts()[-1] == ("SAM menzilindesin: saat 2, 9,0 kilometre. Geçen ölümlerinin çoğu SAM menzilindeydi, "
                             "alçal ya da çık.")
    c.calm = True
    assert c._may_say(10.0, "x", 0) is False  # tilt: 12 değil 24 sn boşluk
    assert c._may_say(27.0, "x", 0) is True


def test_hub_after_death_coaches_and_suggests_break(monkeypatch):
    from tests.test_sims_hub import FakeNotifier, FakeStore

    from alice.sims.flight import FlightCompanion
    from alice.sims.hub import SimHub

    store = FakeStore()
    store.events = lambda since=None, kind=None, limit=50: []
    hb = SimHub({}, store, FakeNotifier())
    comp = FlightCompanion(hb._say)
    monkeypatch.setattr(coach, "load_flights", lambda limit=40: [])
    monkeypatch.setattr(coach, "patterns", lambda ctxs: {"sam": "Son 10 ölümünün 5'i SAM menzilindeyken oldu."})
    monkeypatch.setattr(mood, "current", lambda st, now, flights=None: mood.Mood())
    hb._after_death(comp, now=10_000.0)
    hb._after_death(comp, now=10_100.0)  # aynı gün ve 20 dk dolmadı: tekrar yok
    assert [n.body for n in hb.notifier.sent] == ["Son 10 ölümünün 5'i SAM menzilindeyken oldu."]
    assert comp.coach_flags == {"sam"} and not comp.calm
    monkeypatch.setattr(mood, "current", lambda st, now, flights=None: mood.Mood(level="tilt"))
    hb._after_death(comp, now=12_000.0)
    assert hb.notifier.sent[-1].body == "Üst üste düştün. Bir maç ara vermek iyi gelebilir." and comp.calm
