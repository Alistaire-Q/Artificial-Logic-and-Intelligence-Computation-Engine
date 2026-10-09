from __future__ import annotations

from alice.decider import Answer, Decision
from alice.reflex.loop import ReflexLoop, rank
from alice.reflex.observers import Element, Observation


class FakeObserver:
    kind = "fake"

    def __init__(self):
        self.page = "home"
        self.actions = []

    def observe(self):
        if self.page == "home":
            els = [Element(1, "a", "Haberler", False), Element(2, "input[search]", "Ara", True),
                   Element(3, "button", "Ayarlar", False)]
        elif self.page == "settings":
            els = [Element(4, "a", "Gizlilik", False), Element(5, "a", "Geri", False)]
        else:
            els = [Element(6, "h1", "Gizlilik ayarları", False)]
        return Observation(self.page, f"https://x/{self.page}", els)

    def click(self, element_id):
        self.actions.append(("click", element_id))
        self.page = {3: "settings", 4: "privacy"}.get(element_id, self.page)

    def type(self, element_id, text, submit):
        self.actions.append(("type", element_id, text))

    def scroll(self, down):
        self.actions.append(("scroll", down))

    def back(self):
        self.actions.append(("back",))

    def press_enter(self):
        self.actions.append(("enter",))


class ScriptedDecider:
    names = ["fake"]

    def __init__(self, picks):
        self.picks = list(picks)
        self.states = []

    def decide(self, state, questions):
        self.states.append((state, questions))
        label, conf, done = self.picks.pop(0)
        assert label in questions["next"].criteria, f"{label} seçeneklerde yok"
        return Decision({"next": Answer("choice", label, conf), "done": Answer("noul", done, 1.0)}, "fake", 0.01)


def test_navigates_to_goal():
    obs = FakeObserver()
    dec = ScriptedDecider([("click_3", 0.9, 0.0), ("click_4", 0.95, 0.0), ("done", 0.9, 0.95)])
    res = ReflexLoop(obs, dec, settle=0).run("Ayarlar > Gizlilik sayfasını aç")
    assert res.status == "done" and obs.actions == [("click", 3), ("click", 4)]
    assert "click_3" in res.log[0] or "Ayarlar" in res.log[0]


def test_low_confidence_escalates():
    res = ReflexLoop(FakeObserver(), ScriptedDecider([("click_1", 0.2, 0.0)]), settle=0).run("x")
    assert res.status == "needs_help" and "düşük güven" in res.reason


def test_typing_uses_inputs_then_writer():
    obs = FakeObserver()
    dec = ScriptedDecider([("type_2", 0.9, 0.0), ("press_enter", 0.9, 0.0), ("done", 0.9, 0.9)])
    res = ReflexLoop(obs, dec, settle=0).run("ara: hava durumu", inputs={"ara": "hava durumu ankara"})
    assert ("type", 2, "hava durumu ankara") in obs.actions and ("enter",) in obs.actions
    assert res.status == "done"

    obs2 = FakeObserver()
    dec2 = ScriptedDecider([("type_2", 0.9, 0.0), ("done", 0.9, 0.9)])
    ReflexLoop(obs2, dec2, text_writer=lambda goal, field: "üretilen", settle=0).run("bir şey ara")
    assert ("type", 2, "üretilen") in obs2.actions


def test_repeated_action_detected():
    dec = ScriptedDecider([("scroll_down", 0.9, 0.0)] * 5)
    res = ReflexLoop(FakeObserver(), dec, settle=0).run("x")
    assert res.status == "needs_help" and "tekrar" in res.reason


def test_rank_prefers_goal_words():
    els = [Element(1, "a", "Haberler", False), Element(2, "a", "Gizlilik ayarları", False),
           Element(3, "input", "", True)]
    top = rank(els, "gizlilik ayarlarını aç", 2, set())
    assert top[0].id == 2
