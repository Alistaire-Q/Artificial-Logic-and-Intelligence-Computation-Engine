from __future__ import annotations

import queue
import socket
import time

import pytest

from alice.hud import client, window
from alice.hud.model import MAX_BYTES, HudModel, clock_of, parse
from alice.hud.supervisor import BURST, HudSupervisor


def free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


# ---------------------------------------------------------------- protokol
def test_parse_accepts_known_kinds_and_drops_garbage():
    assert parse(b'{"kind": "arrow", "clock": 4, "ttl": 3}') == {"kind": "arrow", "clock": 4, "ttl": 3}
    assert parse(b'{"kind": "clear"}') == {"kind": "clear"}
    for raw in (b"", b"not json", b"[1, 2]", b'{"kind": "explode"}', b"\xff\xfe", b'{"x": 1}',
                b'{"kind": "label", "text": "' + b"a" * MAX_BYTES + b'"}'):
        assert parse(raw) is None


def test_clock_normalization():
    assert [clock_of(v) for v in (0, 12, 3, "4", 6.4, 13, -1, None, "x")] == [12, 12, 3, 4, 6, None, None, None, None]


def test_invalid_items_are_ignored():
    m = HudModel()
    for msg in ({"kind": "arrow"}, {"kind": "edge", "clock": 15}, {"kind": "box", "x": 1, "y": 2, "w": 3},
                {"kind": "progress", "value": "x"}, {"kind": "label", "text": ""}, {"kind": "card"}):
        assert not m.apply(msg, 0.0)
    assert m.items == [] and m.version == 0


def test_ttl_expiry_and_clamp():
    m = HudModel()
    m.apply({"kind": "arrow", "clock": 4, "ttl": 2}, 10.0)
    m.apply({"kind": "label", "text": "uzun", "ttl": 9999}, 10.0)   # en fazla 60 sn
    m.apply({"kind": "card", "title": "varsayılan"}, 10.0)          # ttl yok: 3 sn
    assert not m.expire(11.9)
    assert m.expire(12.1) and [i.kind for i in m.items] == ["label", "card"]
    assert m.expire(13.1) and [i.kind for i in m.items] == ["label"]
    assert m.items[0].until == 70.0
    assert m.expire(70.0) and m.items == []


def test_same_id_replaces_and_max_items_drops_oldest_calm_first():
    m = HudModel(max_items=3)
    m.apply({"kind": "arrow", "clock": 2, "id": "six", "ttl": 5}, 0.0)
    m.apply({"kind": "arrow", "clock": 6, "id": "six", "ttl": 5}, 1.0)
    assert len(m.items) == 1 and m.items[0].data["clock"] == 6
    m.apply({"kind": "edge", "clock": 6, "severity": "critical"}, 2.0)
    m.apply({"kind": "label", "text": "a"}, 3.0)
    m.apply({"kind": "label", "text": "b"}, 4.0)
    kinds = [(i.kind, i.data.get("text")) for i in m.items]
    assert kinds == [("edge", None), ("label", "a"), ("label", "b")]  # en eski sakin öğe (ok) atıldı
    assert m.items[0].data["severity"] == "critical" and m.items[1].data["severity"] == "info"


def test_clear_and_toggle():
    m = HudModel()
    m.apply({"kind": "arrow", "clock": 1, "id": "a"}, 0.0)
    m.apply({"kind": "arrow", "clock": 2, "id": "b"}, 0.0)
    assert m.apply({"kind": "clear", "id": "a"}, 0.0) and [i.id for i in m.items] == ["b"]
    assert not m.apply({"kind": "clear", "id": "yok"}, 0.0)
    m.apply({"kind": "toggle"}, 0.0)
    assert m.hidden and m.visible() == [] and len(m.items) == 1
    m.apply({"kind": "toggle"}, 0.0)
    assert len(m.visible()) == 1
    assert m.apply({"kind": "clear"}, 0.0) and m.items == []


