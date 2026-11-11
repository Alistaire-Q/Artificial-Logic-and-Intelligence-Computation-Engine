"""git / GitHub araçları: anahtar bulma, kurallar (korumalı dal, anahtar sızıntısı, CI'sız birleştirme), izinler."""

from __future__ import annotations

import base64
import json
import shutil
import subprocess

import httpx
import pytest

from alice.config import Config, default_config_dict
from alice.mcp import MCPTool
from alice.permissions import Permissions
from alice.tools.base import ToolContext, ToolError
from alice.tools.github import Git, GitHub, GitHubAPI, clone_or_update, git_env, github_token, parse_remote
from tests.conftest import make_config

needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="git yok")
TOKEN_VARS = ("GITHUB_TOKEN", "GH_TOKEN", "GITHUB_PERSONAL_ACCESS_TOKEN", "GITHUB_PAT", "GITHUB_MODELS_TOKEN")


@pytest.fixture(autouse=True)
def no_tokens(monkeypatch):
    for k in TOKEN_VARS:
        monkeypatch.delenv(k, raising=False)
    which = shutil.which
    monkeypatch.setattr("alice.tools.github.shutil.which", lambda name: None if name == "gh" else which(name))
    for k, v in {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
                 "GIT_COMMITTER_EMAIL": "t@t"}.items():
        monkeypatch.setenv(k, v)


def cfg_with(**extra) -> Config:
    data = default_config_dict()
    data.update(extra)
    return Config(data)


def git(cwd, *args):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path):
    """main dalında bir commit'i olan depo; origin yerel bir bare depo."""
    remote = tmp_path / "remote.git"
    git(tmp_path, "init", "--bare", "-b", "main", str(remote))
    work = tmp_path / "work"
    work.mkdir()
    git(work, "init", "-b", "main")
    (work / "a.txt").write_text("a\n", encoding="utf-8")
    git(work, "add", "a.txt")
    git(work, "commit", "-m", "ilk")
    git(work, "remote", "add", "origin", str(remote))
    git(work, "push", "-u", "origin", "main")
    return work, remote


def ctx_for(cwd, cfg=None):
    return ToolContext(cwd=cwd, config=cfg or make_config({"brain": ["a:m"]}))


# ------------------------------------------------------------------------------------- yardımcılar
@pytest.mark.parametrize("url", ["https://github.com/o/r.git", "https://github.com/o/r", "git@github.com:o/r.git",
                                 "https://x-access-token:abc@github.com/o/r.git", "ssh://git@github.com/o/r.git", "o/r"])
def test_parse_remote(url):
    assert parse_remote(url) == ("o/r" if url != "o/r" else None)


def test_token_order(monkeypatch):
    assert github_token(cfg_with()) is None
    monkeypatch.setenv("GITHUB_MODELS_TOKEN", "models")
    assert github_token(cfg_with()) == "models"
    monkeypatch.setenv("MY_GH", "mcp")
    cfg = cfg_with(mcp={"servers": {"github": {"command": "npx", "env": {"GITHUB_PERSONAL_ACCESS_TOKEN": "${MY_GH}"}}}})
    assert github_token(cfg) == "mcp"  # MCP sunucusunun anahtarı, Models anahtarından önce
    monkeypatch.setenv("GITHUB_TOKEN", "env")
    assert github_token(cfg) == "env"


def test_gh_cli_token_has_priority_over_env(monkeypatch):
    """Kullanıcı tercihi: `gh` oturumu, .env'deki değişkenlerden önce gelmeli."""
    class FakeProc:
        returncode = 0
        stdout = "gho_cli_token\n"
    import subprocess as sub_mod

    real_run = sub_mod.run
    monkeypatch.setattr(sub_mod, "run",
                        lambda a, **kw: FakeProc() if "auth" in a and "token" in a else real_run(a, **kw))
    real_which = shutil.which
    monkeypatch.setattr("alice.tools.github.shutil.which", lambda name: "gh.exe" if name == "gh" else real_which(name))
    monkeypatch.setenv("GITHUB_TOKEN", "env_token")
    assert github_token(cfg_with()) == "gho_cli_token"


