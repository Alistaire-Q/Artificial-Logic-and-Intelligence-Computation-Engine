"""Ortama göre mod.

Açık programlara ve saate bakarak mod seçilir. Mod, hangi bildirimlerin geçeceğini,
seslinin açık olup olmadığını ve ALICE'in ekran kartını kullanıp kullanamayacağını belirler:

  flight : DCS / War Thunder açık. Sadece uçuşla ilgili ya da kritik şeyler konuşulur,
           yerel GPU kullanılmaz (ağır işler buluta).
  dev    : Unity / editör açık. Normal bildirimler, geliştirme odaklı.
  quiet  : gece saatleri. Sadece uyarı ve üstü, sesli konuşma yok.
  normal : geri kalan her şey.

Config ([core.modes]) ile program listeleri ve politikalar değiştirilebilir.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, time as dtime
from typing import Any, Iterable

SEVERITIES = ("info", "warn", "critical")

DEFAULT_PROGRAMS = {
    "flight": ["dcs.exe", "dcs_server.exe", "aces.exe"],  # aces.exe = War Thunder
    "dev": ["unity.exe", "unity hub.exe", "code.exe", "devenv.exe", "rider64.exe"],
}

DEFAULT_POLICIES: dict[str, dict[str, Any]] = {
    "normal": {"min_severity": "info", "topics": [], "voice": True, "local_gpu": True},
    "dev": {"min_severity": "info", "topics": [], "voice": True, "local_gpu": True},
    # oyunda telefon mesajları da söylensin (WhatsApp/SMS); toplu bildirim özeti oyun bitince
    "flight": {"min_severity": "critical", "topics": ["flight", "phone"], "voice": True, "local_gpu": False},
    "quiet": {"min_severity": "warn", "topics": [], "voice": False, "local_gpu": True},
}

PRIORITY = ("flight", "dev", "quiet", "normal")


@dataclass
class Policy:
    mode: str
    min_severity: str = "info"
    topics: list[str] = field(default_factory=list)  # boş değilse bu konulardakiler min_severity'den bağımsız geçer
    voice: bool = True
    local_gpu: bool = True

    def allows(self, severity: str, topic: str = "") -> bool:
        if topic and topic in self.topics:
            return True
        rank = SEVERITIES.index(severity) if severity in SEVERITIES else 0
        # Ayarda yazım hatası ("warning") her bildirimi ValueError ile düşürmesin: bilinmeyen seviye = info.
        floor = SEVERITIES.index(self.min_severity) if self.min_severity in SEVERITIES else 0
        return rank >= floor


def parse_hours(spec: str) -> tuple[dtime, dtime] | None:
    """'00:30-08:00' -> (00:30, 08:00). Boş ya da geçersizse None."""
    try:
        a, b = (s.strip() for s in spec.split("-", 1))
        return dtime.fromisoformat(a), dtime.fromisoformat(b)
    except (ValueError, AttributeError):
        return None


def in_hours(now: datetime, span: tuple[dtime, dtime]) -> bool:
    a, b = span
    t = now.time()
    return a <= t < b if a <= b else (t >= a or t < b)


class ModeManager:
    def __init__(self, cfg: dict[str, Any] | None = None):
        cfg = cfg or {}
        modes = cfg.get("modes", {})
        self.programs = {m: [p.lower() for p in modes.get(m, {}).get("programs", DEFAULT_PROGRAMS[m])]
                         for m in DEFAULT_PROGRAMS}
        self.quiet_hours = parse_hours(str(cfg.get("quiet_hours", "00:30-08:00")))
        self.policies = {}
        for m, base in DEFAULT_POLICIES.items():
            over = {k: v for k, v in modes.get(m, {}).items() if k in base}
            self.policies[m] = Policy(m, **{**base, **over})
        self.forced: str | None = None
        self.current = "normal"

    def detect(self, processes: Iterable[str], now: datetime | None = None) -> str:
        if self.forced:
            return self.forced
        names = {p.lower() for p in processes}
        for mode in PRIORITY:
            if mode == "quiet":
                if self.quiet_hours and in_hours(now or datetime.now(), self.quiet_hours):
                    return "quiet"
            elif mode in self.programs and names & set(self.programs[mode]):
                return mode
        return "normal"

    def update(self, processes: Iterable[str], now: datetime | None = None) -> tuple[str, bool]:
        """Modu yeniden hesapla. (mod, değişti mi) döndürür."""
        mode = self.detect(processes, now)
        changed = mode != self.current
        self.current = mode
        return mode, changed

    @property
    def policy(self) -> Policy:
        return self.policies.get(self.current, self.policies["normal"])
