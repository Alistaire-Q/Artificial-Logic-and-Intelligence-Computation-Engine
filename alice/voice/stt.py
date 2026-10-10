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


_DLLS_DONE = False


def add_cuda_dlls() -> None:
    """Windows: faster-whisper (ctranslate2) cublas64_12.dll/cudnn'i bulamıyor; PyTorch ve nvidia-* pip
    paketlerinin içindekileri arama yoluna ekle. Diğer sistemlerde bir şey yapmaz."""
    global _DLLS_DONE
    if _DLLS_DONE or os.name != "nt":
        return
    _DLLS_DONE = True
    import importlib.util
    from pathlib import Path

    dirs: list[Path] = []
    spec = importlib.util.find_spec("torch")
    if spec and spec.origin:
        dirs.append(Path(spec.origin).parent / "lib")
    spec = importlib.util.find_spec("nvidia")
    for root in (spec.submodule_search_locations or []) if spec else []:
        dirs += list(Path(root).glob("*/bin"))
    for d in dirs:
        if d.is_dir():
            try:
                os.add_dll_directory(str(d))  # type: ignore[attr-defined]
            except OSError:
                pass
            os.environ["PATH"] = str(d) + os.pathsep + os.environ.get("PATH", "")


def _cuda_ok(model) -> bool:
    """Model kuruldu diye CUDA çalışıyor sanma: DLL hatası ilk yazıya çevirmede çıkıyor. Yarım saniyelik
    sessizlikle dene (ilk CUDA çağrısı ~2,5 sn sürer, böylece ısınma da olur)."""
    try:
        segs, _ = model.transcribe(io.BytesIO(Audio.silence(0.5, 16000).to_wav()), beam_size=1)
        list(segs)
        return True
    except Exception:  # noqa: BLE001
        return False


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

    def words(self, audio: Audio) -> list[tuple[str, float, float]]:
        """Kelime zamanlarıyla yazıya çevir: [(kelime, başlangıç sn, bitiş sn)]."""
        data = {"model": self.model, "language": self.language, "response_format": "verbose_json",
                "temperature": "0", "timestamp_granularities[]": ["word"]}
        try:
            r = self.client.post(self.URL, headers={"Authorization": f"Bearer {self.api_key}"}, data=data,
                                 files={"file": ("speech.wav", audio.resample(16000).to_wav(), "audio/wav")})
        except httpx.HTTPError as e:
            raise STTError(f"groq bağlantı hatası: {e}") from e
        if r.status_code != 200:
            raise STTError(f"groq {r.status_code}: {r.text[:200]}")
        return [(str(w.get("word", "")), float(w.get("start", 0)), float(w.get("end", 0)))
                for w in r.json().get("words", []) or []]

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
        if gpu and "cuda" not in self._models and not self._models.get("cuda_failed"):
            add_cuda_dlls()
            try:
                m = WhisperModel(self.gpu_model, device="cuda", compute_type="float16")
                if _cuda_ok(m):
                    self._models["cuda"] = m
                else:
                    self._models["cuda_failed"] = True
            except Exception:  # noqa: BLE001 - CUDA yoksa işlemciye düş
                self._models["cuda_failed"] = True
        if gpu and "cuda" in self._models:
            return self._models["cuda"]
        if "cpu" not in self._models:
            self._models["cpu"] = WhisperModel(self.cpu_model, device="cpu", compute_type="int8")
        return self._models["cpu"]

    def transcribe(self, audio: Audio) -> str:
        return self.transcribe_file(audio.resample(16000).to_wav(), "speech.wav")

    def transcribe_file(self, data: bytes, filename: str) -> str:
        # faster-whisper, PyAV ile wav/webm/ogg hepsini çözer.
        with self._lock:
            segments, _ = self._model().transcribe(io.BytesIO(data), language=self.language, vad_filter=True,
                                                   beam_size=1, initial_prompt=self.prompt or None)
            return " ".join(s.text.strip() for s in segments).strip()


