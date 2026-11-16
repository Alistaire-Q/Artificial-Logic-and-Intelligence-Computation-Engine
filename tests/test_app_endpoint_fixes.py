"""Uygulama uç noktalarının kenar durumları: bozuk girdi 500 değil 400, kısmi kayıt alanları silmiyor."""

from __future__ import annotations

import httpx
import pytest

from alice.app.server import AppServer, AppState
from tests.conftest import FakeProvider, make_config


@pytest.fixture
def app(workdir):
    cfg = make_config({"brain": ["a:m"], "fast": ["a:m"], "vision": ["a:m"]})
    prov = FakeProvider("a", [])
    state = AppState(cfg, workdir, "auto", provider_factory=lambda pc: prov)
    srv = AppServer(state).start()
    client = httpx.Client(base_url=srv.url.split("/?")[0], headers={"X-Alice-Token": state.token}, timeout=10)
    yield state, client
    srv.stop()


def test_usage_has_session_breakdown_and_today(app):
    state, client = app
    state.router.usage.record("a:m", {"prompt_tokens": 10, "completion_tokens": 2}, True)
    u = client.get("/api/usage").json()
    # Kullanım dökümü penceresi (models/tools/ctx) ve Ayarlar > Kullanım (today) aynı yanıttan beslenir.
    assert {"models", "tools", "ctx", "steps"} <= set(u)
    assert u["today"]["a:m"]["requests"] == 1
