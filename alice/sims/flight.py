"""Uçuş arkadaşı.

F-16'nın kendi sesli uyarıları zaten var; ALICE onları tekrarlamaz. Onun yerine bir
uçuş arkadaşı gibi bakar:
  - Yakıt: üs biliniyorsa joker/bingo (miktara göre), bilinmiyorsa son dakikaların ortalamasıyla kalan süre
  - Yaklaşma: teker açık, alçakta, yavaşken AoA ve alçalma hızına kısa geri bildirim
  - Uzun süre yüksek G, chaff/flare azalması
  - İnişten sonra kısa değerlendirme (+ saat verisi varsa nabız)
  - Pilotun kurduğu hatırlatmalar: "10 bin feet'e gelince söyle", "yakıt 1500 kiloya inince söyle"
  - Kendi yer yaklaşma uyarısı olmayan uçaklarda yere hızla yaklaşma, açık unutulan flap, boşalan dış tank

Kaynaktan (DCS, War Thunder) bağımsızdır: hepsi FlightState üretir, FlightCompanion tüketir.
Birimler SI: m, m/s, kg, derece.
"""

from __future__ import annotations

import json
import math
import re
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
    cruise_kg_nm: float = 5.0        # eve dönüş seyrinde mil başına yakıt (bingo hesabı)
    aoa_on_speed: tuple[float, float] = (11.0, 15.0)
    approach_ias_max: float = 100.0  # m/s, bunun altında ve teker açıksa yaklaşma sayılır
    g_warn: float = 7.0
    g_warn_seconds: float = 8.0      # dogfight'ta kısa çekişler normal: sadece uzun süren yüksek G
    chaff_low: int = 20
    flare_low: int = 10
    has_gpws: bool = False           # uçağın kendi yer yaklaşma uyarısı var: ALICE tekrarlamaz
    auto_flaps: bool = False         # flaplar otomatik: "flap açık kaldı" hatırlatması anlamsız


PROFILES = {
    "F-16C_50": Profile("F-16C", internal_fuel_kg=3249, reserve_kg=450, cruise_kg_nm=4.0, aoa_on_speed=(11.0, 15.0),
                        approach_ias_max=95, g_warn=7.5, has_gpws=True, auto_flaps=True),
    "FA-18C_hornet": Profile("F/A-18C", internal_fuel_kg=4900, reserve_kg=680, cruise_kg_nm=6.0,
                             aoa_on_speed=(7.4, 8.8),
                             approach_ias_max=80, g_warn=6.5, has_gpws=True, auto_flaps=True),
    "default": Profile("uçak"),
}


def profile_for(aircraft: str) -> Profile:
    base = PROFILES.get(aircraft) or PROFILES["default"]
    learned = learned_cruise(base.name if base.name != "uçak" else aircraft)
    if learned is None:
        return base
    from dataclasses import replace

    # tek tük bozuk parça yüzünden bingo saçmalamasın: elle verilen değerin yarısı ile iki katı arası
    return replace(base, cruise_kg_nm=min(max(learned, base.cruise_kg_nm / 2), base.cruise_kg_nm * 2))


# --------------------------------------------------------------- uçuşlardan öğrenilen seyir yakıtı
# Mil başına yakıt elle tahmin ediliyordu (F-16 4, F/A-18 6, diğerleri 5). Kayıtlı uçuşların düz, sakin
# bölümlerinden (G ~1, irtifa sabite yakın, yakıt sıçramasız) gerçek değer çıkarılır ve bingo bunu kullanır.
CRUISE_MIN_S = 60      # bir seyir parçası en az bu kadar sürmeli
CRUISE_MAX_S = 300
CRUISE_MIN_PARTS = 3   # bundan azsa elle verilen değer kalır
_learned_cache: dict[str, Any] = {}


def cruise_parts(series: dict[str, list]) -> list[float]:
    """Bir uçuşun seyir parçalarında mil başına yakıt (kg/nm). Hız IAS'tan kaba TAS'a çevrilir (%2 / 1000 ft)."""
    t, alt, g, ias, fuel = (series.get(k) or [] for k in ("t", "alt_ft", "g", "ias_kt", "fuel_kg"))
    out: list[float] = []
    start = 0
    nm = 0.0

    def close(i: int) -> None:
        if t[i] - t[start] >= CRUISE_MIN_S and nm > 3 and fuel[start] - fuel[i] > 0:
            out.append((fuel[start] - fuel[i]) / nm)

    n = min(len(t), len(alt), len(g), len(ias), len(fuel))
    for i in range(1, n):
        dt = t[i] - t[i - 1]
        ok = (fuel[i] is not None and fuel[i - 1] is not None and 0 < dt <= 10 and 0.6 <= (g[i] or 0) <= 1.6
              and (ias[i] or 0) > 150 and 0 <= fuel[i - 1] - fuel[i] <= 5 * dt  # üstü tank atma ya da AB
              and abs(alt[i] - alt[start]) < 2000)
        if not ok:
            close(i - 1)
            start, nm = i, 0.0
            continue
        nm += ias[i] * (1 + 0.02 * max(alt[i], 0) / 1000) * dt / 3600
        if t[i] - t[start] >= CRUISE_MAX_S:
            close(i)
            start, nm = i, 0.0
    if n > 1 and start < n - 1:  # uçuş seyirdeyken bitti
        close(n - 1)
    return out


