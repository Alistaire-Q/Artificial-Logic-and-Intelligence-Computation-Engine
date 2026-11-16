"""Shell komutları ve arka plan süreçleri.

Windows'ta PowerShell (varsa pwsh) kullanılır ve çıktı UTF-8'e zorlanır; yoksa Türkçe
karakterler cp857 yüzünden bozuk gelir.
"""

from __future__ import annotations

import contextlib
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from alice.tools.base import DANGER, EXEC, Tool, ToolContext, ToolError, ToolResult, obj, truncate

IS_WIN = sys.platform == "win32"
# Konsolsuz çalışan çekirdekten (pythonw) her komut ayrı bir siyah pencere açmasın. Komutun başlattığı
# pencereli programlar (Blender, Not Defteri...) yine görünür.
NO_WINDOW = 0x08000000 if IS_WIN else 0

# Geri dönüşü zor ya da sistemi bozabilecek kalıplar. "auto" modda bunlar için onay istenir.
DANGEROUS = [
    r"\brm\s+(-\w+\s+)*-\w*[rR]",  # özyinelemeli silme
    r"\bmkfs\b", r"\bdd\s+if=", r"\bfdisk\b", r"\bdiskpart\b", r"\bformat\s+[a-z]:",
    r"\b(shutdown|reboot|halt|poweroff)\b", r"Stop-Computer", r"Restart-Computer",
    r"Remove-Item\b.*-Recurse", r"\brd\s+/s\b", r"\brmdir\s+/s\b", r"\bdel\s+/[sq]",
    # PowerShell takma adları, bayrak yoldan sonra: "rm .\build -Recurse -Force", "ri C:\x -r"
    r"\b(rm|ri|del|erase|rd|rmdir)\b[^\n|;&]*\s-r(ecurse)?\b",
    r"\|\s*(Remove-Item|ri|rm|del|erase)\b",  # Get-ChildItem ... | Remove-Item
    r"\breg\s+(delete|add)\b", r"Remove-ItemProperty", r"Set-ItemProperty\s+.*HKLM",
    r"\bbcdedit\b", r"\bvssadmin\b.*delete", r"\bcipher\s+/w", r"Clear-Disk", r"Format-Volume",
    r"\bchmod\s+-R\s+[0-7]{3}\s+/", r"\bchown\s+-R\b.*\s/(\s|$)",
    r"git\s+push\s+.*--force", r"git\s+reset\s+--hard", r"git\s+clean\s+-[a-z]*f",
    r":\(\)\s*\{\s*:\|:&\s*\};:",  # fork bomb
    r"Set-ExecutionPolicy\s+Unrestricted", r"netsh\s+advfirewall\s+set\s+.*off",
    r"\bcurl\b.*\|\s*(sh|bash|iex)", r"\biwr\b.*\|\s*iex", r"Invoke-Expression.*DownloadString",
]
_DANGER_RX = [re.compile(p, re.I) for p in DANGEROUS]
# Aynı anda iki kopyası çalışınca kilit dosyasına takılan ya da kurulumu yarım bırakan programlar: paralel
# çalıştırmada bunlar birbirini bekler (bkz. RunCommand.exclusive_key).
_GIT_RX = re.compile(r"(?:^|[;&|({]|\s)git(?:\.exe)?\s", re.I)
_INSTALLER_RX = re.compile(r"(?:^|[;&|({]|\s)(?:npm|pnpm|yarn|npx|pip3?|uv|poetry|conda|winget|choco|scoop|apt|apt-get|"
                           r"brew|cargo|dotnet|msiexec)(?:\.exe|\.cmd)?\s|-m\s+pip\s", re.I)
_sandbox_lock = threading.Lock()  # ilk kullanımda klasör etiketlenir: paralel komutlar aynı anda etiketlemesin
READ_ONLY = re.compile(r"^\s*(get-\w+|dir|ls|type|cat|where(\.exe)?|test-path|select-string)(\s|$)", re.I)
ACCESS_DENIED = re.compile(r"access (is )?denied|access to the path .* is denied|erişim (engellendi|reddedildi)|UnauthorizedAccess|permission denied|"
                           r"EACCES|EPERM", re.I)


def is_dangerous(command: str) -> bool:
    return any(rx.search(command) for rx in _DANGER_RX)


