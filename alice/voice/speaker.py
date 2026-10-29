"""Konuşmacı: cümleleri sıraya alır, bir sonrakini sentezlerken öncekini çalar.

stop() kuyruğu boşaltır ve çalan sesi hemen keser (araya girildiğinde). Sentez ve çalma
ayrı thread'lerde olduğu için ilk cümle hazır olur olmaz konuşma başlar.
"""

from __future__ import annotations

import queue
import threading
import time
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
        self._now = ""  # çalınan cümle
        self._making: tuple[int, str] | None = None  # sentezlenen cümle (henüz hiçbir kuyrukta değil)
        # Son stop()'ta söylenemeyenler: {"playing": yarıda kesilen cümle, "pending": hiç söylenmeyenler}
        self.last_cut: dict[str, object] | None = None
        self._expect: tuple[float, Callable[[float], None]] | None = None  # ilk sesin gecikmesini ölçmek için
        threading.Thread(target=self._synth_loop, daemon=True, name="alice-tts").start()
        threading.Thread(target=self._play_loop, daemon=True, name="alice-play").start()

    # ------------------------------------------------------------------ public
    def expect(self, since: float | None, on_first: Callable[[float], None] | None = None) -> None:
        """Sıradaki ilk ses çalmaya başlayınca on_first(since'ten beri geçen sn) çağrılsın; since None: vazgeç."""
        with self._cv:
            self._expect = (since, on_first) if since is not None and on_first is not None else None

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
        """Sus: kuyruğu boşalt, çalanı kes. Söylenemeyenler last_cut'a yazılır."""
        with self._cv:
            gen = self._gen
            self._gen += 1
            self._pending = 0
            playing = self._now if self._playing else ""
            self._cv.notify_all()
        making = self._making
        pending: list[tuple[int, str]] = []
        # Söylenme sırası: sentezlenip çalınmayı bekleyenler, sentezlenmekte olan, sentez sırasındakiler.
        for q in (self._play_q, None, self._synth_q):
            if q is None:
                pending += [making] if making is not None else []
                continue
            try:
                while True:
                    item = q.get_nowait()
                    pending.append((item[0], item[1]))
            except queue.Empty:
                pass
        self.player.stop()
        rest = [t for g, t in pending if g == gen and isinstance(t, str)]
        self.last_cut = {"playing": playing, "pending": rest} if playing or rest else None

    def prewarm(self, items: list[tuple[str, str]], limit: int = 12, log: Callable[[str], None] = lambda s: None,
                 background: bool = True) -> int:
        """Sık söylenecek sabit cümlelerin sesini şimdiden üret (önbelleğe girer, sonra anında çalar). Her seferinde
        en fazla `limit` tane: kota bir anda gitmesin, eksikler sonraki açılışlarda tamamlanır. Konuşurken ya da
        uçuşta (ekran kartı oyunda, ses buluttan) beklenir/yapılmaz. Üretilen sayıyı döndürür."""
        cached = getattr(self.tts, "cached", None)
        if cached is None:
            return 0

        def run() -> int:
            made = 0
            for text, emotion in items:
                if made >= limit:
                    break
                text = drop_emoji(for_speech(text))
                policy = getattr(self.tts, "policy", None)
                if callable(policy) and policy().get("local_gpu") is False:
                    break
                try:
                    if cached(text, emotion):
                        continue
                    while self.speaking:
                        time.sleep(1)
                    self.tts.synth(text, emotion)
                except Exception as e:  # noqa: BLE001 - kota/motor yok: sonraki açılışta devam
                    log(f"hazır cümle üretilemedi: {e}")
                    break
                made += 1
            if made:
                log(f"{made} hazır cümlenin sesi önbelleğe alındı")
            return made

        if not background:
            return run()
        threading.Thread(target=run, daemon=True, name="alice-tts-prewarm").start()
        return 0

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
            self._making = (gen, text)
            try:
                audio: Audio | None = self.tts.synth(text, emotion)
            except Exception as e:
                self.on_error(f"ses üretilemedi: {e}")
                audio = None
            finally:
                self._making = None
            if gen != self._gen:
                continue
            if audio is None:
                self._done_one(gen)
                continue
            self._play_q.put((gen, text, audio))

    def _play_loop(self) -> None:
        while True:
            gen, text, audio = self._play_q.get()
            if gen != self._gen:
                continue
            with self._cv:
                self._playing = True
                self._now = text
                first, self._expect = self._expect, None
            if first is not None:
                try:
                    first[1](time.monotonic() - first[0])
                except Exception:  # noqa: BLE001 - ölçüm sesi bozmasın
                    pass
            try:
                self.player.play(audio)
            except Exception as e:
                self.on_error(f"ses çalınamadı: {e}")
            finally:
                with self._cv:
                    self._playing = False
                self._done_one(gen)
