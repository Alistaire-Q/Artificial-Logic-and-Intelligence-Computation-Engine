from __future__ import annotations

import json
import socket
import time
import zipfile

import httpx
import pytest

from alice.sims import checklists, kneeboard
from alice.sims.dcs import DCSListener, install, parse_lua_table, read_mission, to_state, uninstall
from alice.sims.flight import KT, FlightCompanion, FlightState, TouchdownTracker, save_debrief
from alice.sims.warthunder import WarThunderPoller


class Recorder:
    def __init__(self):
        self.said: list[tuple[str, str, str]] = []
        self.debriefs = []

    def say(self, text, severity, emotion):
        self.said.append((text, severity, emotion))

    def texts(self):
        return [t for t, _, _ in self.said]


def make(hr=None):
    r = Recorder()
    c = FlightCompanion(r.say, hr_stats=lambda a, b: {"avg": 110, "max": 142, "min": 80},
                        hr_now=lambda: hr, on_debrief=r.debriefs.append)
    return c, TouchdownTracker(c), r


def st(t, **kw):
    base = dict(source="dcs", aircraft="F-16C_50", ias=150.0, tas=160.0, alt=3000.0, agl=2900.0, vv=0.0, aoa=3.0,
                g=1.0, fuel_kg=3000.0, burn_kgs=0.8, chaff=60, flare=30, gear=0.0, x=0.0, z=0.0, wall=1_000_000 + t)
    base.update(kw)
    return FlightState(t=t, **base)


def test_full_flight():
    c, tr, r = make()
    t = 0.0
    tr.feed(st(t, ias=0, agl=0.5, alt=50))                     # yerde
    t += 1
    tr.feed(st(t, ias=80, agl=30))                              # kalkış
    assert c.phase == "air" and c.home == (0.0, 0.0)
    fuel = 3000.0
    # 100 km uzağa uçarken yakıt yanıyor: bir yerde "eve dön" uyarısı gelmeli
    x = 0.0
    while fuel > 700:
        t += 1
        fuel -= 1.0
        x += 200
        tr.feed(st(t, fuel_kg=fuel, burn_kgs=1.0, x=x))
    texts = " ".join(r.texts())
    assert "Eve dönmek için" in texts and "Bingo" in texts
    bingo = next(s for s in r.said if s[0].startswith("Bingo"))
    assert bingo[1] == "critical" and bingo[2] == "urgent"
    n = len(r.said)
    for _ in range(5):
        t += 1
        tr.feed(st(t, fuel_kg=fuel, burn_kgs=1.0, x=x))
    assert len(r.said) == n                                     # tekrar etmez

    # uzun süre 8 G
    for _ in range(7):
        t += 1
        tr.feed(st(t, g=8.2, fuel_kg=fuel, x=x))
    assert any("G üstündesin" in s for s in r.texts())

    # flare 30 -> 8 -> 0
    tr.feed(st(t + 0.1, flare=8, fuel_kg=fuel, x=x))
    tr.feed(st(t + 0.2, flare=0, fuel_kg=fuel, x=x))
    assert "Flare azaldı, 8 kaldı." in r.texts() and "Flare bitti." in r.texts()

    # yaklaşma: önce hızlı, sonra on speed, sonra teker koyma
    r.said.clear()
    for i, (agl, aoa) in enumerate([(500, 8), (450, 8), (400, 13), (350, 13), (250, 13), (150, 13), (80, 13),
                                    (40, 12.5), (10, 13), (2, 13), (0.3, 13)]):
        t += 2
        tr.feed(st(t, gear=1.0, ias=75, agl=agl, aoa=aoa, vv=-3.5 if agl > 2 else -1.2, fuel_kg=fuel, x=0, flare=0))
    assert r.texts()[:2] == ["Hızlısın.", "On speed, iyi."]
    # durma
    for _ in range(7):
        t += 1
        tr.feed(st(t, gear=1.0, ias=10, agl=0.2, vv=0, fuel_kg=fuel, x=0, flare=0))
    assert len(r.debriefs) == 1 and c.phase == "ground"
    d = r.debriefs[0]
    assert d.max_g >= 8.2 and d.flare_used == 30 and d.approach_on_speed_pct == 100
    assert d.touchdown_vv_ms == pytest.approx(-1.2)
    summary = d.summary()
    assert "8.2 G" in summary and "on speed" in summary and "yumuşaktı" in summary and "Nabzın ortalama 110" in summary
    assert r.said[-1][0] == summary