def shell_argv(command: str) -> list[str]:
    if IS_WIN:
        exe = shutil.which("pwsh") or shutil.which("powershell") or "powershell"
        # Kodlama kurulumunu ayrı bir SATIRA al (eskiden "; " ile aynı satırdaydı). Böylece PowerShell'in
        # ayrıştırma hatalarındaki "At line:N char:M" konumu kullanıcının komutuna (2. satır) denk gelir;
        # önceden ~110 karakterlik önek yüzünden char konumu kayıyor, model hatayı yanlış yerde arıyordu
        # (2026-11-13 oturumunda gh/jq komutlarında çok görüldü). -EncodedCommand denendi ama Windows
        # PowerShell 5.1 onunla stderr'i okunamaz CLIXML'e sarıyor; -Command düz metin bırakıyor.
        setup = "[Console]::OutputEncoding=[System.Text.Encoding]::UTF8; $OutputEncoding=[System.Text.Encoding]::UTF8"
        return [exe, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", setup + "\n" + command]
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
        "Shell komutu çalıştırır (Windows: PowerShell 5.1, diğer: bash); çıkış kodu ve stdout+stderr döner.\n"
        "- Dosya okumak/aramak/düzenlemek için komut değil read_file, grep, glob, edit_file kullan.\n"
        "- Pencereli program ya da site açmak için windows action=open; Blender için blender aracı.\n"
        "- Sürekli çalışan işler (sunucu, izleme) için background=true, sonra process_output.\n"
        "- Birbirinden bağımsız komutları AYNI cevapta ayrı ayrı iste: aynı anda çalışırlar (toplam süre en uzun "
        "komut kadar olur). Biri diğerinin sonucuna muhtaçsa (klasör oluştur + içinde çalış, kur + kullan) ayrı "
        "çağrı yapma, tek komutta ';' ile sırala.\n"
        "- PowerShell 5.1'de '&&' yok: komutları ';' ile ayır. Boşluklu yolları tırnak içine al.\n"
        "- Çıktı sınıra sığmazsa kesilir ama tamamı geçici bir dosyaya yazılır; sonuçta dosya yolu verilir, "
        "gerekirse oradan oku."
    )
    parameters = obj({
        "command": {"type": "string", "description": "Çalıştırılacak komut, ör. 'git status' ya da 'python -m pytest -q'."},
        "cwd": {"type": "string", "description": "Komutun çalışacağı klasör. Varsayılan: çalışma klasörü."},
        "timeout": {"type": "integer", "description": "Saniye; dolunca komut öldürülür. Varsayılan 120, en fazla 1800."},
        "background": {"type": "boolean", "description": "true: beklemeden arka planda başlat ve pid döndür. "
                       "Varsayılan false."},
        "unsandboxed": {"type": "boolean", "description": "Sadece komut sandbox yüzünden başarısız olduysa ve "
                        "proje dışına yazması gerçekten gerekiyorsa true (kullanıcıya her zaman sorulur)."},
    }, ["command"])
    risk = EXEC

    def risk_for(self, args: dict[str, Any]) -> str:
        if args.get("unsandboxed"):
            return DANGER  # sandbox'tan çıkmak her zaman onay ister
        return DANGER if is_dangerous(args.get("command", "")) else EXEC

    def summary(self, args: dict[str, Any]) -> str:
        return args.get("command", "")[:300]

    def parallel_for(self, ctx: ToolContext, args: dict[str, Any]) -> bool:
        # Onay isteyen (tehlikeli, sandbox dışı) komutlar zaten tek tek sorulur; gerisi yan yana çalışabilir.
        return (bool(ctx.config.agent.get("parallel_commands", True)) and not args.get("unsandboxed")
                and not is_dangerous(str(args.get("command", ""))))

    def exclusive_key(self, args: dict[str, Any]) -> str:
        command = str(args.get("command", ""))
        if _INSTALLER_RX.search(command):
            return "kurulum"
        return "git" if _GIT_RX.search(command) else ""

    def run(self, ctx: ToolContext, command: str, cwd: str | None = None, timeout: int = 120,
            background: bool = False, unsandboxed: bool = False) -> ToolResult:
        wd = ctx.resolve(cwd) if cwd else ctx.cwd
        if not wd.is_dir():
            raise ToolError(f"klasör yok: {wd}")
        argv = shell_argv(command)
        env = _env()
        from alice import sandbox

        boxed = sandbox.applies(ctx, {"command": command, "unsandboxed": unsandboxed})
        if boxed:
            try:
                with _sandbox_lock:
                    argv, extra = sandbox.wrap(argv, wd, ctx)
            except (RuntimeError, OSError, subprocess.SubprocessError) as e:
                raise ToolError(f"sandbox kurulamadı, komut çalıştırılmadı: {e}. Gerekiyorsa unsandboxed=true ile "
                                "(kullanıcı onayıyla) çalıştır.") from e
            env = sandbox.scrub_env(env, ctx)  # .env'deki API anahtarları sandbox'a geçmesin
            env.update(extra)
        from alice import gitsafe

        # Sandbox .git'e yazabilir: hook ya da config değişirse (ALICE ya da kullanıcı sonra sandbox dışında git
        # çalıştırınca devreye girer) sonuca uyarı eklenir.
        git_before = gitsafe.snapshot(wd) if boxed and not background else None
        limit = int(ctx.config.agent.get("tool_output_chars", 12000))
        if background:
            # Ad zaman damgasıydı: aynı milisaniyede başlayan iki süreç aynı dosyaya yazıp çıktıları karışıyordu.
            log = Path(tempfile.gettempdir()) / f"alice-bg-{int(time.time() * 1000)}-{uuid.uuid4().hex[:6]}.log"
            kwargs: dict[str, Any] = {}
            if IS_WIN:
                kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP | NO_WINDOW  # type: ignore[attr-defined]
            else:
                kwargs["start_new_session"] = True
            with open(log, "wb") as fh:  # çocuk süreç kendi kopyasını alır; bizdeki tanıtıcı sızmasın
                pop = subprocess.Popen(argv, cwd=wd, stdout=fh, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                       env=env, **kwargs)
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
            pop = subprocess.Popen(argv, cwd=wd, stdout=fo, stderr=fe, stdin=subprocess.DEVNULL, env=env,
                                   creationflags=NO_WINDOW)
            with track_foreground(ctx, pop):
                try:
                    returncode = pop.wait(timeout=timeout)
                    timed_out = False
                except subprocess.TimeoutExpired:
                    # Kabuk ve konsol alt süreçleri (takılan pytest, node...) ölür; komutun açtığı pencereli program
                    # (Not Defteri, Blender penceresi) ve onun alt süreçleri açık kalır. Eskiden yalnız kabuk
                    # öldürülüyordu, takılan alt süreçler arkada çalışmaya devam ediyordu.
                    kill_console_tree(pop.pid)
                    try:
                        pop.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        pop.kill()
                        pop.wait()
                    returncode, timed_out = -1, True
            fo.seek(0)
            fe.seek(0)
            out = fo.read().decode("utf-8", errors="replace")
            err = fe.read().decode("utf-8", errors="replace")
        if timed_out:
            return ToolResult(f"ZAMAN AŞIMI ({timeout} sn). Uzun işler için background=true kullan. Pencereli bir "
                              "program açıyorsan run_command yerine windows action=open kullan; Blender için blender aracını "
                              f"kullan.\n{truncate(out + err, limit)}"
                              + (gitsafe.warning(gitsafe.changes(git_before, gitsafe.snapshot(wd)))
                                 if git_before is not None else ""), is_error=True)
        body = out
        if err.strip():
            body += ("\n[stderr]\n" if out.strip() else "[stderr]\n") + err
        body = truncate(body.strip(), limit) or "(çıktı yok)"
        if len(out) + len(err) > limit:
            # Kesilen çıktı kaybolmasın: tamamı geçici bir dosyaya yazılır, model oradan okuyabilir.
            # mkstemp: POSIX'te dosyayı 0600 ile açar; write_bytes 0644 bırakıp çıktıyı başka kullanıcılara açardı.
            try:
                fd, name = tempfile.mkstemp(prefix="alice-out-", suffix=".log")
                with os.fdopen(fd, "wb") as fh:
                    fh.write((out + ("\n" + err if err.strip() else "")).encode("utf-8", errors="replace"))
                body += f"\n[tam çıktı dosyada: {name} - kesilen kısmı read_file/grep ile oku]"
            except OSError:
                pass
        if IS_WIN and "is not a valid statement separator" in err:
            body += ("\n[ipucu] Bu Windows PowerShell 5.1: '&&' ve '||' yok. Komutları ';' ile ayır, "
                     "koşul için 'if ($?) { ... }' ya da 'if (-not $?) { ... }' kullan.")
        body += _ps_hint(command, err) if IS_WIN else ""
        # Yalnız gerçek erişim hatasında ya da yazmaya çalışan komut başarısız olunca (düşük bütünlükte engellenen
        # yazma bazen hiç çıktı vermiyor). Salt okuma komutunun hatası ("dosya yok") sandbox'tan değil.
        if boxed and (ACCESS_DENIED.search(out + err) or (returncode != 0 and not READ_ONLY.match(command))):
            # Yalnız gerçekten erişim hatası varken: her başarısız komuta eklenince model "dosya yok"u da "erişim
            # engellendi" sanıyordu (olmayan Sonora-Setup.exe'ye bakan Get-Item, 2026-11-02).
            body += (f"\n[sandbox: komut sadece {wd} ve geçici klasöre yazabildi. Bu erişim hatası bu yüzdense ve proje "
                     "dışına yazmak gerçekten gerekiyorsa unsandboxed=true ile tekrar dene.]")
        if git_before is not None:
            body += gitsafe.warning(gitsafe.changes(git_before, gitsafe.snapshot(wd)))
        return ToolResult(f"çıkış kodu {returncode}\n{body}", is_error=returncode != 0)


