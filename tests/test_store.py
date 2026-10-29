from __future__ import annotations

from alice.core.store import Store


def test_old_samples_thinned_to_one_per_minute():
    s = Store(":memory:")
    day = 86400.0
    now = 100 * day
    old = now - 5 * day
    for i in range(120):  # iki dakika, saniyede bir
        s.add_many([("wt.g", float(i % 2), None), ("core.mode", None, "flight" if i < 90 else "normal")], ts=old + i)
    s.add("wt.g", 7.0, ts=now - 60)  # yeni ölçüm dokunulmaz
    removed = s.thin(now)
    assert removed == 240 - 4
    assert [v for _, v in s.series("wt.g")] == [0.5, 0.5, 7.0]
    assert [t for _, t in s.texts("core.mode")] == ["flight", "normal"]  # dakikanın son değeri
    assert s.thin(now) == 0  # aynı aralık bir daha işlenmez
    s.add("wt.g", 1.0, ts=old + 10)  # işlenmiş aralığa sonradan gelen satır olduğu gibi kalır
    assert s.thin(now) == 0
