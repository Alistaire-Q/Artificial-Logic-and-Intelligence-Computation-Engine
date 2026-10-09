from __future__ import annotations

import array
import base64
import json
import math
import threading
import time

import httpx
import pytest

from alice.voice.audio import Audio, rms
from alice.voice.assistant import VoiceAssistant, VoiceUI
from alice.voice.audition import run_audition
from alice.voice.listen import VAD, strip_wake, utterances
from alice.voice.speaker import Speaker
from alice.voice.stt import GroqWhisper, STTError, STTRouter
from alice.voice.tone import SentenceSplitter, for_speech, pick
from alice.voice.tts import EmotionLibrary, GeminiTTS, TTSError, TTSRouter, build_library


def tone(seconds=0.1, rate=16000, amp=0.3, freq=220):
    a = array.array("h", (int(amp * 32767 * math.sin(2 * math.pi * freq * i / rate)) for i in range(int(seconds * rate))))
    return Audio(a.tobytes(), rate)


# --------------------------------------------------------------------- audio
def test_audio_wav_roundtrip_and_resample():
    a = tone(0.5, 24000)
    b = Audio.from_wav(a.to_wav())
    assert b.rate == 24000 and b.pcm == a.pcm
    r = a.resample(16000)
    assert r.rate == 16000 and abs(r.seconds - 0.5) < 0.01
    assert rms(Audio.silence(0.1).pcm) == 0 and rms(a.pcm) > 0.1
    assert (Audio.silence(0.1, 16000) + a).rate == 16000


# ---------------------------------------------------------------------- tone
@pytest.mark.parametrize("text,sev,topic,expected", [
    ("Bingo yakıt, eve dön.", "warn", "flight", "urgent"),
    ("Yedekleme başarısız oldu.", "", "", "serious"),
    ("Derleme tamamlandı, testler geçti!", "", "", "happy"),
    ("Saat on bir.", "", "", "calm"),
    ("Disk çok dolu", "critical", "pc", "urgent"),
])
def test_pick(text, sev, topic, expected):
    assert pick(text, sev, topic) == expected


def test_for_speech_strips_markdown():
    out = for_speech("**Tamam.** Şuna bak: `x.py` ve [doküman](https://a.b)\n```py\nprint(1)\n```\n- madde")
    assert "**" not in out and "`" not in out and "https" not in out
    assert "Kodu ekrana yazdım" in out and "doküman" in out and "madde" in out


def test_splitter_streams_sentences():
    s = SentenceSplitter()
    out = []
    for ch in "Tamam, hemen bakıyorum. GPU 3.5 derece arttı! Kod:\n```\nx=1. y=2\n```\nBitti.":
        out += s.feed(ch)
    out += s.flush()
    assert out == ["Tamam, hemen bakıyorum.", "GPU 3.5 derece arttı!", "Kod:", "Kodu ekrana yazdım.", "Bitti."]


# ----------------------------------------------------------------------- TTS
def test_gemini_tts_request_and_decode():
    seen = {}
    pcm = tone(0.1, 24000).pcm

    def handler(req: httpx.Request):
        seen["url"] = str(req.url)
        seen["body"] = json.loads(req.content)
        seen["key"] = req.headers["x-goog-api-key"]
        return httpx.Response(200, json={"candidates": [{"content": {"parts": [{"inlineData": {
            "mimeType": "audio/L16;codec=pcm;rate=24000", "data": base64.b64encode(pcm).decode()}}]}}]})

    eng = GeminiTTS("k", "Kore", client=httpx.Client(transport=httpx.MockTransport(handler)))
    audio = eng.synth("Yakıt az.", "urgent")
    assert audio.rate == 24000 and audio.pcm.startswith(b"\x00\x00") and len(audio.pcm) > len(pcm)
    assert seen["key"] == "k" and "gemini-3.8-flash-lite-tts:generateContent" in seen["url"]
    cfg = seen["body"]["generationConfig"]
    assert cfg["speechConfig"]["voiceConfig"]["prebuiltVoiceConfig"]["voiceName"] == "Kore"
    prompt = seen["body"]["contents"][0]["parts"][0]["text"]
    # Yönerge ayrı başlıkta, konuşulacak metin sadece TRANSCRIPT altında
    assert prompt.endswith("#### TRANSCRIPT\nYakıt az.") and "DIRECTOR'S NOTES\nStyle: Clear, firm and urgent" in prompt
    assert prompt.index("Do NOT speak them") < prompt.index("#### TRANSCRIPT")

    bad = GeminiTTS("k", client=httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(429, text="quota"))))
    with pytest.raises(TTSError):
        bad.synth("x")


