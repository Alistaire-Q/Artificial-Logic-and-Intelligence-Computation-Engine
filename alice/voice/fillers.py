"""Live'ın beklerken söylediği hazır kısa cümleler ("Bir saniye.", "Bir düşüneyim.").

Model bir araç çağırınca önce bir şey söylemesi isteniyor ama çoğu zaman söylemiyor: araç bitene kadar 8-16 sn
sessizlik oluyordu (voice.log 28 Ekim: hava durumu 10 sn, Spotify 16 sn). Model o turda hiç ses vermediyse bu
cümlelerden biri anında çalınır. Sesler Live'ın kendi sesiyle (Kore...) Gemini TTS'ten bir kez üretilip diske konur;
ses değişse de ALICE'in sesi aynı kalır, çalmak ağ beklemez.
"""

from __future__ import annotations

import hashlib
import random
import threading
from pathlib import Path
from typing import Callable

from alice.voice.audio import Audio

TOOL = ("Bir saniye.", "Hemen bakıyorum.", "Tamam, hallediyorum.")
THINK = ("Bir düşüneyim.", "Hmm, bir bakayım.")
RATE = 24000  # Live'ın çıkış hızı (AudioOut)


def filler_dir(voice: str) -> Path:
    from alice.config import alice_home

    return alice_home() / "voice" / "fillers" / voice


class Fillers:
    """Hazır cümlelerin sesleri. get() ağ beklemez: ses yoksa None döner ve arka planda üretimi başlatır."""

    def __init__(self, voice: str, synth: Callable[[str], Audio] | None, path: Path | None = None):
        self.voice = voice
        self.synth = synth  # metin -> Audio (Live'ın sesiyle); None = üretme, sadece diskte olanları çal
        self.path = path or filler_dir(voice)
        self._mem: dict[str, bytes] = {}
        self._last = ""
        self._lock = threading.Lock()
        self._warming = False

    def _file(self, text: str) -> Path:
        return self.path / (hashlib.sha1(text.encode("utf-8")).hexdigest()[:16] + ".wav")

    def _load(self, text: str) -> bytes | None:
        if text in self._mem:
            return self._mem[text]
        f = self._file(text)
        try:
            pcm = Audio.from_wav(f.read_bytes()).resample(RATE).pcm if f.exists() else None
        except Exception:  # noqa: BLE001 - bozuk dosya: sil, yeniden üretilsin
            f.unlink(missing_ok=True)
            pcm = None
        if pcm:
            self._mem[text] = pcm
        return pcm

    def get(self, kind: str = "tool") -> bytes | None:
        """Çalınacak PCM (24 kHz). Aynı cümle art arda gelmez."""
        texts = [t for t in (THINK if kind == "think" else TOOL) if t != self._last]
        random.shuffle(texts)
        for text in texts:
            pcm = self._load(text)
            if pcm:
                self._last = text
                return pcm
        self.warm()
        return None

    def warm(self) -> None:
        """Eksik sesleri arka planda üret (birkaç saniye, bir kez)."""
        with self._lock:
            if self._warming or self.synth is None:
                return
            missing = [t for t in TOOL + THINK if not self._file(t).exists()]
            if not missing:
                return
            self._warming = True

        def run() -> None:
            try:
                self.path.mkdir(parents=True, exist_ok=True)
                for text in missing:
                    try:
                        audio = self.synth(text)
                    except Exception:  # noqa: BLE001 - kota/ağ: sonraki oturumda yine denenir
                        return
                    tmp = self._file(text).with_suffix(".tmp")
                    audio.save(tmp)
                    tmp.replace(self._file(text))
            finally:
                self._warming = False

        threading.Thread(target=run, daemon=True, name="alice-live-fillers").start()


def gemini_fillers(cfg, voice: str, api_key: str) -> Fillers:
    """Live'ın sesiyle Gemini TTS'ten üreten dolgu sesleri."""
    from alice.voice.tts import GeminiTTS

    models = cfg.data.get("voice", {}).get("gemini_tts_models") or None
    tts = GeminiTTS(api_key, voice=voice, models=models)
    return Fillers(voice, lambda text: tts.synth(text, "calm"))
