"""Uygulama penceresini aç.

Sıra: pywebview (Windows'ta Edge WebView2 ile gerçek uygulama penceresi) ->
Edge/Chrome --app modu (adres çubuğu olmayan ayrı pencere) -> varsayılan tarayıcı.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import threading
import time
import webbrowser
from pathlib import Path
from typing import Callable

from alice.config import alice_home


def _browser_candidates() -> list[str]:
    out: list[str] = []
    if sys.platform == "win32":
        pf = [os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"), os.environ.get("ProgramFiles", r"C:\Program Files"),
              os.environ.get("LOCALAPPDATA", "")]
        for base in pf:
            out += [str(Path(base) / r"Microsoft\Edge\Application\msedge.exe"),
                    str(Path(base) / r"Google\Chrome\Application\chrome.exe")]
    elif sys.platform == "darwin":
        out += ["/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
                "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge"]
    for name in ("msedge", "google-chrome", "chromium", "chromium-browser", "chrome"):
        w = shutil.which(name)
        if w:
            out.append(w)
    return [p for p in out if p and Path(p).exists()]


def _watch(watch: Callable[[], str | None], close: Callable[[], None], load: Callable[[str], None]) -> None:
    """watch() saniyede bir: "" = devam, yeni adres = ona geç, None = kapat (bkz. alice/app/host.py follow)."""
    def loop() -> None:
        while True:
            time.sleep(1.0)
            try:
                r = watch()
            except Exception:  # noqa: BLE001 - izleyici hatası pencereyi kapatmasın
                continue
            try:
                if r is None:
                    close()
                    return
                if r:
                    load(r)
            except Exception:  # noqa: BLE001 - pencere zaten kapanmış olabilir
                return

    threading.Thread(target=loop, daemon=True, name="alice-window-watch").start()


def open_window(url: str, title: str = "ALICE", prefer: str = "auto",
                watch: Callable[[], str | None] | None = None) -> str:
    """Pencereyi açar ve kapanana kadar bekler. Hangi yöntemin kullanıldığını döndürür.

    watch verilirse (çekirdekteki uygulamaya bağlı pencere) çekirdek kapanınca pencere de kapanır, çekirdek yeniden
    başlayınca yeni adrese geçer."""
    if prefer in ("auto", "webview"):
        try:
            import webview  # pywebview

            # Windows'taki pywebview (WinForms) simgeyi System.Drawing.Icon ile açar ve .png verilince
            # yakalanamayan bir .NET hatasıyla çöker; orada .ico kullan.
            icon = Path(__file__).parent / "static" / ("alice.ico" if sys.platform == "win32" else "icon.png")
            win = webview.create_window(title, url, width=1320, height=880, min_size=(820, 560),
                                        background_color="#16161a")
            if watch is not None:
                _watch(watch, win.destroy, win.load_url)
            kwargs = {"icon": str(icon)} if icon.exists() else {}
            try:
                webview.start(**kwargs)
            except TypeError:
                webview.start()
            return "webview"
        except ImportError:
            if prefer == "webview":
                raise
    if prefer in ("auto", "app"):
        for exe in _browser_candidates():
            profile = alice_home() / "app-window"
            profile.mkdir(parents=True, exist_ok=True)
            proc = subprocess.Popen([exe, f"--app={url}", "--window-size=1320,880", f"--user-data-dir={profile}",
                                     "--no-first-run", "--no-default-browser-check"])
            if watch is not None:
                _watch(watch, proc.terminate, lambda _u: None)  # uygulama modunda adres değiştirilemiyor
            proc.wait()
            return "app"
    webbrowser.open(url)
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        pass
    return "browser"


def create_shortcut() -> list[Path]:
    """Masaüstüne ve Başlat menüsüne konsolsuz açılan ALICE kısayolu koy (Windows).

    OneDrive'a taşınmış masaüstünü de bulur. Başlat menüsündeki kısayol sayesinde Windows tuşuna
    basıp "ALICE" yazarak da açılır."""
    if sys.platform != "win32":
        raise RuntimeError("kısayol oluşturma şimdilik sadece Windows'ta")
    pyw = Path(sys.executable).with_name("pythonw.exe")
    target = pyw if pyw.exists() else Path(sys.executable)
    icon = Path(__file__).parent / "static" / "alice.ico"
    ps = (
        "foreach($f in 'Desktop','Programs'){"
        "$d=[Environment]::GetFolderPath($f);if(-not $d){continue};$p=Join-Path $d 'ALICE.lnk';"
        "$s=(New-Object -ComObject WScript.Shell).CreateShortcut($p);"
        f"$s.TargetPath='{target}';$s.Arguments='-m alice app';$s.WorkingDirectory='{Path.home()}';"
        f"$s.IconLocation='{icon}';$s.Description='ALICE';$s.Save();Write-Output $p}}"
    )
    r = subprocess.run(["powershell", "-NoProfile", "-Command", ps], check=True, capture_output=True, text=True)
    # Windows simgeleri önbelleğe alır; yenilemezsek Başlat menüsü eski simgeyi göstermeye devam eder.
    try:
        subprocess.run(["ie4uinit.exe", "-show"], capture_output=True, check=False, timeout=15)
    except (OSError, subprocess.SubprocessError):
        pass
    return [Path(line.strip()) for line in r.stdout.splitlines() if line.strip()]
