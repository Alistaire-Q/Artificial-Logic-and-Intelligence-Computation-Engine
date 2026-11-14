"""PreToolUse/PostToolUse hook'larının JSON kararları: argüman değiştirme, izin kararı, ek bağlam."""

from __future__ import annotations

import json
import sys

from alice.hooks import Hook, HookResult, Hooks, decision
from tests.conftest import tool_call
from tests.test_agent import make_agent


def script_hook(tmp, name: str, payload: dict) -> str:
    """stdout'a JSON yazan küçük hook betiği (kabuk tırnaklarıyla uğraşmamak için dosyaya)."""
    p = tmp / f"{name}.py"
    p.write_text("import json, sys\nsys.stdout.write(" + repr(json.dumps(payload, ensure_ascii=False)) + ")\n",
                 encoding="utf-8")
    return f'"{sys.executable}" "{p}"'


def test_decision_merges_and_ignores_plain_output():
    h = Hook("PreToolUse", "x")
    rs = [HookResult(h, 0, "düz yazı", ""), HookResult(h, 0, '{"updatedInput": {"a": 1}}', ""),
          HookResult(h, 0, json.dumps({"hookSpecificOutput": {"permissionDecision": "deny",
                                                               "permissionDecisionReason": "mesai dışı"}}), ""),
          HookResult(h, 1, '{"permissionDecision": "allow"}', "çöktü")]
    d = decision(rs)
    assert d == {"updatedInput": {"a": 1}, "permissionDecision": "deny", "reason": "mesai dışı"}


def test_hook_rewrites_input_and_adds_context(workdir):
    agent, provs, _ = make_agent([tool_call("write_file", {"path": "a.txt", "content": "eski"}), "tamam"], workdir)
    cmd = script_hook(workdir, "rewrite", {"updatedInput": {"path": "b.txt", "content": "yeni"},
                                           "additionalContext": "dosya biçimlendirildi"})
    agent.hooks = Hooks([Hook("PreToolUse", cmd, "write_file")], workdir)
    agent.run_turn("yaz")
    assert not (workdir / "a.txt").exists() and (workdir / "b.txt").read_text(encoding="utf-8") == "yeni"
    tool_msg = next(m for m in agent.messages if m["role"] == "tool")
    assert "dosya biçimlendirildi" in tool_msg["content"]


def test_hook_deny_and_allow_decisions(workdir):
    agent, _, _ = make_agent([tool_call("write_file", {"path": "a.txt", "content": "x"}), "peki"], workdir)
    agent.hooks = Hooks([Hook("PreToolUse", script_hook(workdir, "deny", {"permissionDecision": "deny",
                                                                          "reason": "dosya kilitli"}))], workdir)
    agent.run_turn("yaz")
    assert not (workdir / "a.txt").exists()
    assert "dosya kilitli" in next(m for m in agent.messages if m["role"] == "tool")["content"]

    # ask modunda normalde sorulurdu (NullUI "n" der); hook allow verince sormadan çalışır
    agent2, _, ui = make_agent([tool_call("write_file", {"path": "c.txt", "content": "x"}), "oldu"], workdir,
                               mode="ask", approve="n")
    agent2.hooks = Hooks([Hook("PreToolUse", script_hook(workdir, "allow", {"permissionDecision": "allow"}))],
                         workdir)
    agent2.run_turn("yaz")
    assert (workdir / "c.txt").exists()


def test_post_hook_replaces_tool_output(workdir):
    # SDK'daki updatedToolOutput: PostToolUse hook'u çıktıyı değiştirir (ör. sır gizleme), model yalnız onu görür.
    (workdir / "s.txt").write_text("API_KEY=gizli123", encoding="utf-8")
    agent, provs, _ = make_agent([tool_call("read_file", {"path": "s.txt"}), "tamam"], workdir)
    cmd = script_hook(workdir, "redact", {"updatedToolOutput": "API_KEY=[gizlendi]"})
    agent.hooks = Hooks([Hook("PostToolUse", cmd, "read_file")], workdir)
    agent.run_turn("oku")
    tool_msg = next(m for m in agent.messages if m["role"] == "tool")
    assert "gizli123" not in tool_msg["content"] and "[gizlendi]" in tool_msg["content"]


def test_failing_hook_ignored_by_default_but_blocks_when_asked(workdir):
    broken = f'"{sys.executable}" -c "import sys; sys.exit(1)"'
    ok = HookResult(Hook("PreToolUse", broken), 1, "", "çöktü")
    assert ok.failed and not ok.blocked  # eski davranış: bozuk hook yok sayılır
    strict = HookResult(Hook("PreToolUse", broken, on_failure="block"), 1, "", "çöktü")
    assert strict.failed and strict.blocked
    assert not HookResult(Hook("PreToolUse", broken, on_failure="block"), 0, "", "").blocked

    agent, provs, _ = make_agent([tool_call("write_file", {"path": "a.txt", "content": "x"}), "tamam"], workdir)
    agent.hooks = Hooks([Hook("PreToolUse", broken, "write_file", on_failure="block")], workdir)
    agent.run_turn("yaz")
    assert not (workdir / "a.txt").exists()
    tool_msg = next(m for m in agent.messages if m["role"] == "tool")
    assert "on_failure=block" in tool_msg["content"]


def test_on_failure_round_trips_through_hooks_file(tmp_path, monkeypatch):
    from alice import hooks as hk

    monkeypatch.setattr(hk, "hooks_path", lambda: tmp_path / "hooks.toml")
    monkeypatch.setattr(hk, "_read_hooks_file", lambda: __import__("tomllib").loads(
        (tmp_path / "hooks.toml").read_text(encoding="utf-8")).get("hooks", {}) if (tmp_path / "hooks.toml").exists()
        else {})
    hk.add_hook("PreToolUse", "denetle", "run_command", 10, on_failure="block")
    hk.add_hook("PostToolUse", "ruff", "")
    rows = hk.file_hooks()
    assert [r.on_failure for r in rows] == ["block", "ignore"]
