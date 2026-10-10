"""Tam çevrimdışı mod: internet yokken (ya da bulut servisleri hiç cevap vermezken) ALICE yerelde çalışır.

- Beyin: Ollama'daki yerel model (varsayılan qwen3:8b, RTX 4060 8 GB'a sığacak bağlamla `alice-local` adıyla).
- Kulak: faster-whisper (uyandırma modeli paylaşılır, ekran kartında ikinci bir model yüklenmez).
- Ses: kuruluysa ALICE'in kendi sesini taklit eden yerel motor (XTTS/Chatterbox), sonra Piper (işlemcide,
  Türkçe, ~0,2 sn), en son Windows'un kendi sesi.

İnternet durumu ucuz bir TCP denemesiyle ölçülür ve önbelleğe alınır: sıcak yolda hiç beklenmez, yenileme
arka planda yapılır. Bulut sağlayıcısı ağ hatası verince hemen yeniden ölçülür. Geçişte yerel model ekran
kartına önceden yüklenir; internet dönünce bellekten atılır (Live ve oyunlar için yer açılsın).

[offline] mode: auto (internet yoksa geç) | on (hep yerel) | off (hiç geçme). Ortam değişkeni ALICE_OFFLINE=1
de zorlar (deneme için).
"""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

PROBE_HOSTS = (("1.1.1.1", 443), ("8.8.8.8", 53), ("generativelanguage.googleapis.com", 443))
LOCAL_HOSTS = ("localhost", "127.0.0.1", "::1", "0.0.0.0")
DEFAULT_MODEL = "ollama:alice-local"
BASE_MODEL = "qwen3:8b"


def probe_internet(timeout: float = 1.2) -> bool:
    """İnternete çıkılabiliyor mu? İlk açılan bağlantı yeter. Ağ kartı kapalıysa hemen, değilse en çok
    birkaç saniyede döner."""
    for host, port in PROBE_HOSTS:
        try:
            with socket.create_connection((host, port), timeout=timeout):
                return True
        except OSError:
            continue
    return False


