"""Tek bir "şu an" durumu ve önsezi motoru.

Şu an: mod, öndeki program, oyun ve maç durumu, bugünün uçuşları (kaç tane, ne kadar sürdü, son ölümlerden önce
arkada düşman var mıydı), nabız ve alışkanlıktan gelen tahmin tek yerde toplanır. Sesli cevapların her cümlesine
kısa hali eklenir (saat gibi), "Nasılım?" sorusu `now` aracıyla ayrıntılı cevaplanır: "Son 4 uçuşunun 3'ünde
düşmeden önce arkanda düşman vardı, nabzın 128. Bir mola iyi gelir."

Önsezi: alışkanlıklar kayıtlardan çıkarılır (ör. War Thunder'ı hangi gün hangi saatte açtığın). Beklenen oturumdan
kısa süre önce hazırlık yapılır: uçuş anonslarının sesleri önceden üretilir, ilk anons beklemeden çalar. Tahmin
ancak yeterli geçmiş varsa yapılır; hazırlık günde bir kez, sessizce.
"""

from __future__ import annotations

import statistics
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Callable

GAME_KEY = "pc.proc.aces.exe.cpu"   # War Thunder açıkken çekirdek bunu 15 sn'de bir yazar
SESSION_GAP = 2 * 3600.0            # bu kadar ara verilip açılırsa yeni oturum
SIX_BEFORE_DEATH = 45.0             # uçuş bitmeden bu kadar önce "Arkanda düşman" dendiyse arkadan vuruldu
HR_HIGH = 120.0
ANTICIPATE_BEFORE = 20 * 60.0       # beklenen oturumdan bu kadar önce hazırlık
MIN_SESSIONS = 3

APP_NAMES = {"aces.exe": "War Thunder", "dcs.exe": "DCS", "opera.exe": "Opera", "chrome.exe": "Chrome",
             "discord.exe": "Discord", "code.exe": "VS Code", "claude.exe": "Claude", "spotify.exe": "Spotify",
             "steam.exe": "Steam", "whatsapp.root.exe": "WhatsApp", "blender.exe": "Blender"}
MODE_NAMES = {"normal": "normal", "dev": "geliştirme", "flight": "uçuş", "quiet": "sessiz"}


@dataclass
class Snapshot:
    at: float
    mode: str = ""
    app: str = ""
    in_game: bool = False
    flights_today: int = 0
    avg_min_today: float | None = None
    avg_min_usual: float | None = None
    recent: int = 0                 # son uçuşlardan kaçı değerlendirildi
    shot_from_six: int = 0          # bunların kaçında düşmeden önce arkada düşman vardı
    hr: float | None = None
    next_game: float | None = None  # alışkanlığa göre beklenen War Thunder açılışı
    advice: list[str] = field(default_factory=list)


# ------------------------------------------------------------------ alışkanlık
def session_starts(times: list[float], gap: float = SESSION_GAP) -> list[float]:
    """Örnek zamanlarından oturum başlangıçları (araya 'gap'ten uzun boşluk girince yeni oturum)."""
    out: list[float] = []
    prev = None
    for t in sorted(times):
        if prev is None or t - prev > gap:
            out.append(t)
        prev = t
    return out


