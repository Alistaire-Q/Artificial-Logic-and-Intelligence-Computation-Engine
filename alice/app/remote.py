"""Telefon uygulaması (/m): bilgisayarı telefondan yönetmek için durum, canlı ekran ve kontroller.

Telefon köprüsünün (alice phone) aynı sunucusunda, aynı anahtarla çalışır; yalnız yerel ağdan ulaşılır.
  GET  /api/remote/status   -> mod, şu an durumu, sıcaklık/kullanım, öndeki program, son olaylar, önsezi
  GET  /api/remote/screen   -> ekranın küçültülmüş JPEG'i (telefonda canlı görmek için)
  GET  /api/remote/apps     -> Başlat menüsündeki uygulamalar (açmak için)
  GET  /api/remote/windows  -> açık pencereler, öndeki işaretli (öne getirmek için: action "focus")
  POST /api/remote/action   -> {"action": ...}: ses, medya, kilitle, uyku, kapatma zamanlayıcısı, uygulama/site aç,
                               panoya yaz, metin yaz, HUD gizle/göster

Kapatma ve uyku telefondaki onaydan sonra gelir (arayüz sorar); kapatma her zaman 60 sn gecikmeli ve iptal edilebilir.
"""

from __future__ import annotations

import io
import sys
import time
from typing import Any

IS_WIN = sys.platform == "win32"
SCREEN_W = 1280          # telefona giden ekran genişliği
SCREEN_QUALITY = 62
SCREEN_MIN_GAP = 0.35    # sn: ekran en sık bu kadar yakalanır (birkaç telefon aynı anda isterse)
_screen_cache: dict[str, Any] = {"at": 0.0, "jpg": b""}
NOW_CACHE = 30.0         # sn: uçuş geçmişinden hesaplanan "şu an" (~1 sn sürüyor) en çok bu kadar eski
_now_cache: dict[str, Any] = {"at": -1e9, "now": {}}


def _last(store, key: str, max_age: float = 120.0) -> float | None:
    row = store.last(key)
    if row and row[1] is not None and time.time() - row[0] < max_age:
        return round(float(row[1]), 1)
    return None


def status() -> dict[str, Any]:
    from alice.core import service
    from alice.core.store import Store

    st = service.read_state()
    import socket

    out: dict[str, Any] = {"core": bool(st.get("running")), "mode": st.get("mode") or "", "voice": st.get("voice") or "",
                           "forced": st.get("forced") or "", "host": socket.gethostname(),
                           "time": time.time()}
    try:
        with Store() as store:
            for name, key in (("cpu", "pc.cpu.percent"), ("ram", "pc.mem.percent"), ("gpu", "pc.gpu.util"),
                              ("gpu_temp", "pc.gpu.temp"), ("cpu_temp", "pc.cpu.temp"), ("vram", "pc.gpu.mem_percent")):
                out[name] = _last(store, key)
            fg = store.last("pc.foreground")
            if fg and time.time() - fg[0] < 120 and fg[2]:
                from alice.now import APP_NAMES

                out["app"] = APP_NAMES.get(fg[2].lower(), fg[2].rsplit(".", 1)[0])
            out["events"] = [{"ts": e.ts, "kind": e.kind, "title": e.title, "body": (e.body or "")[:240],
                              "severity": e.severity} for e in store.events(limit=40)
                             if e.kind not in ("mode",) and e.delivered][:15]
            out["series"] = series(store)
            midnight = time.mktime(time.localtime()[:3] + (0, 0, 0, 0, 0, -1))
            today = [e for e in store.events(since=midnight, limit=500) if e.kind not in ("mode",)]
            out["today"] = {"events": len(today),
                            "warnings": sum(1 for e in today if e.severity in ("warn", "critical"))}
            if time.monotonic() - _now_cache["at"] > NOW_CACHE:
                from alice.now import snapshot

                snap = snapshot(store, state=st)
                _now_cache.update(at=time.monotonic(), now={
                    "flights_today": snap.flights_today, "avg_min_today": snap.avg_min_today,
                    "next_game": snap.next_game, "advice": snap.advice, "in_game": snap.in_game})
            out["now"] = _now_cache["now"]
    except Exception:  # noqa: BLE001 - veritabanı meşgul: kart yine açılsın
        out.setdefault("events", [])
    try:  # medya kartında ne çaldığı (Spotify, Sonora, tarayıcı...)
        from alice.extras import nowplaying

        n = nowplaying.now()
        if n and (n.get("title") or n.get("artist")):
            out["playing"] = {k: n.get(k, "") for k in ("title", "artist", "app", "status")}
    except Exception:  # noqa: BLE001
        pass
    try:
        from alice.anticipate import ready_line

        out["ready"] = ready_line()
    except Exception:  # noqa: BLE001
        out["ready"] = ""
    return out


