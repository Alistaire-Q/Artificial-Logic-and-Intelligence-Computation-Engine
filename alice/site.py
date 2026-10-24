"""Projedeki web sitesi: giriş sayfasını bulma, güvenli sunma ve ekran görüntüsü alma.

Önizleme sekmesi ve kurulun "kullanıcı gözü" incelemesi bunu kullanır. Gizli dosyalar (.env, .git ...) hiçbir
zaman sunulmaz.
"""

from __future__ import annotations

import base64
import mimetypes
import os
import shutil
import subprocess
import tempfile
import threading
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

SKIP_DIRS = {"node_modules", ".git", "__pycache__", ".venv", "venv", ".alice"}
ENTRY_DIRS = ("", "dist", "build", "public", "site", "www", "docs", "src", "web", "frontend")
VIEWPORTS = {"masaüstü": (1366, 860), "telefon": (390, 844)}
# Referans siteleri incelerken: ilk ekran, sayfanın aşağısı (bölüm ritmi) ve telefon.
STUDY_VIEWPORTS = {"masaüstü": (1440, 900), "uzun": (1440, 2600), "telefon": (390, 844)}


def find_entry(root: Path) -> str:
    """Sitenin giriş sayfası (köke göre yol) ya da boş. Önce bilinen klasörler, sonra iki seviye derinlik."""
    root = Path(root)
    for d in ENTRY_DIRS:
        f = root / d / "index.html"
        if f.is_file():
            return f.relative_to(root).as_posix()
    try:
        for sub in sorted(p for p in root.iterdir() if p.is_dir() and p.name not in SKIP_DIRS
                          and not p.name.startswith(".")):
            for f in [sub / "index.html", *sorted(sub.glob("*/index.html"))]:
                if f.is_file() and not any(part in SKIP_DIRS or part.startswith(".") for part in f.relative_to(root).parts):
                    return f.relative_to(root).as_posix()
    except OSError:
        pass
    return ""


def safe_path(root: Path, rel: str) -> Path | None:
    """Köke göre güvenli dosya yolu: dışarı çıkan, gizli (.env, .git) ya da atlanan klasördeki yollar None."""
    root = Path(root).resolve()
    parts = [p for p in rel.replace("\\", "/").split("/") if p not in ("", ".")]
    if any(p == ".." or p.startswith(".") or p in SKIP_DIRS for p in parts):
        return None
    path = (root / Path(*parts)).resolve() if parts else root
    try:
        inside = path.relative_to(root)
    except ValueError:
        return None
    if any(p.startswith(".") or p in SKIP_DIRS for p in inside.parts):  # bağlantı (symlink) gizli dosyaya çıkıyorsa
        return None
    if path.is_dir():
        path = path / "index.html"
    return path if path.is_file() else None


def content_type(path: Path) -> str:
    ctype = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    if path.suffix in (".js", ".mjs"):
        ctype = "text/javascript"
    return ctype + ("; charset=utf-8" if ctype.startswith("text/") or ctype.endswith("javascript") else "")


def last_change(root: Path, limit: int = 3000) -> float:
    """Sitedeki dosyaların en son değişme zamanı (önizleme kendini yenilesin diye)."""
    newest, n = 0.0, 0
    stack = [Path(root)]
    while stack and n < limit:
        d = stack.pop()
        try:
            for p in d.iterdir():
                if p.name.startswith(".") or p.name in SKIP_DIRS:
                    continue
                if p.is_dir():
                    stack.append(p)
                else:
                    n += 1
                    newest = max(newest, p.stat().st_mtime)
        except OSError:
            continue
    return newest


class _Quiet(SimpleHTTPRequestHandler):
    def log_message(self, *args) -> None:  # noqa: D401
        pass

    def guess_type(self, path):  # metin dosyaları utf-8 ilan edilsin (yoksa tarayıcı Türkçe harfleri bozuyor)
        return content_type(Path(str(path)))

    def send_head(self):  # gizli dosyaları sunma (yol çözülmüş haliyle denetlenir: %2eenv gibi kaçışlar da yakalanır)
        from urllib.parse import unquote

        rel = unquote(self.path.split("?", 1)[0].split("#", 1)[0])
        if safe_path(Path(self.directory), rel) is None:
            self.send_error(404)
            return None
        return super().send_head()


def serve(root: Path) -> tuple[ThreadingHTTPServer, str]:
    """Klasörü 127.0.0.1'de geçici olarak sun (ekran görüntüsü için). (sunucu, adres) döner; işin bitince shutdown."""
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), partial(_Quiet, directory=str(root)))
    threading.Thread(target=httpd.serve_forever, daemon=True, name="site-preview").start()
    return httpd, f"http://127.0.0.1:{httpd.server_address[1]}/"


def screenshots(root: Path, entry: str = "", timeout: float = 20.0) -> dict[str, str]:
    """Sitenin masaüstü ve telefon ekran görüntüleri (base64 PNG). Tarayıcı yoksa ya da site açılmazsa boş sözlük:
    inceleme görüntüsüz devam eder."""
    entry = entry or find_entry(root)
    if not entry:
        return {}
    httpd, base = serve(root)
    try:
        return capture(base + entry, timeout)
    finally:
        httpd.shutdown()
        httpd.server_close()