def learn_cruise(folder: Path | None = None) -> dict[str, dict[str, float]]:
    """Kayıtlı bütün uçuşlardan uçak başına seyir yakıtı; ~/.alice/flights/cruise.json'a yazılır."""
    folder = folder or flights_dir()
    parts: dict[str, list[float]] = {}
    for f in sorted(folder.glob("*.json")):
        if f.name == "cruise.json":
            continue
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if d.get("aircraft") and d.get("series"):
            parts.setdefault(d["aircraft"], []).extend(cruise_parts(d["series"]))
    result = {}
    for ac, xs in parts.items():
        xs = sorted(x for x in xs[-200:] if 0.5 <= x <= 40)  # son uçuşlar
        if len(xs) >= CRUISE_MIN_PARTS:
            # Çatışma ve art yakıcılı parçalar yukarı çeker; eve dönüş ekonomik uçulur: alt üçte bir
            result[ac] = {"kg_nm": round(xs[len(xs) // 3], 2), "parts": len(xs)}
    try:
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "cruise.json").write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
    except OSError:
        pass
    _learned_cache.clear()
    return result


def learned_cruise(aircraft: str, folder: Path | None = None) -> float | None:
    path = (folder or flights_dir()) / "cruise.json"
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return None
    if _learned_cache.get("mtime") != (path, mtime):
        try:
            _learned_cache.update(mtime=(path, mtime), data=json.loads(path.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            return None
    entry = (_learned_cache.get("data") or {}).get(aircraft)
    return float(entry["kg_nm"]) if entry else None


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
        if self.fuel_used_kg:  # 0: havada doğdu ya da yakıt ikmali, söylemenin anlamı yok
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


# ------------------------------------------------------------------------------- uçuş koçluğu
def load_history(folder: Path | None = None, exclude: float | None = None, limit: int = 80) -> list[dict[str, Any]]:
    """Kayıtlı uçuşlar, eskiden yeniye (seri verisi olmadan). `exclude`: az önce kaydedilen bu uçuşun başlangıcı."""
    out = []
    for f in sorted((folder or flights_dir()).glob("2*.json"))[-limit:]:
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if exclude is not None and abs(float(d.get("start_wall") or 0) - exclude) < 1:
            continue
        d.pop("series", None)
        out.append(d)
    return out


def _avg(xs: list[float]) -> float:
    return sum(xs) / len(xs)


def coach(d: Debrief, history: list[dict[str, Any]]) -> str:
    """Önceki uçuşlarla karşılaştırıp en fazla iki cümle: gelişme/gerileme ve bir sonraki uçuş için tek hedef.

    İniş ve yaklaşma bütün uçuşlarla, yakıt ve kişisel rekorlar aynı uçakla karşılaştırılır. Az geçmiş varsa susar."""
    out: list[str] = []
    lands = [abs(h["touchdown_vv_ms"]) for h in history if h.get("touchdown_vv_ms") is not None][-5:]
    if d.touchdown_vv_ms is not None and len(lands) >= 3:
        v, avg = abs(d.touchdown_vv_ms), _avg(lands)
        if v < min(lands):
            out.append(f"Son {len(lands)} inişinin en yumuşağıydı (ortalaman {avg:.1f}).")
        elif v < avg - 0.3:
            out.append(f"Son {len(lands)} inişin ortalaması {avg:.1f}, bu {v:.1f}: gelişiyorsun.")
        elif v > avg + 0.5:
            out.append(f"Son {len(lands)} inişin ortalaması {avg:.1f}, bu {v:.1f}; bir dahakine son 50 feet'te "
                       "alçalmayı biraz daha kes.")
    appr = [h["approach_on_speed_pct"] for h in history if h.get("approach_on_speed_pct") is not None][-5:]
    if d.approach_on_speed_pct is not None and len(appr) >= 3 and len(out) < 2:
        p, avg = d.approach_on_speed_pct, _avg(appr)
        if p >= avg + 15:
            out.append(f"Yaklaşmada on speed oranın ortalamanın ({avg:.0f}) epey üstündeydi.")
        elif p <= avg - 15:
            out.append(f"Yaklaşma ortalamanın altındaydı (yüzde {avg:.0f} yerine {p:.0f}); hedef: AoA'yı on speed'de "
                       "tutup gazla düzelt.")
    same = [h for h in history if h.get("aircraft") == d.aircraft and (h.get("duration_s") or 0) >= 60]
    if len(out) < 2 and len(same) >= 3:
        if d.duration_s > max(h["duration_s"] for h in same):
            out.append(f"Bu uçakla en uzun uçuşundu ({fmt_min(d.duration_s / 60)}).")
        elif d.fuel_used_kg and all(h.get("fuel_used_kg") for h in same[-5:]):
            rate = d.fuel_used_kg / (d.duration_s / 60)
            avg = _avg([h["fuel_used_kg"] / (h["duration_s"] / 60) for h in same[-5:]])
            if rate < avg * 0.8:
                out.append(f"Dakikada {rate:.0f} kilo yaktın, bu uçaktaki ortalamandan ({avg:.0f}) belirgin az.")
            elif rate > avg * 1.3:
                out.append(f"Dakikada {rate:.0f} kilo yaktın, ortalaman {avg:.0f}: art yakıcıda çok kaldın.")
    return " ".join(out[:2])


class FlightCompanion:
    """FlightState akışını izler, söylenecekleri say(text, severity, emotion) ile iletir.

    show(kind, **alanlar) verilirse tehditler ekran üstü HUD'a da ok/kenar parlaması olarak gider; uçuş kodu HUD'u
    bilmez (bkz. alice.hud.client.show)."""

    def __init__(self, say: Callable[[str, str, str], None], hr_stats: Callable[[float, float], dict | None] | None = None,
                 hr_now: Callable[[], float | None] | None = None,
                 on_debrief: Callable[[Debrief], None] | None = None,
                 approach_every: float = 4.0, show: Callable[..., Any] | None = None):
        self.say = say
        self.show = show or (lambda kind, **kw: None)
        self.hr_stats = hr_stats or (lambda a, b: None)
        self.hr_now = hr_now or (lambda: None)
        self.on_debrief = on_debrief or (lambda d: None)
        self.approach_every = approach_every
        # Tehdit seslerinin son zamanları ölünce/yeniden doğunca sıfırlanmaz: War Thunder'da her doğuşta aynı
        # gemiler, aynı uçaksavarlar yeniden "menzilindesin" diye sayılıyordu.
        self.threat_mem: dict[str, float] = {}
        self.coach_flags: set[str] = set()  # ölüm desenleri (alice/sims/coach.py): maç içinde bir kez öğüt
        self.calm = False                   # tilt (alice/mood.py): daha az konuş
        self.reset()

    def reset(self) -> None:
        self.phase = "ground"            # ground | air | landing_roll
        self.profile = PROFILES["default"]
        self.aircraft = ""
        self.fuel_hist: deque[tuple[float, float]] = deque()
        self.burn_hist: deque[tuple[float, float]] = deque()
        self.home: tuple[float, float] | None = None
        self.home_alert_nm: float | None = None
        self.warn_seen: set[str] = set()
        self.heat_flags: set[str] = set()
        self.gear_last_warn = -1e9
        self.gear_fast_said = False
        self.wp_called: float | None = None
        self.weapons_last: dict[str, int] = {}
        self.gun_start: float | None = None
        self.gun_flag = False
        self.fuel_flags: set[str] = set()
        self.fuel_cand: dict[str, float] = {}     # uyarı koşulunun ilk görüldüğü an (kısa sıçramalar susar)
        self.endurance0: float | None = None      # ilk ölçümde toplam dayanım (dk): az yakıtla çıkıldı mı
        self.said_at: dict[str, float] = {}       # anahtar -> son söylendiği sim zamanı
        self.info_last = -1e9
        self.g_since: float | None = None
        self.g_last_warn = -1e9
        self.cm_last: dict[str, int] = {}
        self.cm_flags: set[str] = set()
        self.appr_last_say = -1e9
        self.appr_last_kind = ""
        self.appr_cand, self.appr_cand_since = "", 0.0
        self.appr_samples: list[bool] = []
        self.max_sink_final: float | None = None
        self.slow_since: float | None = None
        self.last_state: FlightState | None = None
        self.takeoff: FlightState | None = None
        self.stats = {"max_g": 0.0, "max_ias": 0.0, "max_alt": 0.0, "min_fuel": None}
        self._g_prev = 0.0
        self.series: dict[str, list] = {"t": [], "wall": [], "alt_ft": [], "g": [], "ias_kt": [], "fuel_kg": []}
        self._series_t = -1e9
        self.restricted_said = False
        self._touchdown_vv: float | None = None
        self.alt_alert_ft: float | None = None   # pilotun kurduğu tek seferlik irtifa hatırlatması
        self.alt_alert_above: bool | None = None  # kurulduğunda hedefin üstünde miydik
        self.fuel_alert_kg: float | None = None
        self.ground_last_warn = -1e9
        self.ground_armed = True                 # yer uyarısı kurulu mu (söyleyince güvenli dönüşe kadar susar)
        self.ground_safe_since: float | None = None
        self.flaps_since: float | None = None
        self.flaps_said = False
        self.ext_last: float | None = None
        self.ext_said = False
        self.threat_seen: dict[str, float] = {}   # RWR yayıcı/kilit/füze anahtarı -> son görüldüğü an
        self.threat_said: dict[str, float] = {}
        self.bandit_close = False                 # en yakın düşman 5 km içinde (7 km'de yeniden kurulur)
        self.nose_since: float | None = None      # War Thunder: düşmanın burnu bize dönük (füze atabilir) başlangıcı
        self.sam_inside: set[str] = set()         # menzilinde olduğumuz hava savunması türleri
        self.coached: set[str] = set()            # bu oturumda verilen öğütler
        self.hud_on: dict[str, float] = {}        # HUD oku kimliği -> bu görülmenin başladığı an
        self.hud_seen: dict[str, float] = {}      # tehdidin son görüldüğü an (ara verip dönerse yeni görülme)
        self.hud_sent: dict[str, float] = {}

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
        self._warnings(s)
        self._engine_heat(s)
        self._gear_check(s)
        self._waypoint(s)
        self._weapons(s)
        self._pilot_alerts(s)
        self._ground(s)
        self._flaps(s)
        self._ext_tanks(s)
        self._contacts(s)
        self._hud(s)

    # ------------------------------------------------------------------- tehditler
    SIX_REPEAT = 45.0       # arkadaki düşman uyarısı en sık (20 sn'de bir it dalaşında hiç susmuyordu)
    SIX_RANGE = 2000.0
    MERGE_RANGE = 5000.0
    LOST_AFTER = 10.0       # bu kadar görünmeyen kilit/füze bitmiş sayılır
    NOSE_ASPECT = 12.0      # War Thunder: düşmanın burnu bize bu açıdan dar dönükse "füze atabilir"
    NOSE_RANGE = (2500.0, 14000.0)
    NOSE_HOLD = 2.0         # bu kadar sürmeli (harita saniyede bir yenilenir, tek okuma yanıltabilir)
    NOSE_REPEAT = 60.0
    SAM_REPEAT = 300.0      # aynı tür hava savunması (aynı maçta yeniden doğunca da)
    SAM_ANY = 45.0          # herhangi bir hava savunması (gemi grubundaki muhrip + fırkateyn tek anons)
    MERGE_REPEAT = 90.0     # "Düşman saat 2, 3 kilometre"
    THREAT_GAP = 12.0       # arkadaki / burnu dönük / yaklaşan düşman ve SAM sesleri arasında en az (füze, kilit hariç)
    HUD_FOLLOW = 20.0       # tehdit görüldükten sonra ok (ses susturulsa da) en fazla bu kadar yönünü izler
    HUD_EVERY = 0.5         # izlerken HUD'a en sık bu aralıkla yeni yön gider
    HUD_GAP = 5.0           # bu kadar görünmeyen tehdit dönerse yeni görülme sayılır

    @staticmethod
    def _km(m: float) -> str:
        return f"{m:.0f} metre" if m < 1000 else f"{m / 1000:.1f} kilometre".replace(".", ",")

    @staticmethod
    def _km_short(m: float) -> str:
        return f"{m:.0f} m" if m < 1000 else f"{m / 1000:.1f} km".replace(".", ",")

    def _point(self, t: float, clock: Any, text: str, severity: str, key: str, edge: bool = False,
               ttl: float = 4.0) -> None:
        """Anonsu yapılan tehdidi HUD'a da göster: saat yönünde ok, acilse ekran kenarında parlama."""
        self.hud_on[key] = self.hud_seen[key] = self.hud_sent[key] = t
        self._draw(clock, text, severity, key, edge, ttl)

    def _follow(self, t: float, clock: Any, text: str, severity: str, key: str, edge: bool = False) -> None:
        """Tehdit sürüyor ama ses susuyor (yeni söylendi ya da başka tehdit konuştu): ok yine yönünü göstersin
        (dönerken saat 6'daki düşman saat 4'e geçer). Görülme başından en fazla HUD_FOLLOW sn; kısa ömürlü ok
        tehdit kaybolunca kendiliğinden söner."""
        if t - self.hud_seen.get(key, -1e9) > self.HUD_GAP:
            self.hud_on[key] = t
        self.hud_seen[key] = t
        if t - self.hud_on[key] > self.HUD_FOLLOW or t - self.hud_sent.get(key, -1e9) < self.HUD_EVERY:
            return
        self.hud_sent[key] = t
        self._draw(clock, text, severity, key, edge, 2.5)

    def _draw(self, clock: Any, text: str, severity: str, key: str, edge: bool, ttl: float) -> None:
        try:  # HUD hatası anonsu bozmasın
            self.show("arrow", clock=clock, text=text, severity=severity, id=key, ttl=ttl)
            if edge:
                self.show("edge", clock=clock, severity=severity, id=f"{key}.edge", ttl=ttl)
        except Exception:  # noqa: BLE001
            pass

    def _label(self, text: str, severity: str, key: str, ttl: float) -> None:
        """HUD'a kısa yazı (yakıt gibi); konum verilmez, HUD üst-ortaya çizer."""
        try:  # HUD hatası anonsu bozmasın
            self.show("label", text=text, severity=severity, id=key, ttl=ttl)
        except Exception:  # noqa: BLE001
            pass

    def _hint(self, *keys: str) -> str:
        """Ölüm desenine uyan kısa öğüt, oturumda bir kez (anons değil koçluk)."""
        from alice.sims.coach import HINTS

        for k in keys:
            if k in self.coach_flags and k not in self.coached:
                self.coached.add(k)
                return " " + HINTS[k]
        return ""

    def _may_say(self, t: float, key: str, every: float) -> bool:
        """Tehdit sesine izin: bu anahtar en sık 'every' sn'de bir, bütün tehdit sesleri arasında en az THREAT_GAP.
        İzin verirse zamanı yazar. Füze ve kilit bundan geçmez, her zaman söylenir."""
        m = self.threat_mem
        if any(v > t + 5 for v in m.values()):  # yeni görev: sim zamanı başa döndü
            m.clear()
        gap = self.THREAT_GAP * (2 if self.calm else 1)  # sinirliyken daha az konuş
        if t - m.get(key, -1e9) < every or t - m.get("threat", -1e9) < gap:
            return False
        m[key] = m["threat"] = t
        return True

    def _contacts(self, s: FlightState) -> None:
        """RWR (DCS) ve mini harita (War Thunder) temasları: füze, kilit, yeni radar, arkadaki ve yaklaşan düşman.

        F-16'nın RWR'ı zaten öter; ALICE onun söylemediğini söyler: yönü saat olarak ve radarın adı."""
        cs = s.extra.get("contacts")
        if not isinstance(cs, list):
            return
        t = s.t
        for c in cs:
            if c.get("kind") != "radar":
                continue
            base = f"{c.get('id')}|{c.get('name')}"
            for key, cond in ((f"m|{base}", c.get("missile")), (f"l|{base}", c.get("lock")), (f"s|{base}", True)):
                if not cond:
                    continue
                fresh = t - self.threat_seen.get(key, -1e9) > self.LOST_AFTER
                self.threat_seen[key] = t
                clock, name = c.get("clock"), c.get("name") or "radar"
                if not fresh:
                    if key[0] == "m":
                        self._follow(t, clock, f"FÜZE · {name}", "critical", "missile", edge=True)
                    elif key[0] == "l":
                        self._follow(t, clock, f"KİLİT · {name}", "warn", "lock")
                    continue
                if key[0] == "m" and c.get("vision"):  # ekrandan görüldü: türü bilinmiyor
                    self.say(f"Füze! Saat {clock}. Chaff at, dön!", "critical", "urgent")
                    self._point(t, clock, "FÜZE", "critical", "missile", edge=True)
                elif key[0] == "m":
                    self.say(f"Füze! Saat {clock}, {name}. Chaff at, dön!", "critical", "urgent")
                    self._point(t, clock, f"FÜZE · {name}", "critical", "missile", edge=True)
                elif key[0] == "l":
                    self.say(f"Saat {clock}'den kilit: {name}.", "warn", self._urgent())
                    self._point(t, clock, f"KİLİT · {name}", "warn", "lock")
                elif name != "?" and t - self.threat_said.get(name, -1e9) > 120:
                    self.threat_said[name] = t
                    self._info(s, f"Yeni radar: {name}, saat {clock}.")
                break  # füze varsa kilit/yeni radar ayrıca söylenmez
        self._ground_threats(s, [c for c in cs if c.get("kind") == "sam" and c.get("dist_m") is not None])
        planes = [c for c in cs if c.get("kind") == "aircraft" and c.get("dist_m") is not None]
        if not planes:
            self.bandit_close = False
            self.nose_since = None
            return
        if self._nose_on(s, planes):
            return
        near = planes[0]
        six = next((c for c in planes if c["dist_m"] <= self.SIX_RANGE and c.get("clock") in (4, 5, 6, 7, 8)), None)
        if six is not None and self._may_say(t, "six", self.SIX_REPEAT):
            self.say(f"Arkanda düşman, saat {six['clock']}, {self._km(six['dist_m'])}!"
                     + self._hint("low_turn_six", "six"), "warn", "urgent")
            self._point(t, six["clock"], f"ARKANDA · {self._km_short(six['dist_m'])}", "critical", "six", edge=True)
            self.bandit_close = True
            return
        if six is not None:
            self._follow(t, six["clock"], f"ARKANDA · {self._km_short(six['dist_m'])}", "critical", "six", edge=True)
        if near["dist_m"] <= self.MERGE_RANGE and not self.bandit_close:
            self.bandit_close = True
            if self._may_say(t, "merge", self.MERGE_REPEAT):
                self._info(s, f"Düşman saat {near['clock']}, {self._km(near['dist_m'])}.", "serious")
        elif near["dist_m"] > self.MERGE_RANGE + 2000:
            self.bandit_close = False

    def _nose_on(self, s: FlightState, planes: list[dict]) -> bool:
        """War Thunder RWR vermiyor: burnu bize dönük, füze menzilindeki düşman en yakın şey. Söylediyse True."""
        lo, hi = self.NOSE_RANGE
        hot = [c for c in planes if c.get("aspect") is not None and c["aspect"] <= self.NOSE_ASPECT
               and lo <= c["dist_m"] <= hi]
        if not hot:
            self.nose_since = None
            return False
        if self.nose_since is None:
            self.nose_since = s.t
        if s.t - self.nose_since < self.NOSE_HOLD:
            return False
        if not self._may_say(s.t, "nose", self.NOSE_REPEAT):
            c = hot[0]
            self._follow(s.t, c["clock"], f"BURNU SANA · {self._km_short(c['dist_m'])}", "warn", "nose")
            return False
        c = hot[0]
        self.say(f"Saat {c['clock']}'deki düşman burnunu sana çevirdi, {self._km(c['dist_m'])}. Füze gelebilir!",
                 "warn", self._urgent())
        self._point(s.t, c["clock"], f"BURNU SANA · {self._km_short(c['dist_m'])}", "warn", "nose")
        return True

    def _ground_threats(self, s: FlightState, sams: list[dict]) -> None:
        """Düşman SAM / uçaksavar / füzeli gemi menziline girince bir kez; çıkıp 3 km uzaklaşınca yeniden kurulur."""
        near: dict[str, dict] = {}
        for c in sams:
            if c["name"] not in near or c["dist_m"] < near[c["name"]]["dist_m"]:
                near[c["name"]] = c
        for name in list(self.sam_inside):
            c = near.get(name)
            if c is None or c["dist_m"] > float(c.get("range_m") or 0) + 3000:
                self.sam_inside.discard(name)
        fresh = []
        for name, c in near.items():
            if name in self.sam_inside:
                self._follow(s.t, c["clock"], f"{name} · {self._km_short(c['dist_m'])}", "warn", f"sam:{name}")
                continue
            if c["dist_m"] > float(c.get("range_m") or 0):
                continue
            self.sam_inside.add(name)
            if s.t - self.threat_mem.get(f"sam:{name}", -1e9) >= self.SAM_REPEAT:
                fresh.append(c)
        if fresh and self._may_say(s.t, "sam", self.SAM_ANY):
            for f in fresh:  # aynı anda menziline girilenler (gemi grubu) ayrıca söylenmez
                self.threat_mem[f"sam:{f['name']}"] = s.t
            c = min(fresh, key=lambda f: f["dist_m"])
            name = c["name"]
            self.say(f"{name[0].upper() + name[1:]} menzilindesin: saat {c['clock']}, {self._km(c['dist_m'])}."
                     + self._hint("sam"),
                     "warn", "serious")
            self._point(s.t, c["clock"], f"{name} · {self._km_short(c['dist_m'])}", "warn", f"sam:{name}")

    def _hud(self, s: FlightState) -> None:
        """War Thunder savaş kayıtları: düşürdüğün, seni vuran."""
        for kind, who in s.extra.get("hud") or []:
            if kind == "kill":
                self.say(f"Düşürdün: {who}.", "info", "cheerful")
                try:  # HUD hatası anonsu bozmasın
                    self.show("card", title="Düşürdün", text=str(who), severity="info", ttl=4.0, id="kill")
                except Exception:  # noqa: BLE001
                    pass
            elif kind == "fire":
                self.say(f"Yanıyorsun! {who} vurdu.", "critical", "urgent")
            elif kind == "hit":
                self.say(f"Ağır hasar aldın, {who} vurdu!", "warn", "urgent")

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
                # Teker kapalıyken durmak iniş değil düşüş: War Thunder'da ölünce enkaz yerde durup her ölümden
                # sonra "Uçuş 2 dakika sürdü..." söyletiyordu (1 Kasım, 21 kez)
                self._finish(s, landed=s.gear is None or s.gear >= 0.9)
                home = self.home
                self.reset()
                self.home = home
        else:
            self.slow_since = None

    def _record(self, s: FlightState) -> None:
        st = self.stats
        g = s.g or 0
        st["max_g"] = max(st["max_g"], min(g, self._g_prev))  # tek ölçümlük sıçrama (çarpma, ölüm) sayılmaz
        self._g_prev = g
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
    FUEL_WINDOW = 240.0   # sn: tüketim bu sürenin gerçek yakıt düşüşünden ölçülür
    FUEL_MIN_SPAN = 60.0  # sn: bundan kısa ölçüm tahmin sayılmaz
    MAX_BURN = 40.0       # kg/s

    def burn_rate(self) -> float | None:
        """Son ~4 dakikanın ortalama tüketimi (kg/s), yakıt göstergesinin düşüşünden.

        Oyunun verdiği anlık tüketim art yakıcıda birkaç kat zıplıyor (bazı modüllerde tek motoru iki kez
        sayıyor); ona göre "X dakika kaldı" demek her manevrada uyarı demekti. Sadece düşüşler toplanır:
        dış tanktan iç tanka aktarma göstergede küçük sıçrama yapıyor, tüketimi bozmasın."""
        h = self.fuel_hist
        if len(h) >= 2 and h[-1][0] - h[0][0] >= self.FUEL_MIN_SPAN:
            # Hiçbir motor saniyede 40 kilodan fazla yakmaz: öyle bir düşüş tank atmaktır, tüketim değil.
            used = sum(d for a, b in zip(h, list(h)[1:])
                       if 0 < (d := a[1] - b[1]) <= self.MAX_BURN * max(b[0] - a[0], 0.2))
            if used > 0:
                return used / (h[-1][0] - h[0][0])
        if len(self.burn_hist) >= 5:  # ölçüm birikene kadar oyunun değeri (sadece durum satırı için)
            return sum(b for _, b in self.burn_hist) / len(self.burn_hist)
        return None

    def measured(self) -> bool:
        h = self.fuel_hist
        return len(h) >= 2 and h[-1][0] - h[0][0] >= self.FUEL_MIN_SPAN

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
        bingo = self.bingo_kg(s)
        if bingo is not None:
            nm = self.home_distance(s) / NM
            out.update(home_nm=nm, home_min=nm * NM / max(s.tas or s.ias or 0, 200.0) / 60, bingo_kg=bingo,
                       margin_kg=s.fuel_kg - bingo, margin_min=(s.fuel_kg - bingo) / burn / 60)
        return out

    def bingo_kg(self, s: FlightState) -> float | None:
        """Eve seyirle dönüp rezervle inmek için gereken yakıt. Gerçek bingo gibi miktara bakar, anlık tüketime
        değil: art yakıcı açınca "eve dönme vakti" demesin. Yakıt kilosu tahminse (bilinmeyen uçak, oran
        veren modül) hesaplanmaz."""
        dist = self.home_distance(s)
        if dist is None or s.fuel_kg is None or s.extra.get("fuel_guess"):
            return None
        return self.profile.cruise_kg_nm * dist / NM + self.profile.reserve_kg

    def joker_kg(self) -> float:
        return max(300.0, 0.15 * self.profile.internal_fuel_kg)

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
            # Havada ikmal / yakıt eklendi: tüketimi yeniden ölç. Uyarılar silinmez; koşul gerçekten
            # kalkınca (aşağıdaki geri kurma payları) kendiliğinden yeniden kurulur.
            self.fuel_hist.clear()
            self.burn_hist.clear()
        self.fuel_hist.append((s.t, s.fuel_kg))
        while self.fuel_hist and s.t - self.fuel_hist[0][0] > self.FUEL_WINDOW:
            self.fuel_hist.popleft()
        if s.burn_kgs is not None and s.burn_kgs > 0:
            self.burn_hist.append((s.t, s.burn_kgs))
            while self.burn_hist and s.t - self.burn_hist[0][0] > 120:
                self.burn_hist.popleft()
        bingo = self.bingo_kg(s)
        if bingo is not None:
            self._bingo(s, bingo)
            return
        st = self.fuel_status(s) if self.measured() else None
        if not st:
            return
        left = st["left_min"]
        if self.endurance0 is None and self.takeoff is not None:
            self.endurance0 = (s.t - self.takeoff.t) / 60 + left
        pct = s.extra.get("fuel_pct")
        if isinstance(pct, (int, float)) and pct > 35:
            return  # 3000 kilo yakıtla art yakıcıdayken "4 dakika yeter" diyordu: depo azalmadan dakika uyarısı yok
        for limit, flag, sev in ((10, "low10", "warn"), (5, "low5", "critical")):
            if left > limit + 8:
                self.fuel_flags.discard(flag)  # ikmal yapıldı ya da gaz azaldı: yeniden kur
            if limit == 10 and (self.endurance0 or 99) < 25:
                continue  # zaten az yakıtla çıkmış (War Thunder'da sık): 10 dakika uyarısı gürültü
            if self._held(flag, left <= limit, s.t, 20) and flag not in self.fuel_flags:
                self.fuel_flags.update({flag} | ({"low10"} if flag == "low5" else set()))
                self.say(f"Yakıt yaklaşık {fmt_min(left)} yeter.", sev, self._urgent())
                self._label(f"YAKIT ~{max(0, round(left))} dk", sev, "fuel", 6.0)

    def _bingo(self, s: FlightState, bingo: float) -> None:
        """Bingo: eve dönüş + rezerv kadar yakıt kaldı. Joker: bingo'dan bir pay önce, bir kez haber."""
        fuel, joker = s.fuel_kg, self.joker_kg()
        if fuel > bingo + joker * 1.5:
            self.fuel_flags -= {"joker", "bingo"}
        elif fuel > bingo + joker * 0.5:
            self.fuel_flags.discard("bingo")
        nav = self.home_nav(s) or {}
        where = f"üs {nav.get('nm', 0):.0f} mil, {nav.get('bearing', 0):03.0f} derece"
        if self._held("bingo", fuel <= bingo, s.t, 10) and "bingo" not in self.fuel_flags:
            self.fuel_flags.update({"bingo", "joker"})
            self.say(f"Bingo. Şimdi eve dön, {where}.", "critical", self._urgent())
        elif self._held("joker", fuel <= bingo + joker, s.t, 10) and "joker" not in self.fuel_flags:
            self.fuel_flags.add("joker")
            self.say(f"Joker. Bingo'ya {max(0.0, fuel - bingo):.0f} kilo var, {where}.", "warn", self._urgent())

    def _held(self, key: str, cond: bool, t: float, secs: float) -> bool:
        """Koşul en az secs saniyedir kesintisiz doğru mu (tank aktarması, anlık sıçramalar susar)."""
        if not cond:
            self.fuel_cand.pop(key, None)
            return False
        return t - self.fuel_cand.setdefault(key, t) >= secs

    def _urgent(self) -> str:
        """Nabız yüksekse ALICE acil değil sakin ama net konuşur."""
        hr = self.hr_now()
        return "calm" if hr is not None and hr >= 120 else "urgent"

    # ------------------------------------------------------- kendiliğinden uyarılar
    OIL_MAX, WATER_MAX = 105.0, 115.0  # War Thunder: bunların üstü motoru yakar (uçağa göre değişir)

    # Uyarı sayılmayan ışıklar: otopilot her açılışta "Uyarı: otopilot açık" diyordu.
    LIGHTS_IGNORED = {"AutopilotOn"}
    # Dogfight'ta saniyede bir yanıp sönen ışıklar: kendi uyarısı olan uçakta hiç, yoksa dakikada bir.
    LIGHTS_FLICKER = {"StallSignalization"}
    WARN_REPEAT = 120.0  # sn: sönüp yeniden yanan ışık bu süre dolmadan tekrar söylenmez

    def _warnings(self, s: FlightState) -> None:
        """DCS uyarı/arıza ışıkları: yeni yanan ışığı söyle; yanıp sönen ışığı her seferinde değil."""
        raw = s.extra.get("warnings")
        if raw is None:
            return
        now = {w for w in str(raw).split(",") if w} - self.LIGHTS_IGNORED
        if self.profile.has_gpws:
            now -= self.LIGHTS_FLICKER
        new = sorted(now - self.warn_seen)
        self.warn_seen = now
        new = [w for w in new if s.t - self.said_at.get("light:" + w, -1e9) >= (
            60.0 if w in self.LIGHTS_FLICKER else self.WARN_REPEAT)]
        if len(new) > 1 and "MasterWarning" in new:
            new.remove("MasterWarning")  # asıl arıza adı zaten söyleniyor
        if new:
            for w in new:
                self.said_at["light:" + w] = s.t
            names = ", ".join(warning_name(w) for w in new[:3])
            self.say(f"Uyarı: {names}.", "critical", self._urgent())

    HEAT_REPEAT = 180.0

    def _engine_heat(self, s: FlightState) -> None:
        for key, limit, label in (("oil_temp", self.OIL_MAX, "Yağ"), ("water_temp", self.WATER_MAX, "Su")):
            v = s.extra.get(key)
            if v is None:
                continue
            # Sınırın çevresinde gidip gelen sıcaklık her seferinde söylenmesin: iyice soğuyunca ve
            # 3 dakika geçince yeniden kur.
            if v >= limit and key not in self.heat_flags and s.t - self.said_at.get(key, -1e9) >= self.HEAT_REPEAT:
                self.heat_flags.add(key)
                self.said_at[key] = s.t
                self.say(f"{label} sıcaklığı {v:.0f} derece, motor ısınıyor. Gazı azalt ya da radyatörü aç.",
                         "warn", self._urgent())
            elif v < limit - 15:
                self.heat_flags.discard(key)

    INFO_GAP = 15.0  # sn: bilgi niteliğindeki iki anons arası en az (waypoint, mühimmat, tank)

    def _info(self, s: FlightState, text: str, emotion: str = "calm") -> None:
        """Önemsiz anons: bir öncekinin üstüne binmesin, sıkışırsa söylenmeden geçilsin."""
        if self.calm or s.t - self.info_last < self.INFO_GAP:  # tilt'te önemsiz anons yok
            return
        self.info_last = s.t
        self.say(text, "info", emotion)

    def _pilot_alerts(self, s: FlightState) -> None:
        """'10 bin feet'e gelince söyle' / 'yakıt 1500 kiloya inince söyle': eşik geçilince bir kez."""
        if self.alt_alert_ft is not None and s.alt is not None:
            ft = s.alt / FT
            if self.alt_alert_above is None:
                self.alt_alert_above = ft > self.alt_alert_ft
            elif (ft <= self.alt_alert_ft) == self.alt_alert_above:
                self.say(f"{self.alt_alert_ft:,.0f} feet.".replace(",", "."), "info", "calm")
                self.alt_alert_ft = self.alt_alert_above = None
        if self.fuel_alert_kg is not None and s.fuel_kg is not None and s.fuel_kg <= self.fuel_alert_kg:
            self.say(f"Yakıt {self.fuel_alert_kg:.0f} kiloya indi.", "warn", "calm")
            self.fuel_alert_kg = None

    GROUND_TTI = 5.0     # sn: bu kadar sürede yere çarpacaksa uyar
    GROUND_REPEAT = 8.0  # sn: iki uyarı arası en az (yeniden kurulmuş olsa bile)
    GROUND_SAFE_AGL = 300.0  # m: bunun üstü ya da
    GROUND_SAFE_VV = -2.0    # m/s: bundan yavaş alçalma güvenli sayılır
    GROUND_REARM = 3.0       # sn: uyarı yeniden kurulsun diye güvenli durumda kalma süresi

    def _ground(self, s: FlightState) -> None:
        """Yere hızla yaklaşma (kendi GPWS'i olmayan uçaklarda). İniş yaklaşmasında susar.
        Alçakta dalaşırken art arda söylenmesin (War Thunder'da 10 dakikada 13 kez): söyledikten sonra uçak
        en az GROUND_REARM sn güvenli duruma dönmeden tekrar etmez."""
        if self.profile.has_gpws or s.agl is None or s.vv is None:
            return
        if s.agl > self.GROUND_SAFE_AGL or s.vv > self.GROUND_SAFE_VV:
            self.ground_safe_since = s.t if self.ground_safe_since is None else self.ground_safe_since
            if s.t - self.ground_safe_since >= self.GROUND_REARM:
                self.ground_armed = True
        else:
            self.ground_safe_since = None
        if s.vv >= -8 or s.agl > 600:
            return
        if (s.gear or 0) > 0.9 and (s.ias or 1e9) < self.profile.approach_ias_max * 1.2:
            return  # teker açık, yaklaşma hızında: inişe geliyor
        if s.agl / -s.vv < self.GROUND_TTI and self.ground_armed \
                and s.t - self.ground_last_warn >= self.GROUND_REPEAT:
            self.ground_last_warn, self.ground_armed, self.ground_safe_since = s.t, False, None
            self.say("Yere yaklaşıyorsun, burnu kaldır!", "critical", "urgent")

    def _flaps(self, s: FlightState) -> None:
        """Kalkıştan sonra açık unutulan flap: hızlı ve yüksekte 20 sn sürerse bir kez hatırlat."""
        flaps = s.extra.get("flaps")
        if self.profile.auto_flaps or flaps is None:
            return
        if flaps < 0.1:
            self.flaps_since, self.flaps_said = None, False
            return
        cruising = (s.gear is None or s.gear < 0.1) and (s.ias or 0) > self.profile.approach_ias_max * 1.4 \
            and (s.agl is None or s.agl > 600)
        if not cruising:
            self.flaps_since = None
            return
        self.flaps_since = s.t if self.flaps_since is None else self.flaps_since
        if s.t - self.flaps_since >= 20 and not self.flaps_said:
            self.flaps_said = True
            self.say("Flaplar hâlâ açık, toplamayı unutma.", "warn", "serious")

    def _ext_tanks(self, s: FlightState) -> None:
        """Dış tanklar yavaş yavaş boşaldıysa (atılmadıysa) bir kez söyle."""
        ext = s.extra.get("fuel_ext_kg")
        if ext is None:
            return
        last, self.ext_last = self.ext_last, ext
        if ext > 50:
            self.ext_said = False
        elif last is not None and 0.5 < last < 60 and ext <= 0.5 and not self.ext_said:
            self.ext_said = True
            self._info(s, "Dış tanklar boşaldı.")

    def _gear_check(self, s: FlightState) -> None:
        if s.gear is not None and s.ias is not None and (s.gear < 0.1 or s.ias < 140):
            self.gear_fast_said = False
        if s.gear is None or s.ias is None or s.t - self.gear_last_warn < 30:
            return
        agl = s.agl if s.agl is not None else s.extra.get("radalt", 0) * FT if s.extra.get("radalt") else None
        if s.gear < 0.1 and agl is not None and agl < 300 and s.ias < 85 and (s.vv or 0) < -2:
            self.gear_last_warn = s.t
            self.say("Teker kapalı! Alçak ve yavaşsın.", "critical", "urgent")
        elif s.gear > 0.9 and s.ias > 150 and not self.gear_fast_said:
            self.gear_last_warn = s.t
            self.gear_fast_said = True  # teker kapanana ya da yavaşlayana kadar bir kez
            self.say(f"Teker açık ve hızın {s.ias / 0.514444:.0f} knot, tekeri kapat ya da yavaşla.", "warn", "serious")

    def _waypoint(self, s: FlightState) -> None:
        e = s.extra
        if e.get("wp_x") is None or e.get("wp_z") is None:
            return
        bn = _bearing_nm(s, e["wp_x"], e["wp_z"])
        num = e.get("wp_num")
        if bn is None:
            return
        if bn[1] <= 2 and self.wp_called != num:
            self.wp_called = num
            label = f"{int(num)} numaralı waypoint'e" if num is not None else "Waypoint'e"
            self._info(s, f"{label} {bn[1]:.0f} mil.")

    def _weapons(self, s: FlightState) -> None:
        now = parse_weapons(s.extra.get("weapons"))
        if s.extra.get("weapons") is not None or self.weapons_last:
            for name, before in self.weapons_last.items():
                after = now.get(name, 0)
                if after < before:
                    if after == 1:
                        self._info(s, f"Son {name}.", "serious")
                    elif after == 0:
                        self._info(s, f"{name} bitti.", "serious")
            self.weapons_last = now
        gun = s.extra.get("gun")
        if gun is not None:
            if self.gun_start is None or gun > self.gun_start:
                self.gun_start, self.gun_flag = gun, False
            elif self.gun_start > 0 and gun <= self.gun_start * 0.2 and not self.gun_flag:
                self.gun_flag = True
                self._info(s, f"Top mermisi azaldı, {int(gun)} kaldı.", "serious")

    # --------------------------------------------------------------------- G
    def _g(self, s: FlightState) -> None:
        p = self.profile
        if s.g is None or s.source == "warthunder":
            # War Thunder'ın Ny değeri gürültülü (ölümde 48 G gördü) ve oyun bayılmayı kendisi gösteriyor: dogfight'ta
            # her 3 dakikada "11 G üstündesin" demek sadece rahatsız ediyordu.
            return
        if s.g >= p.g_warn:
            self.g_since = s.t if self.g_since is None else self.g_since
            if s.t - self.g_since >= p.g_warn_seconds and s.t - self.g_last_warn > 180:
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
        # Kalkışta teker henüz açıkken hızlanmak yaklaşma değil: radar irtifası olmayan uçakta (War Thunder) her
        # kalkıştan sonra "Hızlısın." deniyordu (1 Kasım, 21 kez)
        departing = (self.takeoff is not None and s.t - self.takeoff.t < 60) or (s.vv is not None and s.vv > 3)
        return (s.gear or 0) > 0.9 and slow and (low or s.agl is None) and not departing

    APPR_MARGIN = 2.0   # derece: on speed bandının bu kadar dışına çıkmadan "hızlı/yavaş" deme
    APPR_STABLE = 1.5   # sn: durum bu kadar sürmeden söyleme (anlık salınımlar susar)
    APPR_REPEAT = 15.0  # sn: aynı uyarıyı en erken bu kadar sonra tekrarla
    APPR_GAP = 3.0      # sn: iki anons arası en az

    def _approach_kind(self, s: FlightState) -> str:
        lo, hi = self.profile.aoa_on_speed
        final = s.agl is not None and s.agl < 300
        if final and s.vv is not None and s.vv < -7:
            return "sink"
        if s.aoa > hi + self.APPR_MARGIN:
            return "slow"  # stall'a yaklaşıyor
        if s.aoa < lo - self.APPR_MARGIN and (s.ias or 0) >= 0.85 * self.profile.approach_ias_max:
            return "fast"  # yaklaşma hız sınırına geliyor
        if lo <= s.aoa <= hi:
            return "on"
        return "band"  # bandın hemen dışında: söylenecek bir şey yok

    def _approach(self, s: FlightState) -> None:
        if not self.in_approach(s) or s.aoa is None:
            self.appr_last_kind = ""
            self.appr_cand = ""
            return
        lo, hi = self.profile.aoa_on_speed
        if s.agl is not None and s.agl < 300:
            self.appr_samples.append(lo <= s.aoa <= hi)
            if s.vv is not None:
                self.max_sink_final = max(self.max_sink_final or 0.0, -s.vv)
        kind = self._approach_kind(s)
        if kind != self.appr_cand:
            self.appr_cand, self.appr_cand_since = kind, s.t
        stable = s.t - self.appr_cand_since >= self.APPR_STABLE or kind == "sink"
        if not stable or kind == "band" or s.t - self.appr_last_say < self.APPR_GAP:
            return
        last = self.appr_last_kind
        if kind == "on":
            say_it = last in ("fast", "slow", "sink")  # sadece bir düzeltmeden sonra, bir kez
        else:
            say_it = kind != last or s.t - self.appr_last_say >= self.APPR_REPEAT
        if not say_it:
            return
        text = APPROACH[kind]
        self.appr_last_say, self.appr_last_kind = s.t, kind
        self.say(text, "info", "calm" if kind == "on" else "serious")

    # ------------------------------------------------------------ uçuş sonrası
    SPEAK_DEBRIEF_S = 600  # inişle bitmeyen uçuş bundan kısaysa değerlendirme sesli söylenmez

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
        if not landed and d.duration_s < self.SPEAK_DEBRIEF_S:
            return  # kayda ve sayfaya yazıldı; War Thunder'da her ölümden sonra konuşmasın
        try:
            tip = coach(d, load_history(exclude=d.start_wall))
        except Exception:  # noqa: BLE001 - karşılaştırma olmadan da değerlendirme söylensin
            tip = ""
        self.say(d.summary() + (" " + tip if tip else ""), "info", "calm")

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


# DCS MCP uyarı ışıklarının Türkçesi (bilinmeyen ad olduğu gibi okunur).
WARNING_NAMES = {
    "LeftEngineFailure": "sol motor arızası", "RightEngineFailure": "sağ motor arızası",
    "HydraulicsFailure": "hidrolik arızası", "ACSFailure": "otopilot arızası", "AutopilotFailure": "otopilot arızası",
    "AutopilotOn": "otopilot açık", "CannonFailure": "top arızası", "StallSignalization": "stall",
    "LeftMainPumpFailure": "sol yakıt pompası arızası", "RightMainPumpFailure": "sağ yakıt pompası arızası",
    "LeftWingPumpFailure": "sol kanat pompası arızası", "RightWingPumpFailure": "sağ kanat pompası arızası",
    "RadarFailure": "radar arızası", "EOSFailure": "optik sistem arızası", "MLWSFailure": "füze uyarı sistemi arızası",
    "ECMFailure": "karıştırıcı arızası", "GearFailure": "teker arızası", "MFDFailure": "ekran arızası",
    "RWSFailure": "radar uyarı alıcısı arızası", "HUDFailure": "HUD arızası", "LeftTailPlaneFailure": "sol kuyruk hasarı",
    "RightTailPlaneFailure": "sağ kuyruk hasarı", "LeftAileronFailure": "sol eleron hasarı",
    "RightAileronFailure": "sağ eleron hasarı", "CanopyOpen": "kanopi açık", "FuelTankDamage": "yakıt tankı hasarı",
    "MasterWarning": "ana uyarı", "LeftEngineFire": "sol motor yangını", "RightEngineFire": "sağ motor yangını",
}


APPROACH = {"sink": "Çok hızlı alçalıyorsun.", "slow": "Yavaşsın, stall'a yaklaşıyorsun, güç ver.",
            "fast": "Hızlısın.", "on": "On speed, iyi."}


def callouts() -> list[tuple[str, str]]:
    """Uçuşta söylenen sabit cümleler ve tonları. Ses önceden üretilip diske konur: uçuşta ekran kartı oyuna
    kaldığı için ses buluttan geliyor (her uyarı 1-2 sn gecikme, kota); önbellekteki ses anında çalar."""
    fixed = [("Yere yaklaşıyorsun, burnu kaldır!", "urgent"), ("Teker kapalı! Alçak ve yavaşsın.", "urgent"),
             ("Flaplar hâlâ açık, toplamayı unutma.", "serious"), ("Dış tanklar boşaldı.", "calm")]
    fixed += [(text, "calm" if kind == "on" else "serious") for kind, text in APPROACH.items()]
    # Tek ışıklı uyarılar (en sık durum); önce en kritikleri.
    names = sorted(WARNING_NAMES.values(), key=lambda n: ("yangın" not in n, "hasar" in n, n))
    return fixed + [(f"Uyarı: {n}.", "urgent") for n in names]


def warning_name(key: str) -> str:
    return WARNING_NAMES.get(key) or re.sub(r"(?<!^)(?=[A-Z])", " ", key).lower()


def band_name(mhz: float) -> str:
    if 225 <= mhz <= 400:
        return "UHF"
    if 108 <= mhz < 118:
        return "NAV (VOR/ILS)"
    if 118 <= mhz <= 174:
        return "VHF"
    if 30 <= mhz < 88:
        return "FM"
    return "frekans"


def parse_weapons(text: str | None) -> dict[str, int]:
    out: dict[str, int] = {}
    for part in (text or "").split(","):
        m = re.match(r"\s*(.+?)\s+x(\d+)\s*$", part)
        if m:
            out[m.group(1)] = out.get(m.group(1), 0) + int(m.group(2))
    return out


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
            spd = s.tas or s.ias
            eta = f", varış ~{bn[1] * NM / spd / 60:.0f} dk" if spd and spd > 20 else ""
            out.append(f"  sıradaki waypoint{num}: {bn[0]:03.0f}° (gerçek), {bn[1]:.1f} nm{eta}")
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
    if e.get("radios"):
        out.append("  telsiz/seyrüsefer frekansları: " + ", ".join(f"{band_name(float(f))} {f}" for f in
                                                               str(e["radios"]).split(",") if f))
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
