"""Uzun süreli hafıza: kullanıcı tercihleri, kalıcı bilgiler ve geçmiş konuşmalar.

Her şey yerelde, ~/.alice/memory.db (SQLite) içinde durur; ücretli bir API ya da gömme (embedding) modeli yok.

- facts: kalıcı bilgiler ("Kullanıcı kahveyi şekersiz içer"). Türü: tercih, bilgi, kişi, alışkanlık, not.
  En önemlileri sistem istemine "profil" olarak girer, böylece model her konuşmada kullanıcıyı tanır.
- episodes: her konuşma turu (kullanıcının sözü + ALICE'in cevabı, kanal, zaman).
- Hatırlama: kullanıcının sözüne en çok benzeyen bilgiler ve geçmiş konuşmalar her turda mesaja eklenir.
  Benzerlik Türkçe için sade bir yöntemle ölçülür: harfler sadeleştirilir (ç->c, ı->i...), kelimeler ilk 4
  harfine kısaltılır (ekler düşer: "kahveyi", "kahvemi" -> "kahv"), seyrek kelimeler daha ağır basar (IDF).
- Öğrenme: "bunu hatırla: X" gibi açık istekler doğrudan kaydedilir. Kullanıcı kendinden bahsediyorsa
  (seviyorum, benim, her zaman...) konuşma arka planda hızlı modele verilir; kalıcı bilgileri çıkarır,
  eskiyenleri günceller ya da siler.
"""

from __future__ import annotations

import json
import math
import re
import sqlite3
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from alice.config import alice_home

KINDS = {"preference": "tercih", "fact": "bilgi", "person": "kişi", "routine": "alışkanlık", "note": "not"}
_KIND_ORDER = ["preference", "person", "fact", "routine", "note"]