def test_gh_cli_failure_falls_back_to_env(monkeypatch):
    """gh oturumu yoksa (hata/boş) .env değişkenlerine düşülmeli."""
    class FailProc:
        returncode = 1
        stdout = ""
    import subprocess as sub_mod

    real_run = sub_mod.run
    monkeypatch.setattr(sub_mod, "run",
                        lambda a, **kw: FailProc() if "auth" in a and "token" in a else real_run(a, **kw))
    real_which = shutil.which
    monkeypatch.setattr("alice.tools.github.shutil.which", lambda name: "gh.exe" if name == "gh" else real_which(name))
    monkeypatch.setenv("GITHUB_TOKEN", "env_token")
    assert github_token(cfg_with()) == "env_token"


def test_gh_cli_timeout_does_not_break_token_lookup(monkeypatch):
    """gh yanıt vermezse (zaman aşımı) akış bozulmamalı, .env değişkeni kullanılmalı."""
    import subprocess as sub_mod

    def hang(a, **kw):
        if "auth" in a and "token" in a:
            raise sub_mod.TimeoutExpired(a, 10)
        raise AssertionError("beklenmeyen komut")
    monkeypatch.setattr(sub_mod, "run", hang)
    real_which = shutil.which
    monkeypatch.setattr("alice.tools.github.shutil.which", lambda name: "gh.exe" if name == "gh" else real_which(name))
    monkeypatch.setenv("GITHUB_TOKEN", "env_token")
    assert github_token(cfg_with()) == "env_token"


def test_public_only_fine_grained_token_yields_to_one_with_private_access(monkeypatch):
    """.env'deki ince ayarlı anahtar sadece açık depoları görüyordu: özel depoları gören anahtar (gh) seçilmeli."""
    from alice.tools import github as gh_mod

    access = {"github_pat_public": False, "gho_full": True}
    monkeypatch.setattr(gh_mod, "_private_access", lambda t: access.get(t))
    monkeypatch.setattr(gh_mod, "_token_candidates", lambda cfg=None: iter(["github_pat_public", "gho_full"]))
    assert github_token(cfg_with()) == "gho_full"
    access["github_pat_public"] = True  # özel depoları görüyorsa sıra değişmez
    assert github_token(cfg_with()) == "github_pat_public"
    access["github_pat_public"] = None  # sorulamadı (ağ yok): ilk anahtar
    assert github_token(cfg_with()) == "github_pat_public"


def test_git_env_passes_token_without_args():
    env = git_env("ghp_secret")
    n = int(env["GIT_CONFIG_COUNT"]) - 1
    assert env[f"GIT_CONFIG_KEY_{n}"] == "http.https://github.com/.extraheader"
    assert base64.b64decode(env[f"GIT_CONFIG_VALUE_{n}"].split()[-1]).decode() == "x-access-token:ghp_secret"
    assert env["GIT_TERMINAL_PROMPT"] == "0"


# ------------------------------------------------------------------------------------- izinler
def test_risk_rules():
    perms_auto, perms_plan = Permissions("auto"), Permissions("plan")
    g, gh = Git(), GitHub()
    ctx = ToolContext(cwd=None, config=None)  # type: ignore[arg-type]
    assert perms_auto.decide(g, {"action": "commit", "message": "x"}, ctx) == "allow"
    assert perms_auto.decide(g, {"action": "push"}, ctx) == "allow"
    assert perms_auto.decide(g, {"action": "push", "protected": True}, ctx) == "ask"
    assert perms_auto.decide(g, {"action": "push", "force": True}, ctx) == "ask"
    assert perms_auto.decide(gh, {"action": "pr_create", "title": "x"}, ctx) == "allow"
    assert perms_auto.decide(gh, {"action": "pr_merge", "number": 1}, ctx) == "ask"
    assert perms_auto.decide(gh, {"action": "pr_close", "number": 1}, ctx) == "ask"
    perms_auto.always_allow.add("github")  # "hep izin ver" geri alınamaz işleri kapsamaz
    assert perms_auto.decide(gh, {"action": "pr_merge", "number": 1}, ctx) == "ask"
    assert perms_plan.decide(gh, {"action": "prs"}, ctx) == "allow"
    assert perms_plan.decide(g, {"action": "diff"}, ctx) == "allow"
    assert perms_plan.decide(gh, {"action": "pr_create", "title": "x"}, ctx) == "deny"
    assert perms_plan.decide(g, {"action": "commit", "message": "x"}, ctx) == "deny"