SERIES_KEYS = {"cpu": "pc.cpu.percent", "ram": "pc.mem.percent", "gpu": "pc.gpu.util", "gpu_temp": "pc.gpu.temp"}
SERIES_MINUTES = 10
SERIES_POINTS = 20


def series(store, minutes: float = SERIES_MINUTES, points: int = SERIES_POINTS) -> dict[str, list[float]]:
    """Telefondaki küçük grafikler: her ölçümün son birkaç dakikası, eşit aralıklı en fazla `points` değer.
    Aralıkta örnek yoksa bir önceki değer tekrarlanır; hiç örnek yoksa liste boştur."""
    now = time.time()
    since = now - minutes * 60
    step = (now - since) / points
    out: dict[str, list[float]] = {}
    for name, key in SERIES_KEYS.items():
        rows = store.series(key, since=since)
        if not rows:
            out[name] = []
            continue
        vals: list[float] = []
        i, last = 0, rows[0][1]
        for b in range(points):
            end = since + (b + 1) * step
            bucket = []
            while i < len(rows) and rows[i][0] <= end:
                bucket.append(rows[i][1])
                i += 1
            if bucket:
                last = sum(bucket) / len(bucket)
            vals.append(round(last, 1))
        out[name] = vals
    return out


def windows() -> list[dict[str, Any]]:
    """Açık pencereler (başlığı olan, simge durumunda olmayanlar dahil), öndeki işaretli. Telefondan öne getirmek için."""
    rows: list[dict[str, Any]] = []
    if IS_WIN:
        try:
            import pygetwindow as gw
        except ImportError:
            return []
        try:
            active = gw.getActiveWindow()
        except Exception:  # noqa: BLE001
            active = None
        seen = set()
        for w in gw.getAllWindows():
            title = (w.title or "").strip()
            if not title or title in seen or title in ("Program Manager", "Windows Input Experience", "Settings"):
                continue
            try:
                if w.width < 120 or w.height < 80:
                    continue
            except Exception:  # noqa: BLE001
                pass
            seen.add(title)
            app = title.rsplit(" - ", 1)[-1].strip() if " - " in title else title
            rows.append({"title": title, "app": app[:40],
                         "active": bool(active is not None and getattr(active, "_hWnd", None) == getattr(w, "_hWnd", 0)),
                         "minimized": bool(getattr(w, "isMinimized", False))})
    else:
        try:
            from alice.tools.desktop import list_windows

            for title in list_windows():
                app = title.rsplit(" - ", 1)[-1].strip() if " - " in title else title
                rows.append({"title": title, "app": app[:40], "active": False, "minimized": False})
        except Exception:  # noqa: BLE001 - wmctrl yok
            return []
    rows.sort(key=lambda r: (not r["active"], r["minimized"]))
    return rows[:20]