_INLINE_CODE_RX = re.compile(r"(python(\.exe)?\"?|py|node(\.exe)?)\s+-[ce]\s", re.I)


def _ps_hint(command: str, err: str) -> str:
    """PowerShell'in sık yapılan sözdizimi hatalarına tek satırlık çare (oturum kayıtlarında tekrar edenler):
    model hatayı çözmek için birkaç tur harcıyordu."""
    if "was not followed by a valid variable name character" in err:
        return ("\n[ipucu] PowerShell \"$ad: ...\" içindeki ':' işaretini değişken adının parçası sanıyor. "
                "\"${ad}: ...\" ya da \"$($ad): ...\" yaz.")
    if "Unrecognized token" in err and "@@" in command:
        return "\n[ipucu] Sözlük '@{ ... }' ile yazılır; '@@{' geçersiz."
    if _INLINE_CODE_RX.search(command) and ("SyntaxError" in err or "unexpected EOF" in err or "Unexpected token" in err):
        return ("\n[ipucu] PowerShell, -c ile verilen kodun içindeki çift tırnakları ve ters eğik çizgileri bozuyor. "
                "Kodu write_file ile geçici bir dosyaya yaz ve dosyayı çalıştır; satır içi kodda sadece tek tırnak kullan.")
    return ""


