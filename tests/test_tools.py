from __future__ import annotations

import sys
from pathlib import Path

import pytest

from alice.config import ProviderConfig, init_home, load_config
from alice.permissions import Permissions, is_system_path
from alice.tools import ToolContext
from alice.tools.base import ToolError
from alice.tools.files import EditFile, Glob, Grep, ListDir, ReadFile, WriteFile
from alice.tools.shell import ProcessOutput, RunCommand, cleanup
from alice.tools.web import html_to_text
from tests.conftest import make_config


@pytest.fixture
def ctx(workdir):
    return ToolContext(cwd=workdir, config=make_config({"brain": ["a:m"]}))


def test_read_with_offset_and_cp1254(ctx, workdir):
    (workdir / "t.txt").write_bytes("şğü\n".encode("cp1254") + b"line2\nline3\n")
    out = ReadFile().run(ctx, path="t.txt", offset=2, limit=1).text
    assert "line2" in out and "line3" not in out and "devam et" in out
    assert "şğü" in ReadFile().run(ctx, path="t.txt").text


def test_edit_requires_unique(ctx, workdir):
    (workdir / "d.txt").write_text("x\nx\n")
    with pytest.raises(ToolError):
        EditFile().run(ctx, path="d.txt", old_string="x", new_string="y")
    EditFile().run(ctx, path="d.txt", old_string="x", new_string="y", replace_all=True)
    assert (workdir / "d.txt").read_text() == "y\ny\n"


def test_edit_handles_crlf(ctx, workdir):
    (workdir / "w.cs").write_bytes(b"class A {\r\n  int x;\r\n}\r\n")
    EditFile().run(ctx, path="w.cs", old_string="{\n  int x;", new_string="{\n  int y;")
    assert (workdir / "w.cs").read_bytes() == b"class A {\r\n  int y;\r\n}\r\n"


def test_write_list_glob_grep(ctx, workdir):
    WriteFile().run(ctx, path="src/a.py", content="def foo():\n    return 1\n")
    (workdir / "node_modules").mkdir()
    (workdir / "node_modules" / "x.py").write_text("def foo(): pass")
    assert "src/" in ListDir().run(ctx).text
    g = Glob().run(ctx, pattern="**/*.py").text
    assert "a.py" in g and "node_modules" not in g
    assert "def foo" in Grep().run(ctx, pattern=r"def \w+", glob="*.py").text


def test_run_command_and_errors(ctx):
    r = RunCommand().run(ctx, command="echo merhaba")
    assert "merhaba" in r.text and not r.is_error
    r = RunCommand().run(ctx, command="exit 3")
    assert r.is_error and "çıkış kodu 3" in r.text


@pytest.mark.skipif(sys.platform == "win32", reason="posix sleep")
def test_background_process(ctx):
    r = RunCommand().run(ctx, command="echo started; sleep 30", background=True)
    pid = int(r.text.split("pid=")[1].split(",")[0])
    assert "started" in ProcessOutput().run(ctx, pid=pid).text
    cleanup(ctx)


def test_dangerous_classification():
    rc = RunCommand()
    assert rc.risk_for({"command": "rm -rf /"}) == "danger"
    assert rc.risk_for({"command": "Remove-Item C:\\x -Recurse"}) == "danger"
    assert rc.risk_for({"command": "git push --force origin main"}) == "danger"
    assert rc.risk_for({"command": "pip install requests"}) == "exec"


def test_permissions_modes(ctx):
    p = Permissions("auto")
    assert p.decide(ReadFile(), {"path": "x"}, ctx) == "allow"
    assert p.decide(WriteFile(), {"path": "x"}, ctx) == "allow"
    assert p.decide(RunCommand(), {"command": "rm -rf x"}, ctx) == "ask"
    sys_path = "C:\\Windows\\x.dll" if sys.platform == "win32" else "/etc/hosts"
    assert p.decide(WriteFile(), {"path": sys_path}, ctx) == "ask"
    assert Permissions("ask").decide(WriteFile(), {"path": "x"}, ctx) == "ask"
    assert Permissions("yolo").decide(RunCommand(), {"command": "rm -rf x"}, ctx) == "allow"
    p2 = Permissions("ask")
    p2.always_allow.add("write_file")
    assert p2.decide(WriteFile(), {"path": "x"}, ctx) == "allow"


def test_is_system_path():
    if sys.platform == "win32":
        assert is_system_path(Path(r"C:\Windows\System32"))
    else:
        assert is_system_path(Path("/usr/bin/python"))
        assert not is_system_path(Path("/home/user/x"))


