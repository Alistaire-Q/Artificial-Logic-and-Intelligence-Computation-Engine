"""HUD'un çizimden bağımsız durumu: gelen komutu doğrula, öğeleri tut, süresi dolanı sil.

Pencere (window.py) bunu yalnızca okur; böylece protokol ve ömür kuralları tkinter olmadan test edilir.
"""

from __future__ import annotations

import json
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any

KINDS = ("arrow", "edge", "label", "card", "box", "progress", "cursor")
CONTROL = ("clear", "toggle", "mode", "context", "history")
# Yoğunluk: auto oyunda "game", masaüstünde "all" olur. game: yalnız tehdit, uyarı ve oyunla ilgili; quiet: yalnız
# kritik. Oyun HUD'larındaki gibi kullanıcı ayarı (kaldırmak değil, azaltmak).
MODES = ("auto", "all", "game", "quiet")
MODE_NAMES = {"auto": "otomatik", "all": "hepsi", "game": "oyun", "quiet": "sessiz"}
GAME_KEEP = ("kill", "voice", "__mode__")  # oyun modunda bilgi düzeyinde olsa da görünenler (kimlik öneki)
HISTORY_MAX, HISTORY_SHOW = 8, 3
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
    if kind == "edge":
        return clock_of(msg.get("clock")) is not None
    if kind == "arrow":  # yönü bilinmeyen tehdit (yönsüz kilit/füze uyarısı) de gösterilir: oksuz, nabızla
        if msg.get("clock") in (None, ""):
            return bool(str(msg.get("text") or ""))  # yönsüz: en azından ne olduğu yazmalı
        return clock_of(msg.get("clock")) is not None
    if kind == "cursor":
        return all(_num(msg.get(k)) is not None for k in ("x", "y"))
    if kind == "box":
        return all(_num(msg.get(k)) is not None for k in ("x", "y", "w", "h"))
    if kind == "progress":
        return _num(msg.get("value")) is not None
    return bool(str(msg.get("text") or msg.get("title") or ""))  # label, card: boş yazı çizilmez


class HudModel:
    def __init__(self, max_items: int = MAX_ITEMS, mode: str = "auto"):
        self.max_items = max_items
        self.items: list[Item] = []
        self.hidden = False
        self.mode = mode if mode in MODES else "auto"
        self.in_game = False  # çekirdek bildirir (uçuş/oyun modu)
        self.history: deque[tuple[str, str, str]] = deque(maxlen=HISTORY_MAX)  # (saat, başlık, yazı)
        self.version = 0  # her değişiklikte artar: pencere sadece değişince yeniden çizer
        self._seq = 0

    def apply(self, msg: dict[str, Any], now: float) -> bool:
        """Komutu uygula. Bir şey değiştiyse True."""
        kind = msg.get("kind")
        if kind == "toggle":
            self.hidden = not self.hidden
            self.version += 1
            return True
        if kind == "mode":
            value = str(msg.get("value") or "")
            self.mode = value if value in MODES else MODES[(MODES.index(self.mode) + 1) % len(MODES)]
            self.version += 1
            self._put("label", {"text": f"HUD · {MODE_NAMES[self.mode]}", "icon": "sparkle"}, now, 2.0, "__mode__")
            return True
        if kind == "context":
            game = bool(msg.get("game"))
            changed = game != self.in_game
            self.in_game = game
            self.version += changed
            return changed
        if kind == "history":
            return self._show_history(now)
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
        if kind == "card" and not key.startswith(("__", "kill")):
            self.history.append((time.strftime("%H:%M"), str(data.get("title") or "ALICE"),
                                 " ".join(str(data.get("text") or "").split())))
        self._put(kind, data, now, ttl, key)
        return True

    def _put(self, kind: str, data: dict[str, Any], now: float, ttl: float, key: str) -> None:
        data.setdefault("severity", "info")
        if key:  # aynı kimlik yenisiyle değişir: tekrarlanan tehdit ekranda birikmez
            self.items = [i for i in self.items if i.id != key]
        self._seq += 1
        self.items.append(Item(kind, data, now, now + ttl, key, self._seq))
        while len(self.items) > self.max_items:  # önce en eski kritik olmayanı at
            calm = [i for i in self.items if i.data["severity"] != "critical"]
            self.items.remove(min(calm or self.items, key=lambda i: i.seq))
        self.version += 1

    def _show_history(self, now: float) -> bool:
        """Kaçırılan son bildirimler tek kartta (Ada tıklanmaz: kısayolla açılır)."""
        last = list(self.history)[-HISTORY_SHOW:][::-1]
        body = " · ".join(f"{t} {title}: {text[:60]}" if text else f"{t} {title}" for t, title, text in last)
        self._put("card", {"title": "Son bildirimler", "text": body or "Henüz bildirim yok.", "icon": "clock"},
                  now, 10.0, "__history__")
        return True

    def effective_mode(self) -> str:
        if self.mode == "auto":
            return "game" if self.in_game else "all"
        return self.mode

    def allows(self, it: Item) -> bool:
        mode = self.effective_mode()
        if mode == "all" or it.id.startswith("__mode__"):
            return True
        sev = it.data.get("severity")
        if mode == "quiet":
            return sev == "critical"
        return (it.kind in ("arrow", "edge") or sev in ("warn", "critical") or it.id.startswith(GAME_KEEP)
                or it.id == "__history__")

    def expire(self, now: float) -> bool:
        keep = [i for i in self.items if i.until > now]
        if len(keep) == len(self.items):
            return False
        self.items = keep
        self.version += 1
        return True

    def visible(self) -> list[Item]:
        return [] if self.hidden else [i for i in self.items if self.allows(i)]
