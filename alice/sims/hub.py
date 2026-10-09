"""Simülasyonları çekirdeğe bağlar: dinleyiciler -> uçuş arkadaşı -> bildirim (ve ses).

Çekirdekle aynı süreçte çalışır; sesli asistan da aynı süreçte olduğundan `flight` aracı
canlı duruma ACTIVE üzerinden ulaşır.
"""

from __future__ import annotations

import time
from typing import Any

from alice.core.notify import Notification, Notifier
from alice.core.store import Store
from alice.sims.flight import Debrief, FlightCompanion, TouchdownTracker, save_debrief

ACTIVE: "SimHub | None" = None


class SimHub:
    def __init__(self, cfg: dict[str, Any], store: Store, notifier: Notifier, processes=lambda: set()):
        self.cfg = cfg
        self.store = store
        self.notifier = notifier
        self.processes = processes
        self.companions: dict[str, FlightCompanion] = {}
        self.last_debrief: tuple[Debrief, str] | None = None
        self._parts: list = []

    def _say(self, text: str, severity: str, emotion: str) -> None:
        self.notifier.send(Notification("Uçuş", text, severity, "flight", kind="flight", emotion=emotion))

    def _hr_stats(self, a: float, b: float) -> dict | None:
        s = self.store.stats("watch.hr", a, b)
        return {"avg": s.avg, "max": s.max, "min": s.min} if s.count >= 3 else None

    def _hr_now(self) -> float | None:
        last = self.store.last("watch.hr")
        return last[1] if last and time.time() - last[0] < 60 else None

    def _debrief(self, d: Debrief) -> None:
        page = save_debrief(d, self.store.series("watch.hr", d.start_wall, d.end_wall))
        self.last_debrief = (d, str(page))
        self.store.add_event("flight", f"Uçuş değerlendirmesi: {d.aircraft}", f"{d.summary()} ({page})", "info", "flight")

    def companion(self, source: str) -> FlightCompanion:
        if source not in self.companions:
            self.companions[source] = FlightCompanion(self._say, self._hr_stats, self._hr_now, self._debrief)
        return self.companions[source]

    def start(self) -> "SimHub":
        global ACTIVE
        if self.cfg.get("dcs", True):
            from alice.sims.dcs import PORT, DCSListener

            comp = self.companion("dcs")
            tracker = TouchdownTracker(comp)
            try:
                self._parts.append(DCSListener(tracker.feed, comp.restricted, comp.session_end,
                                               lambda rows: self.store.add_many(rows),
                                               port=int(self.cfg.get("dcs_port", PORT))).start())
            except OSError as e:
                self.store.add_event("error", "DCS dinleyicisi başlamadı", str(e), "warn", "flight")
        if self.cfg.get("warthunder", True):
            from alice.sims.warthunder import WarThunderPoller

            comp = self.companion("warthunder")
            tracker = TouchdownTracker(comp)
            running = lambda: any(p.lower() == "aces.exe" for p in self.processes())  # noqa: E731
            self._parts.append(WarThunderPoller(tracker.feed, comp.session_end,
                                                lambda rows: self.store.add_many(rows), running=running).start())
        ACTIVE = self
        return self

    def stop(self) -> None:
        global ACTIVE
        for p in self._parts:
            p.stop()
        if ACTIVE is self:
            ACTIVE = None

    def status(self) -> str:
        lines = []
        for src, c in self.companions.items():
            s = c.last_state
            if s is None or c.phase != "air":
                continue
            lines.append(f"{src}: {c.profile.name}, {(s.alt or 0) / 0.3048:,.0f} ft, {(s.ias or 0) / 0.514444:.0f} kt, "
                         f"{s.g or 0:.1f} G")
            fs = c.fuel_status(s)
            if fs:
                extra = (f", üs {fs['home_nm']:.0f} nm, eve dönüş payı {fs['margin_min']:.0f} dk"
                         if "home_min" in fs else "")
                lines.append(f"  yakıt {s.fuel_kg:.0f} kg, tüketim {fs['burn_kgs'] * 3600:.0f} kg/sa, "
                             f"{fs['left_min']:.0f} dk kaldı{extra}")
            if s.chaff is not None:
                lines.append(f"  chaff {s.chaff}, flare {s.flare}")
        return "\n".join(lines) or "şu an uçuşta değilsin (ya da veri gelmiyor)"
