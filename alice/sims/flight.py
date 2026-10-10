"""Uçuş arkadaşı.

F-16'nın kendi sesli uyarıları zaten var; ALICE onları tekrarlamaz. Onun yerine bir
uçuş arkadaşı gibi bakar:
  - Yakıt: mevcut tüketimle kaç dakika kaldı, eve dönmek için ne zaman ayrılmalı
  - Yaklaşma: teker açık, alçakta, yavaşken AoA ve alçalma hızına kısa geri bildirim
  - Uzun süre yüksek G, chaff/flare azalması
  - İnişten sonra kısa değerlendirme (+ saat verisi varsa nabız)

Kaynaktan (DCS, War Thunder) bağımsızdır: hepsi FlightState üretir, FlightCompanion tüketir.
Birimler SI: m, m/s, kg, derece.
"""

from __future__ import annotations

import json
import math
import time
from collections import deque
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable

from alice.config import alice_home

KT = 0.514444    # 1 knot = m/s
FT = 0.3048
NM = 1852.0


@dataclass
class FlightState:
    t: float                         # simülasyon zamanı (sn)
    source: str = "dcs"
    aircraft: str = ""
    ias: float | None = None         # m/s
    tas: float | None = None
    alt: float | None = None         # deniz seviyesinden m
    agl: float | None = None         # yerden m (bilinmiyorsa None)
    vv: float | None = None          # dikey hız m/s
    aoa: float | None = None
    g: float | None = None
    fuel_kg: float | None = None
    burn_kgs: float | None = None    # anlık tüketim kg/s (biliniyorsa)
    chaff: int | None = None
    flare: int | None = None
    gear: float | None = None        # 0 = yukarı, 1 = aşağı
    x: float | None = None           # harita koordinatı m (DCS: x kuzey, z doğu)
    z: float | None = None
    heading: float | None = None     # burnun yönü, derece (gerçek kuzey)
    extra: dict[str, Any] = field(default_factory=dict)  # oyunun verdiği ek göstergeler (bkz. describe_extra)
    wall: float = field(default_factory=time.time)


@dataclass
class Profile:
    name: str
    internal_fuel_kg: float = 3000
    reserve_kg: float = 450          # inişte kalması istenen yakıt
    aoa_on_speed: tuple[float, float] = (11.0, 15.0)
    approach_ias_max: float = 100.0  # m/s, bunun altında ve teker açıksa yaklaşma sayılır
    g_warn: float = 7.0
    g_warn_seconds: float = 5.0
    chaff_low: int = 20
    flare_low: int = 10


PROFILES = {
    "F-16C_50": Profile("F-16C", internal_fuel_kg=3249, reserve_kg=450, aoa_on_speed=(11.0, 15.0),
                        approach_ias_max=95, g_warn=7.5),
    "FA-18C_hornet": Profile("F/A-18C", internal_fuel_kg=4900, reserve_kg=680, aoa_on_speed=(7.4, 8.8),
                             approach_ias_max=80, g_warn=6.5),
    "default": Profile("uçak"),
}


def profile_for(aircraft: str) -> Profile:
    return PROFILES.get(aircraft) or PROFILES["default"]


def fmt_min(m: float) -> str:
    m = max(0, int(round(m)))
    return f"{m} dakika" if m < 120 else f"{m // 60} saat {m % 60} dakika"