_SCHEMA = """
CREATE TABLE IF NOT EXISTS facts (
    id INTEGER PRIMARY KEY AUTOINCREMENT, created REAL NOT NULL, updated REAL NOT NULL,
    kind TEXT NOT NULL DEFAULT 'fact', text TEXT NOT NULL, terms TEXT NOT NULL DEFAULT '',
    source TEXT NOT NULL DEFAULT '', hits INTEGER NOT NULL DEFAULT 0, last_used REAL);
CREATE TABLE IF NOT EXISTS episodes (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, channel TEXT NOT NULL DEFAULT '',
    user TEXT NOT NULL, reply TEXT NOT NULL DEFAULT '', terms TEXT NOT NULL DEFAULT '');
CREATE INDEX IF NOT EXISTS ix_episodes_ts ON episodes(ts);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""

_FOLD = str.maketrans({"ç": "c", "ğ": "g", "ı": "i", "ö": "o", "ş": "s", "ü": "u", "â": "a", "î": "i", "û": "u"})
_STOP = set("""
ve veya ile ama fakat ki de da mi mı mu mü bu şu o bir iki çok daha en gibi için icin kadar sonra önce once ne neden
nasil nasıl nerede hangi her hiç hic var yok evet hayır hayir tamam şey sey ben sen biz siz onlar bana sana beni seni
benim senin bunu şunu onu ise ya hem diye olan olarak oldu olur olsun misin musun mısın müsün lütfen lutfen alice
hey merhaba selam acar acabilir yapar yapabilir eder edebilir şimdi simdi biraz artık neydi nedir hangisi hangisiydi
kadardı miydi mıydı ediyor ettim dedim demiştim kullanıcı kullanıcının alıce the a an to of and or is are i you it in
on for
""".split())
_STOP = {w.translate(_FOLD) for w in _STOP}
_CLOCK = re.compile(r"\s*\[şu an:[^\]]*\]\s*$")
_RECALL = re.compile(r"\s*\[hafızadan[^\]]*\]\s*$", re.S)

# Açıkça hatırlatma isteği: model olmadan, anında kaydedilir.
_EXPLICIT = [
    re.compile(r"^(?:alice[,\s]+)?(?:(?:bunu|şunu|sunu)\s+)?(?:hatırla|hatirla|aklında tut|aklinda tut|unutma)"
               r"\s*[:,-]?\s+(?:ki\s+)?(.{6,})$", re.I),
    re.compile(r"^(?:alice[,\s]+)?(.{6,}?)[,.;\s]+(?:bunu\s+)?(?:hatırla|hatirla|unutma|aklında tut|aklinda tut)"
               r"(?:\s+(?:lütfen|lutfen|tamam mı|tamam mi))?[.!]?$", re.I),
]
# Kullanıcının kendinden bahsettiğini gösteren kelime kökleri (sadeleştirilmiş). Sadece bunlar varken modele
# çıkarım yaptırılır; "saat kaç" gibi her soru için ücretsiz kota harcanmaz.
_SIGNALS = ("benim", "bana ", "beni ", "seviyor", "sevmiyor", "sevmem", "severim", "hoslan", "nefret", "tercih",
            "istemiyorum", "her zaman", "hep ", "asla", "genelde", "genellikle", "adim ", "ismim", "yasim",
            "yasiyorum", "oturuyorum", "calisiyorum", "okuyorum", "dogum", "kardes", "annem", "babam", "arkadasim",
            "sevgilim", "esim", "kizim", "oglum", "favori", "en sevdig", "aliskanl", "hatirla", "unutma",
            "bundan sonra", "artik ", "diye hitap", "diye cagir", "olarak cagir", "aklinda tut",
            "i like", "i love", "i hate", "i prefer", "my ", "remember", "call me")


def fold(text: str) -> str:
    return text.replace("İ", "i").replace("I", "ı").lower().translate(_FOLD)


def terms(text: str) -> list[str]:
    out = []
    for w in re.findall(r"[a-z0-9]+", fold(text)):
        if (len(w) < 3 and not w.isdigit()) or w in _STOP:
            continue
        out.append(w[:4])  # Türkçe ekler düşsün: kahveyi/kahvemi -> kahv, filmini/film -> film
    return out


def clean_user_text(text: str) -> str:
    """Sesli asistanın eklediği saat ve hafıza notunu at: kayda ve aramaya kullanıcının kendi sözü girsin."""
    return _RECALL.sub("", _CLOCK.sub("", text or "")).strip()


def explicit_note(text: str) -> str | None:
    t = clean_user_text(text).strip().rstrip(".!")
    for rx in _EXPLICIT:
        m = rx.match(t)
        if m:
            note = m.group(1).strip(" ,.:;-")
            if len(note.split()) >= 2:
                return note[0].upper() + note[1:]
    return None


def has_signal(text: str) -> bool:
    t = " " + fold(clean_user_text(text)) + " "
    return any(s in t for s in _SIGNALS)


def _ago(ts: float, now: float | None = None) -> str:
    d = ((now or time.time()) - ts) / 86400
    if d < 1:
        return "bugün"
    if d < 2:
        return "dün"
    if d < 30:
        return f"{int(d)} gün önce"
    if d < 365:
        return f"{int(d // 30)} ay önce"
    return f"{int(d // 365)} yıl önce"


@dataclass
class Fact:
    id: int
    kind: str
    text: str
    created: float
    updated: float
    hits: int = 0

    def line(self) -> str:
        return f"#{self.id} ({KINDS.get(self.kind, self.kind)}) {self.text}"


@dataclass
class Episode:
    id: int
    ts: float
    channel: str
    user: str
    reply: str

    def line(self, now: float | None = None, width: int = 220) -> str:
        reply = f" / ALICE: {self.reply}" if self.reply else ""
        body = f"Kullanıcı: {self.user}{reply}"
        if len(body) > width:
            body = body[:width - 1] + "…"
        return f"{datetime.fromtimestamp(self.ts):%d.%m.%Y %H:%M} ({_ago(self.ts, now)}) {body}"


@dataclass
class Hit:
    score: float
    item: Fact | Episode

    def line(self) -> str:
        return self.item.line()


class LongMemory:
    def __init__(self, path: Path | str, options: dict[str, Any] | None = None,
                 extractor: Callable[[str], str] | None = None):
        o = options or {}
        self.path = Path(path) if str(path) != ":memory:" else path
        self.auto_extract = bool(o.get("auto_extract", True))
        self.recall_limit = int(o.get("recall_limit", 4))
        self.profile_chars = int(o.get("profile_chars", 2500))
        self.keep_days = float(o.get("keep_days", 365))
        self.extractor = extractor  # prompt -> JSON metni; None ise sadece açık "hatırla" istekleri kaydedilir
        self._lock = threading.RLock()
        self._learn_lock = threading.Lock()
        if isinstance(self.path, Path):
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(self.path), check_same_thread=False, timeout=10)
        if isinstance(self.path, Path):
            try:
                self.db.execute("PRAGMA journal_mode=WAL")  # çekirdek, ses ve uygulama aynı dosyayı kullanabilir
            except sqlite3.DatabaseError:
                pass
        self.db.executescript(_SCHEMA)
        self.db.commit()
        self._prune()

    # ------------------------------------------------------------------ kurulum
    @classmethod
    def open(cls, cfg=None, router=None) -> "LongMemory | None":
        o = dict((cfg.data.get("memory") if cfg is not None else None) or {})
        if not o.get("enabled", True):
            return None
        mem = cls(alice_home() / "memory.db", o, make_extractor(o, router))
        mem.import_markdown(alice_home() / "memory.md")
        return mem

    def close(self) -> None:
        with self._lock:
            self.db.close()

    def _prune(self) -> None:
        if self.keep_days > 0:
            with self._lock:
                self.db.execute("DELETE FROM episodes WHERE ts < ?", (time.time() - self.keep_days * 86400,))
                self.db.commit()

    def _meta(self, key: str, value: str | None = None) -> str | None:
        with self._lock:
            if value is not None:
                self.db.execute("INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)", (key, value))
                self.db.commit()
                return value
            row = self.db.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
            return row[0] if row else None

    @property
    def version(self) -> int:
        return int(self._meta("version") or 0)

    def _bump(self) -> None:
        self._meta("version", str(self.version + 1))

    def import_markdown(self, path: Path) -> int:
        """Eski düz hafıza dosyasını (memory.md, "* not  _(tarih)_" satırları) bir kez içeri al."""
        if self._meta("imported_md") or not path.is_file():
            return 0
        n = 0
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line.startswith(("* ", "- ")):
                continue
            text = re.sub(r"\s*_\(\d{4}-\d{2}-\d{2}\)_\s*$", "", line[2:]).strip()
            if text:
                self.remember(text, "note", source="memory.md")
                n += 1
        self._meta("imported_md", datetime.now().isoformat(timespec="seconds"))
        return n

    # ------------------------------------------------------------------ bilgiler
    def facts(self, limit: int | None = None) -> list[Fact]:
        with self._lock:
            rows = self.db.execute("SELECT id, kind, text, created, updated, hits FROM facts "
                                   "ORDER BY updated DESC" + (f" LIMIT {int(limit)}" if limit else "")).fetchall()
        return [Fact(*r) for r in rows]

    def _similar(self, text: str) -> Fact | None:
        mine = set(terms(text))
        if not mine:
            return None
        best, best_score = None, 0.0
        for f in self.facts():
            other = set(terms(f.text))
            if not other:
                continue
            j = len(mine & other) / len(mine | other)
            if j > best_score:
                best, best_score = f, j
        return best if best_score >= 0.75 else None

    def remember(self, text: str, kind: str = "fact", source: str = "") -> tuple[int, bool]:
        """Bilgiyi kaydet. Neredeyse aynısı varsa yenisi onun yerine geçer. (id, yeni_mi) döner."""
        text = " ".join(text.split())
        if not text:
            raise ValueError("boş bilgi")
        kind = kind if kind in KINDS else "fact"
        now = time.time()
        with self._lock:
            old = self._similar(text)
            if old is not None:
                self.db.execute("UPDATE facts SET text = ?, terms = ?, kind = ?, updated = ? WHERE id = ?",
                                (text, " ".join(terms(text)), kind, now, old.id))
                fid, new = old.id, False
            else:
                cur = self.db.execute("INSERT INTO facts(created, updated, kind, text, terms, source) "
                                      "VALUES (?, ?, ?, ?, ?, ?)", (now, now, kind, text, " ".join(terms(text)), source))
                fid, new = int(cur.lastrowid), True
            self.db.commit()
            self._bump()
        return fid, new

    def update(self, fid: int, text: str) -> bool:
        text = " ".join(text.split())
        with self._lock:
            cur = self.db.execute("UPDATE facts SET text = ?, terms = ?, updated = ? WHERE id = ?",
                                  (text, " ".join(terms(text)), time.time(), int(fid)))
            self.db.commit()
            if cur.rowcount:
                self._bump()
        return bool(cur.rowcount)

    def forget(self, fid: int) -> Fact | None:
        with self._lock:
            row = self.db.execute("SELECT id, kind, text, created, updated, hits FROM facts WHERE id = ?",
                                  (int(fid),)).fetchone()
            if row is None:
                return None
            self.db.execute("DELETE FROM facts WHERE id = ?", (int(fid),))
            self.db.commit()
            self._bump()
        return Fact(*row)

    def forget_matching(self, query: str) -> tuple[list[Fact], list[Hit]]:
        """Sorguya çok benzeyen bilgileri sil. Emin olunamazsa hiçbirini silmez, adayları döndürür."""
        hits = [h for h in self.search(query, limit=5, episodes=False) if isinstance(h.item, Fact)]
        sure = [h for h in hits if h.score >= 0.6]
        if not sure or (len(sure) > 1 and sure[0].score - sure[1].score < 0.15 and sure[1].score < 0.9):
            return [], hits
        gone = self.forget(sure[0].item.id)
        return ([gone] if gone else []), hits

    # ------------------------------------------------------------------ konuşmalar
    def record(self, user: str, reply: str, channel: str = "") -> int | None:
        user = clean_user_text(user)
        reply = (reply or "").strip()
        if not user:
            return None
        with self._lock:
            cur = self.db.execute("INSERT INTO episodes(ts, channel, user, reply, terms) VALUES (?, ?, ?, ?, ?)",
                                  (time.time(), channel, user[:2000], reply[:2000], " ".join(terms(user + " " + reply))))
            self.db.commit()
        return int(cur.lastrowid)

    def recent(self, n: int = 5) -> list[Episode]:
        with self._lock:
            rows = self.db.execute("SELECT id, ts, channel, user, reply FROM episodes ORDER BY ts DESC LIMIT ?",
                                   (int(n),)).fetchall()
        return [Episode(*r) for r in reversed(rows)]

    # ------------------------------------------------------------------ arama
    def search(self, query: str, limit: int = 5, facts: bool = True, episodes: bool = True,
               min_score: float = 0.3, exclude_after: float | None = None) -> list[Hit]:
        q = list(dict.fromkeys(terms(clean_user_text(query))))
        if not q:
            return []
        with self._lock:
            frows = self.db.execute("SELECT id, kind, text, created, updated, hits, terms FROM facts").fetchall() \
                if facts else []
            erows = self.db.execute("SELECT id, ts, channel, user, reply, terms FROM episodes "
                                    "ORDER BY ts DESC LIMIT 3000").fetchall() if episodes else []
        docs = [set(r[-1].split()) for r in frows] + [set(r[-1].split()) for r in erows]
        n = len(docs) or 1
        df = {t: sum(1 for d in docs if t in d) for t in q}
        idf = {t: math.log(1 + n / (1 + df[t])) + 0.1 for t in q}
        total = sum(idf.values())
        now = time.time()
        hits: list[Hit] = []
        for r, d in zip(frows, docs[:len(frows)]):
            matched = [t for t in q if t in d]
            if matched:
                score = sum(idf[t] for t in matched) / total
                if score >= min_score:
                    hits.append(Hit(score + min(r[5], 10) * 0.005, Fact(*r[:6])))
        for r, d in zip(erows, docs[len(frows):]):
            if exclude_after is not None and r[1] >= exclude_after:
                continue
            matched = [t for t in q if t in d]
            # Konuşmalarda tek ortak kelime çoğu zaman tesadüf: kısa sorgu dışında en az iki kelime eşleşsin.
            if len(matched) < min(2, len(q)):
                continue
            score = sum(idf[t] for t in matched) / total
            score *= 0.6 + 0.4 * math.exp(-(now - r[1]) / 86400 / 60)  # eski konuşma biraz geride kalır
            if score >= min_score + 0.15:
                hits.append(Hit(score, Episode(*r[:5])))
        hits.sort(key=lambda h: h.score, reverse=True)
        return hits[:limit]

    def recall(self, query: str, exclude_after: float | None = None) -> str:
        """Kullanıcının sözüne eklenecek hafıza notu (boş olabilir)."""
        hits = self.search(query, limit=self.recall_limit, exclude_after=exclude_after)
        if not hits:
            return ""
        ids = [h.item.id for h in hits if isinstance(h.item, Fact)]
        if ids:
            with self._lock:
                self.db.executemany("UPDATE facts SET hits = hits + 1, last_used = ? WHERE id = ?",
                                    [(time.time(), i) for i in ids])
                self.db.commit()
        lines = "\n".join(f"- {h.item.text if isinstance(h.item, Fact) else h.item.line()}" for h in hits)
        return ("[hafızadan, bu konuşmayla ilgili olabilir; işine yararsa kullan, hafızadan okuduğunu söyleme:\n"
                f"{lines}]")

    # ------------------------------------------------------------------ istem
    def profile(self, chars: int | None = None, recent: int = 3) -> str:
        """Sistem istemine giren blok: kullanıcıyı tanıtan bilgiler + son birkaç konuşma."""
        chars = chars or self.profile_chars
        with self._lock:
            rows = self.db.execute("SELECT id, kind, text, created, updated, hits FROM facts").fetchall()
        facts = [Fact(*r) for r in rows]
        facts.sort(key=lambda f: (_KIND_ORDER.index(f.kind) if f.kind in _KIND_ORDER else 9, -f.hits, -f.updated))
        out, used = [], 0
        for f in facts:
            line = f"- ({KINDS.get(f.kind, f.kind)}) {f.text}"
            if used + len(line) > chars:
                break
            out.append(line)
            used += len(line) + 1
        eps = self.recent(recent)
        if not out and not eps:
            return ""
        parts = ["# Long-term memory\nWhat you know about the user from earlier conversations (stored locally). Use it "
                 "naturally to personalize answers; don't recite it. Each user message may end with a [hafızadan ...] "
                 "note of related memories. To save something new, change or delete a memory, use the memory tool."]
        if out:
            parts.append("\n".join(out))
        if eps:
            parts.append("Recent conversations:\n" + "\n".join(f"- {e.line(width=160)}" for e in eps))
        return "\n".join(parts)

    def export_markdown(self) -> str:
        facts = self.facts()
        eps = self.recent(10)
        if not facts and not eps:
            return "(hafıza boş)"
        lines = ["# Bilgiler"] + [f"* {f.line()}" for f in facts]
        if eps:
            lines += ["", "# Son konuşmalar"] + [f"* {e.line()}" for e in eps]
        return "\n".join(lines)

    # ------------------------------------------------------------------ öğrenme
    def learn(self, user: str, reply: str = "", background: bool = True) -> None:
        """Konuşma turundan kalıcı bilgi çıkar. Açık "hatırla" isteği hemen; diğerleri arka planda modelle."""
        user = clean_user_text(user)
        note = explicit_note(user)
        if note:
            self.remember(note, "note", source="kullanıcı")
            return
        if self.extractor is None or not self.auto_extract or not has_signal(user):
            return
        if background:
            threading.Thread(target=self._extract, args=(user, reply), daemon=True, name="alice-memory").start()
        else:
            self._extract(user, reply)

    def _extract(self, user: str, reply: str) -> None:
        if not self._learn_lock.acquire(timeout=60):
            return
        try:
            related = [h.item for h in self.search(user, limit=8, episodes=False, min_score=0.1)]
            known = "\n".join(f"{f.id}: {f.text}" for f in related if isinstance(f, Fact)) or "(yok)"
            prompt = EXTRACT_PROMPT.format(known=known, user=user[:1500], reply=reply[:800])
            raw = self.extractor(prompt) if self.extractor else ""
            self.apply(parse_json(raw))
        except Exception:  # noqa: BLE001 - öğrenme hatası konuşmayı etkilemesin
            pass
        finally:
            self._learn_lock.release()

    def apply(self, data: dict[str, Any]) -> list[str]:
        done = []
        for fid in data.get("remove") or []:
            try:
                if self.forget(int(fid)):
                    done.append(f"silindi #{fid}")
            except (TypeError, ValueError):
                continue
        for u in data.get("update") or []:
            try:
                if isinstance(u, dict) and u.get("text") and self.update(int(u["id"]), str(u["text"])):
                    done.append(f"güncellendi #{u['id']}")
            except (TypeError, ValueError, KeyError):
                continue
        for a in (data.get("add") or [])[:5]:
            text = a.get("text") if isinstance(a, dict) else a
            if isinstance(text, str) and 3 <= len(text) <= 300:
                fid, _ = self.remember(text, (a.get("kind") if isinstance(a, dict) else None) or "fact", "otomatik")
                done.append(f"eklendi #{fid}")
        return done


EXTRACT_PROMPT = """You maintain the long-term memory of ALICE, a personal assistant. From the exchange below, extract only \
DURABLE facts about the user that will still matter in future conversations: preferences and dislikes, personal info \
(name, city, job, birthday), people in their life, habits/routines, how they want ALICE to behave. Ignore one-off \
requests ("open YouTube"), questions, small talk and anything about the current moment.
Write each fact as one short Turkish sentence in the third person, starting with "Kullanıcı" when natural \
(e.g. "Kullanıcı kahveyi şekersiz içer."). kind: preference | fact | person | routine | note.
Existing related memories (id: text):
{known}
If the user contradicts or refines an existing memory, put it in "update" (same id, new text) or "remove" instead of \
adding a duplicate. If there is nothing durable, return empty lists.
Answer ONLY with JSON: {{"add": [{{"text": "...", "kind": "..."}}], "update": [{{"id": 1, "text": "..."}}], "remove": [2]}}

