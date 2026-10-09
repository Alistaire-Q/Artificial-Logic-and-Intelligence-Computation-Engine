"""Ses motorları.

Bulut: Gemini TTS. Sesin kendisi (ör. "Aoede") sabit kalır, ton stil talimatıyla değişir.
Yerel: XTTS v2 ve Chatterbox. İkisi de sesi bir referans kayıttan klonlar. Referans olarak
"duygu kütüphanesi" kullanılır: seçilen bulut sesinin her duyguda söylediği birer kayıt
(~/.alice/voice/library/<duygu>.wav). Böylece DCS açıkken buluttan, kapalıyken yerelden
konuşsa da hep aynı ALICE duyulur.

Seçim (TTSRouter): DCS açıkken (mod politikası local_gpu = false) yerel motor GPU'ya
dokunmaz; ya bulut kullanılır ya da yerel motor işlemcide çalışır. Hata veren motor bir
süre atlanır.
"""

from __future__ import annotations

import base64
import json
import os
import threading
import time
from pathlib import Path
from typing import Any, Callable, Protocol

import httpx

from alice.config import Config, alice_home
from alice.voice.audio import Audio
from alice.voice.tone import CHATTERBOX, EMOTIONS, NAMES_TR, STYLE

# Gemini'nin kadın sesleri (Google'ın tanımlarıyla). Dinleme testinde bunlardan seçilir.
GEMINI_VOICES = {
    "Aoede": "ferah", "Kore": "kararlı", "Leda": "genç", "Sulafat": "sıcak", "Despina": "akıcı",
    "Achernar": "yumuşak", "Vindemiatrix": "nazik", "Callirrhoe": "rahat", "Autonoe": "parlak",
}


class TTSError(Exception):
    pass


class QuotaError(TTSError):
    """Kota doldu. daily: günlük kota (yarına kadar yok); retry_after: kaç saniye sonra tekrar denenebilir."""

    def __init__(self, message: str, retry_after: float | None = None, daily: bool = False):
        super().__init__(message)
        self.retry_after = retry_after
        self.daily = daily


def gemini_quota_error(body: str) -> QuotaError:
    """Gemini'nin 429 cevabından kota türünü ve bekleme süresini çıkar."""
    import re

    retry, daily, limit = None, False, ""
    try:
        err = json.loads(body).get("error", {})
    except ValueError:
        err = {}
    for d in err.get("details", []) or []:
        t = d.get("@type", "")
        if t.endswith("RetryInfo"):
            m = re.match(r"([\d.]+)s", str(d.get("retryDelay", "")))
            retry = float(m.group(1)) if m else None
        if t.endswith("QuotaFailure"):
            for v in d.get("violations", []) or []:
                qid = str(v.get("quotaId", ""))
                daily = daily or "PerDay" in qid
                limit = limit or str(v.get("quotaValue", ""))
    if retry is None:
        m = re.search(r"retry in ([\d.]+)s", str(err.get("message", "")))
        retry = float(m.group(1)) if m else None
    kind = "günlük" if daily else "dakikalık"
    lim = f" (sınır {limit} istek)" if limit else ""
    wait = f", {retry:.0f} sn sonra tekrar denenebilir" if retry and not daily else ""
    return QuotaError(f"gemini {kind} ses kotası doldu{lim}{wait}", retry, daily)


class TTS(Protocol):
    name: str

    def synth(self, text: str, emotion: str = "calm") -> Audio: ...


def library_dir() -> Path:
    return alice_home() / "voice" / "library"


class EmotionLibrary:
    """Her duygu için bir referans kayıt. Yerel motorlar sesi buradan klonlar."""

    def __init__(self, path: Path | None = None):
        self.path = path or library_dir()

    def clip(self, emotion: str) -> Path | None:
        for e in (emotion, "calm", *EMOTIONS):
            p = self.path / f"{e}.wav"
            if p.exists():
                return p
        return None

    @property
    def source(self) -> str:
        try:
            return json.loads((self.path / "meta.json").read_text(encoding="utf-8")).get("source", "")
        except (OSError, ValueError):
            return ""

    def exists(self) -> bool:
        return self.clip("calm") is not None


