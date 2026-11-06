"""Claude Code proxy (CLIProxyAPI) yönetimi: hesap listesi, giriş linki, hesap silme ve HTTP uçları."""

from __future__ import annotations

import httpx
import pytest

from alice.app import cliproxy
from alice.app.server import AppServer, AppState
from tests.conftest import FakeProvider, make_config


@pytest.fixture
def proxy_home(tmp_path, monkeypatch):
    home = tmp_path / "cliproxy"
    home.mkdir()
    (home / "config.yaml").write_text("server:\n  port: 8317\n", encoding="utf-8")
    (home / "keys.txt").write_text("api_key=test-client\nmanagement_key=test-yonetim\n", encoding="utf-8")
    (home / cliproxy.binary_name()).write_bytes(b"")
    monkeypatch.setattr(cliproxy, "home", lambda: home)
    return home


def test_status_lists_accounts_with_quota_and_cooldown(proxy_home, monkeypatch):
    files = [
        {
            "id": "claude-abc-hayate@example.com.json",
            "provider": "claude",
            "email": "hayate@example.com",
            "status": "active",
            "disabled": False,
            "access_token_expires_at": "2026-12-06T11:15:49Z",
            "quota": {"signals": {"Anthropic-Ratelimit-Unified-5h-Utilization": "0.0",
                                  "Anthropic-Ratelimit-Unified-5h-Reset": "1793983800"}},
            "cooldowns": [],
            "failed": 0,
        },
        {
            "id": "codex-hayate@example.com.json",
            "provider": "codex",
            "email": "hayate@example.com",
            "status": "active",
            "quota": {"signals": {"X-Codex-Primary-Used-Percent": "46",
                                  "X-Codex-Primary-Reset-At": "1796553389"}},
            "cooldowns": [{"model": "gpt-5.6-terra", "until": 9999999999}],
            "failed": 1,
        },
    ]
    monkeypatch.setattr(cliproxy, "_req", lambda method, path: {"files": files})
    d = cliproxy.status()
    assert d["running"] and d["installed"] and not d["error"]
    assert [a["provider"] for a in d["accounts"]] == ["claude", "codex"]
    claude, codex = d["accounts"]
    assert claude["provider_name"] == "Claude (Pro / Max)"
    assert claude["quota"] == {"label": "5 saat", "percent": 0, "reset": 1793983800}
    assert codex["quota"]["percent"] == 46
    assert codex["cooldown"]  # beklemede
    assert codex["failed"] == 1
    assert {"key": "claude", "name": "Claude (Pro / Max)"} in d["providers"]


def test_status_does_not_crash_when_proxy_closed(proxy_home, monkeypatch):
    def boom(method, path):
        raise RuntimeError("bağlantı yok")

    monkeypatch.setattr(cliproxy, "_req", boom)
    d = cliproxy.status()
    assert d["installed"] and not d["running"] and d["error"]  # çökmez, başlatmayı dener, olmazsa haber verir


def test_status_marks_not_installed(tmp_path, monkeypatch):
    monkeypatch.setattr(cliproxy, "home", lambda: tmp_path / "yok")
    d = cliproxy.status()
    assert not d["installed"] and not d["running"]


def test_login_url_starts_oauth_and_reports_state(proxy_home, monkeypatch):
    seen = {}

    def fake(method, path):
        seen["method"], seen["path"] = method, path
        return {"status": "ok", "url": "https://claude.ai/oauth/authorize?x=1", "state": "abc123"}

    monkeypatch.setattr(cliproxy, "_req", fake)
    r = cliproxy.login_url("claude")
    assert r == {"ok": True, "url": "https://claude.ai/oauth/authorize?x=1", "state": "abc123",
                 "provider": "claude"}
    assert seen["path"].endswith("provider=claude")
    assert cliproxy.login_url("bilinmeyen")["ok"] is False


def test_login_state_polls(proxy_home, monkeypatch):
    monkeypatch.setattr(cliproxy, "_req", lambda method, path: {"status": "ok", "error": ""})
    assert cliproxy.login_state("abc")["status"] == "ok"
    assert cliproxy.login_state("")["ok"] is False