class FakeTTS:
    def __init__(self, name, fail=False, delay=0.0):
        self.name = name
        self.fail = fail
        self.delay = delay
        self.calls = []

    def synth(self, text, emotion="calm"):
        self.calls.append((text, emotion))
        time.sleep(self.delay)
        if self.fail:
            raise TTSError("yok")
        return Audio(text.encode("utf-8")[:20].ljust(20, b"\0"), 16000)


def test_tts_router_fallback_and_flight_policy():
    cloud, local = FakeTTS("cloud", fail=True), FakeTTS("local")
    r = TTSRouter(cloud, local, policy=lambda: {})
    r.synth("a")
    assert r.last == "local"
    r.synth("b")
    assert len(cloud.calls) == 1  # bulut bekletiliyor

    cloud2, local2 = FakeTTS("cloud"), FakeTTS("local")
    r2 = TTSRouter(cloud2, local2, prefer="local", policy=lambda: {"local_gpu": False})
    r2.synth("uçuşta")
    assert r2.last == "cloud"
    r3 = TTSRouter(cloud2, local2, prefer="local", policy=lambda: {"local_gpu": True})
    r3.synth("yerde")
    assert r3.last == "local"


def test_emotion_library(tmp_path):
    out = build_library(FakeTTS("gemini:Aoede"), tmp_path / "lib")
    lib = EmotionLibrary(tmp_path / "lib")
    assert lib.exists() and lib.source == "gemini:Aoede" and lib.clip("urgent").name == "urgent.wav"
    (out / "urgent.wav").unlink()
    assert lib.clip("urgent").name == "calm.wav"


# ----------------------------------------------------------------------- STT
def test_groq_whisper():
    seen = {}

    def handler(req: httpx.Request):
        seen["auth"] = req.headers["authorization"]
        seen["body"] = req.content
        return httpx.Response(200, json={"text": " Hey Alice, saat kaç? "})

    stt = GroqWhisper("g", client=httpx.Client(transport=httpx.MockTransport(handler)))
    assert stt.transcribe(tone(0.2, 24000)) == "Hey Alice, saat kaç?"
    assert seen["auth"] == "Bearer g" and b"whisper-large-v3-turbo" in seen["body"] and b'name="language"' in seen["body"]


def test_stt_router_fallback():
    class Bad:
        name = "bad"

        def transcribe(self, a):
            raise STTError("kota")

    class Good:
        name = "good"

        def transcribe(self, a):
            return "merhaba"

    r = STTRouter([Bad(), Good()])
    assert r.transcribe(tone()) == "merhaba" and r.last == "good"
    with pytest.raises(STTError):
        STTRouter([Bad()]).transcribe(tone())


# -------------------------------------------------------------------- listen
def test_strip_wake():
    assert strip_wake("Hey Alice, saat kaç?") == "saat kaç?"
    assert strip_wake("alis bugün hava nasıl") == "bugün hava nasıl"
    assert strip_wake("Alice.") == ""
    assert strip_wake("bugün hava nasıl") is None
    assert strip_wake("Alicem nerede") is None


def frames_of(*parts):
    """(saniye, ses var mı) parçalarından 30 ms kareler."""
    for sec, loud in parts:
        a = tone(sec) if loud else Audio.silence(sec, 16000)
        for i in range(0, len(a.pcm) - 959, 960):
            yield a.pcm[i:i + 960]