@dataclass
class Debrief:
    aircraft: str
    source: str
    start_wall: float
    end_wall: float
    duration_s: float
    max_g: float
    max_ias_kt: float
    max_alt_ft: float
    fuel_used_kg: float | None
    min_fuel_kg: float | None
    approach_on_speed_pct: float | None
    max_sink_final_ms: float | None
    touchdown_vv_ms: float | None
    chaff_used: int | None
    flare_used: int | None
    hr: dict[str, float] | None = None
    series: dict[str, list] = field(default_factory=dict)

    def summary(self) -> str:
        parts = [f"Uçuş {fmt_min(self.duration_s / 60)} sürdü."]
        parts.append(f"En yüksek {self.max_g:.1f} G, en yüksek hız {self.max_ias_kt:.0f} knot.")
        if self.fuel_used_kg is not None:
            parts.append(f"{self.fuel_used_kg:.0f} kilo yakıt kullandın, en az {self.min_fuel_kg:.0f} kiloya düştün.")
        if self.approach_on_speed_pct is not None:
            q = "iyiydi" if self.approach_on_speed_pct >= 70 else "biraz dalgalıydı" if self.approach_on_speed_pct >= 40 \
                else "hız kontrolü zayıftı"
            parts.append(f"Son yaklaşmanın yüzde {self.approach_on_speed_pct:.0f}'i on speed'di, {q}.")
        if self.touchdown_vv_ms is not None:
            v = abs(self.touchdown_vv_ms)
            feel = "yumuşaktı" if v < 1.5 else "normaldi" if v < 3 else "sertti"
            parts.append(f"Teker koyarken alçalma hızın saniyede {v:.1f} metre, {feel}.")
        if self.hr:
            parts.append(f"Nabzın ortalama {self.hr['avg']:.0f}, en yüksek {self.hr['max']:.0f}.")
        return " ".join(parts)


