"""HUD'un çizimden bağımsız durumu: gelen komutu doğrula, öğeleri tut, süresi dolanı sil.

Pencere (window.py) bunu yalnızca okur; böylece protokol ve ömür kuralları tkinter olmadan test edilir.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

KINDS = ("arrow", "edge", "label", "card", "box", "progress")
CONTROL = ("clear", "toggle")
MAX_ITEMS = 6          # aynı anda en fazla bu kadar öğe: HUD bilgi verir, ekranı kapatmaz
MAX_BYTES = 4096
TTL_DEFAULT, TTL_MIN, TTL_MAX = 3.0, 0.2, 60.0
SEVERITIES = ("info", "warn", "critical")


@dataclass
class Item:
    kind: str
    data: dict[str, Any]
    born: float
    until: float
    id: str = ""
    seq: int = field(default=0, compare=False)


def parse(raw: bytes) -> dict[str, Any] | None:
    """Ağdan gelen tek datagram -> komut. Bozuk, çok büyük ya da bilinmeyen tür None (sessizce yok sayılır)."""
    if not raw or len(raw) > MAX_BYTES:
        return None
    try:
        msg = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None
    if not isinstance(msg, dict) or msg.get("kind") not in KINDS + CONTROL:
        return None
    return msg


def clock_of(value: Any) -> int | None:
    """Saat yönü 1-12 (0 ve 12 aynı yer). Anlaşılmazsa None."""
    try:
        c = round(float(value))
    except (TypeError, ValueError):
        return None
    if not 0 <= c <= 12:
        return None
    return c or 12


def _num(value: Any) -> float | None:
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    return v if v == v and v not in (float("inf"), float("-inf")) else None


def _valid(msg: dict[str, Any]) -> bool:
    kind = msg["kind"]
    if kind in ("arrow", "edge"):
        return clock_of(msg.get("clock")) is not None
    if kind == "box":
        return all(_num(msg.get(k)) is not None for k in ("x", "y", "w", "h"))
    if kind == "progress":
        return _num(msg.get("value")) is not None
    return bool(str(msg.get("text") or msg.get("title") or ""))  # label, card: boş yazı çizilmez


class HudModel:
    def __init__(self, max_items: int = MAX_ITEMS):
        self.max_items = max_items
        self.items: list[Item] = []
        self.hidden = False
        self.version = 0  # her değişiklikte artar: pencere sadece değişince yeniden çizer
        self._seq = 0

    def apply(self, msg: dict[str, Any], now: float) -> bool:
        """Komutu uygula. Bir şey değiştiyse True."""
        kind = msg.get("kind")
        if kind == "toggle":
            self.hidden = not self.hidden
            self.version += 1
            return True
        if kind == "clear":
            key = str(msg.get("id") or "")
            keep = [i for i in self.items if key and i.id != key]
            changed = len(keep) != len(self.items)
            self.items = keep
            self.version += changed
            return changed
        if kind not in KINDS or not _valid(msg):
            return False
        ttl = _num(msg.get("ttl"))
        ttl = TTL_DEFAULT if ttl is None else min(TTL_MAX, max(TTL_MIN, ttl))
        key = str(msg.get("id") or "")
        data = {k: v for k, v in msg.items() if k not in ("kind", "ttl", "id")}
        if data.get("severity") not in SEVERITIES:
            data["severity"] = "info"
        if key:  # aynı kimlik yenisiyle değişir: tekrarlanan tehdit ekranda birikmez
            self.items = [i for i in self.items if i.id != key]
        self._seq += 1
        self.items.append(Item(kind, data, now, now + ttl, key, self._seq))
        while len(self.items) > self.max_items:  # önce en eski kritik olmayanı at
            calm = [i for i in self.items if i.data["severity"] != "critical"]
            self.items.remove(min(calm or self.items, key=lambda i: i.seq))
        self.version += 1
        return True

    def expire(self, now: float) -> bool:
        keep = [i for i in self.items if i.until > now]
        if len(keep) == len(self.items):
            return False
        self.items = keep
        self.version += 1
        return True

    def visible(self) -> list[Item]:
        return [] if self.hidden else list(self.items)