def test_client_reaches_receiver_over_socket():
    port = free_port()
    sock = window.bind(port)
    assert sock is not None
    assert window.bind(port) is None  # ikinci HUD aynı portu alamaz
    inbox: queue.Queue = queue.Queue()
    rx = window.Receiver(sock, inbox).start()
    try:
        client.configure(port)
        assert client.show("arrow", clock=4, text="SAM · 12 km", severity="warn", id="sam")
        client.send({"kind": "bilinmeyen"})  # alıcı bunu yok sayar
        assert client.clear("sam")
        got = [inbox.get(timeout=2), inbox.get(timeout=2)]
        assert got[0] == {"kind": "arrow", "ttl": 3.0, "clock": 4, "text": "SAM · 12 km", "severity": "warn",
                          "id": "sam"}
        assert got[1] == {"kind": "clear", "id": "sam"}
        assert inbox.empty()
    finally:
        rx.stop()
        client.configure(window.PORT, on=False)


def test_client_never_blocks_when_hud_is_closed():
    client.configure(free_port())  # kimse dinlemiyor
    try:
        t0 = time.monotonic()
        for i in range(200):
            client.show("arrow", clock=i % 12 + 1, ttl=1)
        assert time.monotonic() - t0 < 1.0
        assert client.send({"kind": "arrow", "clock": 1, "bad": object()}) is False  # JSON'a çevrilemez: sessiz
    finally:
        client.configure(window.PORT, on=False)


def test_client_swallows_socket_errors(monkeypatch):
    class Broken:
        def sendto(self, *a):
            raise ConnectionResetError(10054, "kapalı porta önceki paket")

    monkeypatch.setattr(client, "_sock", Broken())
    assert client.show("label", text="x") is False


# ------------------------------------------------------------------ çizim
class FakeCanvas:
    def __init__(self):
        self.ops: list[tuple[str, tuple, dict]] = []

    def __getattr__(self, name):
        def rec(*a, **kw):
            self.ops.append((name, a, kw))

        return rec


def test_painter_draws_every_kind():
    c = FakeCanvas()
    p = window.Painter(c, 1920, 1080, origin=(0, 0))
    m = HudModel(max_items=10)
    for msg in ({"kind": "arrow", "clock": 3, "text": "SAM", "severity": "warn"},
                {"kind": "edge", "clock": 6, "severity": "critical"},
                {"kind": "label", "text": "merhaba"}, {"kind": "card", "title": "ALICE", "text": "deneme"},
                {"kind": "box", "x": 100, "y": 100, "w": 50, "h": 40, "text": "düğme"},
                {"kind": "progress", "value": 0.5, "text": "yükleniyor"}):
        m.apply(msg, 0.0)
    p.draw(m.visible(), 0.5)
    names = [o[0] for o in c.ops]
    assert names[0] == "delete" and "create_polygon" in names and "create_line" in names
    poly = next(o for o in c.ops if o[0] == "create_polygon")
    tip_x, tip_y = poly[1][0][0], poly[1][0][1]
    assert tip_x > 960 + 200 and abs(tip_y - 540) < 1  # saat 3: sağa bakan ok
    assert any(o[2].get("text") == "merhaba" for o in c.ops)


def test_edge_point_and_direction():
    assert window.edge_point(12, 1000, 500)[:2] == pytest.approx((500, 0))
    x, y, side = window.edge_point(3, 1000, 500)
    assert (round(x), round(y), side) == (1000, 250, True)
    x, y, side = window.edge_point(6, 1000, 500)
    assert (round(x), round(y), side) == (500, 500, False)
    assert window.direction(9) == pytest.approx((-1, 0), abs=1e-9)


class FakeWin32:
    def __init__(self):
        self.calls: list[tuple] = []

    def style(self, hwnd):
        self.calls.append(("style", hwnd))

    def exclude_from_capture(self, hwnd):
        self.calls.append(("capture", hwnd))
        return True

    def show(self, hwnd, visible):
        self.calls.append(("show", visible))

    def topmost(self, hwnd):
        self.calls.append(("topmost",))


def test_window_tick_shows_only_while_items_exist():
    """Boşken pencere gizli (oyunun FPS'i için), öğe gelince odak almadan görünür, ttl dolunca yine gizlenir."""
    w = window.HudWindow.__new__(window.HudWindow)
    w.model, w.inbox, w.win32, w.hwnd = HudModel(), queue.Queue(), FakeWin32(), 42
    w.canvas = FakeCanvas()
    w.painter = window.Painter(w.canvas, 800, 600)
    w.shown, w._drawn, w._topmost_at = False, -1, 0.0
    w.tick()
    assert w.win32.calls == []
    w.inbox.put({"kind": "arrow", "clock": 4, "ttl": 0.2})
    w.tick()
    assert ("show", True) in w.win32.calls and w.shown
    time.sleep(0.25)
    w.tick()
    assert w.win32.calls[-1] == ("show", False) and not w.shown