class FlightCompanion:
    """FlightState akışını izler, söylenecekleri say(text, severity, emotion) ile iletir."""

    def __init__(self, say: Callable[[str, str, str], None], hr_stats: Callable[[float, float], dict | None] = None,
                 hr_now: Callable[[], float | None] = None, on_debrief: Callable[[Debrief], None] = None,
                 approach_every: float = 4.0):
        self.say = say
        self.hr_stats = hr_stats or (lambda a, b: None)
        self.hr_now = hr_now or (lambda: None)
        self.on_debrief = on_debrief or (lambda d: None)
        self.approach_every = approach_every
        self.reset()

    def reset(self) -> None:
        self.phase = "ground"            # ground | air | landing_roll
        self.profile = PROFILES["default"]
        self.aircraft = ""
        self.fuel_hist: deque[tuple[float, float]] = deque()
        self.burn_hist: deque[tuple[float, float]] = deque()
        self.home: tuple[float, float] | None = None
        self.home_alert_nm: float | None = None
        self.fuel_flags: set[str] = set()
        self.g_since: float | None = None
        self.g_last_warn = -1e9
        self.cm_last: dict[str, int] = {}
        self.cm_flags: set[str] = set()
        self.appr_last_say = -1e9
        self.appr_last_kind = ""
        self.appr_samples: list[bool] = []
        self.max_sink_final: float | None = None
        self.slow_since: float | None = None
        self.last_state: FlightState | None = None
        self.takeoff: FlightState | None = None
        self.stats = {"max_g": 0.0, "max_ias": 0.0, "max_alt": 0.0, "min_fuel": None}
        self.series: dict[str, list] = {"t": [], "wall": [], "alt_ft": [], "g": [], "ias_kt": [], "fuel_kg": []}
        self._series_t = -1e9
        self.restricted_said = False
        self._touchdown_vv: float | None = None

    # ---------------------------------------------------------------- girişler
    def restricted(self) -> None:
        if not self.restricted_said:
            self.restricted_said = True
            self.say("Bu sunucu uçuş verisi paylaşımını kısıtlıyor. Yakıt ve yaklaşma takibi yapamam ama "
                     "kontrol listeleri, kneeboard ve sesli komutlar çalışıyor.", "info", "calm")

    def session_end(self) -> None:
        """Veri kesildi (görevden çıkıldı). Havadaysa uçuşu yine de değerlendir."""
        if self.phase == "air" and self.last_state is not None:
            self._finish(self.last_state, landed=False)
        self.reset()

    def update(self, s: FlightState) -> None:
        if s.aircraft and s.aircraft != self.aircraft:
            self.aircraft = s.aircraft
            self.profile = profile_for(s.aircraft)
        prev = self.last_state
        if prev is not None and s.t + 5 < prev.t:  # görev yeniden başladı
            self.reset()
            prev = None
        self.last_state = s
        self._phase(s)
        if self.phase != "air":
            return
        self._record(s)
        self._home_alert(s)
        self._fuel(s)
        self._g(s)
        self._countermeasures(s)
        self._approach(s)

    # ------------------------------------------------------------------- evre
    def _airborne(self, s: FlightState) -> bool:
        if s.agl is not None:
            return s.agl > 15 and (s.ias or 0) > 40
        return (s.ias or 0) > 60

    def _phase(self, s: FlightState) -> None:
        if self.phase == "ground":
            if self._airborne(s):
                self.phase = "air"
                self.takeoff = s
                if s.x is not None and s.z is not None:
                    self.home = (s.x, s.z)
            return
        stopped = (s.ias or 0) < 30 and (s.agl is None or s.agl < 5)
        if stopped:
            self.slow_since = self.slow_since if self.slow_since is not None else s.t
            if s.t - self.slow_since >= 5:
                self._finish(s, landed=True)
                home = self.home
                self.reset()
                self.home = home
        else:
            self.slow_since = None

    def _record(self, s: FlightState) -> None:
        st = self.stats
        st["max_g"] = max(st["max_g"], s.g or 0)
        st["max_ias"] = max(st["max_ias"], s.ias or 0)
        st["max_alt"] = max(st["max_alt"], s.alt or 0)
        if s.fuel_kg is not None:
            st["min_fuel"] = s.fuel_kg if st["min_fuel"] is None else min(st["min_fuel"], s.fuel_kg)
        if s.t - self._series_t >= 1.0:
            self._series_t = s.t
            ser = self.series
            ser["t"].append(round(s.t, 1))
            ser["wall"].append(round(s.wall, 1))
            ser["alt_ft"].append(round((s.alt or 0) / FT))
            ser["g"].append(round(s.g or 0, 2))
            ser["ias_kt"].append(round((s.ias or 0) / KT))
            ser["fuel_kg"].append(None if s.fuel_kg is None else round(s.fuel_kg))

    # ------------------------------------------------------------------- yakıt
    def burn_rate(self) -> float | None:
        """Son ~2 dakikanın ortalama tüketimi (kg/s)."""
        if len(self.burn_hist) >= 5:
            return sum(b for _, b in self.burn_hist) / len(self.burn_hist)
        if len(self.fuel_hist) >= 2:
            (t0, f0), (t1, f1) = self.fuel_hist[0], self.fuel_hist[-1]
            if t1 - t0 >= 30 and f0 > f1:
                return (f0 - f1) / (t1 - t0)
        return None

    def home_distance(self, s: FlightState) -> float | None:
        if self.home is None or s.x is None or s.z is None:
            return None
        return math.hypot(s.x - self.home[0], s.z - self.home[1])

    def home_nav(self, s: FlightState) -> dict[str, float] | None:
        """Üsse yön (gerçek kuzeye göre derece), mesafe (nm) ve dönülecek açı (+ sağa, - sola)."""
        if self.home is None or s.x is None or s.z is None:
            return None
        dx, dz = self.home[0] - s.x, self.home[1] - s.z  # DCS: x kuzey, z doğu
        out = {"bearing": math.degrees(math.atan2(dz, dx)) % 360, "nm": math.hypot(dx, dz) / NM}
        if s.heading is not None:
            out["turn"] = (out["bearing"] - s.heading + 540) % 360 - 180
        return out

    def fuel_status(self, s: FlightState) -> dict[str, float] | None:
        burn = self.burn_rate()
        if s.fuel_kg is None or not burn or burn <= 0:
            return None
        left_min = s.fuel_kg / burn / 60
        out = {"left_min": left_min, "burn_kgs": burn}
        dist = self.home_distance(s)
        if dist is not None:
            speed = max(s.tas or s.ias or 0, 120.0)
            home_min = dist / speed / 60
            # Eve dönüşü seyir tüketimiyle hesapla: art yakıcıyla ölçülen anlık tüketim abartır.
            cruise = min(burn, 1.2)
            need = cruise * home_min * 60 + self.profile.reserve_kg
            out.update(home_min=home_min, home_nm=dist / NM, margin_min=(s.fuel_kg - need) / burn / 60)
        return out

    def _home_alert(self, s: FlightState) -> None:
        """'Üsse 10 mil kalınca söyle' (flight alert_home): mesafe altına inince bir kez söyle."""
        if self.home_alert_nm is None:
            return
        nav = self.home_nav(s)
        if nav and nav["nm"] <= self.home_alert_nm:
            self.home_alert_nm = None
            self.say(f"Üsse {nav['nm']:.0f} mil kaldı.", "warn", "calm")

    def _fuel(self, s: FlightState) -> None:
        if s.fuel_kg is None:
            return
        if self.fuel_hist and s.fuel_kg > self.fuel_hist[-1][1] + 150:
            # Havada ikmal / yakıt eklendi: uyarıları yeniden kur.
            self.fuel_hist.clear()
            self.burn_hist.clear()
            self.fuel_flags.clear()
        self.fuel_hist.append((s.t, s.fuel_kg))
        while self.fuel_hist and s.t - self.fuel_hist[0][0] > 120:
            self.fuel_hist.popleft()
        if s.burn_kgs is not None and s.burn_kgs > 0:
            self.burn_hist.append((s.t, s.burn_kgs))
            while self.burn_hist and s.t - self.burn_hist[0][0] > 120:
                self.burn_hist.popleft()
        st = self.fuel_status(s)
        if not st:
            return
        left = st["left_min"]
        if "margin_min" in st:
            m = st["margin_min"]
            if m <= 0 and "bingo" not in self.fuel_flags:
                self.fuel_flags.update({"bingo", "joker"})
                self.say(f"Bingo. Şimdi eve dön. Mevcut tüketimle {fmt_min(left)}lık yakıtın var, "
                         f"üs {st['home_nm']:.0f} mil uzakta.", "critical", self._urgent())
            elif m <= 5 and "joker" not in self.fuel_flags:
                self.fuel_flags.add("joker")
                self.say(f"{fmt_min(left).capitalize()}lık yakıtın kaldı. Eve dönmek için yaklaşık {fmt_min(m)} içinde "
                         f"ayrılman gerekiyor.", "warn", self._urgent())
        else:
            for limit, flag in ((10, "low10"), (5, "low5")):
                if left <= limit and flag not in self.fuel_flags:
                    self.fuel_flags.add(flag)
                    self.say(f"Mevcut tüketimle {fmt_min(left)}lık yakıtın kaldı.", "warn" if limit == 10 else "critical",
                             self._urgent())

    def _urgent(self) -> str:
        """Nabız yüksekse ALICE acil değil sakin ama net konuşur."""
        hr = self.hr_now()
        return "calm" if hr is not None and hr >= 120 else "urgent"

    # --------------------------------------------------------------------- G
    def _g(self, s: FlightState) -> None:
        p = self.profile
        if s.g is None:
            return
        if s.g >= p.g_warn:
            self.g_since = s.t if self.g_since is None else self.g_since
            if s.t - self.g_since >= p.g_warn_seconds and s.t - self.g_last_warn > 60:
                self.g_last_warn = s.t
                self.say(f"Uzun süredir {s.g:.0f} G üstündesin, biraz rahatla.", "warn", "serious")
        else:
            self.g_since = None

    # ---------------------------------------------------------------- chaff/flare
    def _countermeasures(self, s: FlightState) -> None:
        for kind, name, low in (("chaff", "Chaff", self.profile.chaff_low), ("flare", "Flare", self.profile.flare_low)):
            n = getattr(s, kind)
            if n is None:
                continue
            last = self.cm_last.get(kind)
            self.cm_last[kind] = n
            if last is None:
                continue
            if n > last:  # yeniden dolduruldu
                self.cm_flags.discard(f"{kind}_low")
                self.cm_flags.discard(f"{kind}_out")
                continue
            if n == 0 and last > 0 and f"{kind}_out" not in self.cm_flags:
                self.cm_flags.add(f"{kind}_out")
                self.say(f"{name} bitti.", "warn", "serious")
            elif 0 < n <= low < last and f"{kind}_low" not in self.cm_flags:
                self.cm_flags.add(f"{kind}_low")
                self.say(f"{name} azaldı, {n} kaldı.", "warn", "serious")

    # --------------------------------------------------------------- yaklaşma
    def in_approach(self, s: FlightState) -> bool:
        low = s.agl is not None and 1.0 < s.agl < 600  # teker yere değdikten sonra yaklaşma bitti
        slow = 30 < (s.ias or 1e9) < self.profile.approach_ias_max
        return (s.gear or 0) > 0.9 and slow and (low or s.agl is None)

    def _approach(self, s: FlightState) -> None:
        if not self.in_approach(s) or s.aoa is None:
            self.appr_last_kind = ""
            return
        lo, hi = self.profile.aoa_on_speed
        on_speed = lo <= s.aoa <= hi
        final = s.agl is not None and s.agl < 300
        if final:
            self.appr_samples.append(on_speed)
            if s.vv is not None:
                self.max_sink_final = max(self.max_sink_final or 0.0, -s.vv)
        if final and s.vv is not None and s.vv < -7:
            kind, text = "sink", "Çok hızlı alçalıyorsun."
        elif s.aoa < lo:
            kind, text = "fast", "Hızlısın."
        elif s.aoa > hi:
            kind, text = "slow", "Yavaşsın, güç ver."
        else:
            kind, text = "on", "On speed, iyi."
        if kind == "on":
            # "On speed" sadece bir düzeltmeden sonra söylenir; doğru giderken susar.
            say_it = self.appr_last_kind not in ("", "on")
        else:
            say_it = kind != self.appr_last_kind or s.t - self.appr_last_say >= self.approach_every
        if say_it:
            self.appr_last_say = s.t
            self.say(text, "info", "calm" if kind == "on" else "serious")
        self.appr_last_kind = kind

    # ------------------------------------------------------------ uçuş sonrası
    def _finish(self, s: FlightState, landed: bool) -> None:
        t0 = self.takeoff
        if t0 is None:
            return
        fuel_used = None
        if t0.fuel_kg is not None and s.fuel_kg is not None:
            fuel_used = max(0.0, t0.fuel_kg - s.fuel_kg)
        cm_used = {}
        for kind in ("chaff", "flare"):
            a, b = getattr(t0, kind), getattr(s, kind)
            cm_used[kind] = (a - b) if a is not None and b is not None and a >= b else None
        pct = 100 * sum(self.appr_samples) / len(self.appr_samples) if len(self.appr_samples) >= 5 else None
        touchdown = self._touchdown_vv if landed else None
        d = Debrief(
            aircraft=self.profile.name if self.profile.name != "uçak" else (self.aircraft or "uçak"),
            source=s.source, start_wall=t0.wall, end_wall=s.wall, duration_s=s.t - t0.t,  # oyun zamanı (duraklatma dahil değil)
            max_g=self.stats["max_g"], max_ias_kt=self.stats["max_ias"] / KT, max_alt_ft=self.stats["max_alt"] / FT,
            fuel_used_kg=fuel_used, min_fuel_kg=self.stats["min_fuel"], approach_on_speed_pct=pct,
            max_sink_final_ms=self.max_sink_final, touchdown_vv_ms=touchdown, chaff_used=cm_used["chaff"],
            flare_used=cm_used["flare"], hr=self.hr_stats(t0.wall, s.wall), series=self.series)
        if d.duration_s < 60:
            return
        self.on_debrief(d)
        self.say(d.summary(), "info", "calm")

    def note_touchdown(self, s: FlightState) -> None:
        """Teker yere değdiği an (agl ~0) son dikey hızı sakla."""
        self._touchdown_vv = s.vv


