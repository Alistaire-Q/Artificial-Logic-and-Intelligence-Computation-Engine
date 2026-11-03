"""Sesli yolda dil ve çöp kapısı: Türkçe olmayan ya da anlamsız söz işe dönüşmesin, söylenmesin.

Gemini oyun/video/Discord sesini İspanyolca, Almanca, Portekizce cümle diye duyuyor ("Abre la canción Capiaça");
bu cümleler şarkı açtırıyor, Install'a bastırıyordu. Model de bazen yabancı dilde cevap veriyor ya da "---" diyor.
Karar hafif ve kurala dayalı: yazı sistemi, Türkçede olmayan harfler, sık yabancı sözcükler.
"""

from __future__ import annotations

import re

# Latin dışı yazılar (Yunan, Kiril, İbrani/Arap, Hint, Tay, Japon, Çin, Kore)
_NON_LATIN = re.compile("[Ͱ-ӿ֐-ۿऀ-࿿぀-ヿ㐀-鿿가-힯]")
# Türkçede olmayan Latin harfleri (â, î, û Türkçede de var: sayılmaz)
_FOREIGN_LETTERS = re.compile("[ñáéíóúàèìòùãõäëßêôœæøå¿¡]", re.I)
_TURKISH_LETTERS = re.compile("[çğışöüÇĞİŞÖÜ]")
_FOREIGN_WORDS = {
    # İspanyolca / Portekizce
    # Türkçede de sözcük olanlar ("el", "para", "der", "bin", "is") ve Türkçe konuşurken sık söylenenler ("okay",
    # "yes", "sorry") listede yok.
    "que", "los", "las", "una", "por", "con", "como", "está", "esta", "pero", "muy", "sí",
    "você", "não", "nao", "uma", "isso", "obrigado", "gracias", "hola", "bueno", "vamos", "aqui", "también",
    # Almanca
    "die", "das", "und", "ich", "nicht", "ist", "ein", "eine", "mit", "auf", "bereit", "klar",
    "alles", "danke", "ja", "nein", "doch", "aber", "wir", "sie", "es", "gut",
    # İngilizce
    "the", "and", "you", "what", "this", "that", "with", "have", "are", "it's", "i'm",
    "please", "speech", "detected",
}
# Türkçe olduğu belli sık sözcükler: yabancı sözcük sayısını dengeler ("ya", "bu", "bir")
_TURKISH_WORDS = {"bir", "bu", "şu", "ve", "ne", "mi", "mı", "mu", "mü", "ben", "sen", "var", "yok", "değil",
                  "evet", "hayır", "tamam", "aç", "kapat", "şunu", "bunu", "abi", "knk", "lan", "naber", "yap",
                  "olur", "çok", "daha", "sonra", "şimdi", "için", "gibi", "kadar", "oyun", "nasıl", "neden"}
_JUNK = re.compile(r"^[\W_]*$|no speech|speech detected|<\s*noise\s*>|^\(.*\)$", re.I)


def foreign(text: str) -> bool:
    """Bu söz büyük ihtimalle Türkçe değil mi? Kısa ya da kararsız metinde False (yanlışlıkla susturmasın)."""
    t = (text or "").strip()
    letters = [c for c in t if c.isalpha()]
    if len(letters) < 6:
        return False
    if sum(1 for c in letters if _NON_LATIN.match(c)) / len(letters) > 0.3:
        return True
    if _TURKISH_LETTERS.search(t) and not _FOREIGN_LETTERS.search(t):
        return False
    words = [w.lower() for w in re.findall(r"[\w']+", t)]
    if not words:
        return False
    alien = sum(w in _FOREIGN_WORDS for w in words) + 2 * len(_FOREIGN_LETTERS.findall(t))
    native = sum(w in _TURKISH_WORDS for w in words) + len(_TURKISH_LETTERS.findall(t))
    return alien >= 2 and alien > native


def junk(text: str) -> bool:
    """İçeriği olmayan çıktı mı ("---", "...", "<no speech detected>")?"""
    return bool(_JUNK.search((text or "").strip()))


# Kullanıcının bir önceki turu düzelttiği sözler: başarısızlığın en güvenilir işareti. O tur test setine girer.
_CORRECTION = re.compile(
    r"\b(demedim|söylemedim|seslenmedim|yanlış anla\w*|yanlış duy\w*|anlamad[ıi]n|saçmal\w*|ne alaka|"
    r"öyle de(me|mi)\w*|onu demi\w*|bunu demi\w*|sana de(me|mi)\w*|yapmad[ıi]n ki|olmad[ıi] ki)\b|^\s*hayır\b",
    re.I)


def correction(text: str) -> bool:
    """"Sana demedim", "yanlış anladın", "saçmaladın", "hayır, ..." gibi bir düzeltme mi?"""
    return bool(_CORRECTION.search(text or ""))