# --------------------------------------------------------------- denetleyici
class FakeProc:
    def __init__(self):
        self.code = None
        self.terminated = False

    def poll(self):
        return self.code

    def terminate(self):
        self.terminated = True
        self.code = 1

    def wait(self, timeout=None):
        return self.code


def test_supervisor_restarts_crashed_hud_and_backs_off():
    spawned: list[FakeProc] = []

    def spawn(argv):
        assert argv[1:4] == ["-m", "alice", "hud"] and "--parent" in argv
        spawned.append(FakeProc())
        return spawned[-1]

    sv = HudSupervisor(41240, spawn=spawn)
    sv.check(0.0)
    sv.check(1.0)
    assert len(spawned) == 1  # çalışıyor: dokunma
    for i in range(1, BURST):
        spawned[-1].code = 1  # çöktü
        sv.check(float(i))
    assert len(spawned) == BURST and sv.restarts == BURST - 1
    spawned[-1].code = 1
    sv.check(10.0)  # dakikada 5 çökme: bekle
    sv.check(20.0)
    assert len(spawned) == BURST
    sv.check(41.0)
    assert len(spawned) == BURST + 1
    sv.stop()
    assert spawned[-1].terminated


# ---------------------------------------------------------- uçuş -> HUD
def test_flight_threats_also_show_on_hud():
    from tests.test_sims import Recorder, st

    from alice.sims.flight import FlightCompanion, TouchdownTracker

    r, shown = Recorder(), []
    c = FlightCompanion(r.say, show=lambda kind, **kw: shown.append((kind, kw)))
    tr = TouchdownTracker(c)
    tr.feed(st(0, ias=0, agl=0.5, alt=50))
    tr.feed(st(1, ias=80, agl=30))
    radar = {"id": "7", "clock": 2, "name": "SA-6", "lock": True, "missile": False, "dist_m": None, "kind": "radar"}
    tr.feed(st(2, extra={"contacts": [radar]}))
    tr.feed(st(3, extra={"contacts": [{**radar, "missile": True}]}))
    sam = {"id": "", "clock": 10, "dist_m": 12000.0, "kind": "sam", "name": "SAM", "range_m": 18000.0}
    tr.feed(st(4, extra={"contacts": [sam]}))
    bandit = {"id": "", "clock": 6, "dist_m": 1200.0, "kind": "aircraft", "name": "Fighter"}
    tr.feed(st(20, extra={"contacts": [bandit]}))  # SAM'dan 12 sn sonra: ses sırası gelmiş
    hot = {"id": "", "clock": 1, "dist_m": 8000.0, "kind": "aircraft", "name": "Fighter", "aspect": 5.0}
    for t in (40, 41, 43):
        tr.feed(st(t, extra={"contacts": [hot]}))
    got = [(k, kw["clock"], kw.get("text"), kw["severity"], kw["id"]) for k, kw in shown]
    assert got == [
        ("arrow", 2, "KİLİT · SA-6", "warn", "lock"),
        ("arrow", 2, "FÜZE · SA-6", "critical", "missile"),
        ("edge", 2, None, "critical", "missile.edge"),
        ("arrow", 10, "SAM · 12,0 km", "warn", "sam:SAM"),
        ("arrow", 6, "ARKANDA · 1,2 km", "critical", "six"),
        ("edge", 6, None, "critical", "six.edge"),
        ("arrow", 1, "BURNU SANA · 8,0 km", "warn", "nose"),
    ]
    assert all(kw["ttl"] > 0 for _, kw in shown)
    assert any(x.startswith("Arkanda düşman") for x in r.texts())  # ses aynen devam ediyor


def test_flight_hud_failure_does_not_break_callouts():
    from tests.test_sims import Recorder, st

    from alice.sims.flight import FlightCompanion, TouchdownTracker

    def boom(kind, **kw):
        raise RuntimeError("HUD bozuk")

    r = Recorder()
    tr = TouchdownTracker(FlightCompanion(r.say, show=boom))
    tr.feed(st(0, ias=0, agl=0.5, alt=50))
    tr.feed(st(1, ias=80, agl=30))
    radar = {"id": "7", "clock": 2, "name": "SA-6", "lock": False, "missile": True, "dist_m": None, "kind": "radar"}
    tr.feed(st(2, extra={"contacts": [radar]}))
    assert "Füze! Saat 2, SA-6. Chaff at, dön!" in r.texts()


