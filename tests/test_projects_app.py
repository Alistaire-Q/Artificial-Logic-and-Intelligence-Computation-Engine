from __future__ import annotations

import time

import httpx
import pytest

from alice.app.server import AppServer, AppState
from tests.conftest import FakeProvider, make_config, tool_call
from tests.test_team import steps, who


def reply(model, messages, tools, **kw):
    me = who(messages)
    if me == "backend":
        return [tool_call("write_file", {"path": "src/a.py", "content": "x = 1\n"}), "rapor: tamam"][
            min(steps(messages), 1)]
    return "tamam"  # yönetici ve diğerleri


@pytest.fixture
def app(workdir):
    cfg = make_config({"brain": ["a:m"], "fast": ["a:m"], "vision": ["a:m"]}, stream=False)
    prov = FakeProvider("a", reply)
    state = AppState(cfg, workdir, "auto", provider_factory=lambda pc: prov)
    srv = AppServer(state).start()
    client = httpx.Client(base_url=srv.url.split("/?")[0], headers={"X-Alice-Token": state.token}, timeout=10)
    yield state, client, workdir
    srv.stop()


def wait_for(client, pid, pred, timeout=15):
    end = time.time() + timeout
    while time.time() < end:
        d = client.get(f"/api/project?id={pid}").json()
        if pred(d):
            return d
        time.sleep(0.1)
    raise AssertionError(f"beklenen duruma gelmedi: {d.get('status')} {[m['status'] for m in d.get('members', [])]}")


def act(client, pid, **kw):
    return client.post("/api/project/action", json={"id": pid, **kw})


def test_project_lifecycle_over_api(app):
    state, client, workdir = app
    r = client.post("/api/project/create", json={"goal": "Deneme projesi", "review": True, "lead_thinking": "high",
                                                 "agents": [{"name": "backend", "task": "a.py yaz", "model": "fast",
                                                             "thinking": "high", "scope": ["src"]}]}).json()
    assert r["ok"]
    pid = r["id"]
    d = wait_for(client, pid, lambda d: d["members"][0]["status"] == "review")
    assert d["origin"] == "user" and d["review"] and d["progress"] == [0, 1]
    assert client.get("/api/projects").json()[0]["id"] == pid

    det = client.get(f"/api/project?id={pid}&member=backend").json()
    assert [i["type"] for i in det["member"]["history"]][:2] == ["user", "tool"]
    assert det["member"]["files"] == ["src/a.py"]
    assert "+x = 1" in client.get(f"/api/project/diff?id={pid}&member=backend").json()["diff"]
    lead = client.get(f"/api/project?id={pid}&member=alice").json()["member"]
    assert lead["name"] == "alice" and lead["history"]  # yönetici konuşması da görünür

    # kullanıcı düzeltme ister, ajan devam eder, sonra kullanıcı onaylar; tembel yönetici dürtülüp projeyi kapatır
    assert act(client, pid, action="revise", name="backend", text="bir şey daha").json()["ok"]
    wait_for(client, pid, lambda d: d["members"][0]["status"] == "review" and d["members"][0]["reviews"] == 1)
    assert act(client, pid, action="approve", name="backend").json()["ok"]
    d = wait_for(client, pid, lambda d: d["status"] == "done", timeout=20)
    assert d["progress"] == [1, 1] and d["summary"]

    # bitmiş projeye ajan eklenir; proje yeniden açılır
    assert act(client, pid, action="add", agent={"name": "tests", "task": "test"}).json()["ok"]
    wait_for(client, pid, lambda d: any(m["name"] == "tests" and m["status"] == "review" for m in d["members"]))
    assert act(client, pid, action="add", agent={"name": "Alice", "task": "x"}).status_code == 400
    assert act(client, pid, action="revert", name="backend").json()["ok"]
    assert not (workdir / "src/a.py").exists()

    # uygulama yeniden açılmış gibi: proje diskten yüklenir (durmuş), sürdürülebilir
    act(client, pid, action="stop")
    state.ctx.state["teams"].clear()
    d = client.get(f"/api/project?id={pid}").json()
    assert d["status"] == "stopped"
    assert act(client, pid, action="resume").json()["ok"]
    assert client.get(f"/api/project?id={pid}").json()["status"] != "stopped"
    act(client, pid, action="stop")
    assert act(client, pid, action="delete").json()["ok"]
    assert client.get(f"/api/project?id={pid}").status_code == 404


def test_planning_project_and_questions(app):
    state, client, _ = app
    pid = client.post("/api/project/create", json={"goal": "Planla"}).json()["id"]
    team = state.ctx.state["teams"][pid]
    assert team.status() in ("planning", "running", "review")
    q = team.ask_user("Hangi dil?")
    assert client.get(f"/api/project?id={pid}").json()["status"] == "needs_you"
    assert client.get("/api/projects").json()[0]["questions"] == 1
    assert act(client, pid, action="answer", name=q["id"], text="Python").json()["ok"]
    assert client.get(f"/api/project?id={pid}").json()["questions"][0]["answer"] == "Python"
    act(client, pid, action="stop")


def test_create_validation(app):
    _, client, _ = app
    assert client.post("/api/project/create", json={"goal": "", "agents": []}).status_code == 400
    assert client.post("/api/project/create", json={"goal": "g", "lead_model": "yok"}).status_code == 400
    assert client.post("/api/project/create", json={"goal": "g", "cwd": "/yok/klasor"}).status_code == 400
    assert client.get("/api/project?id=yok").status_code == 404
