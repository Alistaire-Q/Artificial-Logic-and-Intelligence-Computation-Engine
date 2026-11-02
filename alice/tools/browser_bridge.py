"""Tarayıcı köprüsü: kullanıcının tarayıcısına tek, kalıcı CDP bağlantısı tutan küçük arka plan süreci.

Opera/Chrome "Allow remote debugging for this browser instance" açıkken her YENİ CDP bağlantısında kullanıcıya
izin penceresi çıkarır. ALICE çekirdeği her yeniden başladığında yeniden bağlanıp tekrar sorduruyordu. Köprü
çekirdekten bağımsız yaşar: tarayıcıya bir kez bağlanır (bir kez izin), çekirdek ise köprüye yerel soketten bağlanır.
Çekirdek yeniden başlasa da tarayıcı bağlantısı kopmaz, izin tekrar sorulmaz.

Aynı anda tek istemci (Playwright) hizmet alır. İstemcinin mesaj kimlikleri köprüde yeniden numaralanır (eski
istemcinin geç gelen cevabı yenisine karışmasın); istemci gidince açtığı oturumlar tarayıcıda kapatılır, böylece
sonraki istemci temiz bir bağlantı görür. Tarayıcı kapanınca köprü de kapanır.

Tarayıcı bağlantısı kapanmadan da ölebilir (Opera uyku/hazırda bekletmeden döndü, sekme süreci çöktü, bağlantı yarı
açık kaldı): soket açık görünür ama hiçbir komuta cevap gelmez, ALICE'in her tarayıcı çağrısı orada asılı kalırdı.
Köprü bu yüzden düzenli aralıklarla tarayıcıya küçük bir komut (Browser.getVersion) gönderir; cevap gelmezse
bağlantıyı ölü sayar ve kapanır. İstemci (Playwright) kopmayı görür, ALICE sonraki çağrıda yeniden bağlanır.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import time
from typing import Any

DEFAULT_PORT = 9339
PATH = "/devtools/browser/alice-bridge"
CLEANUP_BASE = 2_000_000_000  # köprünün kendi komutlarının kimlikleri
HEARTBEAT_ID = CLEANUP_BASE - 1  # kalp atışı komutunun kimliği (istemci kimlikleri 1'den sayar, çakışmaz)
HEARTBEAT_EVERY = 15.0  # saniye
HEARTBEAT_TIMEOUT = 20.0  # bu kadar cevap yoksa tarayıcı bağlantısı ölü sayılır


# Playwright bağlanınca kullanıcının tarayıcısını kendi test tarayıcısı gibi ayarlıyor. Kullanıcının kendi tarayıcısında
# bunlar zarar: indirmeler Playwright'ın geçici klasörüne GUID adıyla iniyor (sonra siliniyor), sayfalar açık temaya
# zorlanıyor. Köprü bu komutları tarayıcıya iletmez, istemciye "oldu" der.
BLOCKED = {"Browser.setDownloadBehavior"}
MEDIA_FEATURES = {"prefers-color-scheme", "prefers-reduced-motion", "forced-colors", "prefers-contrast"}


def neutralize(msg: dict[str, Any]) -> dict[str, Any] | None:
    """İstemciden gelen komut tarayıcıya gitmeli mi? Gitmemeliyse None (köprü sahte başarı döner); gerekirse
    zararsız haline çevrilmiş komut."""
    method = msg.get("method", "")
    if method in BLOCKED:
        return None
    if method == "Emulation.setEmulatedMedia":
        params = dict(msg.get("params") or {})
        feats = [f for f in params.get("features") or [] if f.get("name") not in MEDIA_FEATURES]
        if not feats and not params.get("media"):
            return None
        params["features"] = feats
        return {**msg, "params": params}
    return msg


def endpoint(port: int = DEFAULT_PORT) -> str:
    return f"ws://127.0.0.1:{port}{PATH}"


class Bridge:
    def __init__(self, find_upstream=None, log=lambda s: None, connect=None, heartbeat: float = HEARTBEAT_EVERY,
                 heartbeat_timeout: float = HEARTBEAT_TIMEOUT):
        if find_upstream is None:
            from alice.tools.browser import devtools_active_endpoint

            find_upstream = devtools_active_endpoint
        self.find_upstream = find_upstream
        self.log = log
        self._connect = connect
        self.up = None  # tarayıcı bağlantısı
        self._pump: asyncio.Task | None = None  # olay döngüsü görevleri zayıf tutar: referans burada kalsın
        self._beat: asyncio.Task | None = None
        self.heartbeat, self.heartbeat_timeout = heartbeat, heartbeat_timeout
        self._pong = asyncio.Event()
        self.client = None
        self.gen = 0
        self.next_id = 1
        self.pending: dict[int, tuple[int, Any]] = {}  # köprü kimliği -> (istemci kuşağı, istemci kimliği)
        self.sessions: set[str] = set()  # şu anki istemcinin açtığı CDP oturumları
        self.lock = asyncio.Lock()
        self.closed = asyncio.Event()

    async def _upstream(self):
        if self.up is not None:
            return self.up
        url = self.find_upstream()
        if not url:
            raise ConnectionError("tarayıcıda uzaktan hata ayıklama açık değil")
        connect = self._connect
        if connect is None:
            from websockets.asyncio.client import connect
        # Tarayıcı burada kullanıcıya bir kez izin sorar: cevaplaması için zaman tanı.
        self.up = await connect(url, open_timeout=120, max_size=None, ping_interval=None)
        self.log(f"tarayıcıya bağlanıldı: {url}")
        loop = asyncio.get_running_loop()
        self._pump = loop.create_task(self._pump_up(self.up))
        if self.heartbeat > 0:
            self._beat = loop.create_task(self._heartbeat(self.up))
        return self.up

    async def _heartbeat(self, up) -> None:
        """Tarayıcı hâlâ cevap veriyor mu? Vermiyorsa bağlantıyı kapat (yoksa istemci sonsuza dek bekler)."""
        while self.up is up:
            await asyncio.sleep(self.heartbeat)
            if self.up is not up:
                return
            self._pong.clear()
            try:
                await asyncio.wait_for(up.send(json.dumps({"id": HEARTBEAT_ID, "method": "Browser.getVersion"})),
                                       self.heartbeat_timeout)
                await asyncio.wait_for(self._pong.wait(), self.heartbeat_timeout)
            except Exception:  # noqa: BLE001 - zaman aşımı ya da kopuk soket: ikisi de ölü bağlantı
                self.log(f"tarayıcı {self.heartbeat_timeout:.0f} sn cevap vermedi; bağlantı ölü sayılıyor")
                await self._shutdown(up)
                return

    async def _shutdown(self, up) -> None:
        """Ölü tarayıcı bağlantısını bırak, istemciyi kopar, köprüyü kapat. Kapanış el sıkışması da asılı kalabilir:
        her adım süreyle sınırlı."""
        if self.up is up:
            self.up = None
        for ws in (up, self.client):
            if ws is None:
                continue
            try:
                await asyncio.wait_for(ws.close(), 3)
            except Exception:  # noqa: BLE001
                transport = getattr(ws, "transport", None)
                if transport is not None:
                    transport.abort()
        self.closed.set()

    async def _pump_up(self, up) -> None:
        try:
            async for raw in up:
                await self._from_browser(raw)
        except Exception as e:  # noqa: BLE001
            self.log(f"tarayıcı bağlantısı koptu: {e}")
        finally:
            self.log("tarayıcı kapandı; köprü kapanıyor")
            await self._shutdown(up)

    async def _from_browser(self, raw) -> None:
        msg = json.loads(raw)
        if "id" in msg:
            if msg["id"] == HEARTBEAT_ID:
                self._pong.set()
                return
            ref = self.pending.pop(msg["id"], None)
            if ref is None or ref[0] != self.gen:
                return  # köprünün kendi komutu ya da eski istemcinin cevabı
            msg["id"] = ref[1]
            sid = (msg.get("result") or {}).get("sessionId")
            if sid:
                self.sessions.add(sid)
        else:
            method, params = msg.get("method", ""), msg.get("params") or {}
            if method == "Target.attachedToTarget" and params.get("sessionId"):
                if msg.get("sessionId") and msg["sessionId"] not in self.sessions:
                    return
                self.sessions.add(params["sessionId"])
            elif method == "Target.detachedFromTarget":
                self.sessions.discard(params.get("sessionId"))
            if msg.get("sessionId") and msg["sessionId"] not in self.sessions:
                return  # eski istemcinin oturumundan kalan olay
            if self.client is None:
                return
        client = self.client
        if client is not None:
            try:
                await client.send(json.dumps(msg))
            except Exception:  # noqa: BLE001
                pass

    async def _cleanup(self) -> None:
        """Giden istemcinin izlerini sil: oturumlarını kapat, otomatik bağlanmayı bırak."""
        up = self.up
        if up is None:
            return
        cmds: list[dict[str, Any]] = [{"method": "Target.detachFromTarget", "params": {"sessionId": s}}
                                      for s in self.sessions]
        cmds += [{"method": "Target.setAutoAttach", "params": {"autoAttach": False, "waitForDebuggerOnStart": False,
                                                                "flatten": True}},
                 {"method": "Target.setDiscoverTargets", "params": {"discover": False}},
                 # eski sürüm köprü/istemci indirmeleri geçici klasöre yönlendirdiyse tarayıcının kendi ayarına dön
                 {"method": "Browser.setDownloadBehavior", "params": {"behavior": "default"}}]
        self.sessions = set()
        for i, c in enumerate(cmds):
            c["id"] = CLEANUP_BASE + i
            try:
                await up.send(json.dumps(c))
            except Exception:  # noqa: BLE001
                return

    async def handle(self, ws) -> None:
        async with self.lock:
            old = self.client
            if old is not None:
                await old.close()
            if self.closed.is_set():
                # Tarayıcı bağlantısı öldü, köprü kapanıyor: yeniden bağlanıp bir daha izin sordurmasın; istemci
                # yeni bir köprü başlatır.
                await ws.close(1011, "köprü kapanıyor")
                return
            try:
                up = await self._upstream()
            except Exception as e:  # noqa: BLE001
                self.log(f"tarayıcıya bağlanılamadı: {e}")
                await ws.close(1011, str(e)[:120])
                return
            await self._cleanup()
            self.gen += 1
            gen = self.gen
            self.pending.clear()
            self.client = ws
        self.log("istemci bağlandı")
        try:
            async for raw in ws:
                msg = neutralize(json.loads(raw))
                if msg is None:  # tarayıcıya gitmez: istemci beklemesin
                    orig = json.loads(raw)
                    if "id" in orig:
                        reply = {"id": orig["id"], "result": {}}
                        if orig.get("sessionId"):
                            reply["sessionId"] = orig["sessionId"]
                        await ws.send(json.dumps(reply))
                    continue
                if "id" in msg:
                    bid = self.next_id
                    self.next_id += 1
                    self.pending[bid] = (gen, msg["id"])
                    msg["id"] = bid
                await up.send(json.dumps(msg))
        except Exception:  # noqa: BLE001 - istemci koptu
            pass
        finally:
            async with self.lock:
                if self.client is ws:
                    self.client = None
                    self.gen += 1
                    await self._cleanup()
            self.log("istemci ayrıldı")


def _health(connection, request, bridge: Bridge | None = None):
    if request.path == "/alice-bridge":
        if bridge is not None and bridge.closed.is_set():
            return connection.respond(503, "closing\n")  # kapanan köprüye bağlanılmasın, yenisi başlatılsın
        return connection.respond(200, "alice-bridge\n")
    return None


async def serve(port: int, log=print, bridge: Bridge | None = None) -> None:
    from websockets.asyncio.server import serve as ws_serve

    bridge = bridge or Bridge(log=log)
    async with ws_serve(bridge.handle, "127.0.0.1", port, max_size=None, ping_interval=None,
                        process_request=lambda c, r: _health(c, r, bridge)):
        log(f"köprü dinliyor: {endpoint(port)}")
        await bridge.closed.wait()


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(description="ALICE tarayıcı köprüsü")
    p.add_argument("--port", type=int, default=DEFAULT_PORT)
    a = p.parse_args(argv)
    from alice.config import alice_home

    path = alice_home() / "browser-bridge.log"

    def log(s: str) -> None:
        try:
            with open(path, "a", encoding="utf-8") as f:
                f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {s}\n")
        except OSError:
            pass

    try:
        asyncio.run(serve(a.port, log))
    except OSError as e:  # port dolu: başka köprü zaten çalışıyor
        log(f"köprü başlatılamadı: {e}")


if __name__ == "__main__":
    main()