def test_utterances_split_and_barge_in_callback():
    vad = VAD()
    vad._vad = None  # enerji tabanlı, deterministik
    starts = []
    outs = list(utterances(frames_of((0.5, False), (0.6, True), (1.0, False), (0.4, True), (1.0, False)), vad,
                           on_start=lambda: starts.append(1)))
    assert len(outs) == 2 and len(starts) == 2
    assert 0.5 < outs[0].seconds < 1.2
    # kısa bir tık konuşma sayılmaz
    assert list(utterances(frames_of((0.06, True), (1.0, False)), vad)) == []


# ------------------------------------------------------------------- speaker
class FakePlayer:
    def __init__(self, dur=0.0):
        self.played = []
        self.dur = dur
        self._stop = threading.Event()
        self.available = True

    def play(self, audio):
        self._stop.clear()
        self.played.append(audio.pcm.rstrip(b"\0").decode())
        return not self._stop.wait(self.dur)

    def stop(self):
        self._stop.set()


def test_speaker_keeps_order_and_stops():
    tts, player = FakeTTS("t", delay=0.01), FakePlayer()
    sp = Speaker(tts, player)
    for s in ("bir", "iki", "üç"):
        sp.say(s)
    assert sp.wait(5)
    assert player.played == ["bir", "iki", "üç"]
    assert tts.calls[0][1] == "calm"

    slow = FakePlayer(dur=5)
    sp2 = Speaker(FakeTTS("t"), slow)
    sp2.say("uzun cümle")
    sp2.say("ikinci")
    for _ in range(100):
        if slow.played:
            break
        time.sleep(0.01)
    sp2.stop()
    assert sp2.wait(2) and slow.played == ["uzun cümle"]


# ----------------------------------------------------------------- assistant
class FakeSpeaker:
    def __init__(self):
        self.said = []
        self.speaking = False

    def say(self, text, emotion=None, severity="", topic=""):
        self.said.append(text)
        return True

    def stop(self):
        self.said.append("<stop>")

    def wait(self, timeout=None):
        return True


def test_voice_ui_streams_and_fillers():
    sp = FakeSpeaker()
    ui = VoiceUI(sp)
    ui.begin_turn()
    ui.tool_start("1", "metrics", "")
    for ch in "Dün en yüksek 78 dereceydi. Gayet normal.":
        ui.stream_delta(ch)
    ui.stream_end("Dün en yüksek 78 dereceydi. Gayet normal.", "x")
    assert sp.said[0] in ("Hemen bakıyorum.", "Bir saniye, bakıyorum.", "Tamam, bakıyorum.")
    assert sp.said[1:] == ["Dün en yüksek 78 dereceydi.", "Gayet normal."]
    ui.mute()
    ui.stream_end("söylenmemeli", "x")
    assert "söylenmemeli" not in sp.said


class FakeAgent:
    def __init__(self):
        self.ui = None
        self.turns = []
        self.stopped = 0

    def run_turn(self, text):
        self.turns.append(text)
        self.ui.stream_delta("Tamam, hallettim.")
        self.ui.stream_end("Tamam, hallettim.", "x")
        return "ok"

    def stop(self):
        self.stopped += 1


def wait_for(cond, t=3):
    end = time.monotonic() + t
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.01)
    return False


def test_assistant_wake_gating_and_follow_up():
    agent, sp = FakeAgent(), FakeSpeaker()
    va = VoiceAssistant(agent, sp, stt=None, follow_up=0.5)
    va.handle_text("bugün hava nasıl", direct=False)          # uyandırma yok -> yok sayılır
    time.sleep(0.1)
    assert agent.turns == []
    va.handle_text("Hey Alice, GPU kaç derece?", direct=False)
    assert wait_for(lambda: agent.turns == ["GPU kaç derece?"])
    assert wait_for(lambda: "Tamam, hallettim." in sp.said)
    # cevaptan hemen sonra uyandırma gerekmez
    assert wait_for(lambda: va.armed_until > time.monotonic())
    va.handle_text("peki ya dün?", direct=False)
    assert wait_for(lambda: agent.turns[-1] == "peki ya dün?")
    va.handle_text("Alice", direct=True)
    assert "Efendim?" in sp.said


