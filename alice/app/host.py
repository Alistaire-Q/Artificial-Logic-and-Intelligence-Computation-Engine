"""Uygulama çekirdeğin içinde yaşar.

Eskiden `alice app` her açılışta kendi sürecinde her şeyi sıfırdan kuruyordu (router, araçlar, MCP sunucuları,
sohbet): uygulama her seferinde baştan yükleniyor gibiydi, arka plandaki çekirdekten de habersizdi (çekirdeği
kapatınca pencere açık kalıyordu). Şimdi:

* Çekirdek (`alice core run`) uygulamanın sunucusunu (AppState + AppServer) açılışta kurar ve açık tutar; adresini
  ~/.alice/app.json'a yazar.
* `alice app` çekirdek çalışıyorsa sadece pencereyi o adrese açar: anında açılır, sohbet kaldığı yerde durur.
  Çekirdek kapalıysa önce onu başlatır. Pencereyi kapatmak çekirdeği kapatmaz (arka planda çalışmaya devam eder).
* Çekirdek kapanınca pencere de kapanır. Çekirdek güncelleme için yeniden başlıyorsa pencere kapanmaz,
  yeni çekirdeğin adresine geçer.

`alice app --standalone` (ya da config [app] in_core = false) eski davranış: uygulama kendi sürecinde.
"""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Any, Callable

from alice.config import alice_home

RESTART_GRACE = 90.0  # sn: çekirdek yeniden başlarken pencere yeni adresi bu kadar bekler
ATTACH_WAIT = 60.0    # sn: kapalı çekirdek başlatılınca uygulama sunucusunun hazır olmasını bekleme sınırı


def info_path() -> Path:
    return alice_home() / "app.json"


def restarting_path() -> Path:
    return alice_home() / "core-restarting"


def _alive(pid: Any) -> bool:
    try:
        import psutil

        return bool(pid) and psutil.pid_exists(int(pid))
    except Exception:  # noqa: BLE001 - emin değilsek yaşıyor say: pencere boş yere kapanmasın
        return True


def read_url() -> str:
    """Çekirdekteki uygulama sunucusunun adresi; çekirdek yoksa ya da sunucu henüz hazır değilse ""."""
    try:
        data = json.loads(info_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ""
    if not isinstance(data, dict) or not _alive(data.get("pid")):
        return ""
    return str(data.get("url") or "")


def mark_restarting() -> None:
    """Çekirdek kendini yeniden başlatıyor: açık pencere kapanmasın, yeni adresi beklesin."""
    try:
        restarting_path().write_text(str(time.time()), encoding="utf-8")
    except OSError:
        pass


def restarting() -> bool:
    try:
        return time.time() - float(restarting_path().read_text(encoding="utf-8").strip()) < RESTART_GRACE
    except (OSError, ValueError):
        return False


def follow(url: str) -> Callable[[], str | None]:
    """Pencere için izleyici: "" = aynen devam, yeni adres = ona geç, None = çekirdek kapandı, pencereyi kapat."""
    cur = [url]

    def check() -> str | None:
        u = read_url()
        if u == cur[0]:
            return ""
        if u:
            cur[0] = u
            return u
        return "" if restarting() else None

    return check


def attach(start: Callable[[], bool] | None = None, wait: float = ATTACH_WAIT) -> str:
    """Çekirdekteki uygulamanın adresi. Çekirdek kapalıysa başlatır ve hazır olmasını bekler. Olmazsa ""."""
    url = read_url()
    if url:
        return url
    from alice.core import service

    if service.read_state().get("running") and _alive(service.read_state().get("pid")):
        # Çekirdek çalışıyor ama adres yok: ya sunucuyu hâlâ kuruyor ya da uygulamayı barındırmayan eski bir sürüm.
        wait = min(wait, 20.0)
    elif not (start or service.start_detached)():
        return ""
    end = time.monotonic() + wait
    while time.monotonic() < end:
        time.sleep(0.3)
        url = read_url()
        if url:
            return url
    return ""


class AppHost:
    """Çekirdeğin içindeki uygulama sunucusu. Kurulum arka planda: çekirdeğin açılışını bekletmez."""

    def __init__(self, cfg: Any, cwd: Path | None = None, restart: Callable[[], None] | None = None):
        self.cfg = cfg
        self.cwd = cwd
        self.restart = restart
        self.state: Any = None
        self.server: Any = None
        self.error = ""
        self._stopped = False
        self._lock = threading.Lock()

    def start(self, background: bool = True) -> "AppHost":
        if background:
            threading.Thread(target=self._build, daemon=True, name="alice-app-host").start()
        else:
            self._build()
        return self

    def _build(self) -> None:
        from alice.app.server import AppServer, AppState

        try:
            if self.cwd is None:
                from alice.cli import default_workspace

                self.cwd = default_workspace()
            state = AppState(self.cfg, Path(self.cwd).resolve(), self.cfg.agent.get("permission_mode", "auto"))
            state.restart_hook = self.restart  # arayüzdeki "Güncelle": uygulama değil çekirdek yeniden başlasın
            server = AppServer(state).start()
        except Exception as e:  # noqa: BLE001 - uygulama kurulamazsa çekirdek yine çalışsın
            self.error = repr(e)[:300]
            return
        with self._lock:
            if self._stopped:
                server.stop()
                return
            self.state, self.server = state, server
        state.watch_updates(None)
        self.publish()

    def publish(self) -> None:
        """Adresi app.json'a yaz. Telefon köprüsü ortak durumun anahtarını kendi kalıcı anahtarıyla değiştirince
        yeniden çağrılır; açık pencere yeni adrese geçer."""
        if self.server is None:
            return
        try:
            info_path().write_text(json.dumps({"url": self.server.url, "pid": os.getpid()}), encoding="utf-8")
            restarting_path().unlink(missing_ok=True)
        except OSError:
            pass

    @property
    def busy(self) -> bool:
        """Uygulamada çalışan bir sohbet var mı (çekirdek bu sırada güncelleme için yeniden başlamasın)."""
        st = self.state
        return bool(st is not None and (st.agent.busy or st._queued or st.running()))

    def stop(self) -> None:
        with self._lock:
            self._stopped = True
            server = self.server
        try:
            data = json.loads(info_path().read_text(encoding="utf-8"))
            if data.get("pid") == os.getpid():
                info_path().unlink()
        except (OSError, ValueError, AttributeError):
            pass
        if server is not None:
            try:
                server.stop()
            except Exception:  # noqa: BLE001 - kapanışta hata çekirdeğin kapanmasını engellemesin
                pass
