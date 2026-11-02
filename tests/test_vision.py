from __future__ import annotations

import math

import numpy as np
import pytest

from alice.sims import vision
from alice.sims.vision import Confirmer, Detection, MissileWatcher, detect, region_of


def frame(clock: int | None = None, color=(255, 40, 30), size=120, blob=6, extra=None):
    """Koyu RWR dairesi; istenirse saat yönünde kırmızı bir işaret."""
    a = np.full((size, size, 3), 20, dtype=np.uint8)
    if clock is not None:
        ang = math.radians(clock * 30)
        cx = size / 2 + math.sin(ang) * size * 0.35
        cy = size / 2 - math.cos(ang) * size * 0.35
        a[int(cy) - blob:int(cy) + blob, int(cx) - blob:int(cx) + blob] = color
    if extra is not None:
        extra(a)
    return a


@pytest.mark.parametrize("clock", [12, 2, 4, 6, 9, 11])
def test_detect_clock_from_red_marker(clock):
    d = detect(frame(clock))
    assert d is not None and d.clock == clock and d.pixels >= 100


def test_detect_ignores_white_center_and_specks():
    assert detect(frame()) is None
    assert detect(frame(3, color=(255, 255, 255))) is None  # beyaz/gri işaret füze değil
    assert detect(frame(extra=lambda a: a.__setitem__((slice(55, 65), slice(55, 65)), (255, 30, 30)))) is None
    assert detect(frame(5, blob=1)) is None  # 4 piksel: gürültü
    assert detect(np.zeros((2, 2, 3))) is None


def test_detect_orange_counts_and_thresholds_are_configurable():
    assert detect(frame(8, color=(255, 140, 20))) .clock == 8
    assert detect(frame(8, color=(255, 140, 20)), {"red_margin": 150}) is None


def test_confirmer_needs_several_frames_and_holds_through_blinks():
    c = Confirmer(confirm=3, window=5, hold=1.0)
    hit = Detection(4, 50)
    assert c.feed(hit, 0.0) is None and c.feed(None, 0.1) is None and c.feed(hit, 0.2) is None
    assert c.feed(hit, 0.3) == hit  # 4 karenin 3'ü: doğrulandı
    assert c.feed(None, 0.4) == hit and c.feed(None, 0.5) == hit  # yanıp sönme arası sürüyor
    for t in (0.6, 0.7, 0.8, 0.9, 1.0):
        c.feed(None, t)
    assert c.feed(None, 1.5) is None  # son doğrulamadan (0,4) 1 sn uzun görünmedi: bitti


def test_single_frame_false_alarm_is_ignored():
    c = Confirmer()
    assert all(c.feed(d, i / 10) is None for i, d in enumerate([None, Detection(6, 30), None, None, None, None]))


def test_watcher_reports_confirmed_missile_as_contact():
    frames = iter([frame(4), frame(4), frame(None), frame(4)])
    w = MissileWatcher((0, 0, 120, 120), grab=lambda region: next(frames))
    for i in range(4):
        w.step(i / 10)
    assert w.contacts() == [{"id": "vision", "name": "", "kind": "radar", "clock": 4, "lock": True,
                             "missile": True, "dist_m": None, "vision": True}]


def test_watcher_survives_capture_errors():
    def boom(region):
        raise OSError("ekran kilitli")

    w = MissileWatcher((0, 0, 50, 50), grab=boom)
    assert w.step(0.0) is None and w.errors == 1 and w.contacts() == []


def test_region_parsing():
    assert region_of({"vision_region": [10, 20, 200, 200]}) == (10, 20, 200, 200)
    for bad in (None, [1, 2, 3], [1, 2, 3, 4], ["a", 1, 2, 3]):
        assert region_of({"vision_region": bad}) is None


def test_vision_missile_callout_has_no_type_and_reaches_hud():
    from tests.test_sims import Recorder, st

    from alice.sims.flight import FlightCompanion, TouchdownTracker

    r, shown = Recorder(), []
    tr = TouchdownTracker(FlightCompanion(r.say, show=lambda kind, **kw: shown.append((kind, kw.get("text")))))
    tr.feed(st(0, ias=0, agl=0.5, alt=50))
    tr.feed(st(1, ias=80, agl=30))
    seen = {"id": "vision", "name": "", "kind": "radar", "clock": 4, "lock": True, "missile": True,
            "dist_m": None, "vision": True}
    tr.feed(st(2, extra={"contacts": [seen]}))
    assert r.texts()[-1] == "Füze! Saat 4. Chaff at, dön!"
    assert ("arrow", "FÜZE") in shown


def test_hub_adds_vision_contacts_to_war_thunder_feed(monkeypatch):
    from tests.test_sims_hub import FakeNotifier, FakePart, FakeStore

    from alice.sims import dcs, warthunder
    from alice.sims.hub import SimHub

    FakePart.instances = []
    monkeypatch.setattr(dcs, "ensure_installed", lambda port=0: [])
    monkeypatch.setattr(dcs, "DCSListener", FakePart)
    monkeypatch.setattr(warthunder, "WarThunderPoller", FakePart)

    class Fake:
        def __init__(self, region, cfg):
            self.region, self.poked = region, 0

        def poke(self):
            self.poked += 1

        def contacts(self):
            return [{"id": "vision", "kind": "radar", "clock": 7, "missile": True, "vision": True}]

        def stop(self):
            pass

    monkeypatch.setattr(vision, "MissileWatcher", Fake)
    hb = SimHub({"dcs": False, "missile_vision": True, "vision_region": [0, 0, 100, 100]}, FakeStore(),
                FakeNotifier())
    hb.start()
    try:
        feed = FakePart.instances[0].args[0]
        got = []
        monkeypatch.setattr(hb.companions["warthunder"], "update", lambda s: got.append(s))
        from tests.test_sims import st

        mine = [{"kind": "aircraft", "clock": 1}]
        s = st(0, source="warthunder", extra={"contacts": mine})
        feed(s)
        assert [c["clock"] for c in s.extra["contacts"]] == [1, 7]
        assert mine == [{"kind": "aircraft", "clock": 1}]  # poller'ın listesi değişmedi
    finally:
        hb.stop()


def test_hub_without_region_says_how_to_set_it(monkeypatch):
    from tests.test_sims_hub import FakeNotifier, FakeStore

    from alice.sims.hub import SimHub

    hb = SimHub({"missile_vision": True}, FakeStore(), FakeNotifier())
    assert hb._missile_vision() is None
    assert "alice vision region" in hb.store.events[0][2]
    assert SimHub({}, FakeStore(), FakeNotifier())._missile_vision() is None


def test_test_files_crops_full_screenshots(tmp_path):
    from PIL import Image

    full = np.full((400, 600, 3), 30, dtype=np.uint8)
    full[100:220, 300:420] = frame(2)
    Image.fromarray(full).save(tmp_path / "missile-1.png")
    Image.fromarray(np.full((400, 600, 3), 30, dtype=np.uint8)).save(tmp_path / "clear-1.png")
    lines = vision.test_files(sorted(tmp_path.glob("*.png")), (300, 100, 120, 120), {})
    assert lines == ["clear-1.png: işaret yok", lines[1]]
    assert lines[1].startswith("missile-1.png: işaret var, saat 2")