def test_hub_passes_show_to_companions():
    from alice.sims.hub import SimHub

    marker = []
    hb = SimHub({}, store=None, notifier=None, show=lambda kind, **kw: marker.append(kind))
    hb.companion("warthunder").show("arrow", clock=1)
    assert marker == ["arrow"]


def test_hud_arrow_follows_threat_while_voice_stays_quiet():
    """Dönerken saat 6'daki düşman saat 4'e geçer: ses seyrek, ok yeni yönü gösterir (ses susturulsa da)."""
    from tests.test_sims import Recorder, st

    from alice.sims.flight import FlightCompanion, TouchdownTracker

    r, shown = Recorder(), []
    c = FlightCompanion(r.say, show=lambda kind, **kw: shown.append((kind, kw)))
    tr = TouchdownTracker(c)
    tr.feed(st(0, ias=0, agl=0.5, alt=50))
    tr.feed(st(1, ias=80, agl=30))
    bandit = {"id": "", "clock": 6, "dist_m": 1200.0, "kind": "aircraft", "name": "Fighter"}
    tr.feed(st(2, extra={"contacts": [bandit]}))
    tr.feed(st(2.2, extra={"contacts": [{**bandit, "clock": 5}]}))   # çok sık: gönderilmez
    tr.feed(st(3, extra={"contacts": [{**bandit, "clock": 4, "dist_m": 900.0}]}))
    for t in range(5, 24, 2):  # sürekli arkada: 20 sn'den sonra ok susar
        tr.feed(st(t, extra={"contacts": [{**bandit, "clock": 5}]}))
    tr.feed(st(30, extra={"contacts": [{**bandit, "clock": 8}]}))    # kaybolup döndü: ses yok, ok var
    tr.feed(st(48, extra={"contacts": [{**bandit, "clock": 7}]}))    # 45 sn geçti: yeniden söylenir
    arrows = [(kw["clock"], kw["ttl"]) for k, kw in shown if k == "arrow"]
    assert arrows[:3] == [(6, 4.0), (4, 2.5), (5, 2.5)]
    assert arrows[3:-2] == [(5, 2.5)] * 8  # 5..21: görülmenin ilk 20 sn'si
    assert arrows[-2:] == [(8, 2.5), (7, 4.0)]
    assert [x for x in r.texts() if x.startswith("Arkanda")] == ["Arkanda düşman, saat 6, 1,2 kilometre!",
                                                                 "Arkanda düşman, saat 7, 1,2 kilometre!"]


def test_hud_shows_threats_the_voice_skipped():
    """Gemi grubunda yalnız en yakını söylenir; diğeri sesle değil, HUD'da okla görünür."""
    from tests.test_sims import Recorder, st

    from alice.sims.flight import FlightCompanion, TouchdownTracker

    r, shown = Recorder(), []
    c = FlightCompanion(r.say, show=lambda kind, **kw: shown.append(kw["id"]))
    tr = TouchdownTracker(c)
    tr.feed(st(0, ias=0, agl=0.5, alt=50))
    tr.feed(st(1, ias=80, agl=30))
    ships = [{"id": "", "clock": 11, "dist_m": 7200.0, "kind": "sam", "name": "füzeli muhrip", "range_m": 9000.0},
             {"id": "", "clock": 10, "dist_m": 8200.0, "kind": "sam", "name": "fırkateyn", "range_m": 9000.0}]
    tr.feed(st(2, extra={"contacts": ships}))
    tr.feed(st(3, extra={"contacts": ships}))
    assert [x for x in r.texts() if "menzilindesin" in x] == ["Füzeli muhrip menzilindesin: saat 11, 7,2 kilometre."]
    assert shown == ["sam:füzeli muhrip", "sam:füzeli muhrip", "sam:fırkateyn"]