def test_remove_validates_name_and_calls_delete(proxy_home, monkeypatch):
    seen = {}

    def fake(method, path):
        seen["method"], seen["path"] = method, path
        return {"status": "ok"}

    monkeypatch.setattr(cliproxy, "_req", fake)
    assert cliproxy.remove("../../etc/passwd")["ok"] is False
    assert cliproxy.remove("not-json")["ok"] is False
    assert cliproxy.remove("claude-abc-a@b.c.json")["ok"] is True
    assert seen["method"] == "DELETE" and "name=claude-abc-a%40b.c.json" in seen["path"]


@pytest.fixture
def app_state(tmp_path):
    cfg = make_config({"brain": ["a:m"], "fast": ["a:m"], "vision": ["a:m"]})
    return AppState(cfg, tmp_path, "auto", provider_factory=lambda pc: FakeProvider("a", []))


def test_http_endpoints(app_state, monkeypatch):
    monkeypatch.setattr(cliproxy, "status", lambda: {"installed": True, "running": True,
                                                     "accounts": [{"email": "a@b.c"}], "error": ""})
    monkeypatch.setattr(cliproxy, "login_url", lambda provider: {"ok": True, "url": "https://x", "state": "s"})
    monkeypatch.setattr(cliproxy, "login_state", lambda state: {"ok": True, "status": "wait"})
    monkeypatch.setattr(cliproxy, "remove", lambda name: {"ok": name.endswith(".json")})
    srv = AppServer(app_state).start()
    try:
        c = httpx.Client(base_url=srv.url.split("/?")[0], headers={"X-Alice-Token": app_state.token}, timeout=10)
        d = c.get("/api/cliproxy").json()
        assert d["running"] and d["accounts"][0]["email"] == "a@b.c"
        assert c.post("/api/cliproxy/login", json={"provider": "claude"}).json()["url"] == "https://x"
        assert c.post("/api/cliproxy/login/status", json={"state": "s"}).json()["status"] == "wait"
        assert c.post("/api/cliproxy/remove", json={"name": "a.json"}).json()["ok"] is True
        assert httpx.get(srv.url.split("/?")[0] + "/api/cliproxy").status_code == 403
    finally:
        srv.stop()

def test_start_noop_when_running(proxy_home, monkeypatch):
    monkeypatch.setattr(cliproxy, "running", lambda: True)
    assert cliproxy.start() == {"ok": True, "already": True}


def test_start_skips_when_not_installed(tmp_path, monkeypatch):
    monkeypatch.setattr(cliproxy, "home", lambda: tmp_path / "yok")
    r = cliproxy.ensure_started()
    assert r["ok"] and r["skipped"] == "kurulu değil"


def test_start_launches_detached_process(proxy_home, monkeypatch):

    checks = {"n": 0}

    def fake_running():
        checks["n"] += 1
        return checks["n"] > 2  # kapalı, kapalı -> başlat -> açık

    monkeypatch.setattr(cliproxy, "running", fake_running)
    monkeypatch.setattr(cliproxy, "_last_start", 0.0)
    spawned = {}

    class Popen:
        def __init__(self, args, **kw):
            spawned["args"], spawned["kw"] = args, kw

    monkeypatch.setattr(cliproxy.subprocess, "Popen", Popen)
    r = cliproxy.start(wait_seconds=5)
    assert r == {"ok": True, "started": True}
    assert spawned["args"][0].endswith(cliproxy.binary_name())
    assert spawned["args"][-1].endswith("config.yaml")
    assert spawned["kw"]["cwd"].endswith("cliproxy")


def test_status_starts_proxy_when_closed(proxy_home, monkeypatch):
    state = {"up": False}

    def fake_req(method, path):
        if not state["up"]:
            raise RuntimeError("kapalı")
        return {"files": [{"id": "claude-a.json", "provider": "claude", "email": "a@b.c"}]}

    def fake_start(wait_seconds=8.0):
        state["up"] = True
        return {"ok": True, "started": True}

    monkeypatch.setattr(cliproxy, "_req", fake_req)
    monkeypatch.setattr(cliproxy, "start", fake_start)
    d = cliproxy.status()
    assert d["running"] and d["accounts"][0]["email"] == "a@b.c"
