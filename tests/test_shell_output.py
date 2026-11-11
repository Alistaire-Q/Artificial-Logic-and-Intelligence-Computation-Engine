import pytest
from pathlib import Path
from unittest.mock import MagicMock

from alice.tools.base import ToolContext
from alice.tools.shell import RunCommand


@pytest.fixture
def ctx(tmp_path):
    config = MagicMock()
    config.agent = {"tool_output_chars": 200}
    return ToolContext(cwd=tmp_path, config=config)


def test_output_truncated_and_dumped_to_file(ctx):
    cmd = "py -3 -c \"print('x'*3000)\""
    res = RunCommand().run(ctx, command=cmd, timeout=60)
    assert "karakter kesildi" in res.text
    assert "tam çıktı dosyada:" in res.text
    dump = res.text.split("tam çıktı dosyada:")[1].split(" - ")[0].strip()
    assert Path(dump).exists() and Path(dump).stat().st_size > 2000


def test_no_dump_when_output_fits(ctx):
    res = RunCommand().run(ctx, command="py -3 -c \"print('kisa')\"", timeout=60)
    assert "kisa" in res.text and "tam çıktı dosyada:" not in res.text
