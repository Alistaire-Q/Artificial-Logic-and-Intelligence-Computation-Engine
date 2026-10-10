"""War Thunder: oyunun kendi yayınladığı yerel göstergeleri (http://127.0.0.1:8111) okur.

/state uçuş değerlerini ("IAS, km/h", "H, m", "Ny", "AoA, deg", "Mfuel, kg"...) verir,
/indicators uçak tipini. Oyun açık değilse bağlantı reddedilir; o zaman sessizce bekler.
"""

from __future__ import annotations

import threading
import time
from typing import Any, Callable

import httpx

from alice.sims.flight import FlightState

URL = "http://127.0.0.1:8111"


def _f(d: dict[str, Any], *keys: str) -> float | None:
    for k in keys:
        v = d.get(k)
        if isinstance(v, (int, float)):
            return float(v)
    return None


def to_state(state: dict[str, Any], indicators: dict[str, Any], t: float) -> FlightState | None:
    if not state.get("valid"):
        return None
    kmh = lambda v: None if v is None else v / 3.6  # noqa: E731
    gear = _f(state, "gear, %")
    return FlightState(t=t, source="warthunder", aircraft=str(indicators.get("type") or ""),
                       ias=kmh(_f(state, "IAS, km/h")), tas=kmh(_f(state, "TAS, km/h")), alt=_f(state, "H, m"),
                       agl=_f(indicators, "radio_altitude"), vv=_f(state, "Vy, m/s"), aoa=_f(state, "AoA, deg"),
                       g=_f(state, "Ny"), fuel_kg=_f(state, "Mfuel, kg"), gear=None if gear is None else gear / 100,
                       heading=_f(indicators, "compass", "compass1"), extra=_extra(state, indicators))


def _extra(st: dict[str, Any], ind: dict[str, Any]) -> dict[str, Any]:
    """Oyunun yerel göstergelerinden ek değerler (kokpitte/HUD'da görülenler; başka oyuncu bilgisi yok)."""
    fuel, fuel0 = _f(st, "Mfuel, kg"), _f(st, "Mfuel0, kg")
    pct = lambda v: None if v is None else v / 100  # noqa: E731
    e: dict[str, Any] = {
        "mach": _f(st, "M"),
        "pitch": _f(ind, "aviahorizon_pitch"),
        "bank": _f(ind, "aviahorizon_roll"),
        "radalt": None if _f(ind, "radio_altitude") is None else _f(ind, "radio_altitude") / 0.3048,
        "throttle": _f(st, "throttle 1, %"),
        "rpm": _f(st, "RPM throttle 1, %"),
        "oil_temp": _f(st, "oil temp 1, C"),
        "water_temp": _f(st, "water temp 1, C"),
        "flaps": pct(_f(st, "flaps, %")),
        "speedbrake": pct(_f(st, "airbrake, %")),
        "fuel_pct": None if not fuel0 or fuel is None else 100 * fuel / fuel0,
    }
    if e["rpm"] is None and _f(st, "RPM 1") is not None:
        e["rpm_abs"] = _f(st, "RPM 1")
    return {k: v for k, v in e.items() if v is not None}


class WarThunderPoller:
    def __init__(self, on_state: Callable[[FlightState], None], on_end: Callable[[], None] = lambda: None,
                 on_metrics: Callable[[list], None] = lambda rows: None, hz: float = 5,
                 client: httpx.Client | None = None, running: Callable[[], bool] = lambda: True):
        self.on_state = on_state
        self.on_end = on_end
        self.on_metrics = on_metrics
        self.interval = 1 / hz
        self.client = client or httpx.Client(base_url=URL, timeout=1.0)
        self.running = running  # oyun açık mı (süreç listesinden); kapalıyken yoklamaz
        self.active = False
        self._stop = threading.Event()
        self._metrics_at = 0.0
        self._t0 = time.monotonic()

    def start(self) -> "WarThunderPoller":
        threading.Thread(target=self._loop, daemon=True, name="alice-warthunder").start()
        return self

    def stop(self) -> None:
        self._stop.set()

    def poll_once(self) -> FlightState | None:
        try:
            st = self.client.get("/state").json()
            ind = self.client.get("/indicators").json()
        except (httpx.HTTPError, ValueError):
            if self.active:
                self.active = False
                self.on_end()
            return None
        s = to_state(st, ind, time.monotonic() - self._t0)
        if s is None:
            if self.active:  # hangar / ölüm / maç sonu
                self.active = False
                self.on_end()
            return None
        self.active = True
        self.on_state(s)
        if time.monotonic() - self._metrics_at >= 1.0:
            self._metrics_at = time.monotonic()
            self.on_metrics([(f"wt.{k}", v, None) for k, v in (
                ("ias_kmh", s.ias and s.ias * 3.6), ("alt_m", s.alt), ("g", s.g), ("fuel.kg", s.fuel_kg))
                if v is not None])
        return s

    def _loop(self) -> None:
        while not self._stop.is_set():
            if self.running():
                self.poll_once()
                self._stop.wait(self.interval)
            else:
                if self.active:
                    self.active = False
                    self.on_end()
                self._stop.wait(5)
