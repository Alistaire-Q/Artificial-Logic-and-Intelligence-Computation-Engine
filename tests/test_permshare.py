"""İzin önerisi (hep onaylanan işler) ve Gist ile paylaşma."""

from __future__ import annotations

import pytest

from alice import permissions as perm
from alice.tools.base import ToolContext, ToolError
from alice.tools.permshare import PermissionsTool, Share
from alice.tools.shell import RunCommand
from tests.conftest import make_config


def ctx_for(workdir):
    return ToolContext(cwd=workdir, config=make_config({"brain": ["a:m"]}))


def test_suggests_rules_approved_often_and_never_denied(workdir):
    rc = RunCommand()
    for _ in range(3):
        perm.record_answer(rc, {"command": "npm test -- --watch=false"}, workdir, "exec", "y")
    for _ in range(4):
        perm.record_answer(rc, {"command": "git push origin x"}, workdir, "exec", "y")
    perm.record_answer(rc, {"command": "git push origin y"}, workdir, "exec", "n")  # bir kez reddedildi
    for _ in range(5):
        perm.record_answer(rc, {"command": "rm -rf build"}, workdir, "danger", "y")  # tehlikeli: önerilmez
    rules = [s["rule"] for s in perm.suggestions(workdir)]
    assert rules == ["run_command(npm test*)"]
    out = PermissionsTool().run(ctx_for(workdir), "suggest")
    assert "npm test" in out.text and "3 kez" in out.text


def test_apply_writes_project_rules_and_they_stop_being_suggested(workdir):
    rc = RunCommand()
    for _ in range(3):
        perm.record_answer(rc, {"command": "npm test"}, workdir, "exec", "y")
    ctx = ctx_for(workdir)
    PermissionsTool().run(ctx, "apply", rules=["run_command(npm test*)"])
    assert "run_command(npm test*)" in perm.project_rules(workdir)["allow"]
    assert perm.suggestions(workdir) == []
    with pytest.raises(ToolError, match="geniş"):
        PermissionsTool().run(ctx, "apply", rules=["*"])


def test_share_publishes_secret_gist_and_refuses_secrets(workdir):
    class API:
        def __init__(self):
            self.body = None

        def post(self, path, body):
            self.body = body
            return {"html_url": "https://gist.github.com/x/1",
                    "files": {"rapor.html": {"raw_url": "https://gist.githubusercontent.com/x/1/raw/rapor.html"}}}

    ctx = ctx_for(workdir)
    api = API()
    ctx.state["github_api"] = api
    (workdir / "rapor.html").write_text("<h1>Rapor</h1>", encoding="utf-8")
    out = Share().run(ctx, ["rapor.html"], title="Haftalık")
    assert api.body["public"] is False and "rapor.html" in api.body["files"]
    assert "gistcdn.githack.com" in out.text and "gizli" in out.text
    (workdir / ".env").write_text("OPENAI_API_KEY=sk-abcdefabcdefabcdefabcdefabcdef", encoding="utf-8")
    with pytest.raises(ToolError, match="paylaşılmadı"):
        Share().run(ctx, [".env"])
    assert Share().risk == "send"