class ProcessOutput(Tool):
    name = "process_output"
    parallel_safe = True
    description = ("Arka planda çalışan süreçlerin (run_command background=true) durumunu (çalışıyor / bitti + çıkış kodu) ve "
                   "çıktısının sonunu gösterir; hemen döner, beklemez.\n"
                   "- pid verilmezse ALICE'in başlattığı bütün arka plan süreçlerini listeler.\n"
                   "- Bir sürecin bitmesini ya da çıktısında bir satır (\"ready\", \"error\") görünmesini beklemek için bunu "
                   "tekrar tekrar çağırma; process_wait kullan.\n"
                   "- ALICE'in başlatmadığı programlar için process_list.")
    parameters = obj({
        "pid": {"type": "integer", "description": "run_command'ın döndürdüğü pid. Boş: tüm arka plan süreçleri."},
        "tail_chars": {"type": "integer", "description": "Çıktının sonundan kaç karakter. Varsayılan 4000; "
                       "0: çıktı yok, yalnız durum."},
    })

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
        # data[-0:] bütün logu döndürüyordu: 0 ve negatif "çıktı yok, yalnız durum" demek.
        n = int(tail_chars or 0)
        if n <= 0:
            return ToolResult(f"pid={pid} {state}")
        return ToolResult(f"pid={pid} {state}\n{data[-n:]}")


