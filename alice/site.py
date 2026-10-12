"""Projedeki web sitesi: giriş sayfasını bulma, güvenli sunma ve ekran görüntüsü alma.

Önizleme sekmesi ve kurulun "kullanıcı gözü" incelemesi bunu kullanır. Gizli dosyalar (.env, .git ...) hiçbir
zaman sunulmaz.
"""

from __future__ import annotations

import base64
import mimetypes
import os
import threading
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

SKIP_DIRS = {"node_modules", ".git", "__pycache__", ".venv", "venv", ".alice"}
ENTRY_DIRS = ("", "dist", "build", "public", "site", "www", "docs", "src", "web", "frontend")
VIEWPORTS = {"masaüstü": (1366, 860), "telefon": (390, 844)}


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
        path.relative_to(root)
    except ValueError:
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

    def send_head(self):  # gizli dosyaları sunma
        rel = self.path.split("?", 1)[0].split("#", 1)[0]
        if any(p.startswith(".") or p in SKIP_DIRS for p in rel.split("/") if p):
            self.send_error(404)
            return None
        return super().send_head()


def serve(root: Path) -> tuple[ThreadingHTTPServer, str]:
    """Klasörü 127.0.0.1'de geçici olarak sun (ekran görüntüsü için). (sunucu, adres) döner; işin bitince shutdown."""
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), partial(_Quiet, directory=str(root)))
    threading.Thread(target=httpd.serve_forever, daemon=True, name="site-preview").start()
    return httpd, f"http://127.0.0.1:{httpd.server_address[1]}/"


def screenshots(root: Path, entry: str = "", timeout: float = 20.0) -> dict[str, str]:
    """Sitenin masaüstü ve telefon ekran görüntüleri (base64 PNG). Playwright/Chromium yoksa ya da site açılmazsa
    boş sözlük: inceleme görüntüsüz devam eder."""
    entry = entry or find_entry(root)
    if not entry:
        return {}
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return {}
    httpd, base = serve(root)
    out: dict[str, str] = {}
    try:
        with sync_playwright() as p:
            browser = _launch(p)
            try:
                for name, (w, h) in VIEWPORTS.items():
                    page = browser.new_page(viewport={"width": w, "height": h})
                    page.goto(base + entry, wait_until="load", timeout=timeout * 1000)
                    page.wait_for_timeout(600)  # giriş animasyonları otursun
                    out[name] = base64.b64encode(page.screenshot(full_page=False)).decode()
                    page.close()
            finally:
                browser.close()
    except Exception:  # noqa: BLE001 - tarayıcı yok/çöktü: görüntüsüz devam
        return out
    finally:
        httpd.shutdown()
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
