"""Sayıları ve birimleri sesli okunacak Türkçe yazıya çevirir.

Ses modelleri (özellikle yerel olanlar) "3.5", "%40", "14:05", "9,843 ft" gibi ifadelerde
saçmalayabiliyor; söylenecek her şey önce düz Türkçe kelimelere çevrilir:
  "GPU 78°C, %40"      -> "GPU yetmiş sekiz derece, yüzde kırk"
  "3.5 G", "3,5"        -> "üç virgül beş ge"
  "1.200 kg", "9,843 ft"-> "bin iki yüz kilo", "dokuz bin sekiz yüz kırk üç feet"
  "14:05"               -> "on dört sıfır beş"
  "3'te", "-5"          -> "üçte", "eksi beş"
  "15.10.2026"          -> "on beş ekim iki bin yirmi altı"
  "15. yüzyıl"          -> "on beşinci yüzyıl"
  "3.12.1", "1.234,56"  -> "üç nokta on iki nokta bir", "bin iki yüz otuz dört virgül elli altı"
"""

from __future__ import annotations

import re

ONES = ["", "bir", "iki", "üç", "dört", "beş", "altı", "yedi", "sekiz", "dokuz"]
TENS = ["", "on", "yirmi", "otuz", "kırk", "elli", "altmış", "yetmiş", "seksen", "doksan"]
SCALES = [(10 ** 12, "trilyon"), (10 ** 9, "milyar"), (10 ** 6, "milyon"), (1000, "bin")]

UNITS = {
    "°c": "derece", "°": "derece", "kg": "kilo", "km": "kilometre", "nm": "deniz mili", "kt": "knot",
    "kts": "knot", "ft": "feet", "fpm": "feet dakika", "m/s": "metre bölü saniye", "km/h": "kilometre saat",
    "mb": "megabayt", "gb": "gigabayt", "tb": "terabayt", "mhz": "megahertz", "ghz": "gigahertz",
    "sn": "saniye", "dk": "dakika", "sa": "saat", "ms": "milisaniye", "g": "ge", "w": "vat", "rpm": "devir",
}


