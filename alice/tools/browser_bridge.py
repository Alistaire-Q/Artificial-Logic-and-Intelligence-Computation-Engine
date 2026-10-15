"""Tarayıcı köprüsü: kullanıcının tarayıcısına tek, kalıcı CDP bağlantısı tutan küçük arka plan süreci.

Opera/Chrome "Allow remote debugging for this browser instance" açıkken her YENİ CDP bağlantısında kullanıcıya
izin penceresi çıkarır. ALICE çekirdeği her yeniden başladığında yeniden bağlanıp tekrar sorduruyordu. Köprü
çekirdekten bağımsız yaşar: tarayıcıya bir kez bağlanır (bir kez izin), çekirdek ise köprüye yerel soketten bağlanır.
Çekirdek yeniden başlasa da tarayıcı bağlantısı kopmaz, izin tekrar sorulmaz.

Aynı anda tek istemci (Playwright) hizmet alır. İstemcinin mesaj kimlikleri köprüde yeniden numaralanır (eski
istemcinin geç gelen cevabı yenisine karışmasın); istemci gidince açtığı oturumlar tarayıcıda kapatılır, böylece
sonraki istemci temiz bir bağlantı görür. Tarayıcı kapanınca köprü de kapanır.
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


def endpoint(port: int = DEFAULT_PORT) -> str:
    return f"ws://127.0.0.1:{port}{PATH}"


class Bridge:
    def __init__(self, find_upstream=None, log=lambda s: None, connect=None):
        if find_upstream is None:
            from alice.tools.browser import devtools_active_endpoint

            find_upstream = devtools_active_endpoint
        self.find_upstream = find_upstream
        self.log = log
        self._connect = connect
        self.up = None  # tarayıcı bağlantısı
        self._pump: asyncio.Task | None = None  # olay döngüsü görevleri zayıf tutar: referans burada kalsın
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
        self._pump = asyncio.get_running_loop().create_task(self._pump_up(self.up))
        return self.up

    async def _pump_up(self, up) -> None:
        try:
            async for raw in up:
                await self._from_browser(raw)
        except Exception as e:  # noqa: BLE001
            self.log(f"tarayıcı bağlantısı koptu: {e}")
        finally:
            self.up = None
            self.log("tarayıcı kapandı; köprü kapanıyor")
            client = self.client
            if client is not None:
                await client.close()
            self.closed.set()

    async def _from_browser(self, raw) -> None:
        msg = json.loads(raw)
        if "id" in msg:
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
                 {"method": "Target.setDiscoverTargets", "params": {"discover": False}}]
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
                msg = json.loads(raw)
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


def _health(connection, request):
    if request.path == "/alice-bridge":
        return connection.respond(200, "alice-bridge\n")
    return None


async def serve(port: int, log=print, bridge: Bridge | None = None) -> None:
    from websockets.asyncio.server import serve as ws_serve

    bridge = bridge or Bridge(log=log)
    async with ws_serve(bridge.handle, "127.0.0.1", port, max_size=None, ping_interval=None,
                        process_request=_health):
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