def reachable(url: str, timeout: float = 12.0) -> tuple[bool, str]:
    """Dış adres açılıyor mu? (tarayıcı açılmayan sitenin hata sayfasının da görüntüsünü alıyor; o "referans" sanılmasın)
    Döner: (açılıyor mu, son adres ya da hata)."""
    import httpx

    try:
        r = httpx.get(url, follow_redirects=True, timeout=timeout,
                      headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                                             "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"})
    except httpx.HTTPError as e:
        return False, f"{type(e).__name__}: {e}"[:200]
    if r.status_code >= 400:
        return False, f"HTTP {r.status_code}"
    if "html" not in r.headers.get("content-type", "html"):
        return False, f"web sayfası değil ({r.headers.get('content-type')})"
    return True, str(r.url)


def capture(url: str, timeout: float = 20.0, settle: int = 600,
            viewports: dict[str, tuple[int, int]] | None = None) -> dict[str, str]:
    """Adresin ekran görüntüleri (varsayılan masaüstü + telefon): önce Playwright, yoksa komut satırı tarayıcısı."""
    vp = viewports or VIEWPORTS
    return _pw_capture(url, timeout, settle, vp) or _cli_capture(url, timeout, settle, vp)


def _pw_capture(url: str, timeout: float, settle: int, viewports: dict[str, tuple[int, int]] | None = None
                ) -> dict[str, str]:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return {}
    out: dict[str, str] = {}
    try:
        with sync_playwright() as p:
            browser = _launch(p)
            try:
                for name, (w, h) in (viewports or VIEWPORTS).items():
                    page = browser.new_page(viewport={"width": w, "height": h})
                    page.goto(url, wait_until="load", timeout=timeout * 1000)
                    page.wait_for_timeout(settle)  # giriş animasyonları otursun
                    out[name] = base64.b64encode(page.screenshot(full_page=False)).decode()
                    page.close()
            finally:
                browser.close()
    except Exception:  # noqa: BLE001 - tarayıcı yok/çöktü/site açılmadı: komut satırı tarayıcısı denenir
        return out
    return out


def browser_exe() -> str:
    """Pencere açmadan ekran görüntüsü alabilecek Chromium tabanlı tarayıcı: ALICE_CHROMIUM, sonra Edge (Windows'ta
    her zaman kurulu), Chrome, Chromium. Bulunamazsa boş."""
    exe = os.environ.get("ALICE_CHROMIUM", "")
    if exe and Path(exe).is_file():
        return exe
    cands: list[Path] = []
    for env in ("PROGRAMFILES(X86)", "PROGRAMFILES", "LOCALAPPDATA"):
        base = os.environ.get(env)
        if base:
            cands += [Path(base) / "Microsoft/Edge/Application/msedge.exe",
                      Path(base) / "Google/Chrome/Application/chrome.exe"]
    cands += [Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
              Path("/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge")]
    for c in cands:
        if c.is_file():
            return str(c)
    for name in ("msedge", "chrome", "google-chrome", "google-chrome-stable", "chromium", "chromium-browser",
                 "microsoft-edge"):
        found = shutil.which(name)
        if found:
            return found
    return ""


def cli_screenshots(root: Path, entry: str = "", timeout: float = 20.0) -> dict[str, str]:
    """Playwright olmadan: yerel siteyi sun ve komut satırı tarayıcısıyla görüntüle."""
    entry = entry or find_entry(root)
    if not entry or not browser_exe():
        return {}
    httpd, base = serve(root)
    try:
        return _cli_capture(base + entry, timeout)
    finally:
        httpd.shutdown()
        httpd.server_close()


def _cli_capture(url: str, timeout: float = 20.0, settle: int = 600,
                 viewports: dict[str, tuple[int, int]] | None = None) -> dict[str, str]:
    """Tarayıcıyı komut satırından görünmez (headless) çalıştırıp ekran görüntüsü al; ek kurulum gerekmez."""
    exe = browser_exe()
    if not exe:
        return {}
    out: dict[str, str] = {}
    with tempfile.TemporaryDirectory(prefix="alice-shot-") as tmp:
        for name, (w, h) in (viewports or VIEWPORTS).items():
            png = Path(tmp) / f"{len(out)}.png"
            cmd = [exe, "--headless=new", "--disable-gpu", "--hide-scrollbars", "--no-first-run",
                   "--no-default-browser-check", "--disable-extensions", "--mute-audio",
                   f"--user-data-dir={Path(tmp) / 'profile'}",  # açık tarayıcıya bağlanmasın
                   f"--window-size={w},{h}", f"--virtual-time-budget={1900 + settle}", f"--screenshot={png}", url]
            if os.name != "nt" and hasattr(os, "geteuid") and os.geteuid() == 0:
                cmd.insert(1, "--no-sandbox")
            proxy = os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")
            if proxy and not url.startswith("http://127.0.0.1"):
                cmd.insert(1, f"--proxy-server={proxy}")
            try:
                subprocess.run(cmd, capture_output=True, timeout=timeout, check=False,
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            except (OSError, subprocess.TimeoutExpired):
                break
            if png.is_file() and png.stat().st_size > 1000:
                out[name] = base64.b64encode(png.read_bytes()).decode()
    return out


def _launch(p):
    """Önce ALICE_CHROMIUM ile verilen tarayıcı, sonra Playwright'ın kendi tarayıcısı, sonra sistemdeki Chrome/Edge."""
    exe = os.environ.get("ALICE_CHROMIUM", "")
    if exe:
        return p.chromium.launch(executable_path=exe)
    try:
        return p.chromium.launch()
    except Exception:  # noqa: BLE001 - indirilmiş tarayıcı yoksa sistemdeki Chrome/Edge
        for channel in ("chrome", "msedge"):
            try:
                return p.chromium.launch(channel=channel)
            except Exception:  # noqa: BLE001
                continue
        raise