def test_high_heart_rate_calms_voice():
    c, tr, r = make(hr=150)
    tr.feed(st(0, ias=80, agl=30))
    fuel = 900.0
    for i in range(1, 80):
        fuel -= 1
        tr.feed(st(i, fuel_kg=fuel, burn_kgs=1.0, x=i * 2000))
    assert r.said and all(e == "calm" for _, _, e in r.said)


def test_restricted_and_session_end():
    c, tr, r = make()
    c.restricted()
    c.restricted()
    assert len(r.said) == 1 and "kontrol listeleri" in r.said[0][0]
    tr.feed(st(0, ias=80, agl=30))
    for i in range(1, 90):
        tr.feed(st(i, fuel_kg=3000 - i))
    c.session_end()
    assert len(r.debriefs) == 1 and r.debriefs[0].touchdown_vv_ms is None


def test_save_debrief_html(tmp_path):
    c, tr, r = make()
    tr.feed(st(0, ias=80, agl=30))
    for i in range(1, 120):
        tr.feed(st(i, alt=3000 + i * 10, g=1 + (i % 5)))
    c.session_end()
    page = save_debrief(r.debriefs[0], [(1_000_000 + i, 90 + i % 20) for i in range(0, 120, 5)], out=tmp_path)
    html = page.read_text(encoding="utf-8")
    assert "<svg" in html and "Nabız" in html and "İrtifa" in html
    assert json.loads(page.with_suffix(".json").read_text())["aircraft"] == "F-16C"


# ------------------------------------------------------------------- DCS
def test_to_state_fuel_fraction_and_kg():
    s = to_state({"aircraft": "F-16C_50", "fuel_int": 0.5, "fuel_ext": 0, "ias": 100, "chaff": 60.0})
    assert s.fuel_kg == pytest.approx(3249 * 0.5) and s.chaff == 60
    assert to_state({"aircraft": "X", "fuel_int": 2500.0}).fuel_kg == 2500


def test_dcs_listener_udp():
    got, restricted, ended, metrics = [], [], [], []
    lis = DCSListener(got.append, lambda: restricted.append(1), lambda: ended.append(1), metrics.extend, port=0).start()
    try:
        tx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        port = lis.bound_port
        for msg in ({"event": "start"}, {"t": 1, "aircraft": "F-16C_50", "ias": 120, "fuel_int": 0.4},
                    {"t": 2, "restricted": True}, b"bozuk", {"event": "stop"}):
            tx.sendto(msg if isinstance(msg, bytes) else json.dumps(msg).encode(), ("127.0.0.1", port))
        for _ in range(100):
            if ended:
                break
            time.sleep(0.02)
    finally:
        lis.stop()
    assert len(got) == 1 and got[0].ias == 120 and restricted and ended
    assert ("dcs.ias_kt", pytest.approx(120 / KT), None) in metrics


def test_install_is_idempotent_and_keeps_other_exports(tmp_path):
    d = tmp_path / "DCS"
    (d / "Scripts").mkdir(parents=True)
    (d / "Scripts" / "Export.lua").write_text("dofile(lfs.writedir()..'Scripts/TacviewGameExport.lua')\n")
    install([d], port=45000)
    install([d], port=45000)
    text = (d / "Scripts" / "Export.lua").read_text()
    assert text.count("alice_export.lua") == 1 and "Tacview" in text
    assert '"127.0.0.1", 45000, 10' in (d / "Scripts" / "alice_export.lua").read_text()
    uninstall([d])
    assert "alice" not in (d / "Scripts" / "Export.lua").read_text() and "Tacview" in (d / "Scripts" / "Export.lua").read_text()


MISSION = r'''
mission =
{
    ["theatre"] = "Caucasus",
    ["descriptionText"] = "DictKey_descriptionText_1",
    ["coalition"] =
    {
        ["blue"] =
        {
            ["country"] =
            {
                [1] =
                {
                    ["plane"] =
                    {
                        ["group"] =
                        {
                            [1] =
                            {
                                ["name"] = "Viper-1",
                                ["frequency"] = 305.5,
                                ["route"] = {
                                    ["points"] = {
                                        [1] = { ["x"] = 0, ["y"] = 0, ["alt"] = 15.24, ["name"] = "", ["type"] = "TakeOff", },
                                        [2] = { ["x"] = 18520, ["y"] = 0, ["alt"] = 6096, ["name"] = "DictKey_WptName_2", ["speed"] = 257.2, },
                                        [3] = { ["x"] = 18520, ["y"] = 18520, ["alt"] = 7620, ["name"] = "HEDEF", }, -- end of [3]
                                    },
                                },
                                ["units"] = {
                                    [1] = { ["type"] = "F-16C_50", ["skill"] = "Client", ["name"] = "Viper-1-1", },
                                },
                            },
                        },
                    },
                },
            },
        },
    },
} -- end of mission
'''
DICT = 'dictionary = { ["DictKey_descriptionText_1"] = "Köprüyü vur.\\nSAM tehdidi var.", ["DictKey_WptName_2"] = "IP", }'