class TouchdownTracker:
    """agl'nin sıfıra indiği anı yakalar ve companion'a bildirir (update'ten önce çağrılır)."""

    def __init__(self, companion: FlightCompanion):
        self.c = companion
        self.prev: FlightState | None = None

    def feed(self, s: FlightState) -> None:
        p = self.prev
        if (p is not None and self.c.phase == "air" and p.agl is not None and s.agl is not None
                and p.agl > 0.5 >= s.agl and p.vv is not None):
            self.c.note_touchdown(p)
        self.prev = s
        self.c.update(s)


# ---------------------------------------------------------------- rapor
def _bearing_nm(s: FlightState, x: float, z: float) -> tuple[float, float] | None:
    if s.x is None or s.z is None:
        return None
    dx, dz = x - s.x, z - s.z
    return math.degrees(math.atan2(dz, dx)) % 360, math.hypot(dx, dz) / NM


def describe_extra(s: FlightState) -> list[str]:
    """Ek göstergeleri kısa Türkçe satırlara çevir (sadece oyunun o uçak için verdiği değerler)."""
    e = s.extra
    out: list[str] = []
    bits = []
    if e.get("mach") is not None:
        bits.append(f"Mach {e['mach']:.2f}")
    if e.get("mag_heading") is not None:
        bits.append(f"manyetik yön {e['mag_heading']:03.0f}°")
    elif s.heading is not None:
        bits.append(f"yön {s.heading:03.0f}°")
    if e.get("pitch") is not None and e.get("bank") is not None:
        bits.append(f"burun {e['pitch']:+.0f}°, yatış {e['bank']:+.0f}°")
    if s.vv is not None:
        bits.append(f"dikey hız {s.vv / FT * 60:+,.0f} ft/dk")
    if e.get("radalt") is not None:
        bits.append(f"radar irtifa {e['radalt']:,.0f} ft")
    if bits:
        out.append("  " + ", ".join(bits))
    eng = []
    if e.get("rpm") is not None:
        eng.append(f"devir %{e['rpm']:.0f}")
    if e.get("throttle") is not None:
        eng.append(f"gaz %{e['throttle']:.0f}")
    if e.get("egt") is not None:
        eng.append(f"motor sıcaklığı {e['egt']:.0f}°C")
    if e.get("oil_temp") is not None:
        eng.append(f"yağ {e['oil_temp']:.0f}°C")
    if e.get("water_temp") is not None:
        eng.append(f"su {e['water_temp']:.0f}°C")
    if e.get("fuel_pct") is not None:
        eng.append(f"yakıt %{e['fuel_pct']:.0f}")
    if eng:
        out.append("  motor: " + ", ".join(eng))
    cfg = []
    for k, label in (("flaps", "flap"), ("speedbrake", "hız freni"), ("hook", "kanca")):
        v = e.get(k)
        if v is not None and v > 0.05:
            cfg.append(f"{label} %{v * 100:.0f}" if v <= 1 else f"{label} %{v:.0f}")
    if s.gear is not None:
        cfg.append("teker aşağı" if s.gear > 0.9 else "teker yukarı" if s.gear < 0.1 else "teker hareket halinde")
    if cfg:
        out.append("  " + ", ".join(cfg))
    if e.get("wind_kt") is not None and e["wind_kt"] >= 1:
        out.append(f"  rüzgar {e['wind_from']:03.0f}° yönünden {e['wind_kt']:.0f} kt")
    if e.get("wp_x") is not None and e.get("wp_z") is not None:
        bn = _bearing_nm(s, e["wp_x"], e["wp_z"])
        if bn:
            num = f" {int(e['wp_num'])}" if e.get("wp_num") is not None else ""
            out.append(f"  sıradaki waypoint{num}: {bn[0]:03.0f}° (gerçek), {bn[1]:.1f} nm")
    if e.get("ils_glide") is not None and e.get("ils_side") is not None and (e["ils_glide"] or e["ils_side"]):
        g, sd = e["ils_glide"], e["ils_side"]
        out.append(f"  ILS: süzülüş {'yukarıda' if g > 0 else 'aşağıda'} ({g:+.2f}), "
                   f"merkez {'sağda' if sd > 0 else 'solda'} ({sd:+.2f})")
    if e.get("weapons") or e.get("gun") is not None:
        w = e.get("weapons") or "yok"
        gun = f", top mermisi {int(e['gun'])}" if e.get("gun") is not None else ""
        out.append(f"  silahlar: {w}{gun}")
    if e.get("warnings"):
        out.append(f"  UYARI / ARIZA: {e['warnings']}")
    if e.get("mode"):
        out.append(f"  sistem modu: {e['mode']}")
    return out


