from __future__ import annotations

import time

import httpx
import pytest

from alice.app.server import AppServer, AppState
from tests.conftest import FakeProvider, make_config


@pytest.fixture
def app(workdir):
    cfg = make_config({"brain": ["a:m"], "fast": ["a:m"], "vision": ["a:m"]}, stream=False)
    prov = FakeProvider("a", lambda *a, **k: "rapor: tamam")
    state = AppState(cfg, workdir, "auto", provider_factory=lambda pc: prov)
    srv = AppServer(state).start()
    client = httpx.Client(base_url=srv.url.split("/?")[0], headers={"X-Alice-Token": state.token}, timeout=10)
    yield state, client
    srv.stop()


def wait_done(client, pid, timeout=10):
    end = time.time() + timeout
    while time.time() < end:
        d = client.get(f"/api/project?id={pid}").json()
        if not d["active"]:
            return d
        time.sleep(0.1)
    raise AssertionError("proje bitmedi")


def test_project_lifecycle_over_api(app):
    state, client = app
    r = client.post("/api/project/create", json={"goal": "Deneme projesi", "agents": [
        {"name": "backend", "task": "iş yap", "model": "fast", "thinking": "high"}]}).json()
    assert r["ok"]
    pid = r["id"]
    d = wait_done(client, pid)
    assert d["origin"] == "user" and d["members"][0]["status"] == "done"
    rows = client.get("/api/projects").json()
    assert rows[0]["id"] == pid and rows[0]["title"] == "Deneme projesi"

    det = client.get(f"/api/project?id={pid}&member=backend").json()
    assert [i["type"] for i in det["member"]["history"]] == ["user", "assistant"]

    # kullanıcı bitmiş ajana yazar: devam eder; yeni ajan eklenir
    assert client.post("/api/project/action", json={"id": pid, "action": "send", "name": "backend",
                                                     "text": "bir şey daha"}).json()["ok"]
    assert client.post("/api/project/action", json={"id": pid, "action": "add",
                                                     "agent": {"name": "tests", "task": "test"}}).json()["ok"]
    d = wait_done(client, pid)
    assert {m["name"]: m["status"] for m in d["members"]} == {"backend": "done", "tests": "done"}
    assert any(x["from"] == "kullanıcı" for x in d["log"])

    bad = client.post("/api/project/action", json={"id": pid, "action": "add", "agent": {"name": "Alice", "task": "x"}})
    assert bad.status_code == 400

    # uygulama yeniden açılmış gibi: canlı kayıt silinir, proje diskten yüklenir
    state.ctx.state["teams"].clear()
    det = client.get(f"/api/project?id={pid}&member=tests").json()
    assert det["member"]["status"] == "done" and det["member"]["history"]

    assert client.post("/api/project/action", json={"id": pid, "action": "delete"}).json()["ok"]
    assert client.get(f"/api/project?id={pid}").status_code == 404
    assert client.get("/api/projects").json() == []


def test_create_validation(app):
    _, client = app
    assert client.post("/api/project/create", json={"goal": "", "agents": [{"name": "a", "task": "x"}]}).status_code == 400
    assert client.post("/api/project/create", json={"goal": "g", "agents": []}).status_code == 400
    assert client.get("/api/project?id=yok").status_code == 404
