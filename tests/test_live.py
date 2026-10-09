from __future__ import annotations

import base64
import json
import queue
import threading
import time
from types import SimpleNamespace

from alice.tools.base import ToolResult
from alice.voice.live import LiveSession, declarations, sanitize_schema


def test_sanitize_schema_for_gemini():
    s = {"type": "object", "additionalProperties": False, "properties": {
        "a": {"type": ["string", "null"], "default": "x"}, "b": {"type": "array", "items": {"type": "integer"}}},
        "required": ["a"]}
    assert sanitize_schema(s) == {"type": "OBJECT", "properties": {
        "a": {"type": "STRING", "nullable": True}, "b": {"type": "ARRAY", "items": {"type": "INTEGER"}}},
        "required": ["a"]}
    d = declarations([{"type": "function", "function": {"name": "t", "description": "d",
                                                         "parameters": {"type": "object", "properties": {}}}}])
    assert d == [{"name": "t", "description": "d"}]


class FakeWS:
    def __init__(self, script):
        self.sent: list[dict] = []
        self.inbox: queue.Queue = queue.Queue()
        self.script = script
        self.closed = False

    def send(self, data):
        msg = json.loads(data)
        self.sent.append(msg)
        if "setup" in msg:
            self.inbox.put({"setupComplete": {}})
        elif "clientContent" in msg:
            for m in self.script:
                self.inbox.put(m)
        elif "toolResponse" in msg:
            self.inbox.put({"serverContent": {"outputTranscription": {"text": "YouTube açıldı."},
                                              "turnComplete": True}})

    def recv(self, timeout=None):
        while not self.closed:
            try:
                return json.dumps(self.inbox.get(timeout=0.1))
            except queue.Empty:
                continue
        raise ConnectionError("closed")

    def close(self):
        self.closed = True


class FakeOut:
    def __init__(self):
        self.chunks = []

    def put(self, pcm):
        self.chunks.append(pcm)

    def clear(self):
        pass

    playing = False

    def close(self):
        pass


def test_live_converse_runs_tools_and_plays_audio(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    pcm = base64.b64encode(b"\x01\x00" * 100).decode()
    ws = FakeWS([
        {"serverContent": {"inputTranscription": {"text": "YouTube'u aç"},
                           "modelTurn": {"parts": [{"inlineData": {"data": pcm}}]}}},
        {"toolCall": {"functionCalls": [{"id": "c1", "name": "windows",
                                         "args": {"action": "open", "target": "https://youtube.com"}}]}},
    ])
    ran = []
    agent = SimpleNamespace(
        messages=[{"role": "system", "content": "sys"}], ctx=SimpleNamespace(state={}), ui=None,
        tool_specs=lambda: [{"type": "function", "function": {"name": "windows", "description": "w",
                                                               "parameters": {"type": "object", "properties": {
                                                                   "action": {"type": "string"}}}}}],
        _enable_groups_for=lambda **k: None,
        _execute_calls=lambda calls: ran.append(calls) or [ToolResult("açıldı")])
    cfg = SimpleNamespace(data={"voice": {"live_idle": 0.6, "live_models": ["m1"]}})
    out = FakeOut()
    logs = []
    s = LiveSession(cfg, agent, log=logs.append, connect=lambda url, **k: ws, out_factory=lambda: out)
    assert s.available
    t0 = time.monotonic()
    s.converse("YouTube'u aç", queue.Queue(), threading.Event())
    assert time.monotonic() - t0 < 5
    setup = ws.sent[0]["setup"]
    assert setup["model"] == "models/m1" and setup["tools"][0]["functionDeclarations"][0]["name"] == "windows"
    assert json.loads(ran[0][0]["function"]["arguments"]) == {"action": "open", "target": "https://youtube.com"}
    resp = next(m for m in ws.sent if "toolResponse" in m)["toolResponse"]["functionResponses"][0]
    assert resp == {"id": "c1", "name": "windows", "response": {"result": "açıldı"}}
    assert out.chunks == [b"\x01\x00" * 100]
    assert any("YouTube açıldı." in line for line in logs)
    assert agent.ui is None  # eski UI geri kondu