def flights_dir() -> Path:
    return alice_home() / "flights"


def _svg_line(xs: list[float], ys: list, w: int = 720, h: int = 140, color: str = "var(--a)", label: str = "") -> str:
    pts = [(x, y) for x, y in zip(xs, ys) if y is not None]
    if len(pts) < 2:
        return ""
    x0, x1 = pts[0][0], pts[-1][0]
    lo, hi = min(y for _, y in pts), max(y for _, y in pts)
    if hi - lo < 1e-9:  # sabit değer: çizgiyi ortala
        lo, hi = lo - 1, hi + 1
    span_x, span_y = (x1 - x0) or 1, hi - lo
    path = " ".join(f"{'M' if i == 0 else 'L'}{(x - x0) / span_x * (w - 50) + 45:.1f},"
                    f"{h - 18 - (y - lo) / span_y * (h - 30):.1f}" for i, (x, y) in enumerate(pts))
    return (f'<figure><figcaption>{label}</figcaption><svg viewBox="0 0 {w} {h}" role="img" aria-label="{label}">'
            f'<text x="0" y="14" class="ax">{hi:.0f}</text><text x="0" y="{h - 18}" class="ax">{lo:.0f}</text>'
            f'<line x1="45" y1="{h - 18}" x2="{w - 5}" y2="{h - 18}" class="base"/>'
            f'<path d="{path}" fill="none" stroke="{color}" stroke-width="1.8"/></svg></figure>')


