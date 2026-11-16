"""Kanat adamı koçluğu: anons değil koçluk. Uçuş kayıtlarından ölüm desenleri çıkarılır.

Her inişsiz biten uçuş için son canlı anın (hızın düşmeden önceki son örnek) 15 saniyesine bakılır: alçakta mıydın,
sert dönüyor muydun, öncesinde "Arkanda düşman" dendi mi, SAM menzilinde miydin, seni kim vurdu. Son ölümlerde bir
desen yeterince sık ise söylenir: "Son 10 ölümünün 7'si alçakta dönerken arkadan geldi."

Maç içinde de bir kez hatırlatılır: SAM menzili anonsuna ya da arkadaki düşman anonsuna desene uyan kısa bir öğüt
eklenir. Koç bayrakları SimHub tarafından maç başında hesaplanır, uçuş kodu bunları sadece okur.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from pathlib import Path
from typing import Any

LOW_FT = 2000.0
TURN_G = 4.0
WINDOW = 15.0          # son canlı andan önceki bu kadar saniye
SIX_BEFORE = 45.0
SAM_BEFORE = 90.0
HIT_BEFORE = 30.0
HIT_AFTER = 30.0       # War Thunder "seni X vurdu" yazısı uçağın hızı düştükten sonra da gelebiliyor
ALIVE_KT = 50.0
MIN_SHARE, MIN_COUNT = 0.4, 3   # desen: son ölümlerin en az %40'ı ve en az 3 tanesi

_LAST = {1: "i", 2: "si", 3: "ü", 4: "ü", 5: "i", 6: "sı", 7: "si", 8: "i", 9: "u"}
_TENS = {10: "u", 20: "si", 30: "u", 40: "ı", 50: "si", 60: "ı", 70: "i", 80: "i", 90: "ı"}


def ek(n: int) -> str:
    """Sayının iyelik eki: 7 -> "7'si", 3 -> "3'ü", 10 -> "10'u". Bulunma için sonuna "nde": "7'sinde"."""
    tail = "ı" if n == 0 else _LAST.get(n % 10) or _TENS.get(n % 100) or "ü"
    return f"{n}'{tail}"


def load_flights(folder: Path | None = None, limit: int = 40) -> list[dict[str, Any]]:
    """Seri verisiyle birlikte son uçuşlar (eskiden yeniye)."""
    from alice.sims.flight import flights_dir

    out = []
    for f in sorted((folder or flights_dir()).glob("2*.json"))[-limit:]:
        try:
            out.append(json.loads(f.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            continue
    return out


# War Thunder'daki hava savunması araçları (SPAA/SAM). Vuran bunlardan biri değilse ölüm SAM'e yazılmaz.
AIR_DEFENSE_RX = re.compile(
    r"\b(?:SAM|SAMP/T|TEL|SPAA|pantsir|tor|osa|strela|tunguska|zsu|shilka|buk|kub|2S6|gepard|ozelot|flarakpz|roland|adats|"
    r"chaparral|avenger|vulcan|m163|m247|sergeant york|crotale|rapier|stormer|lvkv|ito|veak|sidam|otomatic|"
    r"skyguard|tan-sam|type 81|type 87|type 93|pgz|hq-?\d+|fk-?\d+|lav-ad|wiesel 2|iris-t|machbet|spyder|"
    r"ystervark|bosvark|marksman)\b", re.I)


def is_air_defense(name: str) -> bool:
    return bool(AIR_DEFENSE_RX.search(name or ""))


def death_context(f: dict[str, Any], events: list[Any]) -> dict[str, Any] | None:
    """İnişsiz biten bir uçuşun son anı. İnişle bittiyse ya da seri yoksa None."""
    if f.get("touchdown_vv_ms") is not None:
        return None
    s = f.get("series") or {}
    wall, alt, g, ias = s.get("wall") or [], s.get("alt_ft") or [], s.get("g") or [], s.get("ias_kt") or []
    n = min(len(wall), len(alt), len(g), len(ias))
    alive = [i for i in range(n) if (ias[i] or 0) > ALIVE_KT]
    if not alive:
        return None
    end_i = alive[-1]
    end = float(wall[end_i])
    win = [i for i in range(n) if end - WINDOW <= float(wall[i]) <= end]
    ctx = {"end": end, "aircraft": f.get("aircraft") or "",
           "low": min(float(alt[i] or 0) for i in win) < LOW_FT,
           "turning": max(float(g[i] or 0) for i in win) >= TURN_G,
           "six": False, "sam": False, "killer": ""}
    downed = ""
    for e in events:
        dt = end - e.ts
        body = e.body or ""
        if 0 <= dt <= SIX_BEFORE and body.startswith("Arkanda düşman"):
            ctx["six"] = True
        if 0 <= dt <= SAM_BEFORE and "menzilindesin" in body:
            ctx["sam"] = True
        m = re.match(r"^(?:Seni (.+) düşürdü|Yanıyorsun! (.+) vurdu|Ağır hasar aldın, (.+) vurdu)", body)
        if m and -HIT_AFTER <= dt <= HIT_BEFORE:
            who = next(g for g in m.groups() if g).strip(" .!")
            if m.group(1):
                downed = who  # düşüren, hasar verenden kesin
            ctx["killer"] = who
    if downed:
        ctx["killer"] = downed
    # Menzilde olmak sebep değil: seni bir uçak vurduysa ölüm SAM'den değil, hava savunması vurduysa menzil bilinmese de SAM.
    if ctx["killer"]:
        ctx["sam"] = is_air_defense(ctx["killer"])
    return ctx


def contexts(flights: list[dict[str, Any]], events: list[Any], last: int = 10) -> list[dict[str, Any]]:
    out = [c for f in flights if (c := death_context(f, events)) is not None]
    return out[-last:]


def patterns(ctxs: list[dict[str, Any]]) -> dict[str, str]:
    """Desen anahtarı -> söylenecek cümle (sadece yeterince sık olanlar)."""
    n = len(ctxs)
    if n < MIN_COUNT:
        return {}

    def often(k: int) -> bool:
        return k >= MIN_COUNT and k / n >= MIN_SHARE

    out: dict[str, str] = {}
    low_turn_six = sum(1 for c in ctxs if c["low"] and c["turning"] and c["six"])
    six = sum(1 for c in ctxs if c["six"])
    if often(low_turn_six):
        out["low_turn_six"] = (f"Son {n} ölümünün {ek(low_turn_six)} alçakta dönerken arkadan geldi. Alçakta uzun "
                               "dönme; enerjini koru, irtifa kazanıp dön.")
    elif often(six):
        out["six"] = f"Son {n} ölümünün {ek(six)}nde düşmeden önce arkanda düşman vardı. Arkanı daha sık kontrol et."
    sam = sum(1 for c in ctxs if c["sam"])
    if often(sam):
        # Vuran görülmediyse sadece "menzildeydin" bilinir: kesin konuşma.
        sure = sum(1 for c in ctxs if c["sam"] and c["killer"])
        where = "hava savunmasından" if sure == sam else "hava savunması menzilindeyken oldu"
        out["sam"] = f"Son {n} ölümünün {ek(sam)} {where}. Menzile girince alçal ya da hemen çık."
    killers = Counter(c["killer"] for c in ctxs if c["killer"])
    if killers:
        who, k = killers.most_common(1)[0]
        if often(k):
            out["killer"] = f"Son {n} ölümünün {ek(k)}nde seni {who} vurdu."
    return out


# Maç içinde anonsa eklenen kısa öğüt (desen -> ek). Uçuş kodu bunu bir kez kullanır.
HINTS = {"sam": "Geçen ölümlerinin çoğu hava savunmasındandı, alçal ya da çık.",
         "low_turn_six": "Alçakta uzun dönme, irtifa kazan.",
         "six": "Arkanı kontrol et."}
