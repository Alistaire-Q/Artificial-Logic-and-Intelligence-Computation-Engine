"""Konuşmayı yazıya çevirme (Whisper).

Bulut: Groq'taki whisper-large-v3-turbo (hızlı, ücretsiz katman, GPU'ya dokunmaz).
Yerel: faster-whisper (aynı modelin hızlı ve hafif sürümü). İnternet ya da kota yoksa
devreye girer; uçuşta GPU kullanmaz, işlemcide küçük modelle çalışır.
"""

from __future__ import annotations

import io
import os
import threading
import time
from typing import Any, Callable, Protocol

import httpx

from alice.config import Config
from alice.voice.audio import Audio


class STTError(Exception):
    pass


class STT(Protocol):
    name: str

    def transcribe(self, audio: Audio) -> str: ...


class GroqWhisper:
    URL = "https://api.groq.com/openai/v1/audio/transcriptions"

    def __init__(self, api_key: str, model: str = "whisper-large-v3-turbo", language: str = "tr",
                 client: httpx.Client | None = None, prompt: str = ""):
        self.api_key = api_key
        self.model = model
        self.language = language
        self.prompt = prompt
        self.name = f"groq:{model}"
        self.client = client or httpx.Client(timeout=30)

    def transcribe(self, audio: Audio) -> str:
        data = {"model": self.model, "language": self.language, "response_format": "json", "temperature": "0"}
        if self.prompt:
            data["prompt"] = self.prompt
        try:
            r = self.client.post(self.URL, headers={"Authorization": f"Bearer {self.api_key}"}, data=data,
                                 files={"file": ("speech.wav", audio.resample(16000).to_wav(), "audio/wav")})
        except httpx.HTTPError as e:
            raise STTError(f"groq bağlantı hatası: {e}") from e
        if r.status_code != 200:
            raise STTError(f"groq {r.status_code}: {r.text[:200]}")
        return str(r.json().get("text", "")).strip()

    def transcribe_file(self, data: bytes, filename: str) -> str:
        """Tarayıcıdan gelen webm/ogg gibi kayıtlar (Groq kendisi çözer)."""
        try:
            r = self.client.post(self.URL, headers={"Authorization": f"Bearer {self.api_key}"},
                                 data={"model": self.model, "language": self.language, "response_format": "json",
                                       "temperature": "0", **({"prompt": self.prompt} if self.prompt else {})},
                                 files={"file": (filename, data, "application/octet-stream")})
        except httpx.HTTPError as e:
            raise STTError(f"groq bağlantı hatası: {e}") from e
        if r.status_code != 200:
            raise STTError(f"groq {r.status_code}: {r.text[:200]}")
        return str(r.json().get("text", "")).strip()


class LocalWhisper:
    """faster-whisper. GPU izinliyse large-v3-turbo, değilse işlemcide küçük model."""

    def __init__(self, gpu_model: str = "large-v3-turbo", cpu_model: str = "small", language: str = "tr",
                 allow_gpu: Callable[[], bool] = lambda: True, prompt: str = ""):
        self.gpu_model = gpu_model
        self.cpu_model = cpu_model
        self.language = language
        self.allow_gpu = allow_gpu
        self.prompt = prompt
        self.name = "local-whisper"
        self._models: dict[str, Any] = {}
        self._lock = threading.Lock()

    def _model(self):
        try:
            from faster_whisper import WhisperModel
        except ImportError as e:
            raise STTError("faster-whisper kurulu değil: pip install faster-whisper") from e
        gpu = False
        if self.allow_gpu():
            try:
                import ctranslate2

                gpu = ctranslate2.get_cuda_device_count() > 0
            except (ImportError, RuntimeError):
                gpu = False
        key = "cuda" if gpu else "cpu"
        if key not in self._models:
            self._models[key] = (WhisperModel(self.gpu_model, device="cuda", compute_type="float16") if gpu
                                 else WhisperModel(self.cpu_model, device="cpu", compute_type="int8"))
        return self._models[key]

    def transcribe(self, audio: Audio) -> str:
        return self.transcribe_file(audio.resample(16000).to_wav(), "speech.wav")

    def transcribe_file(self, data: bytes, filename: str) -> str:
        # faster-whisper, PyAV ile wav/webm/ogg hepsini çözer.
        with self._lock:
            segments, _ = self._model().transcribe(io.BytesIO(data), language=self.language, vad_filter=True,
                                                   beam_size=1, initial_prompt=self.prompt or None)
            return " ".join(s.text.strip() for s in segments).strip()


class STTRouter:
    def __init__(self, engines: list[STT], cooldown: float = 300):
        self.engines = engines
        self.cooldown = cooldown
        self.down: dict[str, float] = {}
        self.last = ""

    def transcribe(self, audio: Audio) -> str:
        return self._try(lambda e: e.transcribe(audio))

    def transcribe_file(self, data: bytes, filename: str) -> str:
        return self._try(lambda e: e.transcribe_file(data, filename))

    def _try(self, fn) -> str:
        errors = []
        now = time.monotonic()
        for e in self.engines:
            if self.down.get(e.name, 0) > now:
                continue
            try:
                text = fn(e)
                self.last = e.name
                return text
            except STTError as ex:
                self.down[e.name] = now + self.cooldown
                errors.append(f"{e.name}: {ex}")
        raise STTError("; ".join(errors) or "konuşma tanıma motoru yok")


# İsimlerin doğru yazılması için Whisper'a ipucu.
PROMPT = "ALICE, DCS, F-16, Unity, War Thunder, bingo, waypoint, kneeboard."


def make_stt(cfg: Config) -> STTRouter:
    from alice.voice.tts import core_policy

    v = cfg.data.get("voice", {})
    lang = v.get("language", "tr")
    engines: list[STT] = []
    for ref in v.get("stt", ["groq:whisper-large-v3-turbo", "local:large-v3-turbo"]):
        kind, _, model = ref.partition(":")
        if kind == "groq":
            env = cfg.data.get("providers", {}).get("groq", {}).get("api_key_env", "GROQ_API_KEY")
            key = os.environ.get(env, "")
            if key:
                engines.append(GroqWhisper(key, model or "whisper-large-v3-turbo", lang, prompt=PROMPT))
        elif kind == "local":
            engines.append(LocalWhisper(model or "large-v3-turbo", v.get("stt_cpu_model", "small"), lang,
                                        allow_gpu=lambda: core_policy().get("local_gpu", True) is not False,
                                        prompt=PROMPT))
    return STTRouter(engines)
