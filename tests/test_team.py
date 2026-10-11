from __future__ import annotations

import subprocess
import threading

import pytest

from alice.agent import Agent, NullUI
from alice.permissions import Permissions
from alice.providers.router import Router
from alice.team import Team, in_scope, patch_paths, write_targets
from alice.tools import all_tools
from alice.tools.base import ToolContext, ToolError
from tests.conftest import FakeProvider, make_config, tool_call


def who(messages) -> str:
    sys = messages[0]["content"]
    for name in ("backend", "tests", "docs"):
        if f'"{name}" adlı üyesi' in sys:
            return name
    return "alice"


def steps(messages) -> int:
    return sum(1 for m in messages if m["role"] == "assistant")


def make(script, workdir, **agent_cfg):
    cfg = make_config({"brain": ["a:m"], "fast": ["a:m"], "vision": ["a:m"]}, **agent_cfg)
    prov = FakeProvider("a", script)
    router = Router(cfg, provider_factory=lambda pc: prov, sleep=lambda s: None)
    ctx = ToolContext(cwd=workdir, config=cfg, router=router)
    ui = NullUI()
    agent = Agent(cfg, router, all_tools(), ctx, Permissions("auto"), ui, system_prompt="sys")
    agent.stream = False
    return agent, prov, ctx


TEAM_START = {"action": "start", "goal": "Toplama API'si ve testi",
              "agents": [{"name": "backend", "task": "src/api.py içine add(a, b) yaz", "scope": ["src"],
                          "thinking": "high"},
                         {"name": "tests", "task": "tests/test_api.py yaz", "scope": ["tests"], "model": "fast",
                          "constraints": "src'ye dokunma"}]}


def scripted(model, messages, tools, **kw):
    me, n = who(messages), steps(messages)
    last = messages[-1]
    if me == "alice":
        if n == 0:
            return tool_call("team", TEAM_START)
        if "Bütün üyeler bitti" in str(last.get("content")):
            return "Takım bitirdi, hepsi tamam."
        return tool_call("team", {"action": "wait", "timeout": 20}, cid=f"w{n}")
    if me == "backend":
        return [tool_call("write_file", {"path": "src/api.py", "content": "def add(a, b):\n    return a + b\n"}),
                tool_call("team_send", {"to": "tests", "text": "api hazır: add(a, b)"}, "c2"),
                "rapor: src/api.py yazıldı"][min(n, 2)]
    if me == "tests":
        return [tool_call("team_wait", {"seconds": 20}),
                tool_call("write_file", {"path": "tests/test_api.py", "content": "from src.api import add\n"}, "c2"),
                tool_call("write_file", {"path": "src/api.py", "content": "bozuk"}, "c3"),
                tool_call("team_send", {"to": "alice", "text": "testler yazıldı"}, "c4"),
                "rapor: tests/test_api.py yazıldı"][min(n, 4)]
    raise AssertionError(me)


def test_team_runs_members_in_parallel_with_messages_and_scope(workdir):
    agent, prov, ctx = make(scripted, workdir)
    events = []
    ctx.state["publish"] = events.append
    out = agent.run_turn("büyük iş: takım kur")
    assert out == "Takım bitirdi, hepsi tamam."
    assert (workdir / "src/api.py").read_text() == "def add(a, b):\n    return a + b\n"  # kapsam dışı yazma engellendi
    assert (workdir / "tests/test_api.py").exists()
    team = ctx.state["team"]
    assert {m.status for m in team.members.values()} == {"done"}
    assert any(m.sender == "backend" and m.to == "tests" for m in team.log)
    tool_texts = "\n".join(str(m.get("content")) for m in team.members["tests"].agent.messages if m["role"] == "tool")
    assert "api hazır" in tool_texts and "kapsam dışı" in tool_texts
    alice_tools = "\n".join(str(m.get("content")) for m in agent.messages if m["role"] == "tool")
    assert "testler yazıldı" in alice_tools and "rapor: src/api.py yazıldı" in alice_tools
    # üye istemi: kişilik yok, görev ve takım listesi var
    sys = team.members["backend"].agent.messages[0]["content"]
    assert "Senin görevin: src/api.py" in sys and "- tests:" in sys and "Your personality" not in sys
    # rol ayarları takım bitince temizlenir
    assert not any(r.startswith("team:") for r in agent.router.role_overrides)
    assert events and events[-1]["type"] == "team" and not events[-1]["active"]


def test_team_member_tools_are_limited(workdir):
    agent, _, ctx = make(lambda *a, **k: "x", workdir)
    team = Team(ctx, "hedef")
    m = team.add({"name": "docs", "task": "oku", "isolation": "readonly"})
    member = team._make_agent(m)
    names = set(member.tools)
    assert {"read_file", "grep", "team_send", "team_wait"} <= names
    assert not names & {"computer", "browser", "task", "team", "run_plan", "github"}
    assert member.permissions.mode == "plan"


def test_team_validation(workdir):
    agent, _, ctx = make(lambda *a, **k: "x", workdir)
    team = Team(ctx, "hedef")
    with pytest.raises(ToolError):
        team.add({"name": "Alice", "task": "x"})
    with pytest.raises(ToolError):
        team.add({"name": "ok", "task": ""})
    with pytest.raises(ToolError):
        team.add({"name": "ok", "task": "x", "model": "yok"})
    team.add({"name": "ok", "task": "x", "model": "a:m"})
    with pytest.raises(ToolError):
        team.add({"name": "ok", "task": "x"})
    with pytest.raises(ToolError):
        team.send("alice", "kimse", "selam")