def test_assistant_yes_no():
    agent, sp = FakeAgent(), FakeSpeaker()
    va = VoiceAssistant(agent, sp, stt=None)
    res = {}
    t = threading.Thread(target=lambda: res.setdefault("ok", va.ask_yes_no("Silinsin mi?", timeout=3)))
    t.start()
    assert wait_for(lambda: va._awaiting_answer)
    va.handle_text("evet sil", direct=False)
    t.join(3)
    assert res["ok"] is True and agent.turns == []


# ------------------------------------------------------------------ audition
def test_audition_page(tmp_path):
    engines = [FakeTTS("gemini:Aoede"), FakeTTS("xtts", fail=True)]
    page = run_audition(engines, ["calm", "urgent"], out=tmp_path)
    html = page.read_text(encoding="utf-8")
    assert (tmp_path / "gemini_Aoede_calm.wav").exists()
    assert "alice voice use gemini:Aoede" in html and "audio" in html and "yok" in html
    n = len(engines[0].calls)
    run_audition(engines[:1], ["calm", "urgent"], out=tmp_path)
    assert len(engines[0].calls) == n  # var olanlar yeniden üretilmez


GEMINI_429 = json.dumps({"error": {"code": 429, "message": "Quota exceeded... Please retry in 41.9s.", "details": [
    {"@type": "type.googleapis.com/google.rpc.QuotaFailure", "violations": [
        {"quotaId": "GenerateRequestsPerMinutePerProjectPerModel-FreeTier", "quotaValue": "3"}]},
    {"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "41s"}]}})


def test_gemini_quota_parsing():
    from alice.voice.tts import QuotaError, gemini_quota_error

    q = gemini_quota_error(GEMINI_429)
    assert isinstance(q, QuotaError) and q.retry_after == 41 and not q.daily and "dakikalık" in str(q)
    d = gemini_quota_error(GEMINI_429.replace("PerMinute", "PerDay"))
    assert d.daily and "günlük" in str(d)
    eng = GeminiTTS("k", client=httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(429, text=GEMINI_429))))
    with pytest.raises(QuotaError):
        eng.synth("x")


def test_audition_waits_on_minute_quota_and_stops_on_daily(tmp_path):
    from alice.voice.tts import QuotaError

    class Flaky(FakeTTS):
        def __init__(self, name, errors):
            super().__init__(name)
            self.errors = list(errors)

        def synth(self, text, emotion="calm"):
            if self.errors:
                raise self.errors.pop(0)
            return super().synth(text, emotion)

    waits = []
    eng = Flaky("gemini:Kore", [QuotaError("dk", 30, False)])
    run_audition([eng], ["calm", "urgent"], out=tmp_path, sleep=waits.append)
    assert waits == [31] and (tmp_path / "gemini_Kore_calm.wav").exists() and (tmp_path / "gemini_Kore_urgent.wav").exists()

    a = Flaky("gemini:Leda", [QuotaError("gün", None, True)])
    b = FakeTTS("gemini:Aoede")
    run_audition([a, b], ["calm", "happy"], out=tmp_path / "2", sleep=waits.append)
    assert len(a.calls) == 0 and b.calls == []  # günlük kota: gemini'nin kalanına hiç istek gitmez
    assert len(waits) == 1


def test_gemini_rotates_models_on_quota():
    from alice.voice.tts import QuotaError

    pcm = tone(0.05, 24000).pcm
    daily = GEMINI_429.replace("PerMinute", "PerDay")
    plan = {"m1": daily, "m2": "404", "m3": "ok"}
    hits = []

    def handler(req):
        model = req.url.path.split("/")[-1].split(":")[0]
        hits.append(model)
        r = plan[model]
        if r == "404":
            return httpx.Response(404, text="not found")
        if r == "ok":
            return httpx.Response(200, json={"candidates": [{"content": {"parts": [{"inlineData": {
                "mimeType": "audio/L16;rate=24000", "data": base64.b64encode(pcm).decode()}}]}}]})
        return httpx.Response(429, text=r)

    eng = GeminiTTS("k", "Aoede", ["m1", "m2", "m3"], client=httpx.Client(transport=httpx.MockTransport(handler)))
    assert eng.synth("a").rate == 24000 and eng.last_model == "m3" and hits == ["m1", "m2", "m3"]
    eng.synth("b")
    assert hits[3:] == ["m3"]  # dolan ve olmayan modeller atlanıyor

    plan["m3"] = GEMINI_429  # dakikalık
    with pytest.raises(QuotaError) as ei:
        eng.synth("c")
    assert not ei.value.daily and ei.value.retry_after
    eng.down["m3"] = 0
    plan["m3"] = daily
    with pytest.raises(QuotaError) as ei:
        eng.synth("d")
    assert ei.value.daily


def test_speech_cache(tmp_path):
    from alice.voice.tts import SpeechCache

    cloud = FakeTTS("cloud")
    r = TTSRouter(cloud, None, policy=lambda: {}, cache=SpeechCache(tmp_path))
    a = r.synth("Hemen bakıyorum.", "calm")
    b = r.synth("Hemen bakıyorum.", "calm")
    assert a.pcm == b.pcm and len(cloud.calls) == 1 and r.last.endswith("(önbellek)")
    r.synth("Hemen bakıyorum.", "urgent")
    r.synth("uzun " * 40, "calm")
    r.synth("uzun " * 40, "calm")
    assert len(cloud.calls) == 4  # farklı ton ve uzun cümle önbelleğe alınmaz


def test_polish_removes_click_and_noisy_tail():
    import array as arr

    rate = 24000
    body = tone(0.4, rate, amp=0.5)
    # sonda hafif ama sessiz olmayan cızırtı (~%2) ve aniden biten sinyal
    noise = arr.array("h", [((i * 7919) % 1300) - 650 for i in range(int(0.3 * rate))]).tobytes()
    out = Audio(body.pcm + noise, rate).polish()
    a = arr.array("h", out.pcm)
    speech_end = int(0.4 * rate)
    assert abs(a[0]) <= 1                                         # başta tık yok
    nz = max(i for i, x in enumerate(a) if x != 0)
    assert nz < speech_end + int(0.08 * rate)                     # cızırtılı kuyruğun çoğu kesildi
    assert all(abs(x) < 400 for x in a[nz - 10:nz + 1])           # sona doğru sıfıra iniyor (tık yok)
    assert len(a) - nz >= int(0.14 * rate)                        # sonda sessizlik var


def test_leak_detection_and_format_fallback(tmp_path):
    from alice.voice.tts import leaked

    assert leaked("Style: serious, composed. Yedekleme yarıda kaldı.", "Yedekleme yarıda kaldı.")
    assert leaked("Ciddi bir sesle söyle yedekleme yarıda kaldı hedef diskte yer yok", "Yedekleme yarıda kaldı.")
    assert not leaked("Yedekleme yarıda kaldı.", "Yedekleme yarıda kaldı.")
    assert not leaked("yedeklemeler yarıda kaldı", "Yedekleme yarıda kaldı.")  # küçük Whisper farkı

    pcm = tone(0.2, 24000).pcm
    prompts = []

    def handler(req):
        prompts.append(json.loads(req.content)["contents"][0]["parts"][0]["text"])
        return httpx.Response(200, json={"candidates": [{"content": {"parts": [{"inlineData": {
            "mimeType": "audio/L16;rate=24000", "data": base64.b64encode(pcm).decode()}}]}}]})

    seen = []
    verify = lambda audio, text: (seen.append(prompts[-1]) or not prompts[-1].startswith("Synthesize"))  # noqa: E731
    f = tmp_path / "fmt.json"
    eng = GeminiTTS("k", "Kore", ["m1"], client=httpx.Client(transport=httpx.MockTransport(handler)),
                    verify=verify, formats_file=f)
    eng.synth("Yedekleme yarıda kaldı.", "serious")
    assert eng.last_format == "tag" and prompts[1] == "[serious, composed] Yedekleme yarıda kaldı."
    assert json.loads(f.read_text()) == {"m1": ["notes"]}
    eng2 = GeminiTTS("k", "Kore", ["m1"], client=httpx.Client(transport=httpx.MockTransport(handler)), formats_file=f)
    eng2.synth("Merhaba.", "calm")
    assert eng2.last_format == "tag" and prompts[-1].startswith("[calm")  # kayıtlı iyi biçim, tek istek