@pytest.fixture
def miz(tmp_path):
    p = tmp_path / "Gorev.miz"
    with zipfile.ZipFile(p, "w") as z:
        z.writestr("mission", MISSION)
        z.writestr("l10n/DEFAULT/dictionary", DICT)
    return p


def test_lua_parser_basics():
    t = parse_lua_table('x = { [1] = "a\\"b", ["k"] = -1.5e2, flag = true, nested = { 1, 2, }, ["n"] = nil, }')
    assert t == {1: 'a"b', "k": -150.0, "flag": True, "nested": {1: 1, 2: 2}, "n": None}


def test_read_mission(miz):
    m = read_mission(miz)
    assert (m.aircraft, m.group, m.frequency_mhz) == ("F-16C_50", "Viper-1", 305.5)
    assert [w.name for w in m.waypoints] == ["Kalkış", "IP", "HEDEF"]
    legs = m.legs()
    assert legs[1][1] == pytest.approx(10.0) and legs[1][2] == pytest.approx(0.0)
    assert legs[2][2] == pytest.approx(90.0) and "Köprüyü vur" in m.description
    assert m.waypoints[2].alt_ft == pytest.approx(25000)


def test_kneeboard_pages(miz, tmp_path, isolated_home):
    kneeboard.add_note("Tanker 251.0, TACAN 50X")
    pages = kneeboard.render(read_mission(miz), kneeboard.load_notes())
    assert len(pages) == 3 and pages[0].size == (768, 1024)
    out = kneeboard.write(pages, [tmp_path / "Kneeboard"])
    assert [p.name for p in out] == ["ALICE_01.png", "ALICE_02.png", "ALICE_03.png"]
    out = kneeboard.write(pages[:1], [tmp_path / "Kneeboard"])
    assert sorted(p.name for p in (tmp_path / "Kneeboard").iterdir()) == ["ALICE_01.png"]


# --------------------------------------------------------------- checklists
def test_checklists(isolated_home):
    assert checklists.find("iniş kontrol listesi").key == "inis"
    assert checklists.find("iniş sonrası").key == "inis-sonrasi"
    assert checklists.find("kalkış").key == "kalkis"
    run = checklists.Runner(checklists.find("iniş"))
    assert run.current().startswith("Adım 1/8: Master Arm")
    for _ in range(8):
        run.next()
    assert run.done and "tamam" in run.current()
    (isolated_home / "checklists").mkdir(parents=True)
    (isolated_home / "checklists" / "inis.md").write_text("# Benim inişim\n- Bir\n- İki\n", encoding="utf-8")
    assert checklists.find("iniş").steps == ["Bir", "İki"]


def test_checklist_tool(tmp_path):
    from alice.config import Config, default_config_dict
    from alice.tools import ToolContext
    from alice.tools.flight import Flight

    ctx = ToolContext(cwd=tmp_path, config=Config(default_config_dict()))
    tool = Flight()
    assert "Adım 1/" in tool.run(ctx, action="checklist_start", name="kalkış").text
    assert "Adım 2/" in tool.run(ctx, action="checklist_next").text
    assert "Adım 1/" in tool.run(ctx, action="checklist_prev").text
    assert tool.risk_for({"action": "kneeboard_note"}) == "write" and tool.risk_for({"action": "status"}) == "read"
    assert "çalışmıyor" in tool.run(ctx, action="status").text


# ------------------------------------------------------------- War Thunder
def test_warthunder_poller():
    def handler(req):
        if req.url.path == "/state":
            return httpx.Response(200, json={"valid": True, "IAS, km/h": 540, "TAS, km/h": 600, "H, m": 2000,
                                             "Ny": 3.5, "AoA, deg": 6.0, "Vy, m/s": -2, "Mfuel, kg": 900,
                                             "gear, %": 0})
        return httpx.Response(200, json={"valid": True, "type": "f_16a"})

    got, metrics = [], []
    p = WarThunderPoller(got.append, on_metrics=metrics.extend,
                         client=httpx.Client(base_url="http://wt", transport=httpx.MockTransport(handler)))
    s = p.poll_once()
    assert s.ias == pytest.approx(150) and s.fuel_kg == 900 and s.aircraft == "f_16a" and s.gear == 0
    assert any(k == "wt.g" for k, _, _ in metrics)

    ended = []
    down = WarThunderPoller(got.append, on_end=lambda: ended.append(1), client=httpx.Client(
        base_url="http://wt", transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"valid": False}))))
    down.active = True
    assert down.poll_once() is None and ended


