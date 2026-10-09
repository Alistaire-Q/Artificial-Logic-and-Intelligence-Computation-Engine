from __future__ import annotations

import sys
from pathlib import Path

from alice.agent import Agent, NullUI
from alice.commands import catalog, expand_custom
from alice.mcp import load_mcp
from alice.mentions import complete_paths, expand_mentions
from alice.permissions import Permissions
from alice.providers.router import Router
from alice.tools import ToolContext, all_tools
from tests.conftest import FakeProvider, make_config, tool_call

FIXTURE = Path(__file__).parent / "fixtures" / "fake_mcp.py"


def make_agent(script, workdir, mode="auto"):
    cfg = make_config({"brain": ["a:m"], "fast": ["a:m"], "vision": ["a:m"]})
    prov = FakeProvider("a", script)
    router = Router(cfg, provider_factory=lambda pc: prov, sleep=lambda s: None)
    ui = NullUI()
    agent = Agent(cfg, router, all_tools(), ToolContext(cwd=workdir, config=cfg, router=router), Permissions(mode), ui,
                  system_prompt="s")
    return agent, prov, ui


def test_rewind_restores_files_and_conversation(workdir):
    (workdir / "a.txt").write_text("orijinal\n")
    script = [tool_call("edit_file", {"path": "a.txt", "old_string": "orijinal", "new_string": "bir"}), "ok1",
              tool_call("write_file", {"path": "yeni.txt", "content": "x"}, "c2"),
              tool_call("edit_file", {"path": "a.txt", "old_string": "bir", "new_string": "iki"}, "c3"), "ok2"]
    agent, _, _ = make_agent(script, workdir)
    agent.run_turn("birinci")
    second = len(agent.messages)
    agent.run_turn("ikinci")
    assert (workdir / "a.txt").read_text() == "iki\n" and (workdir / "yeni.txt").exists()
    text, restored = agent.rewind(second)
    assert text == "ikinci" and len(agent.messages) == second
    assert (workdir / "a.txt").read_text() == "bir\n" and not (workdir / "yeni.txt").exists()
    text, _ = agent.rewind(1)
    assert text == "birinci" and (workdir / "a.txt").read_text() == "orijinal\n"
    assert len(agent.messages) == 1


def test_plan_mode_blocks_writes(workdir):
    script = [tool_call("write_file", {"path": "x.txt", "content": "x"}), "plan: önce x.txt yazacağım"]
    agent, prov, _ = make_agent(script, workdir, mode="plan")
    agent.run_turn("x.txt oluştur")
    assert not (workdir / "x.txt").exists()
    assert "Plan modundasın" in [m for m in agent.messages if m["role"] == "tool"][0]["content"]
    assert "[PLAN MODU" in prov.calls[0]["messages"][1]["content"]
    assert agent.messages[1]["_display"] == "x.txt oluştur"


def test_mentions_and_completion(workdir):
    (workdir / "src").mkdir()
    (workdir / "src" / "main.py").write_text("print('hi')\n")
    out = expand_mentions("şuna bak @src/main.py ve @src lütfen, mail a@b.com", workdir)
    assert "<file path=" in out and "print('hi')" in out and "<folder path=" in out
    assert "a@b.com" in out and out.count("<file") == 1
    assert expand_mentions("@yok.txt", workdir) == "@yok.txt"
    assert complete_paths(workdir, "mai")[0] == "src/main.py"


def test_user_images_sent_as_parts(workdir):
    agent, prov, _ = make_agent(["gördüm"], workdir)
    agent.run_turn("bu ne", images=["AAAA"])
    sent = prov.calls[0]["messages"][1]["content"]
    assert isinstance(sent, str)  # sağlayıcı 'a' görsel desteklemiyor, router metne çevirdi
    assert isinstance(agent.messages[1]["content"], list)


def test_custom_commands(workdir, isolated_home):
    d = workdir / ".alice" / "commands"
    d.mkdir(parents=True)
    (d / "review.md").write_text("---\ndescription: incele\n---\nŞunu incele: $ARGUMENTS\n")
    (isolated_home / "commands").mkdir(parents=True)
    (isolated_home / "commands" / "selam.md").write_text("Bana selam ver")
    names = {c["name"]: c for c in catalog(workdir)}
    assert names["review"]["description"] == "incele" and names["selam"]["kind"] == "custom"
    assert expand_custom("/review hız", workdir) == "Şunu incele: hız"
    assert expand_custom("/selam", workdir) == "Bana selam ver"
    assert expand_custom("/yok", workdir) is None


def test_mcp_client_roundtrip(workdir):
    cfg = make_config({"brain": ["a:m"]})
    cfg.data["mcp"] = {"servers": {"fake": {"command": sys.executable, "args": [str(FIXTURE)]},
                                   "bozuk": {"command": "olmayan-komut-xyz"}}}
    events = []
    servers, tools = load_mcp(cfg, events.append)
    try:
        assert [t.name for t in tools] == ["mcp__fake__echo", "mcp__fake__fail"]  # sayfalama birleşti
        assert tools[0].risk == "read" and tools[1].risk == "exec"
        ctx = ToolContext(cwd=workdir, config=cfg)
        assert tools[0].run(ctx, text="merhaba").text == "echo: merhaba"
        assert tools[1].run(ctx).is_error
        assert any("bozuk" in e for e in events)
    finally:
        for s in servers:
            s.close()
