import sys

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


def _script(tmp_path: Path, code: str) -> str:
    """Kodu geçici bir .py dosyasına yaz ve onu çalıştıracak komutu döndür.

    'py -3' Ubuntu'da yok; satır içi -c ise PowerShell'de tırnakları bozuyor. Betik dosyası iki tarafta da çalışır.
    """
    p = tmp_path / "script.py"
    p.write_text(code, encoding="utf-8")
    # PowerShell tırnaklı bir yolu tek başına komut sanmıyor (UnexpectedToken): Windows'ta '&' çağrı operatörü,
    # bash'te ise düz yol gerekir.
    call = f'& "{sys.executable}" "{p}"' if sys.platform == "win32" else f'"{sys.executable}" "{p}"'
    return call


def test_output_truncated_and_dumped_to_file(ctx, tmp_path):
    res = RunCommand().run(ctx, command=_script(tmp_path, "print('x' * 3000)"), timeout=60)
    assert "karakter kesildi" in res.text
    assert "tam çıktı dosyada:" in res.text
    dump = res.text.split("tam çıktı dosyada:")[1].split(" - ")[0].strip()
    assert Path(dump).exists() and Path(dump).stat().st_size > 2000


def test_no_dump_when_output_fits(ctx, tmp_path):
    res = RunCommand().run(ctx, command=_script(tmp_path, "print('kisa')"), timeout=60)
    assert "kisa" in res.text and "tam çıktı dosyada:" not in res.text


def test_windows_argv_puts_command_on_own_line_not_encoded(monkeypatch):
    # Kurulum ayrı satırda, -Command düz metin: PowerShell'in hata konumu kullanıcı komutuna denk gelsin ve
    # stderr CLIXML'e sarılmasın (bkz. shell_argv yorumu, 2026-11-13 oturumu).
    import alice.tools.shell as sh
    monkeypatch.setattr(sh, "IS_WIN", True)
    monkeypatch.setattr(sh.shutil, "which", lambda n: None)  # pwsh yok -> powershell
    argv = sh.shell_argv("gh api x --jq .content")
    assert argv[0] == "powershell"
    assert "-EncodedCommand" not in argv and "-Command" in argv
    script = argv[-1]
    assert script.startswith("[Console]::OutputEncoding")
    # kullanıcı komutu kurulum satırından sonra, kendi satırında
    assert script.split("\n", 1)[1] == "gh api x --jq .content"