def test_html_to_text():
    t = html_to_text("<html><head><title>x</title><script>bad()</script></head><body><h1>Başlık</h1><p>para</p>"
                     "<ul><li>bir</li></ul></body></html>")
    assert "# Başlık" in t and "* bir" in t and "bad()" not in t


def test_config_init_and_load(isolated_home, monkeypatch, tmp_path):
    written = init_home()
    assert (isolated_home / "config.toml") in written and (isolated_home / ".env").exists()
    (isolated_home / ".env").write_text("GROQ_API_KEY=abc\n")
    (isolated_home / "config.toml").write_text('[agent]\npermission_mode = "yolo"\n[roles]\nbrain = ["groq:x"]\n')
    cfg = load_config(cwd=tmp_path)
    assert cfg.agent["permission_mode"] == "yolo" and cfg.agent["max_steps"] == 80
    assert cfg.roles["brain"] == ["groq:x"] and cfg.roles["fast"]  # diğer roller varsayılandan
    assert cfg.providers()["groq"].api_key == "abc"
    monkeypatch.delenv("GROQ_API_KEY")


def test_provider_vision_glob():
    pc = ProviderConfig("x", "http://x", vision_models=["gemini-*"])
    assert pc.supports_vision("gemini-flash-latest") and not pc.supports_vision("gpt-oss")


def test_write_roles_updates_config(isolated_home):
    from alice.cli import write_roles

    init_home()
    write_roles({"brain": ["groq:new-model", "gemini:x"], "fast": [], "vision": ["gemini:v"]})
    cfg = load_config()
    assert cfg.roles["brain"] == ["groq:new-model", "gemini:x"]
    assert cfg.roles["vision"] == ["gemini:v"]
    assert cfg.roles["fast"][0] == "groq:openai/gpt-oss-20b"  # boş öneri dokunulmadı


def test_cli_status_line_and_prompt_session(workdir):
    from alice.cli import build, make_prompt_session, status_line

    cfg = make_config({"brain": ["a:m"], "fast": ["a:m"]})
    agent, router, ctx = build(cfg, workdir, "auto")
    line = status_line(agent, router, ctx)
    assert "a:m" in line and "izin: auto" in line and "context" in line
    assert make_prompt_session(agent, router, ctx).multiline


def test_cli_slash_commands(workdir, isolated_home, monkeypatch):
    from alice.cli import build, handle_slash

    cfg = make_config({"brain": ["a:m"], "fast": ["a:m"]})
    agent, router, ctx = build(cfg, workdir, "auto")
    assert handle_slash("/plan", agent, router, ctx, cfg) is None and agent.permissions.mode == "plan"
    assert handle_slash("/init", agent, router, ctx, cfg)[0] == "run"
    d = workdir / ".alice" / "commands"
    d.mkdir(parents=True)
    (d / "test.md").write_text("Testleri çalıştır: $ARGUMENTS")
    assert handle_slash("/test hızlı", agent, router, ctx, cfg) == ("run", "Testleri çalıştır: hızlı")
    agent.messages.append({"role": "user", "content": "eski mesaj"})
    monkeypatch.setattr("alice.cli.console.input", lambda *a, **k: "1")
    assert handle_slash("/rewind", agent, router, ctx, cfg) == ("prefill", "eski mesaj")
    assert len(agent.messages) == 1


def test_find_installed_apps_scans_program_folders(tmp_path, monkeypatch):
    from alice.tools import desktop

    pf = tmp_path / "Program Files"
    app = pf / "Blender Foundation" / "Blender 4.2"
    app.mkdir(parents=True)
    for name in ("blender.exe", "blender-launcher.exe", "uninstall.exe"):
        (app / name).write_bytes(b"")
    (pf / "Other").mkdir()
    (pf / "Other" / "other.exe").write_bytes(b"")
    monkeypatch.setattr(desktop, "IS_WIN", True)
    monkeypatch.setattr(desktop.shutil, "which", lambda name: None)
    monkeypatch.setattr(desktop, "_steam_libraries", lambda: [])
    monkeypatch.setattr(desktop, "_shortcut_roots", lambda: [])
    for k in ("ProgramFiles(x86)", "ProgramW6432", "LOCALAPPDATA"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("ProgramFiles", str(pf))

    hits = desktop.find_installed_apps("Blender")
    paths = [h.split(": ", 1)[1] for h in hits]
    assert str(app / "blender.exe") in paths
    assert not any("uninstall" in p or "other.exe" in p for p in paths)
    assert desktop.find_installed_apps("photoshop") == []
