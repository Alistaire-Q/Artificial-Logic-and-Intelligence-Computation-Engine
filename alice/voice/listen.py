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
        self._levels: deque[float] = deque(maxlen=50)
        self.boost = 1.0            # ALICE konuşurken yankıyı konuşma sanmamak için yükseltilir
        try:
            import webrtcvad

            self._vad = webrtcvad.Vad(aggressiveness)
        except ImportError:
            self._vad = None

    def is_speech(self, frame: bytes, rate: int = 16000) -> bool:
        level = rms(frame)
        if self._vad is None:
            floor = self.threshold or max(0.01, self.noise * 3.0)
            result = level > floor * self.boost
            if not result:
                self.noise = self.noise * 0.95 + level * 0.05  # gürültü tabanını yavaşça izle
            return result
        # Gürültü tabanı = son 1,5 sn'nin en sessiz karesi. webrtcvad oyun/motor/fan sesini de konuşma sayıyor;
        # o zaman taban "sessiz" karelerden izlenemiyordu ve kayıt 30 sn'lik gürültü parçası oluyordu: arada
        # söylenen "Hey Alice" başta değil ortada kaldığı için kaçıyordu. Konuşmada heceler arası boşluklar
        # tabanı aşağıda tutar; sabit gürültüde taban gürültünün kendisi olur ve konuşma sayılmaz.
        self._levels.append(level)
        self.noise = min(self._levels)
        floor = self.threshold or max(0.004, self.noise * 1.8)
        loud = level > floor * self.boost
        try:
            speech = self._vad.is_speech(frame, rate)
        except Exception:
            speech = loud
        return speech and loud


class SpeechGate:
    """Canlı konuşmada mikrofonu süzer: konuşma yokken Live'a sessizlik gider. Oyun/motor sesi Live'ın kendi
    konuşma algısını tetikleyip "vote vote", Japonca, Hintçe gibi uydurma cümlelere yol açıyordu."""

    def __init__(self, vad: "VAD | None" = None, preroll: int = 10, hangover: int = 20, start_frames: int = 3):
        self.vad = vad or VAD(aggressiveness=3)
        self.ring: deque[bytes] = deque(maxlen=preroll)
        self.hangover = hangover
        self.start_frames = start_frames
        self.voiced = 0
        self.open_left = 0

    def feed(self, frame: bytes) -> list[bytes]:
        """Kareyi al, Live'a gidecek kareleri döndür (konuşma yoksa aynı uzunlukta sessizlik)."""
        if self.vad.is_speech(frame):
            self.voiced += 1
        else:
            self.voiced = 0
        if self.open_left > 0:
            self.open_left = self.hangover if self.voiced else self.open_left - 1
            return [frame]
        if self.voiced >= self.start_frames:  # konuşma başladı: hemen öncesini de gönder (ilk hece kesilmesin)
            self.open_left = self.hangover
            out = list(self.ring) + [frame]
            self.ring.clear()
            return out
        self.ring.append(frame)
        return [b"\x00" * len(frame)]


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


# Whisper "Hey Alice"i Türkçede çok farklı yazıyor: "Hey Alis", "Ey Alıs", "Hey Ellis", "Heyalis", "Hey Alize"...
_FILLERS = {"e", "ee", "eee", "şey", "ya", "hmm", "hı", "ıı", "a", "ah", "oh", "pardon"}
_HEYS = {"hey", "hay", "hei", "heyy", "ey", "hi", "he", "hej", "hello", "helo", "selam", "alo",
         "merhaba", "merhabalar", "selamlar", "slm", "gunaydin", "naber", "bak", "baksana"}
_NAMES = {"alice", "alis", "alise", "alize", "aliz", "alisa", "alys", "elis", "ellis", "elise", "ales", "ailis",
          "alıs", "alıse", "aliş", "alış", "allis", "alliss", "alist"}
_TR = str.maketrans("ıİçÇşŞğĞöÖüÜ", "iIcCsSgGoOuU")


_REAL_HEYS = {"hey", "hay", "hei", "heyy", "ey", "hej"}
# "Alice"e benzeyen gerçek Türkçe adlar: "Selam Aliye" uyandırmasın.
_NOT_NAMES = {"aliye", "alisan"}


