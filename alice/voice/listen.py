"""Dinleme: konuşma algılama, bas-konuş ve "Hey Alice".

VAD: webrtcvad kuruluysa o, değilse ortam gürültüsüne uyum sağlayan enerji eşiği.
Uyanma kelimesi: openWakeWord modeli verildiyse o (önerilen, tamamen yerel). Yoksa kısa
bir konuşma algılanınca Whisper'a çevrilip "alice" ile başlayıp başlamadığına bakılır.
Bas-konuş: pynput ile global kısayol; tuş basılıyken kaydedilir.
"""

from __future__ import annotations

import re
import threading
from collections import deque
from typing import Callable, Iterator

from alice.voice.audio import Audio, Microphone, rms


class VAD:
    """30 ms'lik 16 kHz kareler için 'konuşma var mı?'"""

    def __init__(self, aggressiveness: int = 2, threshold: float = 0.0):
        self.noise = 0.004
        self.threshold = threshold  # 0 = uyarlanır
        self.boost = 1.0            # ALICE konuşurken yankıyı konuşma sanmamak için yükseltilir
        try:
            import webrtcvad

            self._vad = webrtcvad.Vad(aggressiveness)
        except ImportError:
            self._vad = None

    def is_speech(self, frame: bytes, rate: int = 16000) -> bool:
        level = rms(frame)
        floor = self.threshold or max(0.01, self.noise * 3.0)
        loud = level > floor * self.boost
        if self._vad is not None:
            try:
                speech = self._vad.is_speech(frame, rate)
            except Exception:
                speech = loud
            # webrtcvad yankıya karşı da hassas; ALICE konuşurken enerji eşiğini de iste.
            result = speech and (self.boost <= 1.0 or loud)
        else:
            result = loud
        if not result:
            self.noise = self.noise * 0.95 + level * 0.05  # gürültü tabanını yavaşça izle
        return result


def utterances(frames: Iterator[bytes], vad: VAD, start_frames: int = 6, end_silence: float = 0.8,
               max_seconds: float = 30, preroll: int = 10, rate: int = 16000, frame_len: int = 480,
               stop: Callable[[], bool] = lambda: False, on_start: Callable[[], None] = lambda: None
               ) -> Iterator[Audio]:
    """Kare akışından tek tek konuşmaları çıkar. Konuşma başlayınca on_start çağrılır (araya girme)."""
    ring: deque[bytes] = deque(maxlen=preroll)
    voiced = 0
    buf: list[bytes] | None = None
    silent = 0
    end_frames = int(end_silence * rate / frame_len)
    max_frames = int(max_seconds * rate / frame_len)
    for frame in frames:
        if stop():
            return
        speech = vad.is_speech(frame, rate)
        if buf is None:
            ring.append(frame)
            voiced = voiced + 1 if speech else 0
            if voiced >= start_frames:
                buf = list(ring)
                silent = 0
                on_start()
            continue
        buf.append(frame)
        silent = 0 if speech else silent + 1
        if silent >= end_frames or len(buf) >= max_frames:
            yield Audio(b"".join(buf[: len(buf) - silent + 3]), rate)
            buf, voiced = None, 0
            ring.clear()


_WAKE = re.compile(r"^\W*(hey|hay|hei|ey|hi)?\W*(alice|alis|alıs|elis|ales|alise)\b\W*", re.I)


def strip_wake(text: str) -> str | None:
    """'Hey Alice, saat kaç?' -> 'saat kaç?'. Uyandırma yoksa None."""
    m = _WAKE.match(text.strip())
    if not m:
        return None
    return text.strip()[m.end():].strip()


class WakeWord:
    """openWakeWord modeli (ör. kendi eğittiğin hey_alice.onnx). Kare başına skor."""

    def __init__(self, model_path: str, threshold: float = 0.5):
        from openwakeword.model import Model

        self.model = Model(wakeword_models=[model_path], inference_framework="onnx")
        self.threshold = threshold
        self._buf = b""

    def detect(self, frame: bytes) -> bool:
        # openWakeWord 80 ms (1280 örnek) parçalar ister.
        import numpy as np

        self._buf += frame
        hit = False
        while len(self._buf) >= 2560:
            chunk, self._buf = self._buf[:2560], self._buf[2560:]
            scores = self.model.predict(np.frombuffer(chunk, dtype=np.int16))
            hit = hit or any(v >= self.threshold for v in scores.values())
        if hit:
            self.model.reset()
        return hit


class PushToTalk:
    """Global kısayol: basılıyken pressed True. pynput gerekir."""

    def __init__(self, key: str = "<scroll_lock>"):
        from pynput import keyboard

        self.pressed = threading.Event()
        self.released = threading.Event()
        name = key.strip("<>").lower()
        self._target = getattr(keyboard.Key, name, None) or keyboard.KeyCode.from_char(name[:1])

        def on_press(k):
            if k == self._target and not self.pressed.is_set():
                self.released.clear()
                self.pressed.set()

        def on_release(k):
            if k == self._target:
                self.pressed.clear()
                self.released.set()

        self._listener = keyboard.Listener(on_press=on_press, on_release=on_release)
        self._listener.daemon = True
        self._listener.start()

    def record(self, mic: Microphone) -> Audio:
        """Tuş bırakılana kadar kaydet."""
        frames = []
        while self.pressed.is_set():
            frames.append(mic.read())
        return Audio(b"".join(frames), mic.RATE)

    def stop(self) -> None:
        self._listener.stop()