def save_debrief(d: Debrief, hr_series: list[tuple[float, float]] | None = None, out: Path | None = None) -> Path:
    out = out or flights_dir()
    out.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M", time.localtime(d.start_wall))
    (out / f"{stamp}.json").write_text(json.dumps(asdict(d), ensure_ascii=False), encoding="utf-8")
    ser = d.series
    charts = [_svg_line(ser["t"], ser["alt_ft"], label="İrtifa (ft)"), _svg_line(ser["t"], ser["g"], label="G"),
              _svg_line(ser["t"], ser["ias_kt"], label="Hız (knot)"),
              _svg_line(ser["t"], ser["fuel_kg"], label="Yakıt (kg)")]
    if hr_series and ser.get("wall"):
        # Nabız duvar saatiyle gelir; uçuşun oyun zamanı eksenine taşı.
        w0, t0 = ser["wall"][0], ser["t"][0]
        charts.append(_svg_line([t0 + (t - w0) for t, _ in hr_series], [v for _, v in hr_series], color="var(--hr)",
                                label="Nabız"))
    rows = [("Süre", fmt_min(d.duration_s / 60)), ("En yüksek G", f"{d.max_g:.1f}"),
            ("En yüksek hız", f"{d.max_ias_kt:.0f} kt"), ("En yüksek irtifa", f"{d.max_alt_ft:,.0f} ft")]
    if d.fuel_used_kg is not None:
        rows.append(("Kullanılan yakıt", f"{d.fuel_used_kg:.0f} kg"))
    if d.approach_on_speed_pct is not None:
        rows.append(("Yaklaşma on speed", f"%{d.approach_on_speed_pct:.0f}"))
    if d.touchdown_vv_ms is not None:
        rows.append(("Teker koyma", f"{abs(d.touchdown_vv_ms):.1f} m/s ({abs(d.touchdown_vv_ms) / FT * 60:.0f} fpm)"))
    for k, n in (("Chaff", d.chaff_used), ("Flare", d.flare_used)):
        if n:
            rows.append((f"Kullanılan {k.lower()}", str(n)))
    if d.hr:
        rows.append(("Nabız", f"ort {d.hr['avg']:.0f}, en yüksek {d.hr['max']:.0f}"))
    table = "".join(f"<tr><th>{k}</th><td>{v}</td></tr>" for k, v in rows)
    when = time.strftime("%d.%m.%Y %H:%M", time.localtime(d.start_wall))
    page = out / f"{stamp}.html"
    page.write_text(f"""<!doctype html><html lang="tr"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Uçuş {when}</title><style>
:root{{--bg:#faf9f7;--fg:#222;--mut:#777;--line:#e4e1dc;--a:#c6613f;--hr:#3f7fc6}}
@media (prefers-color-scheme:dark){{:root{{--bg:#1c1b1a;--fg:#eee;--mut:#999;--line:#333;--a:#d97757;--hr:#6fa3e0}}}}
body{{background:var(--bg);color:var(--fg);font:15px system-ui,sans-serif;margin:24px 16px;max-width:760px}}
th{{text-align:left;color:var(--mut);font-weight:400;padding:4px 16px 4px 0}} figure{{margin:18px 0}}
figcaption{{color:var(--mut);font-size:13px}} svg{{width:100%;height:auto}} .ax{{fill:var(--mut);font-size:11px}}
.base{{stroke:var(--line)}}
</style></head><body><h1>{d.aircraft} · {when}</h1><p>{d.summary()}</p><table>{table}</table>{''.join(charts)}
</body></html>""", encoding="utf-8")
    return page
