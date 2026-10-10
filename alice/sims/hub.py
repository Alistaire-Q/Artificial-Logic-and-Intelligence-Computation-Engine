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
            from alice.sims.dcs import PORT, DCSListener, ensure_installed

            try:  # betik kurulu değilse ya da eskiyse kur: kurulmadan DCS hiç veri göndermiyor
                for d in ensure_installed(port=int(self.cfg.get("dcs_port", PORT))):
                    self.store.add_event("info", "DCS veri betiği kuruldu", f"{d} (DCS'i yeniden başlat)", "info",
                                         "flight")
            except OSError as e:
                self.store.add_event("error", "DCS veri betiği kurulamadı", str(e), "warn", "flight")

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

    def alert_home(self, nm: float) -> str:
        """Havadaki uçuşa 'üsse nm mil kalınca söyle' uyarısı kur."""
        for c in self.companions.values():
            if c.last_state is not None and c.phase == "air":
                if c.home is None:
                    return "kurulamadı: üssün konumu bilinmiyor (kalkış kaydedilmedi)"
                c.home_alert_nm = float(nm)
                return f"kuruldu: üsse {nm:.0f} mil kalınca sesli söylenecek"
        return "kurulamadı: şu an uçuşta değilsin"

    def status(self) -> str:
        lines = []
        for src, c in self.companions.items():
            s = c.last_state
            if s is None:
                continue
            if c.phase != "air":
                lines.append(f"{src}: {c.profile.name}, yerde")
                continue
            lines.append(f"{src}: {c.profile.name}, {(s.alt or 0) / 0.3048:,.0f} ft, {(s.ias or 0) / 0.514444:.0f} kt, "
                         f"{s.g or 0:.1f} G")
            fs = c.fuel_status(s)
            if fs:
                extra = (f", üs {fs['home_nm']:.0f} nm, eve dönüş payı {fs['margin_min']:.0f} dk"
                         if "home_min" in fs else "")
                lines.append(f"  yakıt {s.fuel_kg:.0f} kg, tüketim {fs['burn_kgs'] * 3600:.0f} kg/sa, "
                             f"{fs['left_min']:.0f} dk kaldı{extra}")
            nav = c.home_nav(s)
            if nav:
                turn = ""
                if "turn" in nav:
                    t = nav["turn"]
                    turn = (" (burnun zaten üsse dönük)" if abs(t) < 10 else
                            f", şu an burnun {s.heading:03.0f}°: {abs(t):.0f}° {'sağa' if t > 0 else 'sola'} dön")
                lines.append(f"  üs (kalkış noktası): {nav['bearing']:03.0f}° yönünde (gerçek kuzey), "
                             f"{nav['nm']:.0f} nm{turn}")
            elif s.source == "dcs":
                lines.append("  üsse yön bilinmiyor (kalkış noktası kaydedilmedi)")
            from alice.sims.flight import describe_extra

            lines += describe_extra(s)
            if "weapons" not in s.extra:
                lines.append("  silah/mühimmat: " + ("War Thunder bu bilgiyi yayınlamıyor, BİLİNMİYOR" if src == "warthunder"
                                                     else "oyun bu uçak için vermedi, BİLİNMİYOR"))
            if c.home_alert_nm is not None:
                lines.append(f"  kurulu uyarı: üsse {c.home_alert_nm:.0f} mil kalınca söylenecek")
            if s.chaff is not None:
                lines.append(f"  chaff {s.chaff}, flare {s.flare}")
        if lines:
            return "\n".join(lines)
        if self.cfg.get("dcs", True) and "dcs" in self.companions and self.companions["dcs"].last_state is None:
            return ("DCS'ten bu oturumda hiç veri gelmedi. DCS açıksa görevi yeniden başlatmak gerekir (veri betiği "
                    "görev açılırken yüklenir); olmazsa `alice dcs status` ile kontrol et.")
        return "şu an uçuşta değilsin"