def test_mcp_destructive_tools_need_approval():
    srv = type("S", (), {"name": "github"})()
    assert MCPTool(srv, {"name": "merge_pull_request"}).risk == "danger"
    assert MCPTool(srv, {"name": "delete_file"}).risk == "danger"
    assert MCPTool(srv, {"name": "create_pull_request"}).risk == "exec"
    assert MCPTool(srv, {"name": "list_commits", "annotations": {"readOnlyHint": True}}).risk == "read"


# ------------------------------------------------------------------------------------- git aracı
@needs_git
def test_commit_and_push_feature_branch(repo):
    work, remote = repo
    ctx = ctx_for(work)
    t = Git()
    t.run(ctx, action="switch", branch="ozellik", create=True)
    (work / "b.txt").write_text("b\n", encoding="utf-8")
    assert "b.txt" in t.run(ctx, action="status").text
    out = t.run(ctx, action="commit", message="b ekle", all=True)
    assert "b ekle" in out.text
    t.run(ctx, action="push")
    heads = subprocess.run(["git", "branch", "--list"], cwd=remote, capture_output=True, text=True).stdout
    assert "ozellik" in heads
    assert "b ekle" in t.run(ctx, action="log").text


@needs_git
def test_push_to_protected_branch_needs_explicit_flag(repo):
    work, _ = repo
    ctx = ctx_for(work)
    t = Git()
    (work / "a.txt").write_text("değişti\n", encoding="utf-8")
    t.run(ctx, action="commit", message="ana dala", files=["a.txt"])
    with pytest.raises(ToolError, match="korumalı"):
        t.run(ctx, action="push")
    assert t.risk_for({"action": "push", "protected": True}) == "danger"
    t.run(ctx, action="push", protected=True)


@needs_git
@pytest.mark.parametrize("name,content", [(".env", "GROQ_API_KEY=x\n"),
                                          ("conf.py", "TOKEN = 'ghp_" + "a" * 36 + "'\n")])
def test_commit_refuses_secrets(repo, name, content):
    work, _ = repo
    (work / name).write_text(content, encoding="utf-8")
    with pytest.raises(ToolError, match="anahtar"):
        Git().run(ctx_for(work), action="commit", message="x", all=True)


@needs_git
def test_commit_without_changes_and_outside_repo(repo, tmp_path):
    work, _ = repo
    with pytest.raises(ToolError, match="değişiklik yok"):
        Git().run(ctx_for(work), action="commit", message="x", all=True)
    empty = tmp_path / "bos"
    empty.mkdir()
    with pytest.raises(ToolError, match="git deposu değil"):
        Git().run(ctx_for(empty), action="status")


@needs_git
def test_audit(repo, monkeypatch):
    work, remote = repo
    ctx = ctx_for(work)
    t = Git()
    
    # 1. Temiz depo audit
    out = t.run(ctx, action="audit").text
    assert "Remotes:" in out
    assert "Dal: " in out
    assert "Upstream: origin/" in out

    # 2. Ahead durumu
    (work / "c.txt").write_text("c", encoding="utf-8")
    t.run(ctx, action="commit", message="c ekle", all=True)
    out = t.run(ctx, action="audit").text
    assert "1 ahead, 0 behind" in out
    assert "c ekle" in out

    # 3. Auth kaynağı testi (mock)
    monkeypatch.setenv("GITHUB_TOKEN", "env_token_123456789")
    
    # gh taklidi yapalım
    class MockProcess:
        returncode = 0
        stdout = "gh_token_987654321\n"
    
    def mock_run(args, **kwargs):
        if args[:3] == ["gh", "auth", "token"] or (isinstance(args[0], str) and "gh" in args[0] and args[1:3] == ["auth", "token"]):
            return MockProcess()
        return subprocess.RealProcess() # Bu kısım hatalı olabilir, sadece gh'ı yakalayalım

    # Daha basit bir mock:
    import alice.tools.github
    original_run = subprocess.run
    def simple_mock_run(args, **kwargs):
        if "gh" in str(args[0]) and "auth" in args and "token" in args:
            return MockProcess()
        return original_run(args, **kwargs)
    
    monkeypatch.setattr(subprocess, "run", simple_mock_run)
    original_which = shutil.which
    monkeypatch.setattr(shutil, "which", lambda x: "gh" if x == "gh" else original_which(x))
    
    # gh öncelikli olmalı
    out = t.run(ctx, action="audit").text
    assert "Auth: gh CLI (token: gh_t...4321)" in out


