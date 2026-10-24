"""Söylenecek şeye göre ton seçimi.

ALICE her şeyi aynı tonda okumaz: normalde sakin, iyi haberde neşeli, hatada ciddi,
uçuş uyarısında net ve acil. Seçim ucuz: önce bildirimin önemi/konusu, sonra metindeki
ipuçları. Acil tonda espri yapılmaz; bunu kişilik dosyası ve ses talimatı söyler.
"""

from __future__ import annotations

import re

from alice.voice.spoken_numbers import normalize as normalize_numbers
from alice.voice.spoken_numbers import say_ordinal

EMOTIONS = ("calm", "happy", "serious", "urgent", "playful")

NAMES_TR = {"calm": "sakin", "happy": "neşeli", "serious": "ciddi", "urgent": "acil", "playful": "şakacı"}

# Gemini ses modelleri için yönetmen notu (İngilizce talimatı daha iyi izliyorlar; konuşulan metin Türkçe).
DIRECTION = {
    "calm": "Calm, warm and soft-spoken. Unhurried, gentle pace, friendly.",
    "happy": "Cheerful and bright, smiling while speaking, energetic but natural.",
    "serious": "Serious, composed and clear. Steady pace, no playfulness.",
    "urgent": "Clear, firm and urgent like a flight callout. Short, decisive, slightly faster. No jokes.",
    "playful": "Playful, lightly teasing and warm, with a hint of a smile.",
}

# Chatterbox "exaggeration" (0.25-2) ve cfg_weight. Yüksek exaggeration daha duygulu, hızlı.
CHATTERBOX = {
    "calm": (0.35, 0.5), "happy": (0.7, 0.4), "serious": (0.4, 0.55), "urgent": (0.9, 0.3), "playful": (0.75, 0.4)}

_URGENT = re.compile(r"\b(bingo|acil|dikkat|tehlike|hemen|yakıt|pull up|stall|çok sıcak|kritik)\b", re.I)
_SERIOUS = re.compile(r"\b(hata|başarısız|çöktü|olmadı|bulunamadı|yapamadım|uyarı|maalesef|sorun|dolu)\b", re.I)
_HAPPY = re.compile(r"\b(tamamlandı|bitti|başarı|harika|süper|hazır|tebrik|güzel|oldu|geçti)\b|!\s*$", re.I)
_PLAYFUL = re.compile(r"(😄|😉|😏|haha|:\)|;\))", re.I)


def pick(text: str, severity: str = "", topic: str = "") -> str:
    if severity == "critical" or (topic == "flight" and severity in ("warn", "critical")):
        return "urgent"
    if _URGENT.search(text) and topic == "flight":
        return "urgent"
    if severity == "warn" or _SERIOUS.search(text):
        return "serious"
    if _PLAYFUL.search(text):
        return "playful"
    if _HAPPY.search(text):
        return "happy"
    return "calm"


# Emoji ve süslemeler: ses motorları bunları "gülen yüz" diye okuyor ya da cızırtı üretiyor.
_EMOJI = re.compile("[\U0001F000-\U0001FAFF\U0001F1E6-\U0001F1FF\u2600-\u27BF\u2B00-\u2BFF\uFE0F\u200D\u20E3]+")


def drop_emoji(text: str) -> str:
    return re.sub(r"[ \t]{2,}", " ", _EMOJI.sub("", text)).strip()


def for_speech(text: str) -> str:
    """Markdown'ı sesli okunacak düz metne çevir."""
    text = re.sub(r"```.*?```", " Kodu ekrana yazdım. ", text, flags=re.S)
    text = re.sub(r"`([^`]*)`", r"\1", text)
    text = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", text)
    text = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"^\s{0,3}#{1,6}\s*", "", text, flags=re.M)
    text = re.sub(r"^\s*[-*+]\s+", "", text, flags=re.M)
    # Numaralı liste "1. Elma" -> "birinci, Elma" (eskiden "bir. Elma": rakam cümle sonu gibi okunuyordu)
    text = re.sub(r"^\s*(\d{1,3})[.)]\s+", lambda m: f"{say_ordinal(int(m.group(1)))}, ", text, flags=re.M)
    text = re.sub(r"^\s*\|.*\|\s*$", "", text, flags=re.M)  # tablolar okunmaz
    text = re.sub(r"(\*\*|__|\*|_|~~)(.+?)\1", r"\2", text)
    text = re.sub(r"https?://\S+", "bağlantı", text)
    text = normalize_numbers(text)  # "3.5", "%40", "78°C" gibi ifadeler ses modellerini şaşırtıyor
    return re.sub(r"[ \t]+", " ", re.sub(r"\n{2,}", "\n", text)).strip()


# Cümle sonu: noktalama + boşluk (ör. "3.5" bölünmez) ya da satır sonu.
_END = re.compile(r"[.!?…]+\s+|\n+")
# Noktasıyla biten ama cümleyi bitirmeyen kısaltmalar: "Dr. Ahmet", "vb. şeyler" ikiye bölünüp ayrı cümle gibi
# (araya duraklama, düşen tonlama) okunuyordu.
ABBREV = {"dr", "prof", "doç", "doc", "yrd", "uzm", "av", "op", "müh", "sn", "bkz", "bknz", "vb", "vs", "vd", "örn",
          "ör", "yy", "no", "nr", "tel", "mah", "cad", "sok", "sk", "apt", "blv", "bul", "st", "mr", "mrs", "ms",
          "ltd", "şti", "inc", "co", "alb", "gen", "org", "kur", "yzb", "tğm", "ing", "fr", "sf", "s"}


def _holds(buf: str, m: re.Match) -> bool:
    """Bu nokta cümle sonu değil mi (kısaltma, sıra sayısı, baş harf)? Akışta sonraki harf henüz gelmediyse
    sıra sayısı mı belli değil: beklenir."""
    if m.group(0).rstrip() != "." or "\n" in m.group(0):
        return False
    word = re.search(r"(\S+)$", buf[: m.start()])
    if word is None:
        return False
    w = word.group(1).lstrip("([\"'").lower()
    if w in ABBREV or (len(w) == 1 and w.isalpha()):
        return True
    if w.isdigit():  # "15. yüzyıl" sıra sayısı; "Toplam 15. Sonra" cümle sonu
        nxt = buf[m.end(): m.end() + 1]
        return not nxt or nxt.islower()
    return False


class SentenceSplitter:
    """Akan metinden tamamlanan cümleleri çıkarır: ilk cümle hazır olur olmaz konuşmaya başlanır."""

    def __init__(self, min_chars: int = 12):
        self.buf = ""
        self.min_chars = min_chars

    def feed(self, delta: str) -> list[str]:
        self.buf += delta
        out = []
        while self.buf.count("```") % 2 == 0:  # kod bloğu kapanmadan bölme
            self.buf = re.sub(r"```.*?```", "Kodu ekrana yazdım.\n", self.buf, flags=re.S)
            cut = next((m.end() for m in _END.finditer(self.buf)
                        if (m.end() >= self.min_chars or "\n" in m.group(0)) and not _holds(self.buf, m)), None)
            if cut is None:
                break
            piece, self.buf = for_speech(self.buf[:cut]), self.buf[cut:]
            if piece:
                out.append(piece)
        return out

    def flush(self) -> list[str]:
        piece, self.buf = for_speech(self.buf), ""
        return [piece] if piece else []