def test_done_member_resumes_on_message(workdir):
    calls = {"n": 0}

    def script(model, messages, tools, **kw):
        if who(messages) == "backend":
            calls["n"] += 1
            return f"rapor {calls['n']}"
        return "x"

    agent, _, ctx = make(script, workdir)
    team = Team(ctx, "hedef")
    team.add({"name": "backend", "task": "iş"})
    team.start()
    msgs, fin = team.wait_orchestrator(10, lambda: False)
    assert [m.name for m in fin] == ["backend"] and fin[0].result == "rapor 1"
    team.send("alice", "backend", "bir şey daha yap")
    msgs, fin = team.wait_orchestrator(10, lambda: False)
    assert fin and fin[0].result == "rapor 2"
    user_msgs = [m["content"] for m in team.members["backend"].agent.messages if m["role"] == "user"]
    assert "bir şey daha yap" in user_msgs[-1]


def test_max_parallel_queues_extra_members(workdir):
    gate = threading.Event()

    def script(model, messages, tools, **kw):
        gate.wait(10)
        return "bitti"

    agent, _, ctx = make(script, workdir)
    team = Team(ctx, "hedef", max_parallel=1)
    team.add({"name": "backend", "task": "a"})
    team.add({"name": "tests", "task": "b"})
    team.start()
    assert [m.status for m in team.members.values()] == ["running", "queued"]
    gate.set()
    for _ in range(4):
        team.wait_orchestrator(10, lambda: False)
        if team.all_done():
            break
    assert team.all_done()


def test_worktree_isolation(workdir):
    def git(*args):
        subprocess.run(["git", "-C", str(workdir), *args], check=True, capture_output=True)

    git("init", "-q")
    git("-c", "user.email=a@b", "-c", "user.name=a", "commit", "-q", "--allow-empty", "-m", "ilk")
    agent, _, ctx = make(lambda *a, **k: "x", workdir)
    team = Team(ctx, "hedef")
    m = team.add({"name": "backend", "task": "a", "isolation": "worktree"})
    assert m.cwd == workdir / ".alice-team" / "backend" and m.cwd.is_dir()
    assert m.branch.startswith("alice-team/backend-")
    assert ".alice-team/" in (workdir / ".git/info/exclude").read_text()


def test_scope_helpers(tmp_path):
    assert in_scope(tmp_path / "src/a.py", [tmp_path / "src"])
    assert not in_scope(tmp_path / "srcx/a.py", [tmp_path / "src"])
    assert in_scope(tmp_path / "x", [])
    patch = "*** Begin Patch\n*** Update File: a.py\n@@\n-x\n+y\n*** Add File: b/c.py\n+z\n*** End Patch"
    assert patch_paths(patch) == ["a.py", "b/c.py"]
    assert write_targets("file_ops", {"path": "a", "paths": ["b"], "destination": "c"}) == ["a", "c", "b"]


def test_project_is_saved_loaded_and_resumed(workdir):
    from alice.team import list_projects

    def script(model, messages, tools, **kw):
        n = steps(messages)
        if who(messages) == "backend":
            if n == 0:
                return tool_call("write_file", {"path": "src/a.py", "content": "x = 1\n"})
            return f"rapor {n}"
        return "x"

    agent, _, ctx = make(script, workdir)
    team = Team(ctx, "Kaydedilen hedef\nikinci satır")
    team.add({"name": "backend", "task": "a.py yaz", "scope": ["src"]})
    team.start()
    team.wait_orchestrator(10, lambda: False)
    assert team.members["backend"].files == ["src/a.py"]
    rows = list_projects()
    assert rows[0]["id"] == team.id and rows[0]["title"] == "Kaydedilen hedef"
    assert rows[0]["members"] == [{"name": "backend", "status": "done"}]

    # uygulama yeniden açıldı: proje diskten yüklenir, konuşma korunur, mesajla devam eder
    loaded = Team.load(team.id, ctx)
    m = loaded.members["backend"]
    assert m.status == "done" and m.files == ["src/a.py"] and m.agent is None
    assert any(x.get("role") == "tool" for x in m.messages())
    loaded.send("kullanıcı", "backend", "devam et")
    loaded.wait_orchestrator(10, lambda: False)
    assert m.status == "done" and m.result.startswith("rapor")
    contents = [str(x.get("content")) for x in m.agent.messages]
    assert "Görevine başla." in contents and any("devam et" in c for c in contents)


def test_stopped_member_resumes_only_for_user(workdir):
    agent, _, ctx = make(lambda *a, **k: "bitti", workdir)
    team = Team(ctx, "hedef")
    team.add({"name": "backend", "task": "a"})
    team.members["backend"].status = "stopped"
    team.send("alice", "backend", "devam")
    assert team.members["backend"].status == "stopped"
    team.send("kullanıcı", "backend", "devam")
    team.wait_orchestrator(10, lambda: False)
    assert team.members["backend"].status == "done"


def test_add_member_announces_to_others(workdir):
    agent, _, ctx = make(lambda *a, **k: "bitti", workdir)
    team = Team(ctx, "hedef")
    team.add({"name": "backend", "task": "a"})
    team.start()
    team.wait_orchestrator(10, lambda: False)
    team.add_member({"name": "tests", "task": "test yaz", "scope": ["tests"]})
    for _ in range(3):
        team.wait_orchestrator(10, lambda: False)
    assert team.members["tests"].status == "done"
    assert team.members["backend"].status == "done"  # haber verildi ama uyandırılmadı
    assert any(x.sender == "sistem" and "tests" in x.text for x in team.log)
    assert "tests" in team.inbox["backend"][0].text