# Referans cümleleri: yerel motorlar 6-15 sn temiz konuşmadan iyi klonlar.
LIBRARY_TEXT = {
    "calm": "Merhaba, ben ALICE. Bugün her şey yolunda görünüyor; bilgisayarın serin, görevlerin de zamanında "
            "bitti. Bir şeye ihtiyacın olursa buradayım.",
    "happy": "Harika haber! Derleme ilk denemede geçti ve bütün testler yeşil. Bence bunu kutlamayı hak ettin.",
    "serious": "Bir sorun var. Yedekleme görevi yarıda kaldı çünkü hedef disk dolu. Yer açmadan tekrar "
               "denersem yine başarısız olacak.",
    "urgent": "Yakıt kritik seviyede. Mevcut tüketimle on dakikan kaldı. Eve dönmek için şimdi ayrılman gerekiyor.",
    "playful": "Yine mi gece yarısı Unity? Peki, ben kahve içemiyorum ama sana eşlik ederim. Hadi şu hatayı bulalım.",
}


def build_library(engine: "TTS", out: Path | None = None, emit: Callable[[str], None] = lambda s: None) -> Path:
    out = out or library_dir()
    out.mkdir(parents=True, exist_ok=True)
    for emo, text in LIBRARY_TEXT.items():
        emit(f"{NAMES_TR[emo]} kaydı hazırlanıyor")
        engine.synth(text, emo).save(out / f"{emo}.wav")
    (out / "meta.json").write_text(json.dumps({"source": engine.name, "created": time.time()}), encoding="utf-8")
    return out


# ------------------------------------------------------------------- Gemini
class GeminiTTS:
    URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"

    def __init__(self, api_key: str, voice: str = "Aoede", model: str = "gemini-2.5-flash-preview-tts",
                 client: httpx.Client | None = None):
        self.api_key = api_key
        self.voice = voice
        self.model = model
        self.name = f"gemini:{voice}"
        self.client = client or httpx.Client(timeout=60)

    def synth(self, text: str, emotion: str = "calm") -> Audio:
        body = {
            "contents": [{"parts": [{"text": f"{STYLE.get(emotion, STYLE['calm'])}: {text}"}]}],
            "generationConfig": {"responseModalities": ["AUDIO"], "speechConfig": {
                "voiceConfig": {"prebuiltVoiceConfig": {"voiceName": self.voice}}}},
        }
        try:
            r = self.client.post(self.URL.format(model=self.model), json=body,
                                 headers={"x-goog-api-key": self.api_key})
        except httpx.HTTPError as e:
            raise TTSError(f"gemini bağlantı hatası: {e}") from e
        if r.status_code == 429:
            raise gemini_quota_error(r.text)
        if r.status_code != 200:
            raise TTSError(f"gemini {r.status_code}: {r.text[:200]}")
        try:
            part = r.json()["candidates"][0]["content"]["parts"][0]["inlineData"]
        except (KeyError, IndexError, ValueError) as e:
            raise TTSError(f"gemini ses döndürmedi: {r.text[:200]}") from e
        rate = 24000
        mime = part.get("mimeType", "")
        if "rate=" in mime:
            try:
                rate = int(mime.split("rate=")[1].split(";")[0])
            except ValueError:
                pass
        return Audio(base64.b64decode(part["data"]), rate)


# -------------------------------------------------------------- yerel motorlar
def _device(allow_gpu: bool) -> str:
    if not allow_gpu:
        return "cpu"
    try:
        import torch

        return "cuda" if torch.cuda.is_available() else "cpu"
    except ImportError:
        return "cpu"


class XTTS:
    """Coqui XTTS v2 (pip install coqui-tts). Türkçe destekler, referans kayıttan klonlar."""

    def __init__(self, library: EmotionLibrary | None = None, allow_gpu: Callable[[], bool] = lambda: True):
        self.library = library or EmotionLibrary()
        self.allow_gpu = allow_gpu
        self.name = "xtts"
        self._model = None
        self._device = ""
        self._lock = threading.Lock()

    def _load(self):
        dev = _device(self.allow_gpu())
        if self._model is None or dev != self._device:
            try:
                from TTS.api import TTS as CoquiTTS
            except ImportError as e:
                raise TTSError("XTTS kurulu değil: pip install coqui-tts") from e
            self._model = CoquiTTS("tts_models/multilingual/multi-dataset/xtts_v2").to(dev)
            self._device = dev
        return self._model

    def synth(self, text: str, emotion: str = "calm") -> Audio:
        ref = self.library.clip(emotion)
        if ref is None:
            raise TTSError("duygu kütüphanesi yok: alice voice library")
        with self._lock:
            wav = self._load().tts(text=text, speaker_wav=str(ref), language="tr")
        return Audio.from_floats(wav, 24000)