def _name_like(word: str, after_hey: bool | str) -> bool:
    import difflib

    w = word.lower().translate(_TR)
    if w in {n.translate(_TR) for n in _NAMES}:
        return True
    # "Hey Ali" sık bir yanlış duyma; ama "Selam Ali", "Merhaba Ali" gerçek bir Ali'ye (Discord, oda) söylenmiş
    # olabilir: sadece gerçek "hey"den sonra kabul et.
    if after_hey and w == "ali":
        return after_hey is True or after_hey in _REAL_HEYS
    if after_hey and w in _NOT_NAMES:
        return False
    # Benzerlik yalnızca "hey"den sonra: tek başına "Alicem nerede" gibi cümleler uyandırmasın.
    return after_hey and len(w) >= 4 and max(difflib.SequenceMatcher(None, w, n).ratio() for n in ("alis", "alice", "elis")) >= 0.8


YES_WORDS = {"evet", "tamam", "tamamdır", "olur", "onay", "onayla", "onaylıyorum", "yap", "yapabilirsin", "devam",
             "peki", "tabii", "elbette"}
NO_WORDS = {"hayır", "hayir", "yapma", "dur", "durdur", "iptal", "vazgeç", "istemiyorum", "onaylama", "etme",
            "silme", "açma", "kapatma", "gönderme", "olmaz", "yok", "bekle"}


def is_yes(text: str) -> bool:
    """Sesli onay cevabı evet mi? Kelime bazlı: "yapma" ya da "onaylama" evet sayılmasın (eskiden alt dize
    eşleşmesiyle "yap" geçtiği için evet sayılıyordu). Olumsuz bir kelime varsa her zaman hayır."""
    words = re.findall(r"\w+", text.lower())
    if any(w in NO_WORDS for w in words):
        return False
    return any(w in YES_WORDS or w.startswith(("evet", "tamam", "onaylı")) for w in words)


def strip_wake(text: str) -> str | None:
    """'Hey Alice, saat kaç?' -> 'saat kaç?'. Uyandırma yoksa None.
    Başta bir dolgu sözcüğüne ("ee", "şey") ve Whisper'ın farklı yazımlarına izin verir."""
    text = text.strip()
    words = list(re.finditer(r"\w+", text))
    i = 0
    if i < len(words) and words[i].group().lower() in _FILLERS and len(words) > 1:
        i += 1
    if i < len(words):
        w = words[i].group().lower().translate(_TR)
        # "Heyalis" gibi bitişik yazım
        for h in ("hey", "hay", "ey"):
            if w.startswith(h) and len(w) > len(h) + 2 and _name_like(w[len(h):], True):
                return text[words[i].end():].lstrip(" ,.!?;:-").strip()
    after_hey: bool | str = False
    if i < len(words) and words[i].group().lower().translate(_TR) in _HEYS:
        after_hey = words[i].group().lower().translate(_TR)
        i += 1
    # "Alice" -> "Ali C", "Ali J'e" diye bölünmüş yazım: Ali + tek harf (+ ek)
    if (i < len(words) - 1 and i <= 2 and words[i].group().lower().translate(_TR) == "ali"
            and len(words[i + 1].group()) == 1 and words[i + 1].start() - words[i].end() <= 1):
        rest = text[words[i + 1].end():]
        return re.sub(r"^['’]\w{1,3}", "", rest).lstrip(" ,.!?;:-'’").strip()
    if i < len(words) and i <= 2 and _name_like(words[i].group(), after_hey):
        return text[words[i].end():].lstrip(" ,.!?;:-'’").strip()
    # "Bak şimdi Alice ..." gibi: ilk üç sözcükte tam isim (benzerlik değil, yanlış uyanmasın)
    for w in words[:3]:
        if _name_like(w.group(), False):
            return text[w.end():].lstrip(" ,.!?;:-'’").strip()
    return None


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
    """Global kısayol: basılıyken pressed True. Windows'ta kancasız tuş yoklaması, diğerlerinde pynput."""

    def __init__(self, key: str = "<scroll_lock>"):
        self.pressed = threading.Event()
        self.released = threading.Event()
        from alice.hotkeys import IS_WIN, KeyPoller

        if IS_WIN:  # kancasız: pynput'un sistem geneli klavye kancası her yerde yazmayı yavaşlatıyordu
            def down() -> None:
                self.released.clear()
                self.pressed.set()

            def up() -> None:
                self.pressed.clear()
                self.released.set()

            self._listener = KeyPoller(key, down, up)
            self._listener.start()
            return
        from pynput import keyboard

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
