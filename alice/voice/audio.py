"""Ses verisi: 16 bit mono PCM (bytes) + örnekleme hızı. numpy gerektirmez.

Çalma: sounddevice varsa onunla (anında durdurulabilir); yoksa Windows'ta winsound,
Linux'ta paplay/aplay, macOS'ta afplay ile geçici WAV dosyasından.
"""

from __future__ import annotations

import array
import io
import math
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import wave
from dataclasses import dataclass


@dataclass
class Audio:
    pcm: bytes          # int16 little-endian mono
    rate: int = 24000

    @property
    def seconds(self) -> float:
        return len(self.pcm) / 2 / self.rate

    def to_wav(self) -> bytes:
        buf = io.BytesIO()
        with wave.open(buf, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(self.rate)
            w.writeframes(self.pcm)
        return buf.getvalue()

    def save(self, path) -> None:
        with open(path, "wb") as f:
            f.write(self.to_wav())

    @classmethod
    def from_wav(cls, data: bytes) -> "Audio":
        with wave.open(io.BytesIO(data), "rb") as w:
            ch, width, rate = w.getnchannels(), w.getsampwidth(), w.getframerate()
            frames = w.readframes(w.getnframes())
        if width != 2:
            raise ValueError("sadece 16 bit WAV destekleniyor")
        if ch > 1:
            a = array.array("h", frames)
            frames = array.array("h", (sum(a[i:i + ch]) // ch for i in range(0, len(a), ch))).tobytes()
        return cls(frames, rate)

    @classmethod
    def load(cls, path) -> "Audio":
        with open(path, "rb") as f:
            return cls.from_wav(f.read())

    @classmethod
    def from_floats(cls, samples, rate: int) -> "Audio":
        a = array.array("h", (max(-32768, min(32767, int(float(s) * 32767))) for s in samples))
        return cls(a.tobytes(), rate)

    @classmethod
    def silence(cls, seconds: float, rate: int = 24000) -> "Audio":
        return cls(b"\x00\x00" * int(seconds * rate), rate)

    def __add__(self, other: "Audio") -> "Audio":
        if other.rate != self.rate:
            other = other.resample(self.rate)
        return Audio(self.pcm + other.pcm, self.rate)

    def polish(self, fade_in: float = 0.005, fade_out: float = 0.05, pad: float = 0.15,
               rel_floor: float = 0.06, max_trim: float = 2.0) -> "Audio":
        """Sesin başını/sonunu yumuşat: sondaki gürültü/patlama kuyruğunu kes, kısa fade ve sessizlik ekle.
        Aniden biten PCM hoparlörde "tık" ya da cızırtı yapar. Kuyruk eşiği sesin en yüksek yerine göredir
        (konuşmanın %6'sı altı kuyruk sayılır), böylece sondaki hafif ama sessiz olmayan cızırtı da kesilir."""
        a = array.array("h", self.pcm[: len(self.pcm) // 2 * 2])
        if not a:
            return self
        win = max(1, int(self.rate * 0.02))
        levels = [math.sqrt(sum(x * x for x in a[i:i + win]) / max(1, len(a[i:i + win])))
                  for i in range(0, len(a), win)]
        peak = max(levels) or 1.0
        thr = max(0.004 * 32768, peak * rel_floor)
        last = len(levels) - 1
        min_last = max(0, len(levels) - int(max_trim / 0.02))
        while last > min_last and levels[last] < thr:
            last -= 1
        end = min(len(a), (last + 1) * win + int(self.rate * 0.06))
        a = a[:end]
        n_in, n_out = min(len(a), int(self.rate * fade_in)), min(len(a), int(self.rate * fade_out))
        for i in range(n_in):
            a[i] = int(a[i] * i / n_in)
        for i in range(n_out):
            j = len(a) - n_out + i
            a[j] = int(a[j] * (1 - (i + 1) / n_out))
        return Audio(a.tobytes() + b"\x00\x00" * int(self.rate * pad), self.rate)

    def resample(self, rate: int) -> "Audio":
        """Doğrusal ara değerleme; konuşma için yeterli."""
        if rate == self.rate or not self.pcm:
            return Audio(self.pcm, rate)
        src = array.array("h", self.pcm)
        n = int(len(src) * rate / self.rate)
        ratio = self.rate / rate
        out = array.array("h", bytes(2 * n))
        last = len(src) - 1
        for i in range(n):
            x = i * ratio
            j = int(x)
            f = x - j
            out[i] = int(src[j] * (1 - f) + src[min(j + 1, last)] * f)
        return Audio(out.tobytes(), rate)


def rms(pcm: bytes) -> float:
    """0..1 arası ses yüksekliği."""
    if len(pcm) < 2:
        return 0.0
    a = array.array("h", pcm[: len(pcm) // 2 * 2])
    return math.sqrt(sum(s * s for s in a) / len(a)) / 32768


# ----------------------------------------------------------------------- çalma
class Player:
    """Tek bir sesi çalar; stop() ile hemen susturulabilir. play() bitene kadar bloklar."""

    def __init__(self):
        self._stop = threading.Event()
        self._proc: subprocess.Popen | None = None
        try:
            import sounddevice  # noqa: F401
            self.backend = "sounddevice"
        except (ImportError, OSError):
            self.backend = "winsound" if sys.platform == "win32" else self._cli()

    @staticmethod
    def _cli() -> str:
        for name in ("paplay", "aplay", "afplay"):
            if shutil.which(name):
                return name
        return "none"

    @property
    def available(self) -> bool:
        return self.backend != "none"

    def stop(self) -> None:
        self._stop.set()
        if self.backend == "sounddevice":
            try:
                import sounddevice as sd

                sd.stop()
            except Exception:
                pass
        elif self.backend == "winsound":
            import winsound

            winsound.PlaySound(None, winsound.SND_PURGE)
        if self._proc and self._proc.poll() is None:
            self._proc.kill()

    def play(self, audio: Audio) -> bool:
        """Sesi çal. Sonuna kadar çaldıysa True, durdurulduysa False."""
        self._stop.clear()
        if self.backend == "sounddevice":
            import sounddevice as sd

            with sd.RawOutputStream(samplerate=audio.rate, channels=1, dtype="int16") as out:
                chunk = audio.rate // 10 * 2  # 100 ms
                for i in range(0, len(audio.pcm), chunk):
                    if self._stop.is_set():
                        return False
                    out.write(audio.pcm[i:i + chunk])
            return not self._stop.is_set()
        if self.backend == "none":
            return False
        fd, path = tempfile.mkstemp(suffix=".wav")
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(audio.to_wav())
            if self.backend == "winsound":
                import winsound

                winsound.PlaySound(path, winsound.SND_FILENAME | winsound.SND_ASYNC)
                self._stop.wait(audio.seconds + 0.1)
            else:
                self._proc = subprocess.Popen([self.backend, path], stdout=subprocess.DEVNULL,
                                              stderr=subprocess.DEVNULL)
                while self._proc.poll() is None and not self._stop.wait(0.05):
                    pass
            return not self._stop.is_set()
        finally:
            try:
                os.unlink(path)
            except OSError:
                pass


# -------------------------------------------------------------------- mikrofon
class Microphone:
    """Mikrofondan 16 kHz, 30 ms'lik PCM kareleri okur (sounddevice gerekir)."""

    RATE = 16000
    FRAME = 480  # 30 ms

    TARGET_PEAK = 0.35  # otomatik kazanç: konuşmanın tepe seviyesi bu civara getirilir
    MAX_GAIN = 16.0

    def __init__(self, device: int | str | None = None, auto_gain: bool = True):
        import sounddevice as sd

        self._sd = sd
        self.device = device
        self.auto_gain = auto_gain
        self.gain = 1.0
        self._peak = 0.0
        self._stream = None
        self.name = ""

    def _open(self):
        sd = self._sd
        if self.device is None and sys.platform == "win32":
            # Windows'un varsayılanı MME: aynı mikrofon WASAPI'den ~20 kat güçlü geliyor (Razer'da ölçüldü).
            wasapi = wasapi_twin(sd)
            if wasapi is not None:
                try:
                    st = sd.RawInputStream(samplerate=self.RATE, channels=1, dtype="int16", blocksize=self.FRAME,
                                           device=wasapi, extra_settings=sd.WasapiSettings(auto_convert=True))
                    self.name = f"{sd.query_devices(wasapi)['name']} (WASAPI)"
                    return st
                except Exception:  # noqa: BLE001 - açılmazsa varsayılan
                    pass
        st = sd.RawInputStream(samplerate=self.RATE, channels=1, dtype="int16", blocksize=self.FRAME,
                               device=self.device)
        try:
            self.name = str(sd.query_devices(self.device, "input")["name"])
        except Exception:  # noqa: BLE001
            self.name = str(self.device or "varsayılan")
        return st

    def __enter__(self) -> "Microphone":
        self._stream = self._open()
        self._stream.start()
        return self

    def __exit__(self, *exc) -> None:
        if self._stream is not None:
            try:
                self._stream.stop()
                self._stream.close()
            except Exception:  # noqa: BLE001
                pass
            self._stream = None

    def reopen(self) -> None:
        """Kulaklık uyuyup geri gelince eski akış sıfır vermeye devam ediyor: kapat, yeniden aç."""
        self.__exit__()
        self.__enter__()

    def read(self) -> bytes:
        data, _ = self._stream.read(self.FRAME)
        data = bytes(data)
        return self._apply_gain(data) if self.auto_gain else data

    def _apply_gain(self, data: bytes) -> bytes:
        a = array.array("h", data)
        if not a:
            return data
        peak = max(abs(min(a)), max(a)) / 32768
        # Tepe izleyici: hızlı yüksel, yavaş (~10 sn) düş. Kazanç sadece konuşma seviyesine göre ayarlanır.
        self._peak = peak if peak > self._peak else self._peak * 0.997
        if self._peak > 0.003:
            want = min(self.MAX_GAIN, max(1.0, self.TARGET_PEAK / self._peak))
            self.gain += (want - self.gain) * 0.05
        if self.gain <= 1.01:
            return data
        g = self.gain
        out = array.array("h", (max(-32768, min(32767, int(s * g))) for s in a))
        return out.tobytes()


def wasapi_twin(sd) -> int | None:
    """Varsayılan giriş aygıtının WASAPI'deki karşılığı (MME adları 31 karakterde kesilir)."""
    try:
        default = sd.default.device[0]
        if default is None or default < 0:
            default = sd.query_devices(kind="input")["index"]
        info = sd.query_devices(default)
        apis = sd.query_hostapis()
        if "WASAPI" in apis[info["hostapi"]]["name"]:
            return None  # zaten WASAPI
        name = info["name"].strip()[:31].lower()
        for i, d in enumerate(sd.query_devices()):
            if (d.get("max_input_channels", 0) > 0 and "WASAPI" in apis[d["hostapi"]]["name"]
                    and d["name"].lower().startswith(name)):
                return i
    except Exception:  # noqa: BLE001
        return None
    return None


def mic_available() -> bool:
    try:
        import sounddevice as sd

        return any(d.get("max_input_channels", 0) > 0 for d in sd.query_devices())
    except Exception:
        return False