def is_local_url(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return host in LOCAL_HOSTS or host.endswith(".local")


class NetState:
    """İnternet var mı? online() hiç beklemez (ilk çağrı hariç): önbellekteki sonucu döner, eskiyse arka
    planda yeniler. İnternet yokken daha sık bakılır ki dönünce bulut çabuk geri gelsin."""

    def __init__(self, probe: Callable[[], bool] = probe_internet, ttl_online: float = 30.0,
                 ttl_offline: float = 8.0, mode: Callable[[], str] | None = None,
                 clock: Callable[[], float] = time.monotonic):
        self.probe = probe
        self.ttl_online = ttl_online
        self.ttl_offline = ttl_offline
        self.mode = mode or (lambda: "auto")
        self.clock = clock
        self._value: bool | None = None
        self._checked = 0.0
        self._refreshing = False
        self._lock = threading.Lock()
        self._listeners: list[Callable[[bool], None]] = []
        self.changed_at = 0.0

    def forced(self) -> str:
        if os.environ.get("ALICE_OFFLINE", "").strip().lower() in ("1", "true", "on", "evet"):
            return "on"
        m = str(self.mode() or "auto").lower()
        return m if m in ("on", "off") else "auto"

    def online(self) -> bool:
        f = self.forced()
        if f == "on":
            return False
        if f == "off":
            return True
        if self._value is None:
            self._set(bool(self.probe()))
            return bool(self._value)
        ttl = self.ttl_online if self._value else self.ttl_offline
        if self.clock() - self._checked > ttl:
            self._refresh_async()
        return bool(self._value)

    def offline(self) -> bool:
        return not self.online()

    def report_failure(self) -> None:
        """Bulut ağ hatası verdi: sonucu beklemeden yeniden ölç (bir sonraki istek doğru yola gitsin)."""
        if self.forced() == "auto" and self._value is not False:
            self._refresh_async()

    def refresh(self) -> bool:
        self._set(bool(self.probe()))
        return bool(self._value)

    def _refresh_async(self) -> None:
        with self._lock:
            if self._refreshing:
                return
            self._refreshing = True
            self._checked = self.clock()  # yenileme sürerken art arda yeni ölçüm başlatma

        def run() -> None:
            try:
                self._set(bool(self.probe()))
            except Exception:  # noqa: BLE001 - ölçüm hatası "yok" sayılmaz, eski değer kalır
                pass
            finally:
                self._refreshing = False

        threading.Thread(target=run, daemon=True, name="alice-net-probe").start()

    def _set(self, value: bool) -> None:
        old = self._value
        self._value = value
        self._checked = self.clock()
        if old is not None and old != value:
            self.changed_at = self.clock()
            for fn in list(self._listeners):
                try:
                    fn(value)
                except Exception:  # noqa: BLE001 - bir dinleyici diğerlerini bozmasın
                    pass

    def on_change(self, fn: Callable[[bool], None]) -> None:
        self._listeners.append(fn)


def _config_mode() -> str:
    try:
        from alice.config import load_config

        return str(load_config().data.get("offline", {}).get("mode", "auto"))
    except Exception:  # noqa: BLE001
        return "auto"


_mode_cache: tuple[float, str] = (0.0, "auto")


def _cached_mode() -> str:
    """config.toml'u her istekte okuma: 10 sn'de bir yeter (`alice offline on` çalışan çekirdeğe de yansır)."""
    global _mode_cache
    now = time.monotonic()
    if now - _mode_cache[0] > 10:
        _mode_cache = (now, _config_mode())
    return _mode_cache[1]


_STATE: NetState | None = None


def state() -> NetState:
    global _STATE
    if _STATE is None:
        _STATE = NetState(mode=_cached_mode)
    return _STATE


def reset(probe: Callable[[], bool] | None = None, mode: str | None = None) -> NetState:
    """Durumu baştan kur (testler ve `alice offline` için)."""
    global _STATE, _mode_cache
    _mode_cache = (0.0, "auto")
    _STATE = NetState(probe=probe or probe_internet, mode=(lambda: mode) if mode else _cached_mode)
    return _STATE


def online() -> bool:
    return state().online()


def settings(cfg_data: dict[str, Any]) -> dict[str, Any]:
    o = dict(cfg_data.get("offline", {}) or {})
    return {
        "model": str(o.get("model", DEFAULT_MODEL) or DEFAULT_MODEL),
        "base_model": str(o.get("base_model", BASE_MODEL) or BASE_MODEL),
        "context": int(o.get("context", 12288) or 12288),
        "vision_model": str(o.get("vision_model", "") or ""),
        "piper_voice": str(o.get("piper_voice", "tr_TR-dfki-medium") or ""),
        "sapi": bool(o.get("sapi", True)),
        "stt": str(o.get("stt", "wake") or "wake"),
        "preload": bool(o.get("preload", True)),
    }


def offline_note() -> str:
    """Sesli/yazılı her turda modele: internet yok, buna göre davran."""
    return ("[internet yok: yerel moddasın (yerel model). Web araması, site açma ve bulut gerektiren araçlar "
            "çalışmaz; bunları denemeden kısaca söyle. Dosya, uygulama ve bilgisayar araçları çalışır.]")


# ------------------------------------------------------------------- Ollama
def ollama_root(base_url: str) -> str:
    """http://localhost:11434/v1 -> http://localhost:11434"""
    u = base_url.rstrip("/")
    return u[:-3] if u.endswith("/v1") else u


def ollama_exe() -> str | None:
    exe = shutil.which("ollama")
    if exe:
        return exe
    if sys.platform == "win32":
        p = Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Ollama" / "ollama.exe"
        if p.exists():
            return str(p)
    return None


def ollama_models(base_url: str, timeout: float = 2.0) -> list[str] | None:
    """Ollama'daki modeller; sunucu kapalıysa None."""
    import httpx

    try:
        r = httpx.get(f"{ollama_root(base_url)}/api/tags", timeout=timeout)
        if r.status_code != 200:
            return None
        return [str(m.get("name", "")) for m in r.json().get("models", []) or []]
    except (httpx.HTTPError, ValueError):
        return None


def has_model(names: list[str], model: str) -> bool:
    want = model if ":" in model else f"{model}:latest"
    return want in names or model in names


_started = 0.0


def ensure_ollama(base_url: str, wait: float = 12.0) -> bool:
    """Ollama sunucusu açık mı? Kapalıysa (kuruluysa) arka planda başlat ve hazır olmasını bekle.
    Aynı anda tekrar tekrar başlatmamak için 60 sn'de bir dener."""
    global _started
    if ollama_models(base_url, timeout=1.0) is not None:
        return True
    exe = ollama_exe()
    if exe is None or time.monotonic() - _started < 60:
        return False
    _started = time.monotonic()
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(subprocess, "DETACHED_PROCESS", 0)
    try:
        subprocess.Popen([exe, "serve"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         stdin=subprocess.DEVNULL, creationflags=flags, start_new_session=sys.platform != "win32")
    except OSError:
        return False
    end = time.monotonic() + wait
    while time.monotonic() < end:
        time.sleep(0.5)
        if ollama_models(base_url, timeout=1.0) is not None:
            return True
    return False


def ollama_keep(base_url: str, model: str, keep_alive: str | int) -> bool:
    """Modeli ekran kartına yükle ("30m") ya da bellekten at (0). Boş istek: cevap üretmez."""
    import httpx

    try:
        r = httpx.post(f"{ollama_root(base_url)}/api/generate",
                       json={"model": model, "keep_alive": keep_alive}, timeout=120)
        return r.status_code == 200
    except httpx.HTTPError:
        return False


def modelfile(base_model: str, context: int) -> str:
    return f"FROM {base_model}\nPARAMETER num_ctx {int(context)}\n"


class Preloader:
    """İnternet gidince yerel modeli ekran kartına önceden yükler (ilk cevap 10-20 sn beklemesin);
    internet dönünce bellekten atar. Sadece kendi yüklediğini atar."""

    def __init__(self, cfg_data: dict[str, Any], log: Callable[[str], None] = lambda s: None):
        self.cfg_data = cfg_data
        self.log = log
        self.loaded = False

    def _target(self) -> tuple[str, str] | None:
        ref = settings(self.cfg_data)["model"]
        provider, _, model = ref.partition(":")
        p = (self.cfg_data.get("providers", {}) or {}).get(provider, {})
        if provider != "ollama" or not p.get("base_url"):
            return None
        return p["base_url"], model

    def __call__(self, is_online: bool) -> None:
        t = self._target()
        if t is None or not settings(self.cfg_data)["preload"]:
            return
        base, model = t

        def run() -> None:
            if not is_online:
                if ensure_ollama(base) and ollama_keep(base, model, "30m"):
                    self.loaded = True
                    self.log(f"çevrimdışı: {model} ekran kartına yüklendi")
            elif self.loaded:
                ollama_keep(base, model, 0)
                self.loaded = False
                self.log(f"internet döndü: {model} bellekten atıldı")

        threading.Thread(target=run, daemon=True, name="alice-offline-preload").start()
