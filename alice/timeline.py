"""Zaman makinesi hafıza: ekranda gördüğün yazılar yerelde, şifreli ve aranabilir bir zaman çizgisine yazılır.

"Geçen hafta Discord'da paylaşılan o linki bul." Sadece bu bilgisayarda kalır; istediğin an silinir. Varsayılan
olarak KAPALI ([timeline] enabled = true ile açılır).

Nasıl:
  * Her 'every' saniyede öndeki pencere yakalanır; bir öncekinden belirgin farklıysa Windows'un yerleşik OCR'ı ile
    yazıya dökülür. Görüntü saklanmaz, sadece yazı, program adı ve pencere başlığı.
  * Yazı ve başlık Windows DPAPI ile şifrelenir (yalnız senin Windows hesabın çözebilir), ~/.alice/timeline.db.
  * Oyunda (uçuş modu, War Thunder/DCS önde), şifre yöneticilerinde ve gizli/özel tarayıcı pencerelerinde, başlığında
    "şifre/password/banka" geçen pencerelerde kayıt yapılmaz. Liste [timeline] exclude ile genişler.
  * keep_days günden eski kayıtlar kendiliğinden silinir.

  alice timeline search "aranan"   ara (son 30 gün)
  alice timeline status            kaç kayıt, ne kadar yer
  alice timeline wipe              hepsini sil
"""

from __future__ import annotations

import sqlite3
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from alice.config import alice_home

DEFAULTS: dict[str, Any] = {"enabled": False, "every": 30, "keep_days": 30, "exclude": []}
EXCLUDE_APPS = {"keepass.exe", "keepassxc.exe", "1password.exe", "bitwarden.exe", "lastpass.exe", "dashlane.exe",
                "aces.exe", "dcs.exe", "credentialuibroker.exe", "consent.exe", "lockapp.exe"}
EXCLUDE_TITLES = ("inprivate", "incognito", "gizli pencere", "özel pencere", "private browsing", "şifre", "parola",
                  "password", "banka", "bank", "kredi kartı", "1password", "bitwarden")
SAME = 0.03          # küçük görüntüde piksellerin bu orandan azı değiştiyse aynı ekran: yeniden okunmaz
MAX_TEXT = 20_000