def say_int(n: int) -> str:
    if n == 0:
        return "sıfır"
    if n < 0:
        return "eksi " + say_int(-n)
    words: list[str] = []
    for value, name in SCALES:
        if n >= value:
            q, n = divmod(n, value)
            # "bin" tek başına söylenir, "bir bin" denmez
            words += ([name] if (q == 1 and name == "bin") else [say_int(q), name])
    if n >= 100:
        q, n = divmod(n, 100)
        words += (["yüz"] if q == 1 else [ONES[q], "yüz"])
    if n >= 10:
        words.append(TENS[n // 10])
        n %= 10
    if n:
        words.append(ONES[n])
    return " ".join(w for w in words if w)


# Sıra sayısı: son kelimenin sıra hâli ("on beş" -> "on beşinci").
_ORD = {"sıfır": "sıfırıncı", "bir": "birinci", "iki": "ikinci", "üç": "üçüncü", "dört": "dördüncü",
        "beş": "beşinci", "altı": "altıncı", "yedi": "yedinci", "sekiz": "sekizinci", "dokuz": "dokuzuncu",
        "on": "onuncu", "yirmi": "yirminci", "otuz": "otuzuncu", "kırk": "kırkıncı", "elli": "ellinci",
        "altmış": "altmışıncı", "yetmiş": "yetmişinci", "seksen": "sekseninci", "doksan": "doksanıncı",
        "yüz": "yüzüncü", "bin": "bininci", "milyon": "milyonuncu", "milyar": "milyarıncı",
        "trilyon": "trilyonuncu"}

MONTHS = ["ocak", "şubat", "mart", "nisan", "mayıs", "haziran", "temmuz", "ağustos", "eylül", "ekim", "kasım",
          "aralık"]


def say_ordinal(n: int) -> str:
    words = say_int(n).split()
    words[-1] = _ORD.get(words[-1], words[-1])
    return " ".join(words)


def say_digits(s: str) -> str:
    """Ondalık kısım: "05" -> "sıfır beş", "5" -> "beş", "25" -> "yirmi beş"."""
    if s.startswith("0"):
        return " ".join(say_int(int(c)) for c in s)
    return say_int(int(s))


def _number(raw: str) -> str:
    """Tek bir sayı ifadesi (ayraçlarla) -> kelimeler."""
    neg = raw.startswith("-")
    s = raw.lstrip("-+")
    if re.fullmatch(r"\d{1,3}([.,]\d{3})+", s) and not s.startswith("0"):
        # Binlik ayracı: 1.200 / 1.200.000 / 9,843 (tam 3 haneli gruplar)
        out = say_int(int(re.sub(r"[.,]", "", s)))
    elif m := (re.fullmatch(r"(\d{1,3}(?:\.\d{3})+),(\d+)", s) or re.fullmatch(r"(\d{1,3}(?:,\d{3})+)\.(\d+)", s)):
        # Binlik + ondalık: 1.234,56 (Türkçe) / 1,234.56 (İngilizce). Eskiden "yüz yirmi üç bin..." okunuyordu.
        out = f"{say_int(int(re.sub(r'[.,]', '', m.group(1))))} virgül {say_digits(m.group(2))}"
    elif re.fullmatch(r"\d+(?:\.\d+){2,}", s):
        # Sürüm/IP gibi çok noktalı: 3.12.1 -> "üç nokta on iki nokta bir" (eskiden tek büyük sayı)
        out = " nokta ".join(say_int(int(x)) for x in s.split("."))
    elif re.fullmatch(r"\d+[.,]\d+", s):
        whole, frac = re.split(r"[.,]", s)
        out = f"{say_int(int(whole))} virgül {say_digits(frac)}"
    else:
        out = say_int(int(re.sub(r"[.,]", "", s)))
    return ("eksi " + out) if neg else out


_UNIT_RE = "|".join(sorted((re.escape(u) for u in UNITS), key=len, reverse=True))
_NUM = r"[-+]?\d+(?:[.,]\d+)*"

def _date(m: re.Match) -> str:
    return f"{say_int(int(m.group(1)))} {MONTHS[int(m.group(2)) - 1]} {say_int(int(m.group(3)))}"


_PATTERNS = [
    # tarih: 15.10.2026 / 5.1.2025 (eskiden "on beş milyon yüz iki bin..." okunuyordu)
    (re.compile(r"(?<![\w.,])(0?[1-9]|[12]\d|3[01])[./](0?[1-9]|1[0-2])[./](\d{4})(?![\w.,]\d)"), _date),
    # saat: 14:05
    (re.compile(r"\b([01]?\d|2[0-3]):([0-5]\d)\b"),
     lambda m: f"{say_int(int(m.group(1)))} {'sıfır ' + say_int(int(m.group(2))) if m.group(2).startswith('0') and m.group(2) != '00' else ('' if m.group(2) == '00' else say_int(int(m.group(2))))}".strip()),
    # yüzde: %40 / % 40 / 40%
    (re.compile(r"%\s?(" + _NUM + r")"), lambda m: f"yüzde {_number(m.group(1))}"),
    (re.compile(r"(" + _NUM + r")\s?%"), lambda m: f"yüzde {_number(m.group(1))}"),
    # sayı + birim: 78°C, 3.5 G, 1200 kg
    (re.compile(r"(?<![\w.,])(" + _NUM + r")\s?(" + _UNIT_RE + r")(?![\w])", re.I),
     lambda m: f"{_number(m.group(1))} {UNITS[m.group(2).lower()]}"),
    # sayı + kesme işaretli ek: 3'te, 1990'larda
    (re.compile(r"(?<![\w.,])(" + _NUM + r")['’](\w+)"), lambda m: f"{_number(m.group(1))}{m.group(2)}"),
    # sıra sayısı: "15. yüzyıl", "3. kez" (noktadan sonra küçük harfle devam: cümle sonu değil)
    (re.compile(r"(?<![\w.,])(\d{1,9})\.(?=[ \t]+[a-zçğıöşü])"), lambda m: say_ordinal(int(m.group(1)))),
    # çıplak sayı
    (re.compile(r"(?<![\w.,])(" + _NUM + r")(?![\w])"), lambda m: _number(m.group(1))),
]


def normalize(text: str) -> str:
    for rx, fn in _PATTERNS:
        text = rx.sub(fn, text)
    return re.sub(r"[ \t]+", " ", text)
