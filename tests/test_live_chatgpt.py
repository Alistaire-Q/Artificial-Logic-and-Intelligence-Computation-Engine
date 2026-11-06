"""ALICE Live (ChatGPT Realtime) birim testleri: protokol, ses dönüşümü, araç çağrısı ve arka uç seçimi."""

from __future__ import annotations

import base64
import json
import queue
import threading
import time
from types import SimpleNamespace as NS

from alice.tools.base import ToolResult
from alice.voice import assistant as voice_assistant
from alice.voice import live_chatgpt
from alice.voice.live_chatgpt import RealtimeSession, resample_16_to_24


class RecordingOut:
    def __init__(self):
        self.data = bytearray()
        self.playing = False
        self.cleared = 0

    def put(self, pcm):
        self.data.extend(pcm)

    def clear(self):
        self.cleared += 1

    def close(self):
        pass


class FakeWS:
    def __init__(self, events):
        self.events = list(events)
        self.sent = []
        self.closed = False

    def recv(self, timeout=None):
        if not self.events:
            raise TimeoutError()
        ev = self.events.pop(0)
        return json.dumps(ev) if isinstance(ev, dict) else ev

    def send(self, raw):
        self.sent.append(json.loads(raw))

    def close(self):
        self.closed = True


def make_session(**voice):
    v = {"mode": "live", "live_backend": "chatgpt", "live_realtime_model": "gpt-realtime-2.1",
         "live_realtime_voice": "marin", "live_idle": 0.2, "live_silence_ms": 600}
    v.update(voice)
    cfg = NS(data={"voice": v, "providers": {}})
    agent = NS(messages=[{"role": "system", "content": "Sen ALICE'sin."}], ui=NS(), tools={},
               refresh_memory=lambda force=False: None)
    return RealtimeSession(cfg, agent, out_factory=RecordingOut, connect=lambda url, secret: None)


def test_resample_16_to_24_length_and_shape():
    pcm = b"\x00\x00" * 480  # 30 ms @16 kHz
    out = resample_16_to_24(pcm)
    assert len(out) == 480 * 3 // 2 * 2  # 720 örnek, 16-bit
    assert out[:2] == b"\x00\x00"


def test_hush_detection():
    assert RealtimeSession._is_hush("Alice sus lütfen")
    assert RealtimeSession._is_hush("tamam yeter")
    assert not RealtimeSession._is_hush("bugün hava nasıl")


def test_session_config_has_audio_vad_and_tools():
    s = make_session()
    s.agent.tools = {"weather": NS(spec=lambda: {"name": "weather",
                                                 "description": "hava durumu",
                                                 "parameters": {"type": "object", "properties": {"city": {"type": "string"}}}})}
    cfg = s._session_config()
    session = cfg["session"]
    assert session["model"] == "gpt-realtime-2.1"
    assert session["audio"]["output"]["voice"] == "marin"
    assert session["audio"]["input"]["turn_detection"]["silence_duration_ms"] == 600
    names = [t["name"] for t in session["tools"]]
    assert "think" in names and "weather" in names


def test_think_delegates_to_agent():
    s = make_session()
    s.agent.run_turn = lambda text: "Güçlü model cevabı: 23 derece"
    call = {"id": "c", "type": "function",
            "function": {"name": "think", "arguments": json.dumps({"request": "hava nasıl?"})}}
    res = s._run_tool(call)
    assert not res.is_error and "23 derece" in res.text


def test_silence_frames_do_not_keep_session_open():
    ws = FakeWS([{"type": "session.created"}])
    s = make_session(live_idle=0.3)
    s._connect = lambda url, secret: ws
    s._secret = lambda: "test-secret"  # noqa: SLF001
    s._out_factory = RecordingOut
    frames: queue.Queue = queue.Queue()
    stop = threading.Event()
    running = threading.Event()
    running.set()

    def mic():
        while running.is_set():
            frames.put(b"\x00\x00" * 480)  # sessizlik
            time.sleep(0.03)

    threading.Thread(target=mic, daemon=True).start()
    t0 = time.monotonic()
    s.converse("", frames, stop)
    elapsed = time.monotonic() - t0
    running.clear()
    assert elapsed < 2.0  # sessizlikte kapanır (5 sn'lik ayar burada 0.3 sn)