User: {user}
ALICE: {reply}"""


def parse_json(raw: str) -> dict[str, Any]:
    raw = (raw or "").strip()
    m = re.search(r"\{.*\}", raw, re.S)
    if not m:
        return {}
    try:
        data = json.loads(m.group(0))
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def make_extractor(options: dict[str, Any], router) -> Callable[[str], str] | None:
    """Bilgi çıkarımı için mevcut ücretsiz model zincirini (varsayılan "fast" rolü) kullanan fonksiyon."""
    if router is None or not options.get("auto_extract", True):
        return None
    role = str(options.get("extract_role", "fast"))

    def extract(prompt: str) -> str:
        msgs = [{"role": "user", "content": prompt}]
        try:
            res = router.chat(role, msgs, temperature=0, max_tokens=500, response_format={"type": "json_object"})
        except Exception:  # noqa: BLE001 - bazı modeller response_format'ı reddediyor
            res = router.chat(role, msgs, temperature=0, max_tokens=500)
        return res.message.get("content") or ""

    return extract


_shared: dict[str, LongMemory] = {}
_shared_lock = threading.Lock()


def shared_memory(cfg=None, router=None) -> LongMemory | None:
    """Süreç başına tek hafıza nesnesi (araç, ajan ve Live aynı bağlantıyı kullanır)."""
    key = str(alice_home())
    with _shared_lock:
        mem = _shared.get(key)
        if mem is None:
            mem = LongMemory.open(cfg, router)
            if mem is None:
                return None
            _shared[key] = mem
        elif mem.extractor is None and router is not None and mem.auto_extract:
            mem.extractor = make_extractor(dict((cfg.data.get("memory") if cfg is not None else None) or {}), router)
        return mem


def attach_memory(agent, channel: str) -> Any:
    """Etkileşimli bir ajana hafızayı bağla: profil sistem istemine girer, her tur kaydedilir ve öğrenilir."""
    try:
        mem = shared_memory(agent.config, agent.router)
    except Exception:  # noqa: BLE001 - bozuk hafıza dosyası ALICE'i açılmaz yapmasın
        mem = None
    agent.memory, agent.memory_channel = mem, channel
    if mem is not None:
        agent.ctx.state["memory"] = mem
        agent.refresh_memory(force=True)
    return mem


def profile_text(home: Path | None = None) -> str:
    """Sistem istemi için profil (hafıza dosyası yoksa boş; dosya yaratmaz)."""
    home = home or alice_home()
    if not (home / "memory.db").is_file() and not (home / "memory.md").is_file():
        return ""
    mem = shared_memory()
    return mem.profile() if mem is not None else ""
