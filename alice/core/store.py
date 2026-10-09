"""Akan verilerin deposu.

Her veri bir anahtar ("pc.gpu.temp", "dcs.fuel.kg", "watch.hr") altında zaman damgalı
sayı (ya da metin) olarak saklanır. Mod değişimleri de "core.mode" anahtarına yazılır;
böylece "dün DCS açıkken GPU kaç dereceydi?" sorusu, uçuş modundaki aralıklara
bakılarak cevaplanabilir. Bildirimler ve görev sonuçları `events` tablosunda durur.
"""

from __future__ import annotations

import fnmatch
import re
import sqlite3
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Iterable

from alice.config import alice_home

MODE_KEY = "core.mode"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS samples (ts REAL NOT NULL, key TEXT NOT NULL, value REAL, text TEXT);
CREATE INDEX IF NOT EXISTS ix_samples_key_ts ON samples(key, ts);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, kind TEXT NOT NULL, title TEXT NOT NULL,
    body TEXT NOT NULL DEFAULT '', severity TEXT NOT NULL DEFAULT 'info', topic TEXT NOT NULL DEFAULT '',
    delivered INTEGER NOT NULL DEFAULT 1);
CREATE INDEX IF NOT EXISTS ix_events_ts ON events(ts);
"""


@dataclass
class Stats:
    key: str
    count: int
    min: float | None
    max: float | None
    avg: float | None
    last: float | None
    max_ts: float | None = None

    def line(self) -> str:
        if not self.count:
            return f"{self.key}: veri yok"
        when = f" ({datetime.fromtimestamp(self.max_ts):%d.%m %H:%M})" if self.max_ts else ""
        return (f"{self.key}: ort {self.avg:.1f}, en düşük {self.min:.1f}, en yüksek {self.max:.1f}{when}, "
                f"son {self.last:.1f} ({self.count} ölçüm)")


@dataclass
class Event:
    id: int
    ts: float
    kind: str
    title: str
    body: str
    severity: str
    topic: str
    delivered: bool

    def line(self) -> str:
        mark = "" if self.delivered else " [susturuldu]"
        body = f": {self.body}" if self.body else ""
        return f"{datetime.fromtimestamp(self.ts):%d.%m %H:%M} {self.kind}/{self.severity}{mark} {self.title}{body}"


_REL = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*(s|sn|m|dk|h|sa|d|g|w|hf)\s*$", re.I)
_UNITS = {"s": 1, "sn": 1, "m": 60, "dk": 60, "h": 3600, "sa": 3600, "d": 86400, "g": 86400, "w": 604800, "hf": 604800}


def parse_time(value: str | float | None, now: float | None = None) -> float | None:
    """'15m', '2h', '3d' (şu andan geriye), 'dün', 'bugün', ISO tarih ya da epoch saniye."""
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return float(value)
    now = time.time() if now is None else now
    s = value.strip().lower()
    m = _REL.match(s)
    if m:
        return now - float(m.group(1)) * _UNITS[m.group(2)]
    today = datetime.fromtimestamp(now).replace(hour=0, minute=0, second=0, microsecond=0)
    if s in ("bugün", "today"):
        return today.timestamp()
    if s in ("dün", "yesterday"):
        return (today - timedelta(days=1)).timestamp()
    if s in ("now", "şimdi"):
        return now
    try:
        return float(s)
    except ValueError:
        pass
    try:
        return datetime.fromisoformat(value.strip()).timestamp()
    except ValueError as e:
        raise ValueError(f"zaman anlaşılamadı: {value!r} (ör. 2h, 3d, dün, 2026-10-08T20:00)") from e


class Store:
    def __init__(self, path: Path | str | None = None):
        self.path = Path(path) if path else alice_home() / "core.db"
        if str(self.path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._db = sqlite3.connect(str(self.path), check_same_thread=False, timeout=10)
        self._db.executescript(_SCHEMA)
        try:
            self._db.execute("PRAGMA journal_mode=WAL")
        except sqlite3.DatabaseError:
            pass

    def close(self) -> None:
        with self._lock:
            self._db.close()

    # ------------------------------------------------------------------ yazma
    def add(self, key: str, value: float | None = None, text: str | None = None, ts: float | None = None) -> None:
        self.add_many([(key, value, text)], ts)

    def add_many(self, rows: Iterable[tuple[str, float | None, str | None]], ts: float | None = None) -> None:
        ts = time.time() if ts is None else ts
        with self._lock:
            self._db.executemany("INSERT INTO samples(ts, key, value, text) VALUES (?, ?, ?, ?)",
                                 [(ts, k, None if v is None else float(v), t) for k, v, t in rows])
            self._db.commit()

    def add_event(self, kind: str, title: str, body: str = "", severity: str = "info", topic: str = "",
                  delivered: bool = True, ts: float | None = None) -> int:
        with self._lock:
            cur = self._db.execute(
                "INSERT INTO events(ts, kind, title, body, severity, topic, delivered) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (time.time() if ts is None else ts, kind, title, body, severity, topic, int(delivered)))
            self._db.commit()
            return int(cur.lastrowid)

    def prune(self, retention_days: float, now: float | None = None) -> int:
        cutoff = (time.time() if now is None else now) - retention_days * 86400
        with self._lock:
            n = self._db.execute("DELETE FROM samples WHERE ts < ?", (cutoff,)).rowcount
            self._db.execute("DELETE FROM events WHERE ts < ?", (cutoff,))
            self._db.commit()
        return n

    # ----------------------------------------------------------------- okuma
    def _q(self, sql: str, args: tuple = ()) -> list[tuple]:
        with self._lock:
            return self._db.execute(sql, args).fetchall()

    def keys(self, pattern: str = "*", since: float | None = None) -> list[str]:
        rows = self._q("SELECT DISTINCT key FROM samples WHERE ts >= ?", (since or 0,))
        return sorted(k for (k,) in rows if fnmatch.fnmatchcase(k, pattern))

    def last(self, key: str) -> tuple[float, float | None, str | None] | None:
        rows = self._q("SELECT ts, value, text FROM samples WHERE key = ? ORDER BY ts DESC LIMIT 1", (key,))
        return rows[0] if rows else None

    def series(self, key: str, since: float | None = None, until: float | None = None,
               mode: str | None = None) -> list[tuple[float, float]]:
        rows = self._q("SELECT ts, value FROM samples WHERE key = ? AND ts >= ? AND ts <= ? AND value IS NOT NULL "
                       "ORDER BY ts", (key, since or 0, until if until is not None else 1e18))
        if mode:
            spans = self.mode_spans(mode, since, until)
            rows = [r for r in rows if any(a <= r[0] < b for a, b in spans)]
        return [(float(t), float(v)) for t, v in rows]

    def texts(self, key: str, since: float | None = None, until: float | None = None) -> list[tuple[float, str]]:
        rows = self._q("SELECT ts, text FROM samples WHERE key = ? AND ts >= ? AND ts <= ? AND text IS NOT NULL "
                       "ORDER BY ts", (key, since or 0, until if until is not None else 1e18))
        return [(float(t), str(x)) for t, x in rows]

    def stats(self, key: str, since: float | None = None, until: float | None = None, mode: str | None = None) -> Stats:
        pts = self.series(key, since, until, mode)
        if not pts:
            return Stats(key, 0, None, None, None, None)
        vals = [v for _, v in pts]
        mx = max(pts, key=lambda p: p[1])
        return Stats(key, len(vals), min(vals), mx[1], sum(vals) / len(vals), vals[-1], mx[0])

    def mode_spans(self, mode: str, since: float | None = None, until: float | None = None) -> list[tuple[float, float]]:
        """Verilen modun açık olduğu [başlangıç, bitiş) aralıkları."""
        until = time.time() if until is None else until
        rows = self._q("SELECT ts, text FROM samples WHERE key = ? AND ts <= ? ORDER BY ts", (MODE_KEY, until))
        spans: list[tuple[float, float]] = []
        start = None
        for ts, text in rows:
            if text == mode and start is None:
                start = ts
            elif text != mode and start is not None:
                spans.append((start, ts))
                start = None
        if start is not None:
            spans.append((start, until))
        lo = since or 0
        return [(max(a, lo), b) for a, b in spans if b > lo]

    def events(self, since: float | None = None, kind: str | None = None, limit: int = 50) -> list[Event]:
        sql = "SELECT id, ts, kind, title, body, severity, topic, delivered FROM events WHERE ts >= ?"
        args: list = [since or 0]
        if kind:
            sql += " AND kind = ?"
            args.append(kind)
        rows = self._q(sql + " ORDER BY ts DESC LIMIT ?", (*args, limit))
        return [Event(r[0], r[1], r[2], r[3], r[4], r[5], r[6], bool(r[7])) for r in rows]
