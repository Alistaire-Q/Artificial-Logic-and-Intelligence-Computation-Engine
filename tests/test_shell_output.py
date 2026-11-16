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


def _spawn_sleeper_script(tmp_path: Path, pidfile: Path) -> str:
    code = (
        "import subprocess, sys, time\n"
        "c = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
        f"open(r'{pidfile}', 'w').write(str(c.pid))\n"
        "time.sleep(60)\n"
    )
    return _script(tmp_path, code)


def _wait_gone(pid: int, secs: float = 10) -> bool:
    import time

    import psutil
    end = time.monotonic() + secs
    while time.monotonic() < end:
        try:
            if psutil.Process(pid).status() == psutil.STATUS_ZOMBIE:
                return True
        except psutil.NoSuchProcess:
            return True
        time.sleep(0.2)
    return False


def test_timeout_kills_console_children(ctx, tmp_path):
    # Zaman aşımında yalnız kabuk değil, takılan konsol alt süreçleri de ölmeli.
    pidfile = tmp_path / "child.pid"
    res = RunCommand().run(ctx, command=_spawn_sleeper_script(tmp_path, pidfile), timeout=4)
    assert res.is_error and "ZAMAN AŞIMI" in res.text
    child = int(pidfile.read_text())
    assert _wait_gone(child)


def test_timeout_keeps_gui_children(ctx, tmp_path, monkeypatch):
    # Pencereli program (burada GUI sayılan uyuyan alt süreç) zaman aşımında açık kalır.
    import psutil

    import alice.tools.shell as sh
    pidfile = tmp_path / "child.pid"
    monkeypatch.setattr(sh, "_is_gui_process", lambda proc, wp: pidfile.exists() and proc.pid == int(pidfile.read_text()))
    RunCommand().run(ctx, command=_spawn_sleeper_script(tmp_path, pidfile), timeout=4)
    child = int(pidfile.read_text())
    try:
        assert psutil.Process(child).is_running()
    finally:
        sh.kill_tree(child)


@pytest.mark.skipif(sys.platform != "win32", reason="PE alt sistemi yalnız Windows'ta")
def test_is_gui_exe_reads_pe_subsystem():
    import alice.tools.shell as sh
    exe = Path(sys.executable)
    assert sh._is_gui_exe(str(exe)) is False  # python.exe konsol
    pyw = exe.with_name("pythonw.exe")
    if pyw.exists():
        assert sh._is_gui_exe(str(pyw)) is True


def test_process_output_tail_chars_zero_or_negative_returns_no_output(ctx, tmp_path):
    # data[-0:] eskiden bütün logu döndürüyordu.
    import time

    from alice.tools.shell import BgProc, ProcessOutput
    log = tmp_path / "bg.log"
    log.write_text("satir1\nsatir2\nSON", encoding="utf-8")
    pop = MagicMock()
    pop.poll.return_value = 0
    pop.returncode = 0
    ctx.state["bg_procs"] = {42: BgProc(42, "x", pop, log, time.time())}
    po = ProcessOutput()
    assert po.run(ctx, pid=42, tail_chars=3).text.endswith("SON")
    for n in (0, -5):
        assert po.run(ctx, pid=42, tail_chars=n).text == "pid=42 bitti (kod 0)"
