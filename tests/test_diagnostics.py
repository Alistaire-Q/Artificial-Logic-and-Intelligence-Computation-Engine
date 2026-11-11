import pytest
from pathlib import Path
from unittest.mock import MagicMock

from alice.tools.base import ToolContext
from alice.tools.diagnostics import AppState, ServiceDiagnose, TestRun


@pytest.fixture
def ctx(tmp_path):
    config = MagicMock()
    config.agent = {}
    return ToolContext(cwd=tmp_path, config=config)


def test_app_state_runs(ctx):
    tool = AppState()
    res = tool.run(ctx)
    assert not res.is_error
    assert "Servis ve Mod" in res.text


def test_service_diagnose_runs(ctx):
    tool = ServiceDiagnose()
    res = tool.run(ctx)
    assert not res.is_error
    assert "Çekirdek Servis Teşhisi" in res.text


def test_test_run_success(ctx):
    tool = TestRun()
    res = tool.run(ctx, command="py -3 -c \"print('test passed')\"")
    assert not res.is_error
    assert "Bütün testler başarıyla geçti" in res.text
