import asyncio
import json

import pytest

from alice.tools import browser_bridge as bb


def test_bridge_keeps_one_browser_connection_across_clients():
    pytest.importorskip("websockets")  # "voice" ekstrası; CI bunu kurar
    from websockets.asyncio.client import connect
    from websockets.asyncio.server import serve

    seen: list[dict] = []
    upstream_conns = []

    async def browser(ws):
        upstream_conns.append(ws)
        async for raw in ws:
            msg = json.loads(raw)
            seen.append(msg)
            result = {"sessionId": "S1"} if msg["method"] == "Target.attachToBrowserTarget" else {}
            await ws.send(json.dumps({"id": msg["id"], "result": result}))

    async def main():
        async with serve(browser, "127.0.0.1", 0) as up:
            port = up.sockets[0].getsockname()[1]
            bridge = bb.Bridge(find_upstream=lambda: f"ws://127.0.0.1:{port}")
            async with serve(bridge.handle, "127.0.0.1", 0, process_request=bb._health) as srv:
                url = f"ws://127.0.0.1:{srv.sockets[0].getsockname()[1]}{bb.PATH}"
                answers = []
                for _ in range(2):  # çekirdek iki kez başladı
                    async with connect(url) as c:
                        await c.send(json.dumps({"id": 1, "method": "Target.attachToBrowserTarget"}))
                        answers.append(json.loads(await c.recv()))
                    await asyncio.sleep(0.1)
        return answers

    answers = asyncio.run(main())
    assert len(upstream_conns) == 1  # tarayıcıya tek bağlantı: izin bir kez sorulur
    assert [a["id"] for a in answers] == [1, 1]  # istemci kendi kimliğini geri alır
    ids = [m["id"] for m in seen if m["method"] == "Target.attachToBrowserTarget"]
    assert len(set(ids)) == 2  # tarayıcıya giden kimlikler çakışmaz
    assert {"method": "Target.detachFromTarget", "params": {"sessionId": "S1"}}.items() <= next(
        m for m in seen if m["method"] == "Target.detachFromTarget").items()


def test_bridge_drops_stale_answers_and_sessions():
    b = bb.Bridge(find_upstream=lambda: None)
    sent = []

    class Client:
        async def send(self, raw):
            sent.append(json.loads(raw))

    async def main():
        b.client = Client()
        b.gen = 2
        b.pending = {10: (1, 5), 11: (2, 6)}
        b.sessions = {"NEW"}
        await b._from_browser(json.dumps({"id": 10, "result": {}}))  # eski istemcinin cevabı
        await b._from_browser(json.dumps({"id": 11, "result": {}}))
        await b._from_browser(json.dumps({"method": "Page.loadEventFired", "sessionId": "OLD", "params": {}}))
        await b._from_browser(json.dumps({"method": "Page.loadEventFired", "sessionId": "NEW", "params": {}}))

    asyncio.run(main())
    assert sent == [{"id": 6, "result": {}}, {"method": "Page.loadEventFired", "sessionId": "NEW", "params": {}}]


def test_user_endpoint_prefers_bridge(monkeypatch):
    from alice.tools import browser

    monkeypatch.setattr(browser, "devtools_active_endpoint", lambda dirs=None: "ws://127.0.0.1:9333/devtools/browser/x")
    monkeypatch.setattr(browser, "ensure_bridge", lambda port, wait=8.0: True)
    assert browser.user_browser_endpoint(None, 9339) == bb.endpoint(9339)
    monkeypatch.setattr(browser, "ensure_bridge", lambda port, wait=8.0: False)
    assert browser.user_browser_endpoint(None, 9339) == "ws://127.0.0.1:9333/devtools/browser/x"
    assert browser.user_browser_endpoint(None, None) == "ws://127.0.0.1:9333/devtools/browser/x"
