"""Silinen sohbet hafızadan da gider; yeni sohbet eski sohbetin devamı sanılmaz."""

from __future__ import annotations

import time

from alice import session as sess
from alice.memory import LongMemory
from tests.conftest import tool_call  # noqa: F401 - app fikstürü için
from tests.test_app import app, collect  # noqa: F401 - fikstür


def _age(m: LongMemory, seconds: float) -> None:
    with m._lock:
        m.db.execute("UPDATE episodes SET ts = ts - ?", (seconds,))
        m.db.commit()


def test_forget_session_by_id_and_by_legacy_text():
    m = LongMemory(":memory:", {})
    a = m.record("rastgele bir şey yazıyorum burada", "tamam", "yazı", sid="s1")
    b = m.record("başka sohbetteki önemli konu", "peki", "yazı", sid="s2")
    old = m.record("kimliksiz eski tur metni burada", "evet", "yazı")  # sid sütunundan önceki kayıt
    other_old = m.record("kimliksiz ama başka sohbetin turu", "hı", "yazı")
    now = time.time()
    with m._lock:
        m.db.execute("INSERT INTO summaries(start, end, channel, text) VALUES (?, ?, '', 'silinecek sohbetin özeti')",
                     (now - 5, now + 5))
        m.db.commit()
    got = m.forget_session("s1", ["kimliksiz eski tur metni burada [şu an: 15 Kas 03:00]"], now - 60, now + 60)
    left = {e.id for e in m.recent(10)}
    assert a not in left and old not in left
    assert b in left and other_old in left  # metni tutmayan ve başka kimlikli turlar kalır
    assert got["episodes"] == 2
    # Özetin aralığında hâlâ başka sohbetin turları var: özet silinmez.
    assert got["summaries"] == 0 and m.summaries(5)


def test_forget_session_drops_summary_when_nothing_left():
    m = LongMemory(":memory:", {})
    m.record("tek başına bir sohbet turu yazdım", "cevap", "yazı", sid="x")
    now = time.time()
    with m._lock:
        m.db.execute("INSERT INTO summaries(start, end, channel, text) VALUES (?, ?, '', 'o sohbetin özeti')",
                     (now - 5, now + 5))
        m.db.commit()
    assert m.forget_session("x") == {"episodes": 1, "summaries": 1}
    assert m.summaries(5) == [] and m.recent(5) == []


def test_profile_keeps_old_turns_out_and_marks_chat_as_new():
    m = LongMemory(":memory:", {})
    m.record("dünkü konuşmada küfürlü bir cümle kurdum", "anladım", "yazı")
    _age(m, 86400)
    prof = m.profile()
    assert "küfürlü" not in prof  # dünkü son tur yeni sohbete "kaldığımız yer" diye girmez
    assert "NEW, separate conversation" in prof
    m.record("az önce sesle sorduğum soru buydu", "cevap", "ses")
    assert "az önce sesle" in m.profile() and "not this conversation" in m.profile()


def test_deleting_open_chat_removes_memory_and_is_not_saved_again(app):  # noqa: F811
    state, srv, client, prov = app
    if state.agent.memory is None:
        state.agent.memory = LongMemory(":memory:", {})
    prov.script.append("selam, buradayım")
    events, t = collect(state, lambda e: e["type"] == "status")
    assert client.post("/api/send", json={"text": "silinecek sohbette yazdığım mesaj"}).json()["ok"]
    t.join()
    sid = state.chat.sid
    assert (sess.sessions_dir() / f"{sid}.json").exists()
    assert any(e.user.startswith("silinecek sohbette") for e in state.agent.memory.recent(5))
    r = client.post("/api/session/delete", json={"id": sid}).json()
    assert r["ok"] and r["forgot"]["episodes"] == 1
    assert not any(e.user.startswith("silinecek sohbette") for e in state.agent.memory.recent(5))
    state.chat.save()  # bellekte hâlâ açık: kapanırken ya da sonraki kayıtta dosyayı geri yazmamalı
    assert not (sess.sessions_dir() / f"{sid}.json").exists()
    assert sid not in sess.load_meta()


def test_memory_tab_lists_and_forgets_history(app):  # noqa: F811
    state, srv, client, prov = app
    m = state.agent.memory = LongMemory(":memory:", {})
    e1 = m.record("hafıza sekmesinden silinecek tur", "tamam", "yazı")
    m.record("kalacak olan ikinci tur burada", "peki", "yazı")
    view = client.get("/api/memory").json()
    assert {x["id"] for x in view["episodes"]} >= {e1}
    assert client.post("/api/memory/action", json={"action": "forget_episode", "id": e1}).json()["ok"]
    assert e1 not in {e.id for e in m.recent(10)}
    r = client.post("/api/memory/action", json={"action": "forget_history"}).json()
    assert r["ok"] and r["episodes"] == 1 and m.recent(10) == []
