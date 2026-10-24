"""Konuşmacı: cümleleri sıraya alır, bir sonrakini sentezlerken öncekini çalar.

stop() kuyruğu boşaltır ve çalan sesi hemen keser (araya girildiğinde). Sentez ve çalma
ayrı thread'lerde olduğu için ilk cümle hazır olur olmaz konuşma başlar.
"""

from __future__ import annotations

import queue
import threading
from typing import Callable

from alice.voice.audio import Audio, Player
from alice.voice.tone import drop_emoji, for_speech, pick


class Speaker:
    def __init__(self, tts, player: Player | None = None, on_error: Callable[[str], None] = lambda s: None,
                 enabled: Callable[[], bool] = lambda: True):
        self.tts = tts
        self.player = player or Player()
        self.on_error = on_error
        self.enabled = enabled
        self._gen = 0
        self._synth_q: queue.Queue = queue.Queue()
        self._play_q: queue.Queue = queue.Queue()
        self._pending = 0
        self._cv = threading.Condition()
        self._playing = False
        threading.Thread(target=self._synth_loop, daemon=True, name="alice-tts").start()
        threading.Thread(target=self._play_loop, daemon=True, name="alice-play").start()

    # ------------------------------------------------------------------ public
    def say(self, text: str, emotion: str | None = None, severity: str = "", topic: str = "") -> bool:
        """Kuyruğa ekle. Ses kapalıysa (ör. sessiz mod) False."""
        text = for_speech(text)
        emotion = emotion or pick(text, severity, topic)  # emoji (😄) tonu belirler, sonra atılır
        text = drop_emoji(text)
        if not text or not self.enabled():
            return False
        with self._cv:
            self._pending += 1
            gen = self._gen
        self._synth_q.put((gen, text, emotion))
        return True

    def stop(self) -> None:
        """Sus: kuyruğu boşalt, çalanı kes."""
        with self._cv:
            self._gen += 1
            self._pending = 0
            self._cv.notify_all()
        for q in (self._synth_q, self._play_q):
            try:
                while True:
                    q.get_nowait()
            except queue.Empty:
                pass
        self.player.stop()

    @property
    def speaking(self) -> bool:
        with self._cv:
            return self._playing or self._pending > 0

    def wait(self, timeout: float | None = None) -> bool:
        """Söylenecek her şey bitene kadar bekle."""
        with self._cv:
            return self._cv.wait_for(lambda: self._pending == 0 and not self._playing, timeout)

    # ---------------------------------------------------------------- thread'ler
    def _done_one(self, gen: int) -> None:
        with self._cv:
            if gen == self._gen and self._pending > 0:
                self._pending -= 1
            self._cv.notify_all()

    def _synth_loop(self) -> None:
        while True:
            gen, text, emotion = self._synth_q.get()
            if gen != self._gen:
                continue
            try:
                audio: Audio | None = self.tts.synth(text, emotion)
            except Exception as e:
                self.on_error(f"ses üretilemedi: {e}")
                audio = None
            if gen != self._gen:
                continue
            if audio is None:
                self._done_one(gen)
                continue
            self._play_q.put((gen, audio))

    def _play_loop(self) -> None:
        while True:
            gen, audio = self._play_q.get()
            if gen != self._gen:
                continue
            with self._cv:
                self._playing = True
            try:
                self.player.play(audio)
            except Exception as e:
                self.on_error(f"ses çalınamadı: {e}")
            finally:
                with self._cv:
                    self._playing = False
                self._done_one(gen)
