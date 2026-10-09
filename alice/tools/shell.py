"""Shell komutları ve arka plan süreçleri.

Windows'ta PowerShell (varsa pwsh) kullanılır ve çıktı UTF-8'e zorlanır; yoksa Türkçe
karakterler cp857 yüzünden bozuk gelir.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from alice.tools.base import DANGER, EXEC, Tool, ToolContext, ToolError, ToolResult, obj, truncate

IS_WIN = sys.platform == "win32"

# Geri dönüşü zor ya da sistemi bozabilecek kalıplar. "auto" modda bunlar için onay istenir.
DANGEROUS = [
    r"\brm\s+(-\w+\s+)*-\w*[rR]",  # özyinelemeli silme
    r"\bmkfs\b", r"\bdd\s+if=", r"\bfdisk\b", r"\bdiskpart\b", r"\bformat\s+[a-z]:",
    r"\b(shutdown|reboot|halt|poweroff)\b", r"Stop-Computer", r"Restart-Computer",
    r"Remove-Item\b.*-Recurse", r"\brd\s+/s\b", r"\brmdir\s+/s\b", r"\bdel\s+/[sq]",
    r"\breg\s+(delete|add)\b", r"Remove-ItemProperty", r"Set-ItemProperty\s+.*HKLM",
    r"\bbcdedit\b", r"\bvssadmin\b.*delete", r"\bcipher\s+/w", r"Clear-Disk", r"Format-Volume",
    r"\bchmod\s+-R\s+[0-7]{3}\s+/", r"\bchown\s+-R\b.*\s/(\s|$)",
    r"git\s+push\s+.*--force", r"git\s+reset\s+--hard", r"git\s+clean\s+-[a-z]*f",
    r":\(\)\s*\{\s*:\|:&\s*\};:",  # fork bomb
    r"Set-ExecutionPolicy\s+Unrestricted", r"netsh\s+advfirewall\s+set\s+.*off",
    r"\bcurl\b.*\|\s*(sh|bash|iex)", r"\biwr\b.*\|\s*iex", r"Invoke-Expression.*DownloadString",
]
_DANGER_RX = [re.compile(p, re.I) for p in DANGEROUS]


def is_dangerous(command: str) -> bool:
    return any(rx.search(command) for rx in _DANGER_RX)


def shell_argv(command: str) -> list[str]:
    if IS_WIN:
        exe = shutil.which("pwsh") or shutil.which("powershell") or "powershell"
        prefix = "[Console]::OutputEncoding=[System.Text.Encoding]::UTF8; $OutputEncoding=[System.Text.Encoding]::UTF8; "
        return [exe, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", prefix + command]
    sh = shutil.which("bash") or "/bin/sh"
    return [sh, "-lc", command]


def shell_name() -> str:
    if IS_WIN:
        return "pwsh" if shutil.which("pwsh") else "powershell"
    return "bash" if shutil.which("bash") else "sh"


def _env() -> dict[str, str]:
    env = dict(os.environ)
    env.setdefault("PYTHONIOENCODING", "utf-8")
    env.setdefault("PYTHONUTF8", "1")
    return env


@dataclass
class BgProc:
    pid: int
    command: str
    popen: subprocess.Popen
    log: Path
    started: float


class RunCommand(Tool):
    name = "run_command"
    description = (
        "Shell komutu çalıştır (Windows: PowerShell, diğer: bash). stdout+stderr ve çıkış kodu döner. "
        "Uzun süren sunucu/izleme işleri için background=true ver, sonra process_output ile çıktıyı oku."
    )
    parameters = obj({
        "command": {"type": "string"},
        "cwd": {"type": "string", "description": "varsayılan: çalışma klasörü"},
        "timeout": {"type": "integer", "description": "saniye, varsayılan 120, en fazla 1800"},
        "background": {"type": "boolean"},
    }, ["command"])
    risk = EXEC

    def risk_for(self, args: dict[str, Any]) -> str:
        return DANGER if is_dangerous(args.get("command", "")) else EXEC

    def summary(self, args: dict[str, Any]) -> str:
        return args.get("command", "")[:300]

    def run(self, ctx: ToolContext, command: str, cwd: str | None = None, timeout: int = 120,
            background: bool = False) -> ToolResult:
        wd = ctx.resolve(cwd) if cwd else ctx.cwd
        if not wd.is_dir():
            raise ToolError(f"klasör yok: {wd}")
        argv = shell_argv(command)
        limit = int(ctx.config.agent.get("tool_output_chars", 12000))
        if background:
            log = Path(tempfile.gettempdir()) / f"alice-bg-{int(time.time() * 1000)}.log"
            fh = open(log, "wb")
            kwargs: dict[str, Any] = {}
            if IS_WIN:
                kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP  # type: ignore[attr-defined]
            else:
                kwargs["start_new_session"] = True
            pop = subprocess.Popen(argv, cwd=wd, stdout=fh, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                   env=_env(), **kwargs)
            procs: dict[int, BgProc] = ctx.state.setdefault("bg_procs", {})
            procs[pop.pid] = BgProc(pop.pid, command, pop, log, time.time())
            time.sleep(1.5)
            head = log.read_bytes().decode("utf-8", errors="replace")[-2000:]
            state = "çalışıyor" if pop.poll() is None else f"bitti (kod {pop.returncode})"
            return ToolResult(f"arka planda başlatıldı pid={pop.pid}, durum: {state}\nlog: {log}\n{head}")
        timeout = max(1, min(int(timeout), 1800))
        # Çıktı boru yerine geçici dosyaya yazılır: komutun başlattığı pencereli bir program (ör. Blender)
        # boruları devralırsa subprocess.run zaman aşımından sonra bile sonsuza kadar bekliyordu.
        with tempfile.TemporaryFile() as fo, tempfile.TemporaryFile() as fe:
            pop = subprocess.Popen(argv, cwd=wd, stdout=fo, stderr=fe, stdin=subprocess.DEVNULL, env=_env())
            try:
                returncode = pop.wait(timeout=timeout)
                timed_out = False
            except subprocess.TimeoutExpired:
                pop.kill()  # sadece kabuğu öldür; açtığı pencereli program açık kalsın
                pop.wait()
                returncode, timed_out = -1, True
            fo.seek(0)
            fe.seek(0)
            out = fo.read().decode("utf-8", errors="replace")
            err = fe.read().decode("utf-8", errors="replace")
        if timed_out:
            return ToolResult(f"ZAMAN AŞIMI ({timeout} sn). Uzun işler için background=true kullan. Pencereli bir "
                              "program açıyorsan run_command yerine windows action=open kullan; Blender için blender aracını "
                              f"kullan.\n{truncate(out + err, limit)}", is_error=True)
        body = out
        if err.strip():
            body += ("\n[stderr]\n" if out.strip() else "[stderr]\n") + err
        body = truncate(body.strip(), limit) or "(çıktı yok)"
        if IS_WIN and "is not a valid statement separator" in err:
            body += ("\n[ipucu] Bu Windows PowerShell 5.1: '&&' ve '||' yok. Komutları ';' ile ayır, "
                     "koşul için 'if ($?) { ... }' ya da 'if (-not $?) { ... }' kullan.")
        return ToolResult(f"çıkış kodu {returncode}\n{body}", is_error=returncode != 0)


class ProcessOutput(Tool):
    name = "process_output"
    parallel_safe = True
    description = "run_command(background=true) ile başlatılan süreçlerin durumunu ve son çıktısını göster. pid verilmezse hepsini listeler."
    parameters = obj({"pid": {"type": "integer"}, "tail_chars": {"type": "integer"}})

    def run(self, ctx: ToolContext, pid: int | None = None, tail_chars: int = 4000) -> ToolResult:
        procs: dict[int, BgProc] = ctx.state.get("bg_procs", {})
        if pid is None:
            if not procs:
                return ToolResult("arka plan süreci yok")
            rows = [f"pid={p.pid} {'çalışıyor' if p.popen.poll() is None else f'bitti({p.popen.returncode})'} :: {p.command[:100]}"
                    for p in procs.values()]
            return ToolResult("\n".join(rows))
        p = procs.get(pid)
        if not p:
            raise ToolError(f"pid {pid} ALICE tarafından başlatılmamış")
        data = p.log.read_bytes().decode("utf-8", errors="replace") if p.log.exists() else ""
        state = "çalışıyor" if p.popen.poll() is None else f"bitti (kod {p.popen.returncode})"
        return ToolResult(f"pid={pid} {state}\n{data[-tail_chars:]}")


class ProcessStop(Tool):
    name = "process_stop"
    description = "ALICE'in arka planda başlattığı bir süreci durdur."
    parameters = obj({"pid": {"type": "integer"}}, ["pid"])
    risk = EXEC

    def run(self, ctx: ToolContext, pid: int) -> ToolResult:
        procs: dict[int, BgProc] = ctx.state.get("bg_procs", {})
        p = procs.get(pid)
        if not p:
            raise ToolError(f"pid {pid} ALICE tarafından başlatılmamış; başka süreçler için process_kill kullan")
        kill_tree(pid)
        return ToolResult(f"durduruldu: {pid}")


def kill_tree(pid: int) -> None:
    import psutil

    try:
        parent = psutil.Process(pid)
    except psutil.NoSuchProcess:
        return
    for child in parent.children(recursive=True):
        try:
            child.kill()
        except psutil.Error:
            pass
    try:
        parent.kill()
    except psutil.Error:
        pass


def cleanup(ctx: ToolContext) -> None:
    for p in ctx.state.get("bg_procs", {}).values():
        if p.popen.poll() is None:
            kill_tree(p.pid)


TOOLS = [RunCommand(), ProcessOutput(), ProcessStop()]
