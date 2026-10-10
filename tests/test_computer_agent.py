import json

from alice.computer_agent import ComputerAgent, Screen, Shot, parse_action


class FakeScreen:
    def __init__(self):
        self.acts = []

    def observe(self):
        return Shot("IMG", 0, 0, 1920, 1080, [{"id": 1, "role": "Button", "name": "Kaydet", "center": (100, 50),
                                               "rect": (90, 40, 110, 60)}], "Not Defteri")

    def act(self, a, shot):
        self.acts.append(a)
        return "ok"


def test_agent_loops_until_done_and_sends_screenshot():
    answers = iter(['{"action": "click", "id": 1, "thought": "kaydet"}',
                    'Tamam: {"action": "done", "summary": "Kaydedildi."}'])
    seen = []

    def ask(messages):
        seen.append(messages)
        return next(answers)

    s = FakeScreen()
    res = ComputerAgent(s, ask, settle=0).run("dosyayı kaydet")
    assert res.status == "done" and res.summary == "Kaydedildi."
    assert s.acts == [{"action": "click", "id": 1, "thought": "kaydet"}]
    parts = seen[0][1]["content"]
    assert "[1] Button 'Kaydet'" in parts[0]["text"] and parts[1]["image_url"]["url"].endswith("IMG")
    assert "1. click" in seen[1][1]["content"][0]["text"]  # geçmiş adımlar sonraki adıma gider


def test_agent_stops_on_repeated_action():
    s = FakeScreen()
    res = ComputerAgent(s, lambda m: '{"action": "key", "keys": "enter"}', settle=0).run("x")
    assert res.status == "fail" and len(s.acts) == 2


def test_point_mapping_uses_ids_and_normalized_coords():
    shot = Shot("", 100, 0, 2000, 1000, [{"id": 3, "center": (500, 400)}])
    assert Screen.point({"id": 3}, shot) == (500, 400)
    assert Screen.point({"point": [500, 250]}, shot) == (600, 500)  # [y, x] 0-1000
    assert parse_action('```json\n{"action": "wait"}\n```') == {"action": "wait"}


def test_live_runs_computer_task_in_background(monkeypatch):
    import queue
    import threading
    from types import SimpleNamespace

    from alice.tools.base import ToolResult
    from alice.voice.live import LiveSession

    monkeypatch.setenv("GEMINI_API_KEY", "k")
    sent = []
    done = threading.Event()

    class WS:
        def send(self, data):
            sent.append(json.loads(data))
            if "clientContent" in data:
                done.set()

    def execute(calls):
        return [ToolResult("done: ev yapıldı")]

    agent = SimpleNamespace(_enable_groups_for=lambda **k: None, _execute_calls=execute)
    s = LiveSession(SimpleNamespace(data={"voice": {}}), agent)
    s._run_call(WS(), {"id": "c1", "name": "computer_task", "args": {"goal": "ev yap"}})
    assert "başladı" in sent[0]["toolResponse"]["functionResponses"][0]["response"]["result"]
    assert done.wait(3)
    assert "ev yapıldı" in sent[-1]["clientContent"]["turns"][0]["parts"][0]["text"]
    assert s._bg == 0