class WakeGate:
    """Yerel küçük Whisper ile sadece cümlenin başına bakıp "Hey Alice" var mı diye kontrol eder.

    Böylece odadaki her konuşma Groq'a gönderilmez (kota ve gecikme); uyandırma yoksa ses hiç
    buluta gitmez. faster-whisper kurulu değilse available() False döner ve eski davranış sürer."""

    HEAD_SECONDS = 2.5

    def __init__(self, model: str = "base", language: str = "tr", gpu_model: str = "",
                 allow_gpu: Callable[[], bool] = lambda: False):
        self.model_name = model
        self.gpu_model = gpu_model  # ekran kartı varsa daha iyi duyan model (ör. small): ~0,1-0,2 sn
        self.allow_gpu = allow_gpu
        self.device = ""
        self.last_seconds = 0.0
        self.language = language
        self._model: Any = None
        self._failed = False
        self._lock = threading.Lock()

    def available(self) -> bool:
        if self._failed:
            return False
        try:
            import faster_whisper  # noqa: F401
        except ImportError:
            self._failed = True
            return False
        return True

    def _load(self):
        from faster_whisper import WhisperModel

        if self.gpu_model and self.allow_gpu():
            add_cuda_dlls()
            try:
                m = WhisperModel(self.gpu_model, device="cuda", compute_type="float16")
                if _cuda_ok(m):
                    self.device, self.model_name = "cuda", self.gpu_model
                    return m
            except Exception:  # noqa: BLE001 - CUDA yoksa işlemcide küçük model
                pass
        self.device = "cpu"
        return WhisperModel(self.model_name, device="cpu", compute_type="int8")

    def warm_up(self) -> None:
        """Modeli arka planda yükle: ilk "Hey Alice" model yüklemesini beklemesin."""
        def run() -> None:
            if self.available():
                with self._lock:
                    if self._model is None:
                        try:
                            self._model = self._load()
                        except Exception:  # noqa: BLE001
                            self._failed = True

        threading.Thread(target=run, daemon=True, name="alice-wake-warm").start()

    def heard(self, audio: Audio) -> str | None:
        """Cümlenin başının yerel yazıya dökümü; yerel model kullanılamıyorsa None."""
        if not self.available():
            return None
        head = Audio(audio.pcm[: int(self.HEAD_SECONDS * audio.rate) * 2], audio.rate).resample(16000)
        t0 = time.monotonic()
        try:
            with self._lock:
                if self._model is None:
                    self._model = self._load()
                segments, _ = self._model.transcribe(io.BytesIO(head.to_wav()), language=self.language,
                                                     beam_size=1, initial_prompt="Hey Alice.",
                                                     condition_on_previous_text=False)
                text = " ".join(s.text.strip() for s in segments).strip()
                self.last_seconds = time.monotonic() - t0
                return text
        except Exception:  # noqa: BLE001 - yerel model bozuksa buluta düş
            self._failed = True
            return None


class STTRouter:
    def __init__(self, engines: list[STT], cooldown: float = 300):
        self.wake_gate: WakeGate | None = None
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


# Whisper sessizlikte/gürültüde YouTube altyazılarından ezberlediği cümleleri uydurur ("Altyazı M.K." gibi).
HALLUCINATIONS = ("altyazı", "alt yazı", "m.k.", "abone ol", "izlediğiniz için", "izlediğin için", "teşekkürler",
                  "teşekkür ederim", "bir sonraki videoda", "videoyu beğen", "kanalıma", "hoşça kalın",
                  "amara.org", "subtitles by", "thanks for watching", "www.", "sesli betimleme")


def is_hallucination(text: str) -> bool:
    t = text.lower().strip(" .,!?…\"'")
    if not t:
        return True
    if "alice" in t:
        return False
    if len(t) > 3 and t in PROMPT.lower():  # ipucu metnini aynen geri vermiş
        return True
    return len(t) < 60 and any(h in t for h in HALLUCINATIONS)


# İsimlerin doğru yazılması için Whisper'a ipucu.
PROMPT = "Hey Alice. ALICE, DCS, F-16, Unity, War Thunder, bingo, waypoint, kneeboard."


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
    router = STTRouter(engines)
    gate_model = v.get("wake_local", "base")
    if gate_model:
        router.wake_gate = WakeGate(gate_model, lang, gpu_model=str(v.get("wake_local_gpu", "small") or ""),
                                    allow_gpu=lambda: core_policy().get("local_gpu", True) is not False)
    return router