def test_export_lua_runs_in_luajit():
    """Betiği DCS'in kullandığı LuaJIT'te sahte DCS fonksiyonlarıyla çalıştır (lupa kuruluysa)."""
    lupa = pytest.importorskip("lupa.luajit21")
    from alice.sims.dcs import SCRIPT

    lua = lupa.LuaRuntime(unpack_returned_tuples=True)
    sent = []
    lua.globals().py_send = sent.append
    lua.execute('''
      package.preload["socket"] = function() return { udp = function() return {
        settimeout = function() end, setpeername = function() end, close = function() end,
        send = function(self, s) py_send(s) end } end } end
      T = 0
      function LoGetModelTime() return T end
      SELF = { Name = "F-16C_50", LatLongAlt = {Alt = 1500}, Position = {x = 1, z = 2} }
      function LoGetSelfData() return SELF end
      function LoGetIndicatedAirSpeed() return 150 end
      function LoGetEngineInfo() return {fuel_internal = 0.5, FuelConsumption = {left = 0.8, right = 0}} end
      function LoGetSnares() return {chaff = 60, flare = 30} end
      LoGetPilotName = function() error("yok") end
      CALLS = 0
      LuaExportAfterNextFrame = function() CALLS = CALLS + 1 end
    ''')
    lua.execute(SCRIPT.read_text(encoding="utf-8"))
    g = lua.globals()
    g.LuaExportStart()
    for i in range(10):
        g.T = i * 0.05
        g.LuaExportAfterNextFrame()
    lua.execute("SELF = nil")
    g.T = 5
    g.LuaExportAfterNextFrame()
    g.LuaExportStop()
    msgs = [json.loads(s) for s in sent]
    assert msgs[0] == {"event": "start"} and msgs[-1] == {"event": "stop"}
    assert g.CALLS == 11  # önceki export betiği de çalışmaya devam ediyor
    data = [m for m in msgs if "aircraft" in m]
    assert len(data) == 5 and data[0]["burn"] == 0.8 and data[0]["chaff"] == 60
    assert msgs[-2] == {"t": 5, "restricted": True}


def test_dcs_ensure_installed_only_when_missing_or_changed(tmp_path):
    from alice.sims import dcs

    d = tmp_path / "DCS"
    d.mkdir()
    assert dcs.ensure_installed([d]) == [d]
    assert "alice_export.lua" in (d / "Scripts" / "Export.lua").read_text(encoding="utf-8")
    assert dcs.ensure_installed([d]) == []  # zaten kurulu
    (d / "Scripts" / "alice_export.lua").write_text("eski", encoding="utf-8")
    assert dcs.ensure_installed([d]) == [d]  # betik değişmiş: yenile
    assert (d / "Scripts" / "Export.lua").read_text(encoding="utf-8").count("alice_export.lua") == 1


def test_checklist_after_takeoff_exists():
    from alice.sims.checklists import find

    cl = find("kalkış sonrası")
    assert cl is not None and cl.key == "kalkis-sonrasi"


def test_home_nav_bearing_turn_and_alert():
    from alice.sims.flight import NM, FlightCompanion, FlightState

    said = []
    c = FlightCompanion(lambda text, sev="info", emo=None: said.append(text), lambda a, b: None, lambda: None,
                        lambda d: None)
    c.home = (0.0, 0.0)
    # üssün 20 nm güneybatısındayız (x kuzey, z doğu), burun 212°: üs kuzeydoğuda (~045°)
    s = FlightState(t=1, x=-20 * NM * 0.7071, z=-20 * NM * 0.7071, heading=212)
    nav = c.home_nav(s)
    assert round(nav["bearing"]) == 45 and round(nav["nm"]) == 20
    assert round(nav["turn"]) == -167  # 167° sola (en kısa dönüş)
    c.home_alert_nm = 10
    c._home_alert(s)
    assert said == []
    c._home_alert(FlightState(t=2, x=-9 * NM, z=0, heading=0))
    assert said == ["Üsse 9 mil kaldı."] and c.home_alert_nm is None
