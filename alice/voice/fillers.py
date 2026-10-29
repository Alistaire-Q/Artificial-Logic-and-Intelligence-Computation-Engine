"""Live'ın beklerken söylediği hazır kısa cümleler ("Bir saniye.", "Bir düşüneyim.").

Model bir araç çağırınca önce bir şey söylemesi isteniyor ama çoğu zaman söylemiyor: araç bitene kadar 8-16 sn
sessizlik oluyordu (voice.log 28 Ekim: hava durumu 10 sn, Spotify 16 sn). Model o turda hiç ses vermediyse bu
cümlelerden biri anında çalınır. Sesler Live'ın kendi sesiyle (Kore...) Gemini TTS'ten bir kez üretilip diske konur;
ses değişse de ALICE'in sesi aynı kalır, çalmak ağ beklemez.

Cümle kullanıcının tonuna uyar: acele eden birine sakin "Bir düşüneyim." tuhaf kaçıyordu. Acil ve neşeli cümleler
ilk istendiklerinde üretilir (kota boşa gitmesin); o ana kadar sakin olanı çalınır.
"""

from __future__ import annotations

import hashlib
import random
import re
import threading
from pathlib import Path
from typing import Callable

from alice.voice.audio import Audio

TOOL = ("Bir saniye.", "Hemen bakıyorum.", "Tamam, hallediyorum.")
THINK = ("Bir düşüneyim.", "Hmm, bir bakayım.")
PHRASES = {
    "calm": {"tool": TOOL, "think": THINK},
    "urgent": {"tool": ("Hemen.", "Tamam, hemen bakıyorum."), "think": ("Hemen düşünüyorum.",)},
    "happy": {"tool": ("Tabii, hemen bakalım!", "Süper, bir saniye!"), "think": ("Güzel soru, bir düşüneyim!",)},
}
RATE = 24000  # Live'ın çıkış hızı (AudioOut)

_URGENT = re.compile(r"\b(acil|hemen|çabuk|cabuk|acele|hızlı|hizli|koş|kos|yetiş|yetis|imdat|tehlike)\w*", re.I)
_HAPPY = re.compile(r"\b(harika|süper|super|mükemmel|mukemmel|yaşasın|yasasin|oley|helal|kazandı\w*|"
                    r"ha(ha)+)\b|!{2,}|[😄😂🎉]", re.I)


def user_tone(text: str) -> str:
    """Kullanıcının bu sözündeki ton: "urgent", "happy" ya da "calm"."""
    if _URGENT.search(text or ""):
        return "urgent"
    if _HAPPY.search(text or ""):
        return "happy"
    return "calm"


def filler_dir(voice: str) -> Path:
    from alice.config import alice_home

    return alice_home() / "voice" / "fillers" / voice


class Fillers:
    """Hazır cümlelerin sesleri. get() ağ beklemez: ses yoksa None döner ve arka planda üretimi başlatır."""

    def __init__(self, voice: str, synth: Callable[[str], Audio] | None, path: Path | None = None):
        self.voice = voice
        self.synth = synth  # (metin, ton) -> Audio (Live'ın sesiyle); None = üretme, sadece diskte olanları çal
        self.path = path or filler_dir(voice)
        self._mem: dict[str, bytes] = {}
        self._last = ""
        self._lock = threading.Lock()
        self._warming = False

    def _file(self, text: str, tone: str = "calm") -> Path:
        key = text if tone == "calm" else f"{tone}|{text}"  # sakin sesler eski adlarıyla kalsın
        return self.path / (hashlib.sha1(key.encode("utf-8")).hexdigest()[:16] + ".wav")

    def _load(self, text: str, tone: str = "calm") -> bytes | None:
        if (tone, text) in self._mem:
            return self._mem[(tone, text)]
        f = self._file(text, tone)
        try:
            pcm = Audio.from_wav(f.read_bytes()).resample(RATE).pcm if f.exists() else None
        except Exception:  # noqa: BLE001 - bozuk dosya: sil, yeniden üretilsin
            f.unlink(missing_ok=True)
            pcm = None
        if pcm:
            self._mem[(tone, text)] = pcm
        return pcm

    def get(self, kind: str = "tool", tone: str = "calm") -> bytes | None:
        """Çalınacak PCM (24 kHz). Aynı cümle art arda gelmez. O tonun sesi henüz yoksa sakini çalınır."""
        kind = "think" if kind == "think" else "tool"
        for t in dict.fromkeys((tone if tone in PHRASES else "calm", "calm")):
            texts = [x for x in PHRASES[t][kind] if x != self._last]
            random.shuffle(texts)
            for text in texts:
                pcm = self._load(text, t)
                if pcm:
                    self._last = text
                    if t != tone:
                        self.warm(tone)
                    return pcm
        self.warm(tone)
        return None

    def warm(self, tone: str = "calm") -> None:
        """Bir tonun eksik seslerini arka planda üret (birkaç saniye, bir kez)."""
        tone = tone if tone in PHRASES else "calm"
        with self._lock:
            if self._warming or self.synth is None:
                return
            missing = [t for t in PHRASES[tone]["tool"] + PHRASES[tone]["think"] if not self._file(t, tone).exists()]
            if not missing:
                return
            self._warming = True

        def run() -> None:
            try:
                self.path.mkdir(parents=True, exist_ok=True)
                for text in missing:
                    try:
                        audio = self.synth(text, tone)
                    except Exception:  # noqa: BLE001 - kota/ağ: sonraki oturumda yine denenir
                        return
                    tmp = self._file(text, tone).with_suffix(".tmp")
                    audio.save(tmp)
                    tmp.replace(self._file(text, tone))
            finally:
                self._warming = False

        threading.Thread(target=run, daemon=True, name="alice-live-fillers").start()


def gemini_fillers(cfg, voice: str, api_key: str) -> Fillers:
    """Live'ın sesiyle Gemini TTS'ten üreten dolgu sesleri."""
    from alice.voice.tts import GeminiTTS

    models = cfg.data.get("voice", {}).get("gemini_tts_models") or None
    tts = GeminiTTS(api_key, voice=voice, models=models)
    return Fillers(voice, lambda text, tone="calm": tts.synth(text, tone))
