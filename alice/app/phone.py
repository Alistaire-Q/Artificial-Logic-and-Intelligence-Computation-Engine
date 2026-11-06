"""Telefon köprüsü: ALICE'e telefonun tarayıcısından ulaşma ve canlı saat verisi.

Yerel ağa açılan ayrı bir sunucu (varsayılan port 8765). Telefonda adres:
  http://<bilgisayarın-IP'si>:8765/m?t=<anahtar>         -> telefon uygulaması (sohbet, bilgisayar, ekran)
  POST http://<IP>:8765/api/watch  (X-Alice-Token: <anahtar>, JSON {"hr": 88})  -> canlı nabız
Anahtar ~/.alice/phone-token dosyasında kalıcıdır; böylece telefondaki yer imi ve otomasyon
her açılışta çalışır. `alice phone --new-token` ile değiştirilir.

Android uygulaması (android/) aynı sayfayı açar; konuşma tanıma, bildirim, arama ve QR ile bağlanma ekler. APK'yı
bilgisayar verir (/api/phone/app.apk, bkz. phone_apk.py); uygulama yeni sürüm çıkınca kendini oradan günceller. Tarayıcıda düz HTTP'de mikrofon açılmaz; orada sesli yazma telefonun ses kaydedicisiyle yapılır.
"""

from __future__ import annotations

import secrets
import socket
import time
from pathlib import Path

from alice.config import Config, alice_home


def lan_ip() -> str:
    """Bilgisayarın yerel ağ adresi (paket göndermeden, yönlendirme tablosundan)."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("10.255.255.255", 1))
            return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"


def token(new: bool = False) -> str:
    p = alice_home() / "phone-token"
    if not new and p.exists():
        t = p.read_text(encoding="utf-8").strip()
        if len(t) >= 24:
            return t
    t = secrets.token_urlsafe(24)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(t, encoding="utf-8")
    try:
        p.chmod(0o600)
    except OSError:
        pass
    return t


class PhoneServer:
    def __init__(self, cfg: Config, store=None, port: int | None = None, new_token: bool = False, state=None):
        from alice.app.server import AppServer, AppState
        from alice.core.store import Store

        self.ip = lan_ip()
        self.state = state or AppState(cfg, Path.home(), cfg.agent.get("permission_mode", "auto"))
        self.state.token = token(new_token)
        self.state.allowed_hosts = {"127.0.0.1", "localhost", self.ip}
        self._own_store = store is None  # kendi açtığımız depoyu stop() kapatır
        self.state.watch_store = store or Store()
        self.port = int(port if port is not None else cfg.data.get("phone", {}).get("port", 8765))
        self.server = AppServer(self.state, self.port, host="0.0.0.0", owns_state=state is None).start()
        from alice.app import phone_apk

        phone_apk.refresh_async()  # telefon uygulamasının son sürümü bilgisayarda hazır olsun

    @property
    def url(self) -> str:
        return f"http://{self.ip}:{self.server.httpd.server_address[1]}/m?t={self.state.token}"

    @property
    def app_url(self) -> str:
        return f"http://{self.ip}:{self.server.httpd.server_address[1]}/api/phone/app.apk?t={self.state.token}"

    @property
    def projects_url(self) -> str:
        return f"http://{self.ip}:{self.server.httpd.server_address[1]}/?t={self.state.token}#projects"

    @property
    def watch_url(self) -> str:
        return f"http://{self.ip}:{self.server.httpd.server_address[1]}/api/watch"

    def stop(self) -> None:
        self.server.stop()
        if self._own_store and self.state.watch_store is not None:
            self.state.watch_store.close()
            self.state.watch_store = None


def print_qr(console, text: str) -> None:
    try:
        import qrcode

        qr = qrcode.QRCode(border=1)
        qr.add_data(text)
        qr.make()
        for row in qr.get_matrix():
            console.print("".join("██" if c else "  " for c in row))
    except ImportError:
        pass


def phone_cmd(argv: list[str]) -> int:
    from rich.console import Console

    from alice.config import load_config

    console = Console(highlight=False)
    cfg = load_config()
    ps = PhoneServer(cfg, new_token="--new-token" in argv)
    console.print(f"[bold magenta]ALICE telefon köprüsü[/] açık.\n"
                  f"QR'yi telefonun kamerasıyla okut (aynı Wi-Fi): [bold]{ps.url}[/]\n"
                  f"Uygulama kuruluysa \"ALICE ile aç\"ı seç; değilse sayfa tarayıcıda açılır, oradan kur.\n"
                  f"Android uygulaması (doğrudan): {ps.app_url}\n"
                  f"Projeler (izle, cevapla, onayla): {ps.projects_url}\n"
                  f"Canlı saat verisi: POST {ps.watch_url}  başlık X-Alice-Token: {ps.state.token}  "
                  'gövde {"hr": 88}')
    print_qr(console, ps.url)
    console.print("[dim]Windows güvenlik duvarı ilk açılışta izin isteyebilir (özel ağ). Ctrl+C çık[/]")
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        pass
    finally:
        ps.stop()
    return 0


def watch_cmd(argv: list[str]) -> int:
    """alice watch status | import <Gadgetbridge.db>"""
    from rich.console import Console

    from alice.core.store import Store
    from alice.health.watch import cadence, import_gadgetbridge

    console = Console(highlight=False)
    sub = argv[0] if argv else "status"
    with Store() as st:
        if sub == "import" and len(argv) > 1:
            added = import_gadgetbridge(Path(argv[1]).expanduser(), st)
            console.print("[green]içe alındı:[/] " + (", ".join(f"{k} {v}" for k, v in added.items()) or "yeni veri yok"))
            sub = "status"
        if sub == "status":
            for key in ("watch.hr", "watch.steps", "watch.stress", "watch.spo2"):
                c = cadence(st, key)
                if not c["count"] and c["last_age_s"] is None:
                    continue
                age = c["last_age_s"]
                age_s = "hiç" if age is None else (f"{age:.0f} sn önce" if age < 120 else f"{age / 60:.0f} dk önce"
                                                   if age < 7200 else f"{age / 3600:.1f} sa önce")
                gap = c.get("median_gap_s")
                gap_s = f", tipik aralık {gap:.0f} sn" if gap is not None else ""
                console.print(f"[bold]{key}[/]: son 24 saatte {c['count']} örnek, son örnek {age_s}{gap_s}\n"
                              f"  → {c['verdict']}")
            if not st.keys("watch.*"):
                console.print("[yellow]henüz saat verisi yok.[/] Gadgetbridge'de otomatik dışa aktarmayı aç, dosyayı "
                              "bilgisayara senkronla ve config'te \\[watch] export_path'i ayarla; ya da "
                              "alice watch import <Gadgetbridge.db>")
            return 0
        console.print(watch_cmd.__doc__, markup=False)
        return 2
