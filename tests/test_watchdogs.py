from __future__ import annotations

from alice.core.store import Store
from alice.core.watchdogs import GameHealth, NetWatch, outages


def test_game_health_waits_a_minute_names_memory_hog_and_rearms():
    st = Store(":memory:")
    sent = []
    g = GameHealth(st, lambda n: sent.append(n.body) or True)
    t = 1000.0
    st.add("pc.proc.discord.exe.mem_mb", 1843.0, ts=t)
    st.add("pc.proc.aces.exe.mem_mb", 6000.0, ts=t)  # oyunun kendisi önerilmez
    for dt in (0, 30, 61):
        st.add("pc.mem.percent", 94.0, ts=t + dt)
        g.check("flight", t + dt)
    assert sent == ["RAM yüzde 94 dolu, oyun takılabilir. En çok Discord kullanıyor (1.8 GB), kapatabilirsin."]
    st.add("pc.mem.percent", 94.0, ts=t + 200)
    g.check("flight", t + 200)
    assert len(sent) == 1  # 15 dk dolmadan tekrar yok
    st.add("pc.mem.percent", 80.0, ts=t + 260)
    g.check("flight", t + 260)  # düştü: yeniden kuruldu
    for dt in (300, 361):
        st.add("pc.mem.percent", 93.0, ts=t + dt)
        g.check("flight", t + dt)
    assert len(sent) == 2
    st.add("pc.gpu.temp", 95.0, ts=t + 400)
    assert g.check("normal", t + 400) == [] and g.check("normal", t + 470) == []  # oyun dışında susar


def test_net_watch_announces_drop_and_return():
    st = Store(":memory:")
    sent = []
    clock = [0.0]
    results = iter([20.0, None, None, None, None, 25.0])
    w = NetWatch(st, lambda n: sent.append(n.title) or True, probe=lambda: next(results), clock=lambda: clock[0],
                 background=False)
    rows = []
    for t in (0, 30, 60, 90, 120, 150):
        clock[0] = t
        rows += w.collect()
    assert sent == ["İnternet koptu", "İnternet geri geldi"]
    assert ("net.online", 0.0, None) in rows and ("net.latency_ms", 25.0, None) in rows
    assert outages(st, 0) == (1, 2.0)


def test_short_blip_is_silent():
    st = Store(":memory:")
    sent = []
    clock = [0.0]
    results = iter([None, 20.0])
    w = NetWatch(st, lambda n: sent.append(n.title) or True, probe=lambda: next(results), clock=lambda: clock[0],
                 background=False)
    for t in (0, 30):
        clock[0] = t
        w.collect()
    assert sent == [] and outages(st, 0) == (0, 0.0)