def predict_next(starts: list[float], now: float) -> float | None:
    """Önümüzdeki 24 saatte beklenen açılış: aynı haftanın günü yeterince örnek varsa ondan, yoksa bütün
    günlerden; dakika cinsinden ortanca. Yeterli geçmiş yoksa None."""
    if len(starts) < MIN_SESSIONS:
        return None
    today = datetime.fromtimestamp(now)
    for offset in (0, 1):
        day = today + timedelta(days=offset)
        same = [s for s in starts if datetime.fromtimestamp(s).weekday() == day.weekday()]
        pool = same if len(same) >= MIN_SESSIONS else starts
        mins = [datetime.fromtimestamp(s).hour * 60 + datetime.fromtimestamp(s).minute for s in pool]
        m = int(statistics.median(mins))
        at = day.replace(hour=m // 60, minute=m % 60, second=0, microsecond=0).timestamp()
        if at > now:
            return at
    return None


def game_times(store: Any, now: float, days: int = 28) -> list[float]:
    return [t for t, _ in store.series(GAME_KEY, since=now - days * 86400)]


# ---------------------------------------------------------------------- şu an
def snapshot(store: Any, now: float | None = None, flights: list[dict[str, Any]] | None = None,
             state: dict[str, Any] | None = None) -> Snapshot:
    now = time.time() if now is None else now
    if state is None:
        from alice.core.service import read_state

        state = read_state()
    snap = Snapshot(at=now, mode=MODE_NAMES.get(str(state.get("mode") or ""), str(state.get("mode") or "")))
    fg = store.last("pc.foreground")
    if fg and now - fg[0] < 120 and fg[2]:
        snap.app = APP_NAMES.get(fg[2].lower(), fg[2].rsplit(".", 1)[0])
    game = store.last(GAME_KEY)
    snap.in_game = bool(game and now - game[0] < 60)
    hr = store.last("watch.hr")
    if hr and now - hr[0] < 120 and hr[1]:
        snap.hr = float(hr[1])
    if flights is None:
        from alice.sims.flight import load_history

        flights = load_history(limit=200)
    day0 = datetime.fromtimestamp(now).replace(hour=6, minute=0, second=0, microsecond=0).timestamp()
    if datetime.fromtimestamp(now).hour < 6:
        day0 -= 86400  # gece 2'deki maç "bugünün"
    fs = sorted((f for f in flights if f.get("start_wall") and f.get("duration_s")), key=lambda f: f["start_wall"])
    today = [f for f in fs if float(f["start_wall"]) >= day0]
    snap.flights_today = len(today)
    if today:
        snap.avg_min_today = sum(float(f["duration_s"]) for f in today) / len(today) / 60
    older = [f for f in fs if float(f["start_wall"]) < day0]
    if len(older) >= 10:
        snap.avg_min_usual = sum(float(f["duration_s"]) for f in older) / len(older) / 60
    recent = fs[-5:]
    if recent and now - float(recent[-1].get("end_wall") or 0) < 6 * 3600:
        lo = float(recent[0]["start_wall"])
        sixes = [e.ts for e in store.events(since=lo, kind="flight", limit=2000)
                 if (e.body or "").startswith("Arkanda düşman")]
        snap.recent = len(recent)
        snap.shot_from_six = sum(1 for f in recent if f.get("touchdown_vv_ms") is None and any(
            0 <= float(f.get("end_wall") or 0) - t <= SIX_BEFORE_DEATH for t in sixes))
    snap.next_game = predict_next(session_starts(game_times(store, now)), now)
    snap.advice = advise(snap)
    return snap


def advise(s: Snapshot) -> list[str]:
    out = []
    if s.recent >= 3 and s.shot_from_six >= max(2, s.recent // 2 + 1):
        from alice.sims.coach import ek

        out.append(f"Son {s.recent} uçuşunun {ek(s.shot_from_six)}nde düşmeden önce arkanda düşman vardı: dönüşlerde "
                   "arkanı kontrol et, alçakta uzun dönme.")
    if s.avg_min_today and s.avg_min_usual and s.flights_today >= 3 and s.avg_min_today < s.avg_min_usual * 0.7:
        out.append(f"Bugün uçuşların ortalama {s.avg_min_today:.1f} dakika sürüyor, normalde {s.avg_min_usual:.1f}.")
    if s.hr is not None and s.hr >= HR_HIGH:
        out.append(f"Nabzın {s.hr:.0f}.")
    if len(out) >= 2 or (s.hr is not None and s.hr >= HR_HIGH and s.in_game):
        out.append("Bir maç ara vermek iyi gelebilir.")
    return out


def line(s: Snapshot) -> str:
    """Her sesli cümleye eklenen kısa durum (model gerekirse kullanır, okumaz)."""
    parts = []
    if s.mode:
        parts.append(f"mod {s.mode}")
    if s.in_game:
        parts.append("War Thunder'da")
    elif s.app:
        parts.append(f"önde {s.app}")
    if s.flights_today:
        parts.append(f"bugün {s.flights_today} uçuş" + (f", ort {s.avg_min_today:.1f} dk" if s.avg_min_today else ""))
    if s.recent and s.shot_from_six:
        from alice.sims.coach import ek

        parts.append(f"son {s.recent} uçuşun {ek(s.shot_from_six)}nde arkadan")
    if s.hr is not None:
        parts.append(f"nabız {s.hr:.0f}")
    return ", ".join(parts)


def describe(s: Snapshot) -> str:
    lines = [line(s) or "Şu an kayda değer bir şey yok."]
    if s.avg_min_usual and s.avg_min_today:
        lines.append(f"Uçuşların normalde ortalama {s.avg_min_usual:.1f} dakika sürüyor.")
    if s.next_game:
        lines.append(f"Alışkanlığına göre War Thunder'ı genelde {datetime.fromtimestamp(s.next_game):%H:%M} gibi "
                     "açıyorsun.")
    lines += s.advice
    return "\n".join(lines)


_cache: dict[str, Any] = {"at": -1e9, "line": "", "busy": False}
VOICE_CACHE = 30.0  # sn: durum en çok bu kadar eski olur


def _refresh(now: float) -> None:
    try:
        from alice.core.store import Store

        with Store() as st:
            text = line(snapshot(st))
    except Exception:  # noqa: BLE001
        text = ""
    try:  # önsezi: sabah genelde ilk sorduğun şeylerin hazır cevabı
        from alice.anticipate import ready_line

        ready = ready_line()
        if ready:
            text = (text + "; " if text else "") + "hazır cevaplar: " + ready
    except Exception:  # noqa: BLE001
        pass
    _cache.update(at=now, line=text, busy=False)


def voice_line(now: float | None = None, wait: bool = False) -> str:
    """Sesli asistan için kısa durum. Hesap (~0,3 sn: uçuş kayıtları, veritabanı) arka planda yenilenir; sesli cevap
    hiç beklemez, en çok 30 sn eski durumu görür. Hata vermez (çekirdek kaydı yoksa boş)."""
    import threading

    now = time.monotonic() if now is None else now
    if now - _cache["at"] >= VOICE_CACHE and not _cache["busy"]:
        _cache["busy"] = True
        if wait:
            _refresh(now)
        else:
            threading.Thread(target=_refresh, args=(now,), daemon=True, name="alice-now").start()
    return _cache["line"]


# --------------------------------------------------------------------- önsezi
class Anticipator:
    """Beklenen War Thunder oturumundan önce hazırlık kancalarını günde bir kez çalıştırır."""

    def __init__(self, store: Any, hooks: list[Callable[[], None]] | None = None):
        self.store = store
        self.hooks = hooks if hooks is not None else []
        self.done_day = ""

    def check(self, now: float | None = None) -> bool:
        now = time.time() if now is None else now
        day = datetime.fromtimestamp(now).strftime("%Y-%m-%d")
        if self.done_day == day or not self.hooks:
            return False
        times = game_times(self.store, now)
        if times and now - max(times) < 3600:  # zaten oynuyor ya da az önce oynadı
            return False
        at = predict_next(session_starts(times), now)
        if at is None or not 0 <= at - now <= ANTICIPATE_BEFORE:
            return False
        self.done_day = day
        for h in self.hooks:
            try:
                h()
            except Exception:  # noqa: BLE001 - hazırlık başarısız olsa da bir şey bozulmaz
                pass
        self.store.add_event("info", "Önsezi: uçuşa hazırlık",
                             f"War Thunder'ı genelde {datetime.fromtimestamp(at):%H:%M} gibi açıyorsun; uçuş sesleri "
                             "önceden hazırlandı.", "info", "now")
        return True