def test_exclusive_fullscreen_said_once_only_with_hud(monkeypatch):
    from alice.hud import screen
    from alice.sims.hub import SimHub

    monkeypatch.setattr(screen, "exclusive_fullscreen", lambda: True)
    said = []
    quiet = SimHub({}, store=None, notifier=None)
    monkeypatch.setattr(quiet, "_say", lambda *a: said.append(a))
    quiet._fullscreen_check("War Thunder")
    assert said == []  # HUD kapalı: söylenecek bir şey yok
    hb = SimHub({}, store=None, notifier=None, show=lambda kind, **kw: None)
    monkeypatch.setattr(hb, "_say", lambda *a: said.append(a))
    hb._fullscreen_check("War Thunder")
    hb._fullscreen_check("War Thunder")
    assert len(said) == 1 and said[0][0].startswith("War Thunder özel tam ekranda")


def test_exclusive_fullscreen_reads_notification_state(monkeypatch):
    import sys
    from types import SimpleNamespace

    from alice.hud import screen

    if sys.platform != "win32":
        assert screen.exclusive_fullscreen() is False
        return

    def fake(value, ret=0):
        def q(ref):
            ref._obj.value = value
            return ret

        return SimpleNamespace(shell32=SimpleNamespace(SHQueryUserNotificationState=q))

    import ctypes

    for value, ret, want in ((3, 0, True), (2, 0, False), (3, 1, False)):
        monkeypatch.setattr(ctypes, "windll", fake(value, ret))
        assert screen.exclusive_fullscreen() is want


# ------------------------------------------------------- bildirim, konuşma, tıklama
@pytest.fixture
def hud_on(monkeypatch):
    sent = []
    monkeypatch.setattr(client, "active", True)
    monkeypatch.setattr(client, "send", lambda msg: sent.append(msg) or True)
    return sent


def test_views_do_nothing_when_hud_is_off(monkeypatch):
    from types import SimpleNamespace

    from alice.hud import views

    monkeypatch.setattr(client, "active", False)
    monkeypatch.setattr(client, "send", lambda msg: pytest.fail("HUD kapalıyken gönderilmemeli"))
    t0 = time.monotonic()
    assert not views.click_target(10, 10)
    assert not views.speech("1. bir\n2. iki")
    assert not views.notification(SimpleNamespace(kind="alert", topic="pc", severity="warn", title="t", body="b"))
    assert time.monotonic() - t0 < 0.1  # tıklama önizlemesi beklemez


def test_notification_card_skips_flight(hud_on):
    from alice.core.notify import Notification
    from alice.hud import views

    assert views.notification(Notification("ALICE", "Ekran kartı  88°C,\nçok sıcak.", "critical", "pc"))
    assert not views.notification(Notification("Uçuş", "Arkanda düşman", "warn", "flight", kind="flight"))
    assert hud_on == [{"kind": "card", "ttl": 12.0, "title": "ALICE", "text": "Ekran kartı 88°C, çok sıcak.",
                       "severity": "critical", "id": "note:ALICE"}]


def test_speech_summary_shows_lists_and_numbers_only():
    from alice.hud.views import summarize

    assert summarize("Tamam, açıyorum.") is None
    assert summarize("Bugün üç işin var:\n- **Okul** 9'da\n- Staj 14'te\n- Spor") == "Okul 9'da · Staj 14'te · Spor"
    assert summarize("Ekran kartı 72 derece.") == "Ekran kartı 72 derece."
    long = summarize("Uzun bir cümle. " * 40)
    assert long.endswith("…") and len(long) <= 260


def test_click_target_previews_before_click(hud_on, monkeypatch):
    from alice.hud import views

    waited = []
    monkeypatch.setattr(views.time, "sleep", waited.append)
    assert views.click_target(500, 300, "double_click")
    assert hud_on[0]["kind"] == "box" and (hud_on[0]["x"], hud_on[0]["y"]) == (472, 272)
    assert hud_on[0]["text"] == "çift tıklıyorum" and waited == [views.CLICK_PREVIEW]


def test_voice_ui_sends_speech_card(hud_on):
    from alice.voice.assistant import VoiceUI

    class Spk:
        speaking = False

        def say(self, *a, **kw):
            return True

        def stop(self):
            pass

    ui = VoiceUI(Spk())
    ui.begin_turn()
    ui.stream_end("Yarın:\n1. Okul\n2. Staj", "model")
    assert [m["text"] for m in hud_on if m.get("id") == "speech"] == ["Okul · Staj"]