def screen_jpeg(monitor: int = 1) -> bytes:
    """Ekranın küçültülmüş hali. HUD ve ALICE'in kendi pencereleri görüntüye girmez (ekran görüntüsünden hariç)."""
    now = time.monotonic()
    if now - _screen_cache["at"] < SCREEN_MIN_GAP and _screen_cache["jpg"]:
        return _screen_cache["jpg"]
    import mss
    from PIL import Image

    with mss.mss() as sct:
        mons = sct.monitors
        shot = sct.grab(mons[monitor] if 0 < monitor < len(mons) else mons[1])
        img = Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")
    if img.width > SCREEN_W:
        img = img.resize((SCREEN_W, round(img.height * SCREEN_W / img.width)), Image.BILINEAR)
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=SCREEN_QUALITY, optimize=False)
    _screen_cache.update(at=now, jpg=buf.getvalue())
    return _screen_cache["jpg"]


def apps(limit: int = 400) -> list[str]:
    from alice.tools.desktop import start_apps

    seen, out = set(), []
    for name, _id in start_apps():
        low = name.lower()
        if low in seen or any(w in low for w in ("uninstall", "kaldır", "readme", "help", "yardım")):
            continue
        seen.add(low)
        out.append(name)
    return sorted(out, key=str.lower)[:limit]


def _lock() -> str:
    if not IS_WIN:
        raise ValueError("sadece Windows")
    import ctypes

    ctypes.windll.user32.LockWorkStation()
    return "bilgisayar kilitlendi"


def _sleep() -> str:
    if not IS_WIN:
        raise ValueError("sadece Windows")
    import ctypes

    ctypes.windll.powrprof.SetSuspendState(0, 0, 0)
    return "bilgisayar uyku moduna geçiyor"


def _type(text: str) -> str:
    from alice.tools.computer import type_text

    type_text(text)
    return "yazıldı"


def _clipboard(text: str) -> str:
    import pyperclip

    pyperclip.copy(text)
    return "bilgisayarın panosuna kopyalandı"


def _hud_toggle() -> str:
    from alice.config import load_config
    from alice.hud import PORT, client

    hud = load_config().data.get("hud", {}) or {}
    client.configure(int(hud.get("port") or PORT))
    client.send({"kind": "toggle"})
    return "HUD gizlendi/gösterildi"


def action(d: dict[str, Any]) -> str:
    """Telefondan gelen bir kontrol. Başarısızsa ValueError (mesajı telefona gider)."""
    from alice.extras import media

    a = str(d.get("action") or "")
    if a in ("play_pause", "next", "previous"):
        from alice.extras import nowplaying

        if nowplaying.control(a):  # tam çalan uygulamaya (medya tuşu son odaklanana gidiyordu)
            return "tamam"
    if a in ("play_pause", "next", "previous", "mute", "volume_up", "volume_down"):
        return media.key(a, int(d.get("times") or (3 if a.startswith("volume") else 1)))
    if a == "set_volume":
        return media.set_volume(int(d.get("percent") or 0))
    if a == "lock":
        return _lock()
    if a == "sleep":
        return _sleep()
    if a == "shutdown":
        return media.shutdown_in(1)  # 60 sn: telefondan "İptal" ile geri alınır
    if a == "cancel_shutdown":
        return media.cancel_shutdown()
    if a == "open":
        from alice.tools.desktop import open_app

        target = str(d.get("target") or "").strip()
        if not target:
            raise ValueError("açılacak bir şey yazılmadı")
        return open_app(target)
    if a == "type":
        text = str(d.get("text") or "")
        if not text:
            raise ValueError("yazılacak metin boş")
        return _type(text[:2000])
    if a == "clipboard":
        return _clipboard(str(d.get("text") or "")[:20000])
    if a == "hud":
        return _hud_toggle()
    if a == "focus":
        from alice.tools.desktop import focus_window

        title = str(d.get("title") or "").strip()
        if not title:
            raise ValueError("pencere seçilmedi")
        return focus_window(title) + " öne getirildi"
    raise ValueError(f"bilinmeyen işlem: {a}")