class ProcessStop(Tool):
    name = "process_stop"
    description = ("ALICE'in run_command(background=true) ile başlattığı bir süreci, alt süreçleriyle birlikte "
                   "durdurur ve son çıktısını döndürür.\n"
                   "- İşi biten sunucu/izleme süreçlerini açık bırakma; bitince bununla kapat.\n"
                   "- ALICE'in başlatmadığı programlar (kullanıcının açtığı uygulamalar) için process_kill kullan.")
    parameters = obj({"pid": {"type": "integer", "description": "run_command'ın döndürdüğü pid."}}, ["pid"])

    risk = EXEC

    def run(self, ctx: ToolContext, pid: int) -> ToolResult:
        procs: dict[int, BgProc] = ctx.state.get("bg_procs", {})
        p = procs.get(pid)
        if not p:
            raise ToolError(f"pid {pid} ALICE tarafından başlatılmamış; başka süreçler için process_kill kullan")
        kill_tree(pid)
        _reap(p.popen)
        return ToolResult(f"durduruldu: {pid}")


@contextlib.contextmanager
def track_foreground(ctx: ToolContext, pop: subprocess.Popen):
    """Ön planda beklenen süreci kaydet ki Durdur tuşu onu (ve alt süreçlerini) sonlandırabilsin."""
    procs: set = ctx.state.setdefault("fg_procs", set())
    procs.add(pop)
    try:
        yield pop
    finally:
        procs.discard(pop)


def kill_foreground(ctx: ToolContext) -> None:
    for pop in list(ctx.state.get("fg_procs", ())):
        if pop.poll() is None:
            try:
                kill_tree(pop.pid)
            except Exception:  # noqa: BLE001 - durdururken hata yutulur
                pass


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


def _visible_window_pids() -> set[int]:
    """Görünür üst düzey penceresi olan süreçlerin pid'leri (yalnız Windows)."""
    if not IS_WIN:
        return set()
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32  # type: ignore[attr-defined]
    pids: set[int] = set()
    proto = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def cb(hwnd, _lparam):  # noqa: ANN001
        if user32.IsWindowVisible(hwnd):
            pid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            pids.add(int(pid.value))
        return True

    try:
        user32.EnumWindows(proto(cb), 0)
    except OSError:
        pass
    return pids


def _is_gui_exe(path: str) -> bool:
    """PE başlığındaki alt sistem alanı: 2 = Windows GUI (pencereli program), 3 = konsol."""
    try:
        with open(path, "rb") as fh:
            head = fh.read(4096)
        if head[:2] != b"MZ":
            return False
        pe = int.from_bytes(head[0x3C:0x40], "little")
        if head[pe:pe + 4] != b"PE\0\0":
            return False
        return int.from_bytes(head[pe + 0x5C:pe + 0x5E], "little") == 2
    except (OSError, ValueError):
        return False


def _is_gui_process(proc: Any, window_pids: set[int]) -> bool:
    if not IS_WIN:
        return False  # POSIX'te güvenilir ayrım yok; zaman aşımında bütün ağaç ölür
    if proc.pid in window_pids:
        return True
    try:
        return _is_gui_exe(proc.exe())
    except Exception:  # noqa: BLE001 - erişilemeyen süreç konsol sayılır
        return False


def kill_console_tree(pid: int) -> None:
    """Zaman aşımında: kabuğu ve konsol alt süreçlerini öldür, pencereli programları (ve onların alt
    süreçlerini) açık bırak. Pencereli = görünür penceresi var ya da exe'si GUI alt sistemli."""
    import psutil

    try:
        parent = psutil.Process(pid)
        children = parent.children(recursive=True)
    except psutil.Error:
        return
    window_pids = _visible_window_pids()
    keep: set[int] = set()
    for child in children:
        if child.pid in keep:
            continue
        if _is_gui_process(child, window_pids):
            keep.add(child.pid)
            try:
                keep.update(c.pid for c in child.children(recursive=True))
            except psutil.Error:
                pass
    for child in children:
        if child.pid in keep:
            continue
        try:
            child.kill()
        except psutil.Error:
            pass
    try:
        parent.kill()
    except psutil.Error:
        pass


