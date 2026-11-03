"""Ne çalıyor ve onu kontrol et: Windows'un medya oturumları (ses simgesinin yanındaki oynatıcı kutusu).

Spotify, Sonora, YouTube Music, tarayıcıdaki video... medya kontrolüne bağlanan her uygulama burada görünür; ALICE
çalanı okur ve tam o uygulamayı durdurur/geçer. Medya tuşları (extras.media) son odaklanan uygulamaya gidiyordu ve ne
çaldığını söyleyemiyordu. Windows dışında ya da paket yoksa None/False: çağıran medya tuşlarına düşer.
"""

from __future__ import annotations

import asyncio
import re
import sys
import threading
import time
from typing import Any, Callable

STATUS = {4: "playing", 5: "paused", 3: "stopped", 2: "changing", 1: "opened", 0: "closed"}
CONTROL = {"play_pause": "try_toggle_play_pause_async", "next": "try_skip_next_async",
           "previous": "try_skip_previous_async", "stop": "try_stop_async", "play": "try_play_async",
           "pause": "try_pause_async"}
APPS = {"spotify": "Spotify", "sonora": "Sonora", "chrome": "Chrome", "opera": "Opera", "msedge": "Edge",
        "firefox": "Firefox", "zunemusic": "Medya Oynatıcı", "vlc": "VLC", "applemusic": "Apple Music",
        "youtube": "YouTube", "discord": "Discord"}


def app_name(aumid: str) -> str:
    """'SpotifyAB.SpotifyMusic_zpdnek!Spotify' / 'sonora.exe' -> okunur ad."""
    low = (aumid or "").lower()
    for key, name in APPS.items():
        if key in low:
            return name
    base = re.split(r"[!\\/]", aumid or "")[-1]
    return re.sub(r"\.exe$", "", base, flags=re.I) or "bilinmeyen uygulama"


def _run(coro_fn: Callable[[], Any]) -> Any:
    if sys.platform != "win32":
        return None
    try:
        return asyncio.run(coro_fn())
    except Exception:  # noqa: BLE001 - paket yok, oturum kapandı, COM hatası: çağıran yedeğe düşer
        return None


async def _session():
    from winrt.windows.media.control import GlobalSystemMediaTransportControlsSessionManager as Manager

    mgr = await Manager.request_async()
    return mgr.get_current_session()


def now() -> dict[str, Any] | None:
    """Şu anki medya: {app, title, artist, album, status} ya da None (çalan bir şey yok)."""

    async def go():
        s = await _session()
        if s is None:
            return None
        p = await s.try_get_media_properties_async()
        info = s.get_playback_info()
        return {"app": app_name(s.source_app_user_model_id), "title": p.title or "", "artist": p.artist or "",
                "album": p.album_title or "", "status": STATUS.get(int(info.playback_status), "unknown")}

    return _run(go)


def control(action: str) -> bool:
    """Şu anki oturumda play_pause/next/previous/stop/play/pause. Olmadıysa False."""
    method = CONTROL.get(action)
    if method is None:
        return False

    async def go():
        s = await _session()
        if s is None:
            return False
        return bool(await getattr(s, method)())

    return bool(_run(go))


def describe(n: dict[str, Any] | None) -> str:
    if not n or not (n.get("title") or n.get("artist")):
        return "şu an medya kontrolüne bağlı çalan bir şey yok"
    who = f"{n['artist']} – {n['title']}" if n.get("artist") else n["title"]
    state = {"playing": "çalıyor", "paused": "duraklatıldı", "stopped": "durdu"}.get(n.get("status", ""), n.get("status"))
    return f"{who} ({n['app']}, {state})"


class Watcher:
    """Parça değişince HUD'da kısa süre "♪ şarkı · sanatçı" göster. Ayrı iş parçacığı, birkaç saniyede bir bakar."""

    def __init__(self, show: Callable[[dict[str, Any]], Any], every: float = 2.5,
                 read: Callable[[], dict[str, Any] | None] = now):
        self.show, self.every, self.read = show, every, read
        self.last: tuple[str, str] | None = None
        self._stop = threading.Event()

    def tick(self) -> bool:
        n = self.read()
        if not n or n.get("status") != "playing" or not n.get("title"):
            return False
        key = (n.get("title", ""), n.get("artist", ""))
        if key == self.last:
            return False
        first = self.last is None
        self.last = key
        if first:
            return False  # ALICE açılırken zaten çalan şarkıyı duyurma
        self.show(n)
        return True

    def start(self) -> "Watcher":
        def loop():
            while not self._stop.wait(self.every):
                try:
                    self.tick()
                except Exception:  # noqa: BLE001
                    time.sleep(self.every)

        threading.Thread(target=loop, daemon=True, name="alice-nowplaying").start()
        return self

    def stop(self) -> None:
        self._stop.set()