# ------------------------------------------------------------------------------------- GitHub API
def fake_api(routes: dict[tuple[str, str], object], calls: list | None = None) -> GitHubAPI:
    def handler(req: httpx.Request) -> httpx.Response:
        key = (req.method, req.url.path)
        if calls is not None:
            calls.append((req.method, req.url.path, json.loads(req.content) if req.content else None))
        if key not in routes:
            return httpx.Response(404, json={"message": "Not Found"})
        body = routes[key]
        return httpx.Response(200, json=body(req) if callable(body) else body)

    return GitHubAPI("tok", client=httpx.Client(base_url="https://api.github.com",
                                                transport=httpx.MockTransport(handler)))


def pr_routes(conclusion="success", draft=False, mergeable=True):
    return {
        ("GET", "/repos/o/r/pulls/5"): {"number": 5, "state": "open", "draft": draft, "merged": False,
                                         "mergeable": mergeable, "title": "t", "html_url": "u",
                                         "head": {"sha": "abc", "ref": "f"}, "base": {"ref": "main"}},
        ("GET", "/repos/o/r/commits/abc/check-runs"): {"check_runs": [
            {"name": "test (windows)", "status": "completed", "conclusion": conclusion}]},
        ("GET", "/repos/o/r/commits/abc/status"): {"statuses": []},
        ("PUT", "/repos/o/r/pulls/5/merge"): {"sha": "def1234567", "merged": True},
    }


def gh_ctx(api, cwd=None):
    ctx = ctx_for(cwd)
    ctx.state["github_api"] = api
    return ctx


def test_merge_refuses_red_ci_and_drafts():
    with pytest.raises(ToolError, match="kırmızı: test \\(windows\\)"):
        GitHub().run(gh_ctx(fake_api(pr_routes("failure"))), action="pr_merge", repo="o/r", number=5)
    with pytest.raises(ToolError, match="taslak"):
        GitHub().run(gh_ctx(fake_api(pr_routes(draft=True))), action="pr_merge", repo="o/r", number=5)
    with pytest.raises(ToolError, match="ÇAKIŞ|çakış"):
        GitHub().run(gh_ctx(fake_api(pr_routes(mergeable=False))), action="pr_merge", repo="o/r", number=5)


def test_merge_green_uses_configured_method():
    calls: list = []
    out = GitHub().run(gh_ctx(fake_api(pr_routes(), calls)), action="pr_merge", repo="o/r", number=5)
    assert "birleştirildi (squash)" in out.text
    put = [c for c in calls if c[0] == "PUT"][0]
    assert put[2] == {"merge_method": "squash", "sha": "abc"}
    # force: kullanıcı açıkça isterse kırmızı CI'da da birleşir
    out = GitHub().run(gh_ctx(fake_api(pr_routes("failure"))), action="pr_merge", repo="o/r", number=5, force=True)
    assert "birleştirildi" in out.text


def test_api_errors_are_explained():
    with pytest.raises(ToolError, match="depo yok ya da token"):
        GitHub().run(gh_ctx(fake_api({})), action="prs", repo="o/yok")
    with pytest.raises(ToolError, match="GITHUB_TOKEN"):
        GitHubAPI(None)


def test_repos_listing_filters():
    api = fake_api({("GET", "/user/repos"): [
        {"full_name": "hayat/alice", "private": True, "description": "asistan", "default_branch": "main"},
        {"full_name": "hayat/oyun", "private": False, "description": None, "default_branch": "main"}]})
    out = GitHub().run(gh_ctx(api), action="repos", query="asis")
    assert "hayat/alice (gizli)" in out.text and "oyun" not in out.text