def _reap(pop: subprocess.Popen) -> None:
    """Öldürülen süreci bekle: uzun oturumda zombi süreç birikmesin."""
    try:
        pop.wait(timeout=5)
    except subprocess.TimeoutExpired:
        pass


def cleanup(ctx: ToolContext) -> None:
    for p in ctx.state.get("bg_procs", {}).values():
        if p.popen.poll() is None:
            kill_tree(p.pid)
            _reap(p.popen)


class ProcessWait(Tool):
    """Claude Code'daki Monitor/until-döngüsünün karşılığı: tek çağrıda bekle, araç çağrısı israf etme."""

    name = "process_wait"
    parallel_safe = True
    description = ("Arka plandaki bir süreç bitene ya da çıktısında aranan desen görünene kadar bekler, sonra "
                   "eşleşen satırları (ya da bitiş kodunu) ve çıktının sonunu döndürür.\n"
                   "- \"Sunucu hazır olunca\", \"derleme bitince\", \"testler bitince\" gibi beklemeler için. "
                   "process_output'u döngüde çağırmak ya da run_command ile sleep yazmak yerine bunu kullan.\n"
                   "- pattern hem başarıyı hem hatayı yakalasın: ör. 'ready|listening|error|Traceback'. Sadece "
                   "başarıyı ararsan süreç çöktüğünde zaman aşımına kadar boşuna beklersin.\n"
                   "- Desen verilmezse süreç bitene kadar bekler. Sadece bu çağrıdan SONRA gelen çıktıya bakılır.\n"
                   "- Zaman aşımında süreç durdurulmaz; durum ve çıktı döner, istersen tekrar bekle.")
    parameters = obj({
        "pid": {"type": "integer", "description": "run_command(background=true)'ın döndürdüğü pid."},
        "pattern": {"type": "string", "description": "Çıktıda beklenen düzenli ifade (büyük/küçük harf duyarsız), "
                    "ör. 'Local:.*http|error'. Boş: süreç bitene kadar bekle."},
        "timeout": {"type": "integer", "description": "En fazla kaç saniye beklensin. Varsayılan 60, en fazla 600."},
    }, ["pid"])

    def summary(self, args: dict[str, Any]) -> str:
        return f"pid {args.get('pid')} {args.get('pattern') or 'bitene kadar'}"

    def run(self, ctx: ToolContext, pid: int, pattern: str = "", timeout: int = 60) -> ToolResult:
        procs: dict[int, BgProc] = ctx.state.get("bg_procs", {})
        p = procs.get(pid)
        if not p:
            raise ToolError(f"pid {pid} ALICE tarafından başlatılmamış; listelemek için process_output")
        try:
            rx = re.compile(pattern, re.I) if pattern else None
        except re.error as e:
            raise ToolError(f"geçersiz desen: {e}") from e

        def read() -> str:
            return p.log.read_bytes().decode("utf-8", errors="replace") if p.log.exists() else ""

        start = len(read())
        end = time.monotonic() + max(1, min(int(timeout or 60), 600))
        while True:
            # Önce bitti mi bak, sonra oku: tersinde süreç son satırını yazıp okuma ile poll arasında çıkınca o satır
            # (çoğu zaman aranan "error"/"done") hiç görülmeden "desen görünmedi" deniyordu.
            code = p.popen.poll()
            data = read()
            new = data[start:]
            if rx is not None:
                hits = [ln for ln in new.splitlines() if rx.search(ln)]
                if hits:
                    return ToolResult(f"pid={pid} desen göründü:\n" + "\n".join(hits[-20:]) +
                                      f"\n--- çıktının sonu ---\n{data[-2000:]}")
            if code is not None:
                return ToolResult(f"pid={pid} bitti (kod {code})" + (" — desen görünmedi" if rx else "") +
                                  f"\n{data[-4000:]}", is_error=bool(rx) or code != 0)
            if ctx.cancelled():
                return ToolResult(f"pid={pid} bekleme durduruldu (süreç çalışmaya devam ediyor)")
            if time.monotonic() >= end:
                return ToolResult(f"pid={pid} zaman aşımı: hâlâ çalışıyor" + (", desen görünmedi" if rx else "") +
                                  f"\n{data[-2000:]}")
            time.sleep(0.5)


TOOLS = [RunCommand(), ProcessOutput(), ProcessStop(), ProcessWait()]