def test_start_hud_replaces_windows_toast_with_card(monkeypatch):
    from types import SimpleNamespace

    from alice.core import cli as core_cli
    from alice.core.notify import Notifier
    from alice.hud import supervisor

    monkeypatch.setattr(supervisor.HudSupervisor, "start", lambda self: self)
    nt = Notifier(store=None, policy=lambda: None)
    core = SimpleNamespace(notifier=nt)
    cfg = SimpleNamespace(data={"hud": {"enabled": True, "port": 41277}})
    try:
        sv = core_cli._start_hud(cfg, core)
        assert sv.port == 41277 and client.active
        assert nt._desktop not in nt.channels and len(nt.channels) == 1
    finally:
        client.configure(window.PORT, on=False)


# ------------------------------------------------------- sistem göstergesi
def test_system_gauge_below_and_above_limit(hud_on):
    from alice.hud import views

    assert not views.system_gauge("GPU", 80, 93, "°C")  # %90'ın altı: sessiz
    assert views.system_gauge("GPU", 87, 93, "°C", ttl=20)
    assert views.system_gauge("RAM", 96, 95, "%")
    assert not views.system_gauge("GPU", 87, 93, "°C", warn=88)  # özel başlangıç değeri
    assert hud_on == [
        {"kind": "progress", "ttl": 20, "value": 0.935, "text": "GPU 87°C", "severity": "warn", "id": "sys:GPU"},
        {"kind": "progress", "ttl": 25.0, "value": 1.0, "text": "RAM %96", "severity": "critical", "id": "sys:RAM"},
    ]


def test_system_gauge_silent_when_hud_is_off(monkeypatch):
    from alice.core.watchdogs import SystemGauges
    from alice.hud import views

    monkeypatch.setattr(client, "active", False)
    monkeypatch.setattr(client, "send", lambda msg: pytest.fail("HUD kapalıyken gönderilmemeli"))

    class NoStore:
        def last(self, key):
            pytest.fail("HUD kapalıyken depoya bakılmamalı")

    assert not views.system_gauge("GPU", 99, 93, "°C")
    assert SystemGauges(NoStore())(None) == []


class GaugeStore:
    def __init__(self, values, ts=1000.0):
        self.values, self.ts = values, ts

    def last(self, key):
        return (self.ts, self.values[key], None) if key in self.values else None


def test_system_gauges_hook_refreshes_same_id_and_fades(hud_on):
    from alice.core.watchdogs import SystemGauges

    store = GaugeStore({"pc.gpu.temp": 89.0, "pc.mem.percent": 85.0, "pc.cpu.temp": 70.0})
    hook = SystemGauges(store, interval=15, clock=lambda: 1005.0)
    assert hook(None) == ["GPU"] and hook(None) == ["GPU"]  # her turda yeni paket, aynı kimlik
    assert [m["id"] for m in hud_on] == ["sys:GPU", "sys:GPU"]
    assert hud_on[0]["ttl"] == 27.5 and hud_on[0]["severity"] == "warn"
    store.values["pc.mem.percent"] = 96.0
    assert hook(None) == ["GPU", "RAM"] and hud_on[-1]["severity"] == "critical"
    store.values.update({"pc.gpu.temp": 75.0, "pc.mem.percent": 60.0})
    n = len(hud_on)
    assert hook(None) == [] and len(hud_on) == n  # normale döndü: gönderim durur, çubuk ttl ile söner
    store.values["pc.gpu.temp"] = 99.0
    store.ts = 800.0  # bayat ölçüm çubuk açmaz
    assert hook(None) == []


def test_start_hud_adds_system_gauge_hook(monkeypatch):
    from types import SimpleNamespace

    from alice.core import cli as core_cli
    from alice.core.notify import Notifier
    from alice.core.watchdogs import SystemGauges
    from alice.hud import supervisor

    monkeypatch.setattr(supervisor.HudSupervisor, "start", lambda self: self)
    core = SimpleNamespace(notifier=Notifier(store=None, policy=lambda: None), hooks=[], store=GaugeStore({}),
                           interval=10.0)
    try:
        core_cli._start_hud(SimpleNamespace(data={"hud": {"enabled": True, "port": 41278}}), core)
        assert len(core.hooks) == 1 and isinstance(core.hooks[0], SystemGauges) and core.hooks[0].ttl == 20.0
    finally:
        client.configure(window.PORT, on=False)