@needs_git
def test_pr_create_refuses_protected_branch(repo):
    work, _ = repo
    api = fake_api({("GET", "/repos/o/r"): {"default_branch": "main"}})
    with pytest.raises(ToolError, match="korumalı daldan"):
        GitHub().run(gh_ctx(api, work), action="pr_create", repo="o/r", title="x")


@needs_git
def test_pr_create_from_feature_branch(repo):
    work, _ = repo
    git(work, "switch", "-c", "ozellik")
    calls: list = []
    api = fake_api({("GET", "/repos/o/r"): {"default_branch": "main"},
                    ("POST", "/repos/o/r/pulls"): {"number": 9, "html_url": "https://github.com/o/r/pull/9"}}, calls)
    out = GitHub().run(gh_ctx(api, work), action="pr_create", repo="o/r", title="Yeni", body="açıklama")
    assert "#9" in out.text
    post = [c for c in calls if c[0] == "POST"][0][2]
    assert post == {"title": "Yeni", "body": "açıklama", "base": "main", "head": "ozellik", "draft": False}


def test_clone_or_update_layout(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr("alice.tools.github.run_git", lambda args, cwd, token=None, **kw: calls.append(args) or (0, ""))
    cfg = cfg_with(github={"clone_dir": str(tmp_path / "gh")})
    path, note = clone_or_update(cfg, "https://github.com/hayat/alice.git", token="t")
    assert path == tmp_path / "gh" / "hayat" / "alice" and note == "klonlandı"
    assert calls[0][:2] == ["clone", "https://github.com/hayat/alice.git"]
    with pytest.raises(ToolError, match="sahip/depo"):
        clone_or_update(cfg, "../etc", token="t")


def test_clone_switches_working_folder(monkeypatch, tmp_path):
    target = tmp_path / "gh" / "o" / "r"
    target.mkdir(parents=True)
    monkeypatch.setattr("alice.tools.github.clone_or_update", lambda cfg, repo, branch, token: (target, "klonlandı"))
    ctx = gh_ctx(fake_api({}), tmp_path)
    moved = []
    ctx.state["set_cwd"] = moved.append
    out = GitHub().run(ctx, action="clone", repo="o/r")
    assert moved == [target] and "Çalışma klasörü" in out.text


# ------------------------------------------------------------------------------------- arayüz
def test_app_lists_and_opens_github_repos(monkeypatch, tmp_path, workdir):
    from alice.app.server import AppServer, AppState
    from tests.conftest import FakeProvider

    cfg = make_config({"brain": ["a:m"], "fast": ["a:m"], "vision": ["a:m"]})
    cfg.data["github"] = {"clone_dir": str(tmp_path / "gh")}
    (tmp_path / "gh" / "hayat" / "alice" / ".git").mkdir(parents=True)
    monkeypatch.setenv("GITHUB_TOKEN", "tok")
    monkeypatch.setattr(GitHubAPI, "user_repos", lambda self, q="": [
        {"full_name": "hayat/alice", "private": True, "description": "", "default_branch": "main"},
        {"full_name": "hayat/oyun", "private": False, "description": "", "default_branch": "main"}])
    target = tmp_path / "gh" / "hayat" / "oyun"
    target.mkdir(parents=True)
    monkeypatch.setattr("alice.tools.github.clone_or_update", lambda cfg, repo, branch="": (target, "klonlandı"))
    state = AppState(cfg, workdir, "auto", provider_factory=lambda pc: FakeProvider("a", []))
    srv = AppServer(state).start()
    try:
        client = httpx.Client(base_url=srv.url.split("/?")[0], headers={"X-Alice-Token": state.token}, timeout=10)
        r = client.get("/api/github/repos").json()
        assert r["ok"] and [x["local"] != "" for x in r["repos"]] == [True, False]
        r = client.post("/api/github/open", json={"repo": "hayat/oyun"}).json()
        assert r["ok"] and r["cwd"] == str(target) and state.ctx.cwd == target
        monkeypatch.delenv("GITHUB_TOKEN")
        r = client.get("/api/github/repos").json()
        assert not r["ok"] and "GITHUB_TOKEN" in r["error"]
    finally:
        srv.stop()
