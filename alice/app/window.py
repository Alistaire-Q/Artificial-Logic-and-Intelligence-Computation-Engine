"""Uygulama penceresini aç.

Sıra: pywebview (Windows'ta Edge WebView2 ile gerçek uygulama penceresi) ->
Edge/Chrome --app modu (adres çubuğu olmayan ayrı pencere) -> varsayılan tarayıcı.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
import webbrowser
from pathlib import Path

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


def open_window(url: str, title: str = "ALICE", prefer: str = "auto") -> str:
    """Pencereyi açar ve kapanana kadar bekler. Hangi yöntemin kullanıldığını döndürür."""
    if prefer in ("auto", "webview"):
        try:
            import webview  # pywebview

            icon = Path(__file__).parent / "static" / "icon.png"
            webview.create_window(title, url, width=1320, height=880, min_size=(820, 560), background_color="#16161a")
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
    icon = Path(__file__).parent / "static" / "icon.ico"
    ps = (
        "foreach($f in 'Desktop','Programs'){"
        "$d=[Environment]::GetFolderPath($f);if(-not $d){continue};$p=Join-Path $d 'ALICE.lnk';"
        "$s=(New-Object -ComObject WScript.Shell).CreateShortcut($p);"
        f"$s.TargetPath='{target}';$s.Arguments='-m alice app';$s.WorkingDirectory='{Path.home()}';"
        f"$s.IconLocation='{icon}';$s.Description='ALICE';$s.Save();Write-Output $p}}"
    )
    r = subprocess.run(["powershell", "-NoProfile", "-Command", ps], check=True, capture_output=True, text=True)
    return [Path(line.strip()) for line in r.stdout.splitlines() if line.strip()]