def test_converse_plays_audio_and_sends_mic_frames():
    events = [
        {"type": "session.created"},
        {"type": "conversation.item.input_audio_transcription.completed", "transcript": "merhaba"},
        {"type": "response.output_audio.delta", "delta": base64.b64encode(b"\x01\x02" * 12).decode()},
        {"type": "response.done", "response": {"status": "completed"}},
    ]
    ws = FakeWS(events)
    out = RecordingOut()
    s = make_session()
    s._connect = lambda url, secret: ws
    s._secret = lambda: "test-secret"  # noqa: SLF001 - ağ çağrısı yok
    s._out_factory = lambda: out
    frames: queue.Queue = queue.Queue()
    frames.put(b"\x10\x00" * 480)  # 30 ms mikrofon karesi
    stop = threading.Event()
    t = threading.Thread(target=s.converse, args=("", frames, stop), daemon=True)
    t.start()
    t.join(10)
    assert not t.is_alive()
    assert out.data == b"\x01\x02" * 12  # ALICE'in sesi çalındı
    appends = [m for m in ws.sent if m.get("type") == "input_audio_buffer.append"]
    assert appends and len(base64.b64decode(appends[0]["audio"])) == 720 * 2
    assert ws.closed


def test_converse_runs_tool_and_sends_result():
    events = [
        {"type": "session.created"},
        {"type": "response.function_call_arguments.done", "call_id": "c1", "name": "weather",
         "arguments": "{\"city\": \"Ankara\"}"},
        {"type": "response.done", "response": {"status": "completed"}},
    ]
    ws = FakeWS(events)
    s = make_session()
    s._connect = lambda url, secret: ws
    s._secret = lambda: "test-secret"  # noqa: SLF001
    s._out_factory = RecordingOut
    s._run_tool = lambda call: ToolResult("23 derece")  # noqa: SLF001
    frames: queue.Queue = queue.Queue()
    stop = threading.Event()
    t = threading.Thread(target=s.converse, args=("selam", frames, stop), daemon=True)
    t.start()
    end = time.time() + 5
    sent = []
    while time.time() < end:
        sent = [m for m in ws.sent if m.get("type") == "conversation.item.create"]
        if any((m.get("item") or {}).get("type") == "function_call_output" for m in sent):
            break
        time.sleep(0.05)
    t.join(10)
    outputs = [(m.get("item") or {}) for m in ws.sent if m.get("type") == "conversation.item.create"]
    results = [o for o in outputs if o.get("type") == "function_call_output"]
    assert results and results[0]["call_id"] == "c1" and "23 derece" in results[0]["output"]
    assert any(m.get("type") == "response.create" for m in ws.sent)


def test_make_live_prefers_realtime_and_falls_back(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    cfg = NS(data={"voice": {"mode": "live", "live_backend": "auto"}, "providers": {}})
    agent = NS(messages=[{"role": "system", "content": "x"}], ui=NS(), tools={})

    monkeypatch.setattr(live_chatgpt.RealtimeSession, "available", property(lambda self: True))
    assert isinstance(voice_assistant.make_live(cfg, agent), live_chatgpt.RealtimeSession)

    monkeypatch.setattr(live_chatgpt.RealtimeSession, "available", property(lambda self: False))
    from alice.voice.live import LiveSession

    got = voice_assistant.make_live(cfg, agent)
    assert isinstance(got, LiveSession)

    cfg_only = NS(data={"voice": {"mode": "live", "live_backend": "chatgpt"}, "providers": {}})
    assert voice_assistant.make_live(cfg_only, agent) is None