class Chatterbox:
    """Resemble Chatterbox çok dilli (pip install chatterbox-tts). exaggeration ile duygu yoğunluğu."""

    def __init__(self, library: EmotionLibrary | None = None, allow_gpu: Callable[[], bool] = lambda: True):
        self.library = library or EmotionLibrary()
        self.allow_gpu = allow_gpu
        self.name = "chatterbox"
        self._model = None
        self._device = ""
        self._lock = threading.Lock()

    def _load(self):
        dev = _device(self.allow_gpu())
        if self._model is None or dev != self._device:
            try:
                from chatterbox.mtl_tts import ChatterboxMultilingualTTS
            except ImportError as e:
                raise TTSError("Chatterbox kurulu değil: pip install chatterbox-tts") from e
            self._model = ChatterboxMultilingualTTS.from_pretrained(device=dev)
            self._device = dev
        return self._model

    def synth(self, text: str, emotion: str = "calm") -> Audio:
        ref = self.library.clip(emotion)
        if ref is None:
            raise TTSError("duygu kütüphanesi yok: alice voice library")
        exaggeration, cfg_weight = CHATTERBOX.get(emotion, CHATTERBOX["calm"])
        with self._lock:
            model = self._load()
            wav = model.generate(text, language_id="tr", audio_prompt_path=str(ref), exaggeration=exaggeration,
                                 cfg_weight=cfg_weight)
        samples = wav.squeeze().detach().cpu().tolist() if hasattr(wav, "squeeze") else list(wav)
        return Audio.from_floats(samples, int(getattr(model, "sr", 24000)))


# --------------------------------------------------------------------- seçim
def core_policy() -> dict[str, Any]:
    from alice.core.service import read_state

    s = read_state()
    return s.get("policy", {}) if s.get("running") else {}


class TTSRouter:
    """Sıradaki uygun motoru dener. Hata veren motor `cooldown` saniye atlanır."""

    def __init__(self, cloud: TTS | None, local: TTS | None, prefer: str = "cloud",
                 policy: Callable[[], dict[str, Any]] = core_policy, cooldown: float = 300):
        self.cloud = cloud
        self.local = local
        self.prefer = prefer
        self.policy = policy
        self.cooldown = cooldown
        self.down: dict[str, float] = {}
        self.last = ""

    @property
    def name(self) -> str:
        return self.last or (self.order()[0].name if self.order() else "yok")

    def order(self) -> list[TTS]:
        engines = [self.local, self.cloud] if self.prefer == "local" else [self.cloud, self.local]
        if self.policy().get("local_gpu") is False and self.local is not None:
            # Uçuşta GPU oyuna kalsın: önce bulut; yerel ancak işlemcide, son çare olarak.
            engines = [self.cloud, self.local]
        return [e for e in engines if e is not None]

    def synth(self, text: str, emotion: str = "calm") -> Audio:
        errors = []
        now = time.monotonic()
        for e in self.order():
            if self.down.get(e.name, 0) > now:
                errors.append(f"{e.name}: bekletiliyor")
                continue
            try:
                audio = e.synth(text, emotion)
                self.last = e.name
                return audio
            except TTSError as ex:
                wait = self.cooldown
                if isinstance(ex, QuotaError):
                    wait = 3600 if ex.daily else (ex.retry_after or self.cooldown)
                self.down[e.name] = now + wait
                errors.append(f"{e.name}: {ex}")
        raise TTSError("; ".join(errors) or "ses motoru yok")


def make_cloud(cfg: Config, voice: str | None = None) -> GeminiTTS | None:
    v = cfg.data.get("voice", {})
    ref = voice or v.get("tts_cloud", "gemini:Aoede")
    provider, _, name = ref.partition(":")
    if provider != "gemini":
        return None
    key = os.environ.get(cfg.data.get("providers", {}).get("gemini", {}).get("api_key_env", "GEMINI_API_KEY"), "")
    if not key:
        return None
    return GeminiTTS(key, name or "Aoede", v.get("gemini_tts_model", "gemini-2.5-flash-preview-tts"))


def make_local(cfg: Config, name: str | None = None) -> TTS | None:
    name = name if name is not None else cfg.data.get("voice", {}).get("tts_local", "")
    allow = lambda: core_policy().get("local_gpu", True) is not False  # noqa: E731
    if name == "xtts":
        return XTTS(allow_gpu=allow)
    if name == "chatterbox":
        return Chatterbox(allow_gpu=allow)
    return None


def make_tts(cfg: Config) -> TTSRouter:
    v = cfg.data.get("voice", {})
    return TTSRouter(make_cloud(cfg), make_local(cfg), v.get("prefer", "cloud"))
