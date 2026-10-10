from __future__ import annotations

from alice import offline
from alice.providers.openai_compat import ProviderError
from alice.providers.router import Router
from alice.voice.audio import Audio
from alice.voice.stt import STTError, STTRouter
from alice.voice.tts import SpeechCache, TTSError, TTSRouter, piper_urls
from tests.conftest import FakeProvider, make_config

PROVIDERS = {
    "cloud": {"base_url": "https://api.example.com/v1", "api_key_env": "", "max_input_tokens": 100000},
    "ollama": {"base_url": "http://localhost:11434/v1", "api_key_env": "", "max_input_tokens": 100000},
}


def net(value: bool) -> offline.NetState:
    box = {"v": value}
    st = offline.NetState(probe=lambda: box["v"], mode=lambda: "auto")
    st.box = box  # type: ignore[attr-defined]
    return st


def test_netstate_modes_and_change_listener(monkeypatch):
    st = net(True)
    seen = []
    st.on_change(seen.append)
    assert st.online()
    st.box["v"] = False
    assert st.refresh() is False and seen == [False]
    assert offline.NetState(probe=lambda: True, mode=lambda: "on").online() is False
    assert offline.NetState(probe=lambda: False, mode=lambda: "off").online() is True
    monkeypatch.setenv("ALICE_OFFLINE", "1")
    assert offline.NetState(probe=lambda: True, mode=lambda: "auto").online() is False


def test_netstate_cached_and_never_blocks_after_first_probe():
    calls = []
    t = [0.0]
    st = offline.NetState(probe=lambda: calls.append(1) or True, mode=lambda: "auto", clock=lambda: t[0])
    assert st.online() and st.online() and len(calls) == 1  # önbellekten


def test_router_uses_only_local_models_when_offline():
    cfg = make_config({"brain": ["cloud:big", "ollama:qwen3:8b"]}, providers=PROVIDERS)
    cloud = FakeProvider("cloud", ["from cloud"])
    local = FakeProvider("ollama", ["yerel cevap"])
    st = net(False)
    r = Router(cfg, provider_factory=lambda pc: {"cloud": cloud, "ollama": local}[pc.name], sleep=lambda s: None,
               net=st)
    assert r.chain("brain") == ["ollama:qwen3:8b", "ollama:alice-local"]
    res = r.chat("brain", [{"role": "user", "content": "selam"}])
    assert res.text == "yerel cevap" and not cloud.calls
    st.box["v"] = True
    st.refresh()
    assert r.chain("brain")[0] == "cloud:big"


def test_router_adds_offline_model_when_chain_has_no_local():
    cfg = make_config({"brain": ["cloud:big"]}, providers=PROVIDERS)
    r = Router(cfg, provider_factory=lambda pc: FakeProvider(pc.name, ["x"]), net=net(False))
    assert r.chain("brain") == ["ollama:alice-local"]


def test_cloud_network_error_triggers_reprobe():
    cfg = make_config({"brain": ["cloud:big", "ollama:m"]}, providers=PROVIDERS)
    cloud = FakeProvider("cloud", [ProviderError("network", "timeout")])
    local = FakeProvider("ollama", ["ok"])
    st = net(True)
    reported = []
    st.report_failure = lambda: reported.append(1)  # type: ignore[method-assign]
    r = Router(cfg, provider_factory=lambda pc: {"cloud": cloud, "ollama": local}[pc.name], sleep=lambda s: None,
               net=st)
    assert r.chat("brain", [{"role": "user", "content": "x"}]).text == "ok"
    assert reported == [1]


class Eng:
    def __init__(self, name: str, fail: bool = False):
        self.name = name
        self.fail = fail
        self.calls = 0

    def synth(self, text: str, emotion: str = "calm") -> Audio:
        self.calls += 1
        if self.fail:
            raise TTSError("x")
        return Audio(b"\x01\x00" * 10, 24000)


def test_tts_skips_cloud_and_edge_when_offline(tmp_path):
    cloud, edge, piper = Eng("gemini"), Eng("edge"), Eng("piper")
    online = {"v": False}
    r = TTSRouter(cloud, None, policy=lambda: {}, backup=edge, offline_engines=[piper], online=lambda: online["v"])
    r.synth("merhaba")
    assert r.last == "piper" and cloud.calls == 0 and edge.calls == 0
    online["v"] = True
    assert [e.name for e in r.order()] == ["gemini", "edge", "piper"]


def test_tts_offline_still_plays_cached_cloud_voice(tmp_path):
    cache = SpeechCache(tmp_path)
    cloud, piper = Eng("gemini"), Eng("piper")
    cache.put("gemini", "calm", "Hemen bakıyorum.", Audio(b"\x02\x00" * 10, 24000))
    r = TTSRouter(cloud, None, policy=lambda: {}, cache=cache, offline_engines=[piper], online=lambda: False)
    r.synth("Hemen bakıyorum.")
    assert r.last == "gemini (önbellek)" and piper.calls == 0


class CloudSTT:
    name = "groq:x"
    cloud = True

    def transcribe(self, audio):
        raise AssertionError("internet yokken buluta gitmemeli")


class Gate:
    def available(self):
        return True

    def transcribe(self, audio, prompt=""):
        return ("saat kaç", -0.2)


def test_stt_uses_wake_model_when_offline():
    r = STTRouter([CloudSTT()], online=lambda: False)
    r.wake_gate = Gate()  # type: ignore[assignment]
    assert r.transcribe(Audio.silence(0.5, 16000)) == "saat kaç" and r.last == "local-wake"
    r.wake_gate = None
    try:
        r.transcribe(Audio.silence(0.5, 16000))
        raise AssertionError("motor yokken hata beklenirdi")
    except STTError:
        pass


def test_voice_turn_mentions_offline():
    from alice.voice.assistant import with_clock

    assert "internet yok" not in with_clock("selam")
    offline.reset(probe=lambda: False, mode="auto")
    assert "internet yok" in with_clock("selam")


def test_piper_urls_and_local_detection():
    model, conf = piper_urls("tr_TR-dfki-medium")
    assert model.endswith("/tr/tr_TR/dfki/medium/tr_TR-dfki-medium.onnx")
    assert conf.endswith(".onnx.json")
    assert offline.is_local_url("http://localhost:11434/v1") and not offline.is_local_url("https://api.groq.com")
    assert offline.ollama_root("http://localhost:11434/v1") == "http://localhost:11434"
    assert "num_ctx 12288" in offline.modelfile("qwen3:8b", 12288)


def test_offline_cli_sets_mode(capsys):
    from alice.config import load_config
    from alice.offline_cli import offline_cmd

    assert offline_cmd(["on"]) == 0
    assert load_config().data["offline"]["mode"] == "on"