# ----------------------------------------------------------------------- DPAPI
def _blob_cls():
    import ctypes
    from ctypes import wintypes

    class BLOB(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    return BLOB


def protect(data: bytes) -> bytes:
    """Windows DPAPI (CryptProtectData): yalnız bu Windows hesabı çözebilir. Windows dışında kayıt yapılmaz."""
    if sys.platform != "win32":
        raise RuntimeError("zaman çizgisi şimdilik sadece Windows'ta (DPAPI şifrelemesi)")
    import ctypes

    blob = _blob_cls()
    buf = ctypes.create_string_buffer(data, len(data))
    src, dst = blob(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char))), blob()
    if not ctypes.windll.crypt32.CryptProtectData(ctypes.byref(src), None, None, None, None, 0x1, ctypes.byref(dst)):
        raise OSError("CryptProtectData başarısız")
    try:
        return ctypes.string_at(dst.pbData, dst.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(dst.pbData)


def unprotect(data: bytes) -> bytes:
    if sys.platform != "win32":
        raise RuntimeError("zaman çizgisi şimdilik sadece Windows'ta (DPAPI şifrelemesi)")
    import ctypes

    blob = _blob_cls()
    buf = ctypes.create_string_buffer(data, len(data))
    src, dst = blob(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char))), blob()
    if not ctypes.windll.crypt32.CryptUnprotectData(ctypes.byref(src), None, None, None, None, 0x1,
                                                    ctypes.byref(dst)):
        raise OSError("CryptUnprotectData başarısız")
    try:
        return ctypes.string_at(dst.pbData, dst.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(dst.pbData)


# ---------------------------------------------------------------------- kayıt
def db_path() -> Path:
    return alice_home() / "timeline.db"


class Timeline:
    def __init__(self, path: Path | None = None, enc: Callable[[bytes], bytes] = protect,
                 dec: Callable[[bytes], bytes] = unprotect):
        self.path = path or db_path()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.enc, self.dec = enc, dec
        self._lock = threading.Lock()
        self._closed = False
        self._db = sqlite3.connect(str(self.path), check_same_thread=False)
        self._db.execute("CREATE TABLE IF NOT EXISTS shots (ts REAL, app TEXT, title BLOB, text BLOB)")
        self._db.execute("CREATE INDEX IF NOT EXISTS shots_ts ON shots(ts)")
        self._db.commit()

    def add(self, app: str, title: str, text: str, ts: float | None = None) -> None:
        with self._lock:
            if self._closed:  # kapandıktan sonra gelen son kare (kaydedici iş parçacığı) sessizce düşer
                return
            self._db.execute("INSERT INTO shots VALUES (?, ?, ?, ?)",
                             (time.time() if ts is None else ts, app, self.enc(title.encode("utf-8")),
                              self.enc(text[:MAX_TEXT].encode("utf-8"))))
            self._db.commit()

    def search(self, query: str, days: float = 30, limit: int = 10, now: float | None = None) -> list[dict[str, Any]]:
        """Büyük/küçük harf duyarsız; bütün kelimeler geçmeli. Yeniden eskiye."""
        now = time.time() if now is None else now
        words = [w for w in query.casefold().split() if w]
        with self._lock:
            rows = self._db.execute("SELECT ts, app, title, text FROM shots WHERE ts >= ? ORDER BY ts DESC",
                                    (now - days * 86400,)).fetchall()
        out = []
        for ts, app, title_b, text_b in rows:
            try:
                title, text = self.dec(title_b).decode("utf-8"), self.dec(text_b).decode("utf-8")
            except Exception:  # noqa: BLE001 - başka hesapta yazılmış/bozuk satır
                continue
            hay = f"{title}\n{text}".casefold()
            if all(w in hay for w in words):
                out.append({"ts": ts, "app": app, "title": title, "snippet": snippet(text, words)})
                if len(out) >= limit:
                    break
        return out

    def prune(self, keep_days: float, now: float | None = None) -> int:
        now = time.time() if now is None else now
        with self._lock:
            if self._closed:
                return 0
            n = self._db.execute("DELETE FROM shots WHERE ts < ?", (now - keep_days * 86400,)).rowcount
            self._db.commit()
        return n

    def wipe(self) -> int:
        with self._lock:
            n = self._db.execute("DELETE FROM shots").rowcount
            self._db.commit()
            self._db.execute("VACUUM")
        return n

    def count(self) -> int:
        with self._lock:
            return int(self._db.execute("SELECT COUNT(*) FROM shots").fetchone()[0])

    def close(self) -> None:
        """Kilit altında: kaydedici iş parçacığı o an yazıyorsa yazımın bitmesi beklenir (kilitsiz kapatma yarım
        işlemi/ProgrammingError'ı kaydediciye bırakıyordu); sonra gelen yazımlar sessizce düşer."""
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._db.close()


def snippet(text: str, words: list[str], width: int = 160) -> str:
    flat = " ".join(text.split())
    i = flat.casefold().find(words[0]) if words else 0
    i = max(0, i)
    start = max(0, i - width // 3)
    out = flat[start:start + width]
    return ("…" if start else "") + out + ("…" if start + width < len(flat) else "")


# ------------------------------------------------------------------- yakalama
def excluded(app: str, title: str, extra: list[str] | None = None) -> bool:
    a, t = (app or "").lower(), (title or "").casefold()
    extra_l = [x.casefold() for x in (extra or [])]
    return a in EXCLUDE_APPS or any(x in t for x in EXCLUDE_TITLES) or any(x in a or x in t for x in extra_l)


def different(prev: Any, cur: Any) -> bool:
    """İki küçük gri görüntü belirgin farklı mı (ekran değişmediyse OCR boşuna çalışmasın)."""
    if prev is None or cur is None or prev.size != cur.size:
        return True
    from PIL import ImageChops

    hist = ImageChops.difference(prev, cur).histogram()  # gri: 256 kutu, numpy gerekmez
    return sum(hist[25:]) / max(1, sum(hist)) >= SAME


def _foreground() -> tuple[str, str, tuple[int, int, int, int] | None]:
    """(program, başlık, pencere kutusu)."""
    import ctypes
    from ctypes import wintypes

    from alice.core.monitors import foreground_process

    user32 = ctypes.windll.user32
    hwnd = user32.GetForegroundWindow()
    buf = ctypes.create_unicode_buffer(512)
    user32.GetWindowTextW(hwnd, buf, 512)
    r = wintypes.RECT()
    box = None
    if user32.GetWindowRect(hwnd, ctypes.byref(r)) and r.right - r.left > 50 and r.bottom - r.top > 50:
        box = (r.left, r.top, r.right - r.left, r.bottom - r.top)
    return foreground_process() or "", buf.value, box


def _grab(box: tuple[int, int, int, int]):
    import mss
    from PIL import Image

    from alice.tools.computer import _dpi_aware

    _dpi_aware()
    x, y, w, h = box
    with mss.MSS() if hasattr(mss, "MSS") else mss.mss() as sct:
        shot = sct.grab({"left": x, "top": y, "width": w, "height": h})
    return Image.frombytes("RGB", shot.size, shot.rgb)


@dataclass
class Recorder:
    timeline: Timeline
    cfg: dict[str, Any]
    mode: Callable[[], str] = lambda: ""
    foreground: Callable[[], tuple[str, str, Any]] = _foreground
    grab: Callable[[Any], Any] = _grab
    ocr: Callable[[Any], list[tuple[str, Any]]] | None = None

    def __post_init__(self) -> None:
        self._stop = threading.Event()
        self._last_small = None
        self._last_text = ""
        self._pruned = 0.0
        self.saved = 0

    def step(self, now: float | None = None) -> bool:
        """Bir kez bak; kaydettiyse True."""
        now = time.time() if now is None else now
        if now - self._pruned > 3600:
            self._pruned = now
            self.timeline.prune(float(self.cfg.get("keep_days", DEFAULTS["keep_days"])), now)
        if self.mode() in ("flight", "quiet"):  # oyunda ekran okunmaz (performans ve odak)
            return False
        app, title, box = self.foreground()
        if box is None or excluded(app, title, self.cfg.get("exclude")):
            return False
        img = self.grab(box)
        small = img.convert("L").resize((64, 36))
        if not different(self._last_small, small):
            return False
        self._last_small = small
        if self.ocr is not None:
            lines = self.ocr(img)
        else:
            from alice.reflex.perceive import ocr_image

            lines = ocr_image(img)
        text = "\n".join(t for t, _ in lines).strip()
        if not text or text == self._last_text:
            return False
        self._last_text = text
        self.timeline.add(app, title, text, now)
        self.saved += 1
        return True

    def run(self) -> None:
        every = max(5.0, float(self.cfg.get("every", DEFAULTS["every"])))
        while not self._stop.wait(every):
            try:
                self.step()
            except Exception:  # noqa: BLE001 - bir kare okunamasa da kayıt sürer
                pass

    def start(self) -> "Recorder":
        threading.Thread(target=self.run, daemon=True, name="alice-timeline").start()
        return self

    def stop(self) -> None:
        self._stop.set()


def fmt(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return "Zaman çizgisinde bulunamadı."
    return "\n".join(f"- {datetime.fromtimestamp(r['ts']):%d.%m %H:%M} · {r['app']} · {r['title'][:60]}\n  {r['snippet']}"
                     for r in rows)


def timeline_cmd(argv: list[str]) -> int:
    sub = argv[0] if argv else "status"
    tl = Timeline()
    try:
        if sub == "search" and len(argv) > 1:
            print(fmt(tl.search(" ".join(argv[1:]))))
            return 0
        if sub == "wipe":
            print(f"{tl.wipe()} kayıt silindi.")
            return 0
        if sub == "status":
            from alice.config import load_config

            on = bool((load_config().data.get("timeline") or {}).get("enabled"))
            size = db_path().stat().st_size / 1e6 if db_path().exists() else 0
            print(f"zaman çizgisi: {'açık' if on else 'kapalı'}, {tl.count()} kayıt, {size:.1f} MB ({db_path()})")
            return 0
    finally:
        tl.close()
    print(__doc__)
    return 2
