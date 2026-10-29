"""Uzun süreli hafıza: kullanıcı tercihleri, kalıcı bilgiler ve geçmiş konuşmalar.

Her şey yerelde, ~/.alice/memory.db (SQLite) içinde durur.

- facts: kalıcı bilgiler ("Kullanıcı kahveyi şekersiz içer"). Türü: tercih, bilgi, kişi, alışkanlık, not.
  En önemlileri sistem istemine "profil" olarak girer, böylece model her konuşmada kullanıcıyı tanır.
- episodes: her konuşma turu (kullanıcının sözü + ALICE'in cevabı, kanal, zaman).
- Hatırlama: kullanıcının sözüne en çok benzeyen bilgiler ve geçmiş konuşmalar her turda mesaja eklenir.
  Benzerlik Türkçe için sade bir yöntemle ölçülür: harfler sadeleştirilir (ç->c, ı->i...), kelimeler ilk 4
  harfine kısaltılır (ekler düşer: "kahveyi", "kahvemi" -> "kahv"), seyrek kelimeler daha ağır basar (IDF).
  Ücretsiz bir embedding anahtarı varsa (varsayılan Gemini) anlam araması da eklenir: "Unity projem" ile "oyun
  motorunda yaptığım şey" aynı hatırayı bulur (bkz. alice/memvec.py). Yoksa sadece kelime araması.
- Öğrenme: "bunu hatırla: X" gibi açık istekler doğrudan kaydedilir. Kullanıcı kendinden bahsediyorsa
  (seviyorum, benim, her zaman...) konuşma arka planda hızlı modele verilir; kalıcı bilgileri çıkarır,
  eskiyenleri günceller ya da siler.
- Özetleme (digest): bir konuşma bitince (varsayılan 20 dk sessizlik) bütün konuşma tek seferde hızlı modele
  verilir: kısa özet, sinyal kelimesi olmadan geçen kalıcı bilgiler ("yarın sınavım var", "Unity'de FPS
  yapıyorum") ve takip edilecekler (sınav nasıl geçti diye sorulacak, yarım kalan iş) çıkarılır. Özetler ve
  günü gelen takipler sistem istemine girer: ALICE dün ne konuşulduğunu bilir, sınavı kendisi sorar.
"""

from __future__ import annotations

import json
import logging
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

# Yutulan hatalar için iz: varsayılan olarak sessiz, `logging.basicConfig(level=logging.DEBUG)` ile görünür.
log = logging.getLogger(__name__)

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
CREATE TABLE IF NOT EXISTS summaries (
    id INTEGER PRIMARY KEY AUTOINCREMENT, start REAL NOT NULL, end REAL NOT NULL, channel TEXT NOT NULL DEFAULT '',
    text TEXT NOT NULL, terms TEXT NOT NULL DEFAULT '');
CREATE INDEX IF NOT EXISTS ix_summaries_end ON summaries(end);
CREATE TABLE IF NOT EXISTS followups (
    id INTEGER PRIMARY KEY AUTOINCREMENT, created REAL NOT NULL, due REAL, kind TEXT NOT NULL DEFAULT 'ask',
    text TEXT NOT NULL, done INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS vectors (
    kind TEXT NOT NULL, ref INTEGER NOT NULL, vec BLOB NOT NULL, PRIMARY KEY (kind, ref));
"""
FOLLOWUP_KINDS = {"ask": "sor", "remind": "hatırlat", "open": "yarım kaldı"}
# Eski "sevgili/mommy" kişiliğinden kalan izler. Hafızada kaldıkça yeni kişilik "Buradayım bebeğim" diye geri
# dönüyordu (voice.log); Ayarlar > Hafıza bunları gösterip tek tıkla temizler.
OLD_ROLE_RX = re.compile(
    r"\b(bebeğim|bebegim|aşkım|askim|tatlım|tatlim|sevgilin|sevgilim|sevgili(si)?|kız ?arkadaş\w*|mommy|mami|"
    r"canım benim|iyi çocu\w*|choker|kıskan\w*|cilve\w*|öpücük|sarıl\w*)\b", re.I)

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


RECALL_ITEM_CHARS, RECALL_TOTAL_CHARS = 400, 2000


def _clip(text: str, n: int) -> str:
    return text if len(text) <= n else text[:n - 1] + "…"


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


# Model kurala rağmen bir turun sonucunu "bilgi" diye yazarsa (eski hafızada çoğu buydu): dosya yolu, başarısız/denenen
# iş, onaylanan değer, ALICE'i test etmek. Sadece otomatik çıkarılanlara uygulanır; "bunu hatırla" her zaman kaydedilir.
_EPHEMERAL = re.compile(
    r"[a-z]:\\|(?:^|\s)/[\w.-]+/|\b(?:başarısız|basarisiz|onayladı|onayladi|denedi|deniyor|test (?:et|ed)\w*|"
    r"deneme klasör\w*|indirilenler|kurulum dosyası|şu anda|su anda|şu an\b|bu konuşmada|endişe duy\w*)", re.I)


def ephemeral(text: str) -> bool:
    return bool(_EPHEMERAL.search(text))


# Ses tanımanın gürültüden uydurduğu yabancı yazı ("हे या इनिशियलाइज़ेशन"): sohbet kaydına girip aramayı kirletiyordu.
# Yunan, Kiril, İbrani/Arap, Hint, Tay/Tibet, Japon, Çin, Kore yazıları.
_FOREIGN_RANGES = ((0x0370, 0x04FF), (0x0590, 0x06FF), (0x0900, 0x0FFF), (0x3040, 0x30FF), (0x3400, 0x9FFF),
                   (0xAC00, 0xD7AF))
_FOREIGN = re.compile("[" + "".join(f"{chr(a)}-{chr(b)}" for a, b in _FOREIGN_RANGES) + "]")


# Kullanıcının ALICE'ten kalıcı isteği ("... sormasını ister", "asla ... yapmasın").
_INSTRUCTION = re.compile(r"\b(ister|istemez|istiyor|istemiyor|asla|her zaman|hitap|çağır|cagir|sorulmasını)\w*",
                          re.I)


def noise_turn(user: str) -> bool:
    """Kaydedilmeye değmeyen tur: harflerin çoğu Latin dışı yazıysa (ses tanıma gürültüyü başka dilde yazmış)."""
    letters = [c for c in user if c.isalpha()]
    return bool(letters) and len(_FOREIGN.findall(user)) > len(letters) * 0.3


# Cümle geçmiş zamanla bitiyorsa ("şarkısını açtı", "iletişime geçti", "dinlemeye çalıştı") bir olaydır, alışkanlık
# değil ("dinler", "oynar"). Özetlerden çıkarılan bilgilerde bu ayrım modele bırakılamadı.
_PAST = re.compile(r"\w+(?:dı|di|du|dü|tı|ti|tu|tü)(?:lar|ler)?[.!]?$", re.I)


def past_event(text: str) -> bool:
    return bool(_PAST.search(text.strip()))


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
class Summary:
    id: int
    start: float
    end: float
    channel: str
    text: str

    def line(self, now: float | None = None) -> str:
        return f"{datetime.fromtimestamp(self.start):%d.%m.%Y %H:%M} ({_ago(self.start, now)}) özet: {self.text}"


@dataclass
class Followup:
    id: int
    created: float
    due: float | None
    kind: str
    text: str
    done: int = 0

    def line(self, now: float | None = None) -> str:
        when = ""
        if self.due is not None:
            when = f", {datetime.fromtimestamp(self.due):%d.%m}"
        return f"#{self.id} ({FOLLOWUP_KINDS.get(self.kind, self.kind)}{when}) {self.text}"


@dataclass
class Hit:
    score: float
    item: Fact | Episode | Summary

    def line(self) -> str:
        return self.item.line()


class LongMemory:
    def __init__(self, path: Path | str, options: dict[str, Any] | None = None,
                 extractor: Callable[[str], str] | None = None, embedder: Any = None):
        o = options or {}
        self.embedder = embedder  # alice.memvec.Embedder ya da None (sadece kelime araması)
        self.embed_min = float(o.get("embed_min", 0.68))  # bu kosinüsün altı "ilgisiz" sayılır
        self.digest_enabled = bool(o.get("digest", True))
        self.consolidate_enabled = bool(o.get("consolidate", True))
        self.max_facts = int(o.get("max_facts", 60))  # toparlamada hedef: profil kısa ve öz kalsın
        self.session_gap = float(o.get("session_gap_minutes", 20)) * 60
        self._digest_lock = threading.Lock()
        self._index_lock = threading.Lock()
        self._last_digest_check = 0.0
        self.path = Path(path) if str(path) != ":memory:" else path
        self.auto_extract = bool(o.get("auto_extract", True))
        self.recall_limit = int(o.get("recall_limit", 4))
        self.profile_chars = int(o.get("profile_chars", 2500))
        self.keep_days = float(o.get("keep_days", 365))
        self.extractor = extractor  # prompt -> JSON metni; None ise sadece açık "hatırla" istekleri kaydedilir
        # Günlük toparlama ve tanıtım için daha güçlü model (günde iki çağrı). Hızlı model kuralları tutmuyordu:
        # "WhatsApp'ta X'e yazdı"yı kalıcı bilgi yaptı, bütün özetleri sildi. None: extractor kullanılır.
        self.synthesizer: Callable[[str], str] | None = None
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
        from alice.memvec import Embedder

        try:
            embedder = Embedder.from_config(cfg, o)
        except Exception:  # noqa: BLE001 - embedding opsiyonel: yoksa kelime araması
            embedder = None
        mem = cls(alice_home() / "memory.db", o, make_extractor(o, router), embedder)
        mem.synthesizer = make_extractor(o, router, str(o.get("synthesis_role", "brain")))
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
            self.db.execute("DELETE FROM vectors WHERE kind = 'fact' AND ref = ?", (fid,))
            self.db.commit()
            self._bump()
        self.index_soon()
        return fid, new

    def update(self, fid: int, text: str) -> bool:
        text = " ".join(text.split())
        with self._lock:
            cur = self.db.execute("UPDATE facts SET text = ?, terms = ?, updated = ? WHERE id = ?",
                                  (text, " ".join(terms(text)), time.time(), int(fid)))
            self.db.execute("DELETE FROM vectors WHERE kind = 'fact' AND ref = ?", (int(fid),))
            self.db.commit()
            if cur.rowcount:
                self._bump()
        if cur.rowcount:
            self.index_soon()
            self._stale_portrait()
        return bool(cur.rowcount)

    def forget(self, fid: int) -> Fact | None:
        with self._lock:
            row = self.db.execute("SELECT id, kind, text, created, updated, hits FROM facts WHERE id = ?",
                                  (int(fid),)).fetchone()
            if row is None:
                return None
            self.db.execute("DELETE FROM facts WHERE id = ?", (int(fid),))
            self.db.execute("DELETE FROM vectors WHERE kind = 'fact' AND ref = ?", (int(fid),))
            self.db.commit()
            self._bump()
        self._stale_portrait()
        return Fact(*row)

    def _stale_portrait(self) -> None:
        """Silinen/değişen bilgi eski tanıtımda kalmasın: sonraki toparlamaya kadar madde listesine dön."""
        if self._meta("portrait"):
            self._meta("portrait", "")

    def old_role(self) -> dict[str, list[Any]]:
        """Eski rolden kalan izler: bilgiler, özetler ve o dönemin cevapları."""
        with self._lock:
            facts = [Fact(*r) for r in self.db.execute(
                "SELECT id, kind, text, created, updated, hits FROM facts").fetchall() if OLD_ROLE_RX.search(r[2])]
            sums = [Summary(*r) for r in self.db.execute(
                "SELECT id, start, end, channel, text FROM summaries").fetchall() if OLD_ROLE_RX.search(r[4])]
            eps = [Episode(*r) for r in self.db.execute(
                "SELECT id, ts, channel, user, reply FROM episodes").fetchall() if OLD_ROLE_RX.search(r[4])]
        return {"facts": facts, "summaries": sums, "episodes": eps}

    def purge_old_role(self) -> dict[str, int]:
        """Eski rol izlerini sil: bilgiler ve özetler gider, konuşmaların sadece eski cevabı silinir (kullanıcının
        söyledikleri kalır, "geçen hafta ne konuşmuştuk" aramaları bozulmasın)."""
        found = self.old_role()
        with self._lock:
            for f in found["facts"]:
                self.db.execute("DELETE FROM facts WHERE id = ?", (f.id,))
                self.db.execute("DELETE FROM vectors WHERE kind = 'fact' AND ref = ?", (f.id,))
            for s in found["summaries"]:
                self.db.execute("DELETE FROM summaries WHERE id = ?", (s.id,))
                self.db.execute("DELETE FROM vectors WHERE kind = 'summary' AND ref = ?", (s.id,))
            for e in found["episodes"]:
                self.db.execute("UPDATE episodes SET reply = '' WHERE id = ?", (e.id,))
                self.db.execute("DELETE FROM vectors WHERE kind = 'episode' AND ref = ?", (e.id,))  # yeniden dizinlensin
            self.db.commit()
            self._bump()
        self._stale_portrait()
        return {k: len(v) for k, v in found.items()}

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
        if not user or noise_turn(user):
            return None
        with self._lock:
            cur = self.db.execute("INSERT INTO episodes(ts, channel, user, reply, terms) VALUES (?, ?, ?, ?, ?)",
                                  (time.time(), channel, user[:2000], reply[:2000], " ".join(terms(user + " " + reply))))
            self.db.commit()
        self.index_soon()
        return int(cur.lastrowid)

    def add_reply(self, eid: int, reply: str) -> bool:
        """Tura sonradan gelen cevabı ekle. Canlı sesli modda araç çağrılınca model önce susup cevabı araçtan sonra
        ayrı bir turda söylüyor; o cevap eskiden kaydedilmiyordu (kayıtların yarısında cevap boştu)."""
        reply = (reply or "").strip()
        if not reply:
            return False
        with self._lock:
            row = self.db.execute("SELECT user, reply FROM episodes WHERE id = ?", (int(eid),)).fetchone()
            if row is None:
                return False
            full = (row[1] + " " + reply).strip()[:2000]
            self.db.execute("UPDATE episodes SET reply = ?, terms = ? WHERE id = ?",
                            (full, " ".join(terms(row[0] + " " + full)), int(eid)))
            self.db.execute("DELETE FROM vectors WHERE kind = 'episode' AND ref = ?", (int(eid),))
            self.db.commit()
        self.index_soon()
        return True

    def recent(self, n: int = 5) -> list[Episode]:
        with self._lock:
            rows = self.db.execute("SELECT id, ts, channel, user, reply FROM episodes ORDER BY ts DESC LIMIT ?",
                                   (int(n),)).fetchall()
        return [Episode(*r) for r in reversed(rows)]

    # ------------------------------------------------------------------ vektörler
    def index_soon(self) -> None:
        """Vektörü olmayan kayıtları arka planda vektöre çevir (tek iş parçacığı; embedding yoksa hiçbir şey)."""
        if self.embedder is None or not self.embedder.available or self._index_lock.locked():
            return
        threading.Thread(target=self.index_pending, kwargs={"wait": False}, daemon=True,
                         name="alice-memory-index").start()

    def index_pending(self, limit: int = 60, wait: bool = True) -> int:
        """Vektörü olmayanları vektöre çevir; bu çağrının eklediği sayıyı döndürür. wait: arka planda süren bir
        indeksleme varsa bitmesini bekle (sonra kalanları yap). Her parti ayrı kaydedilir: kota yarıda dolarsa
        yapılan iş kaybolmaz; kalanlar kota açılınca kendiliğinden devam eder."""
        if self.embedder is None or not self._index_lock.acquire(timeout=30 if wait else 0):
            return 0
        from alice.memvec import BATCH, pack

        done, left = 0, False
        try:
            with self._lock:
                todo = [("fact", r[0], r[1]) for r in self.db.execute(
                    "SELECT id, text FROM facts WHERE id NOT IN (SELECT ref FROM vectors WHERE kind = 'fact')")]
                todo += [("summary", r[0], r[1]) for r in self.db.execute(
                    "SELECT id, text FROM summaries WHERE id NOT IN (SELECT ref FROM vectors WHERE kind = 'summary')")]
                todo += [("episode", r[0], f"Kullanıcı: {r[1]}\nALICE: {r[2][:400]}") for r in self.db.execute(
                    "SELECT id, user, reply FROM episodes WHERE id NOT IN (SELECT ref FROM vectors WHERE kind = "
                    "'episode') ORDER BY ts DESC LIMIT ?", (limit + 1,))]
            left = len(todo) > limit
            todo = todo[:limit]
            for i in range(0, len(todo), BATCH):
                part = todo[i:i + BATCH]
                try:
                    vecs = self.embedder.embed([t[2][:2000] for t in part])
                except Exception:  # noqa: BLE001 - embedding opsiyonel; kelime araması çalışmaya devam eder
                    left = True
                    break
                with self._lock:
                    # Embedding sürerken güncellenen/silinen bilgi eski metnin vektörüyle kaydedilmesin.
                    facts = dict(self.db.execute("SELECT id, text FROM facts").fetchall())
                    rows = [(k, ref, pack(v)) for (k, ref, text), v in zip(part, vecs)
                            if k != "fact" or facts.get(ref) == text]
                    self.db.executemany("INSERT OR REPLACE INTO vectors(kind, ref, vec) VALUES (?, ?, ?)", rows)
                    self.db.commit()
                done += len(part)
        finally:
            self._index_lock.release()
        if left and isinstance(self.path, Path):
            self._index_later()
        return done

    def _index_later(self) -> None:
        """Birikmiş kayıtlar var (kota doldu ya da çok fazla): kota açılınca devam et."""
        if getattr(self, "_index_timer", None) is not None and self._index_timer.is_alive():
            return
        from alice.memvec import RATE_WAIT

        def run() -> None:
            self._index_timer = None
            self.index_soon()

        self._index_timer = threading.Timer(RATE_WAIT + 5, run)
        self._index_timer.daemon = True
        self._index_timer.start()

    def _semantic(self, query: str, kinds: list[str], limit: int = 12) -> dict[tuple[str, int], float]:
        """(tür, id) -> 0..1 anlam benzerliği puanı (embed_min'in altı yok sayılır). Embedding yoksa boş."""
        if self.embedder is None or not self.embedder.available or not kinds:
            return {}
        with self._lock:
            rows = self.db.execute(f"SELECT kind, ref, vec FROM vectors WHERE kind IN "
                                   f"({','.join('?' * len(kinds))})", kinds).fetchall()
        if not rows:
            return {}
        try:
            from alice.memvec import top

            qvec = self.embedder.query(query)
        except Exception:  # noqa: BLE001
            return {}
        lo = self.embed_min
        return {ref: 0.3 + 0.7 * (cos - lo) / max(1e-6, 1 - lo)
                for ref, cos in top(qvec, [((k, r), v) for k, r, v in rows], k=limit, min_score=lo)}

    # ------------------------------------------------------------------ arama
    def search(self, query: str, limit: int = 5, facts: bool = True, episodes: bool = True,
               min_score: float = 0.3, exclude_after: float | None = None, summaries: bool = True) -> list[Hit]:
        clean = clean_user_text(query)
        q = list(dict.fromkeys(terms(clean)))
        kinds = [k for k, on in (("fact", facts), ("summary", summaries and episodes), ("episode", episodes)) if on]
        sem = self._semantic(clean, kinds) if clean.strip() else {}
        if not q and not sem:
            return []
        with self._lock:
            frows = self.db.execute("SELECT id, kind, text, created, updated, hits, terms FROM facts").fetchall() \
                if facts else []
            srows = self.db.execute("SELECT id, start, end, channel, text, terms FROM summaries "
                                    "ORDER BY end DESC LIMIT 500").fetchall() if summaries and episodes else []
            erows = self.db.execute("SELECT id, ts, channel, user, reply, terms FROM episodes "
                                    "ORDER BY ts DESC LIMIT 3000").fetchall() if episodes else []
        docs = [set(r[-1].split()) for r in frows + srows + erows]
        n = len(docs) or 1
        df = {t: sum(1 for d in docs if t in d) for t in q}
        idf = {t: math.log(1 + n / (1 + df[t])) + 0.1 for t in q}
        total = sum(idf.values()) or 1.0
        now = time.time()

        def kw(d: set[str], need: int = 1) -> float:
            matched = [t for t in q if t in d]
            return sum(idf[t] for t in matched) / total if len(matched) >= need and matched else 0.0

        def mix(a: float, b: float) -> float:  # kelime ve anlam puanı: güçlü olan + diğerinden küçük katkı
            return max(a, b) + 0.1 * min(a, b)

        hits: list[Hit] = []
        for r, d in zip(frows, docs[:len(frows)]):
            score = mix(kw(d), sem.get(("fact", r[0]), 0.0))
            if score >= min_score:
                hits.append(Hit(score + min(r[5], 10) * 0.005, Fact(*r[:6])))
        off = len(frows)
        for r, d in zip(srows, docs[off:off + len(srows)]):
            if exclude_after is not None and r[1] >= exclude_after:
                continue
            score = mix(kw(d, min(2, len(q))), sem.get(("summary", r[0]), 0.0))
            score *= 0.7 + 0.3 * math.exp(-(now - r[2]) / 86400 / 90)
            if score >= min_score + 0.1:
                hits.append(Hit(score, Summary(*r[:5])))
        off += len(srows)
        for r, d in zip(erows, docs[off:]):
            if exclude_after is not None and r[1] >= exclude_after:
                continue
            # Konuşmalarda tek ortak kelime çoğu zaman tesadüf: kısa sorgu dışında en az iki kelime eşleşsin.
            score = mix(kw(d, min(2, len(q))), sem.get(("episode", r[0]), 0.0))
            score *= 0.6 + 0.4 * math.exp(-(now - r[1]) / 86400 / 60)  # eski konuşma biraz geride kalır
            if score >= min_score + 0.15:
                hits.append(Hit(score, Episode(*r[:5])))
        hits.sort(key=lambda h: h.score, reverse=True)
        return hits[:limit]

    # ------------------------------------------------------------------ geçmiş sohbetler (isteyince)
    # Claude'un conversation_search / recent_chats araçları gibi: eski konuşmalar her mesaja kendiliğinden eklenmez
    # (yanlış eşleşen bir konuşma modelin kafasını karıştırıyordu); kullanıcı geçmişe değinince model bunlarla bakar.
    def sessions(self, after: float | None = None, before: float | None = None, limit: int = 400) -> list[list[Episode]]:
        """Zaman aralığındaki konuşmalar, session_gap'ten uzun sessizlikle ayrılmış tur grupları (eskiden yeniye)."""
        sql, args = "SELECT id, ts, channel, user, reply FROM episodes WHERE 1=1", []
        if after is not None:
            sql, args = sql + " AND ts >= ?", args + [after]
        if before is not None:
            sql, args = sql + " AND ts < ?", args + [before]
        with self._lock:
            rows = self.db.execute(sql + " ORDER BY ts DESC LIMIT ?", args + [int(limit)]).fetchall()
        groups: list[list[Episode]] = []
        for r in reversed(rows):
            e = Episode(*r)
            if groups and e.ts - groups[-1][-1].ts <= self.session_gap:
                groups[-1].append(e)
            else:
                groups.append([e])
        return groups

    def _session_text(self, group: list[Episode], turns: int = 4, now: float | None = None) -> str:
        """Bir konuşmanın kısa hâli: özeti varsa özet, ayrıca ilk ve son birkaç tur."""
        start, end = group[0].ts, group[-1].ts
        with self._lock:
            row = self.db.execute("SELECT text FROM summaries WHERE start <= ? AND end >= ? ORDER BY id DESC LIMIT 1",
                                  (end, start)).fetchone()
        head = f"## {datetime.fromtimestamp(start):%d.%m.%Y %H:%M} ({_ago(start, now)}, {len(group)} tur)"
        lines = [head] + ([f"Özet: {row[0]}"] if row else [])

        def turn(e: Episode) -> str:
            return f"- Kullanıcı: {_clip(e.user, 240)}" + (f"\n  ALICE: {_clip(e.reply, 240)}" if e.reply else "")

        if turns <= 0:
            return "\n".join(lines)
        if len(group) <= turns:
            return "\n".join(lines + [turn(e) for e in group])
        first, last = group[: turns // 2], group[len(group) - (turns - turns // 2):]
        return "\n".join(lines + [turn(e) for e in first] + [f"  … {len(group) - turns} tur daha …"]
                         + [turn(e) for e in last])

    def recent_chats(self, n: int = 5, after: float | None = None, before: float | None = None) -> str:
        """Son n konuşma (ya da verilen zaman aralığındakiler), yeniden eskiye."""
        groups = self.sessions(after, before)[-max(1, min(int(n), 20)):]
        now = time.time()
        return "\n\n".join(self._session_text(g, now=now) for g in reversed(groups)) or "bu aralıkta konuşma yok"

    def search_chats(self, query: str, limit: int = 5, after: float | None = None,
                     before: float | None = None) -> str:
        """Konuya göre geçmiş konuşma arama: eşleşen turların bulunduğu konuşmalar (özet + bağlam turları)."""
        hits = self.search(query, limit=30, facts=False, min_score=0.2)
        picked: list[tuple[float, float]] = []  # (puan, tur zamanı)
        for h in hits:
            ts = h.item.ts if isinstance(h.item, Episode) else h.item.start
            if (after is not None and ts < after) or (before is not None and ts >= before):
                continue
            picked.append((h.score, ts))
        if not picked:
            return f"geçmiş konuşmalarda '{query}' ile ilgili bir şey bulunamadı"
        out, seen, now = [], set(), time.time()
        for _, ts in sorted(picked, reverse=True):
            group = next((g for g in self.sessions(ts - 6 * 3600, ts + 6 * 3600)
                          if g[0].ts - 1 <= ts <= g[-1].ts + self.session_gap), None)
            if not group or group[0].id in seen:
                continue
            seen.add(group[0].id)
            # Eşleşen tur görünsün: konuşma uzunsa onun etrafındaki turlar.
            i = min(range(len(group)), key=lambda k: abs(group[k].ts - ts))
            window = group[max(0, i - 2): i + 3]
            text = self._session_text(group, turns=0, now=now).splitlines()
            text += [f"- Kullanıcı: {_clip(e.user, 300)}" + (f"\n  ALICE: {_clip(e.reply, 300)}" if e.reply else "")
                     for e in window]
            out.append("\n".join(text))
            if len(out) >= limit:
                break
        return "\n\n".join(out)

    def recall(self, query: str, exclude_after: float | None = None) -> str:
        """Kullanıcının sözüne eklenecek hafıza notu (boş olabilir): kalıcı bilgiler ve güçlü eşleşen konuşma
        özetleri. Tek tek eski turlar artık eklenmez; onları model search_chats / recent_chats ile kendisi bulur."""
        hits = [h for h in self.search(query, limit=self.recall_limit + 4, exclude_after=exclude_after)
                if isinstance(h.item, Fact) or (isinstance(h.item, Summary) and h.score >= 0.6)][: self.recall_limit]
        if not hits:
            return ""
        ids = [h.item.id for h in hits if isinstance(h.item, Fact)]
        if ids:
            with self._lock:
                self.db.executemany("UPDATE facts SET hits = hits + 1, last_used = ? WHERE id = ?",
                                    [(time.time(), i) for i in ids])
                self.db.commit()
        # Her kullanıcı mesajına eklenir: elle kaydedilmiş dev bir bilgi (yapıştırılmış metin) bağlamı şişirmesin.
        lines = "\n".join(f"- {_clip(h.item.text, RECALL_ITEM_CHARS) if isinstance(h.item, Fact) else h.item.line()}"
                          for h in hits)
        lines = _clip(lines, RECALL_TOTAL_CHARS)
        return ("[hafızadan, bu konuşmayla ilgili olabilir; işine yararsa kullan, hafızadan okuduğunu söyleme:\n"
                f"{lines}]")

    # ------------------------------------------------------------------ özetler ve takipler
    def summaries(self, n: int = 5, within_days: float | None = None) -> list[Summary]:
        sql = "SELECT id, start, end, channel, text FROM summaries"
        args: tuple = ()
        if within_days is not None:
            sql += " WHERE end >= ?"
            args = (time.time() - within_days * 86400,)
        with self._lock:
            rows = self.db.execute(sql + " ORDER BY end DESC LIMIT ?", args + (int(n),)).fetchall()
        return [Summary(*r) for r in reversed(rows)]

    def add_summary(self, start: float, end: float, text: str, channel: str = "") -> int:
        text = " ".join(text.split())
        with self._lock:
            cur = self.db.execute("INSERT INTO summaries(start, end, channel, text, terms) VALUES (?, ?, ?, ?, ?)",
                                  (start, end, channel, text[:1200], " ".join(terms(text))))
            self.db.commit()
            self._bump()
        self.index_soon()
        return int(cur.lastrowid)

    def add_followup(self, text: str, due: float | None = None, kind: str = "ask") -> int:
        text = " ".join(text.split())
        if not text:
            raise ValueError("boş takip")
        kind = kind if kind in FOLLOWUP_KINDS else "ask"
        with self._lock:
            for f in self.followups():  # aynı takip ikinci kez eklenmesin
                if f.kind == kind and set(terms(f.text)) == set(terms(text)):
                    return f.id
            cur = self.db.execute("INSERT INTO followups(created, due, kind, text) VALUES (?, ?, ?, ?)",
                                  (time.time(), due, kind, text[:300]))
            self.db.commit()
            self._bump()
        return int(cur.lastrowid)

    def followups(self, include_done: bool = False) -> list[Followup]:
        sql = "SELECT id, created, due, kind, text, done FROM followups"
        if not include_done:
            sql += " WHERE done = 0"
        with self._lock:
            rows = self.db.execute(sql + " ORDER BY COALESCE(due, created)").fetchall()
        return [Followup(*r) for r in rows]

    def due_followups(self, now: float | None = None) -> list[Followup]:
        """Bugün ya da daha önce günü gelmiş takipler + yarım kalan işler. Çok eskiyenler kendiliğinden düşer
        (sınavdan 4 gün sonra "nasıl geçti" sorulmaz; iki hafta önce yarım kalan iş artık güncel değil)."""
        now = now or time.time()
        end_of_day = datetime.fromtimestamp(now).replace(hour=23, minute=59, second=59).timestamp()
        out, stale = [], []
        for f in self.followups():
            if f.due is not None and f.due > end_of_day:
                continue
            age = now - (f.due if f.due is not None else f.created)
            if age > (14 if f.kind == "open" else 4) * 86400:
                stale.append(f.id)
                continue
            out.append(f)
        if stale:
            with self._lock:
                self.db.executemany("UPDATE followups SET done = 2 WHERE id = ?", [(i,) for i in stale])
                self.db.commit()
        return out

    def done_followup(self, fid: int) -> Followup | None:
        with self._lock:
            row = self.db.execute("SELECT id, created, due, kind, text, done FROM followups WHERE id = ?",
                                  (int(fid),)).fetchone()
            if row is None:
                return None
            self.db.execute("UPDATE followups SET done = 1 WHERE id = ?", (int(fid),))
            self.db.commit()
            self._bump()
        return Followup(*row)

    # ------------------------------------------------------------------ özetleme
    def pending_sessions(self, now: float | None = None, force: bool = False) -> list[list[Episode]]:
        """Henüz özetlenmemiş, bitmiş konuşmalar (aralarında session_gap'ten uzun sessizlik olan tur grupları)."""
        now = now or time.time()
        raw = self._meta("digested_until")
        since = float(raw) if raw else now - 3 * 86400  # ilk açılışta bütün geçmiş değil, son üç gün
        with self._lock:
            rows = self.db.execute("SELECT id, ts, channel, user, reply FROM episodes WHERE ts > ? "
                                   "ORDER BY ts LIMIT 400", (since,)).fetchall()
        groups: list[list[Episode]] = []
        for r in rows:
            e = Episode(*r)
            if groups and e.ts - groups[-1][-1].ts <= self.session_gap:
                groups[-1].append(e)
            else:
                groups.append([e])
        if groups and not force and now - groups[-1][-1].ts < self.session_gap:
            groups.pop()  # konuşma sürüyor
        return groups

    def maybe_digest(self) -> None:
        """Ucuz kontrol (dakikada bir): bitmiş ve özetlenmemiş konuşma varsa arka planda özetle."""
        if not self.digest_enabled or self.extractor is None or self._digest_lock.locked():
            return
        now = time.monotonic()
        if now - self._last_digest_check < 60:
            return
        self._last_digest_check = now
        if float(self._meta("digest_fail_until") or 0) > time.time() or not self.pending_sessions():
            return
        threading.Thread(target=self.digest, daemon=True, name="alice-memory-digest").start()

    def digest(self, force: bool = False, max_sessions: int = 3) -> int:
        """Bitmiş konuşmaları özetle; kalıcı bilgileri ve takipleri çıkar. Özetlenen konuşma sayısını döndürür."""
        if self.extractor is None or not self._digest_lock.acquire(blocking=False):
            return 0
        done = 0
        try:
            for group in self.pending_sessions(force=force)[:max_sessions]:
                if sum(len(e.user) for e in group) >= 40:  # "saat kaç" gibi tek kısa tur özetlenmez
                    try:
                        self._digest_one(group)
                    except Exception:  # noqa: BLE001 - model yok/kota: sonra tekrar denenir
                        self._meta("digest_fail_until", repr(time.time() + 900))
                        break
                    done += 1
                self._meta("digested_until", repr(group[-1].ts))
        finally:
            self._digest_lock.release()
        return done

    def _digest_one(self, group: list[Episode]) -> None:
        lines, used = [], 0
        for e in reversed(group):  # uzun konuşmada son kısım önemli: sondan geriye 6000 karaktere kadar
            line = f"{datetime.fromtimestamp(e.ts):%H:%M} Kullanıcı: {e.user[:600]}\nALICE: {e.reply[:400]}"
            if used + len(line) > 6000:
                break
            lines.append(line)
            used += len(line)
        transcript = "\n".join(reversed(lines))
        related = self.search(" ".join(e.user for e in group)[:1500], limit=10, episodes=False, min_score=0.1)
        known = "\n".join(f"{h.item.id}: {h.item.text}" for h in related if isinstance(h.item, Fact)) or "(yok)"
        open_items = "\n".join(f.line() for f in self.followups()) or "(yok)"
        start = datetime.fromtimestamp(group[0].ts)
        prompt = DIGEST_PROMPT.format(date=f"{start:%Y-%m-%d %A}", known=known, open=open_items,
                                      transcript=transcript)
        data = parse_json(self.extractor(prompt) if self.extractor else "")
        if not data:
            raise ValueError("özet cevabı boş")
        self.apply(data)
        summary = strip_filler(data.get("summary") or "") if isinstance(data.get("summary"), str) else ""
        if summary:
            channels = ", ".join(dict.fromkeys(e.channel for e in group if e.channel))
            self.add_summary(group[0].ts, group[-1].ts, summary, channels)
        for fid in data.get("done") or []:
            try:
                self.done_followup(int(fid))
            except (TypeError, ValueError):
                continue
        for f in (data.get("followups") or [])[:5]:
            if not isinstance(f, dict) or not isinstance(f.get("text"), str):
                continue
            due = _parse_day(f.get("due"))
            kind = str(f.get("kind") or ("open" if due is None else "ask"))
            if kind != "open" and due is None:
                continue
            try:
                self.add_followup(f["text"], due, kind)
            except ValueError:
                continue

    # ------------------------------------------------------------------ istem
    def profile(self, chars: int | None = None, recent: int = 3) -> str:
        """Sistem istemine giren blok: kullanıcıyı tanıtan bilgiler, son günlerin özeti, günü gelen takipler ve son
        birkaç konuşma."""
        chars = chars or self.profile_chars
        portrait = self._meta("portrait") or ""
        since = float(self._meta("portrait_at") or 0) if portrait else 0.0
        with self._lock:
            # Günlük tanıtım varsa o yazıldıktan sonra eklenen/değişen bilgiler madde olarak eklenir; gerisi tanıtımda.
            rows = self.db.execute("SELECT id, kind, text, created, updated, hits FROM facts WHERE updated > ?",
                                   (since,)).fetchall()
        facts = [Fact(*r) for r in rows]
        if portrait:
            chars = max(400, chars - len(portrait))
        facts.sort(key=lambda f: (_KIND_ORDER.index(f.kind) if f.kind in _KIND_ORDER else 9, -f.hits, -f.updated))
        out, used = [], 0
        for f in facts:
            line = f"- ({KINDS.get(f.kind, f.kind)}) {f.text}"
            if used + len(line) > chars:
                continue  # tek uzun bilgi sıradaki kısa bilgileri profilden atmasın
            out.append(line)
            used += len(line) + 1
        eps = self.recent(recent)
        sums = self.summaries(4, within_days=10)
        due = self.due_followups()
        if not portrait and not out and not eps and not sums and not due:
            return ""
        parts = ["# Long-term memory\nWhat you know about the user from earlier conversations (stored locally). Use it "
                 "naturally to personalize answers; don't recite it. Each user message may end with a [hafızadan ...] "
                 "note of related memories. To save something new, change or delete a memory, use the memory tool. "
                 "Older conversations are not shown here: when the user refers to one, look it up with memory "
                 "action=search_chats (by topic) or recent_chats (by time) before answering."]
        if portrait:
            parts.append(portrait)
        if out:
            parts.append(("Newer notes:\n" if portrait else "") + "\n".join(out))
        if sums:
            parts.append("Earlier conversations, summarized (continue from them naturally, e.g. ask how a thing went):\n"
                         + "\n".join(f"- {s.line()}" for s in sums))
        if due:
            parts.append("Follow-ups due now: bring each up ONCE at a natural moment (a greeting or a pause is fine), "
                         "briefly, in your own words; then call memory action=done id=<n>. Unfinished threads: offer "
                         "to continue.\n" + "\n".join(f"- {f.line()}" for f in due))
        if eps:
            parts.append("Recent conversations:\n" + "\n".join(f"- {e.line(width=160)}" for e in eps))
        return "\n".join(parts)

    def export_markdown(self) -> str:
        facts = self.facts()
        eps = self.recent(10)
        if not facts and not eps:
            return "(hafıza boş)"
        lines = ["# Bilgiler"] + [f"* {f.line()}" for f in facts]
        sums = self.summaries(5)
        if sums:
            lines += ["", "# Konuşma özetleri"] + [f"* {s.line()}" for s in sums]
        fups = self.followups()
        if fups:
            lines += ["", "# Takipler"] + [f"* {f.line()}" for f in fups]
        if eps:
            lines += ["", "# Son konuşmalar"] + [f"* {e.line()}" for e in eps]
        return "\n".join(lines)

    # ------------------------------------------------------------------ öğrenme
    def learn(self, user: str, reply: str = "", background: bool = True) -> None:
        """Konuşma turundan kalıcı bilgi çıkar. Açık "hatırla" isteği hemen; diğerleri arka planda modelle."""
        user = clean_user_text(user)
        self.maybe_digest()  # önceki (bitmiş) konuşma henüz özetlenmediyse şimdi arka planda
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
            log.debug("konuşmadan bilgi çıkarılamadı", exc_info=True)
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
            if isinstance(text, str) and 3 <= len(text) <= 300 and not ephemeral(text):
                fid, _ = self.remember(text, (a.get("kind") if isinstance(a, dict) else None) or "fact", "otomatik")
                done.append(f"eklendi #{fid}")
        return done

    # ------------------------------------------------------------------ toparlama
    def maybe_consolidate(self, every_hours: float = 24.0) -> bool:
        """Günde bir: bütün bilgileri baştan gözden geçir (Claude'un günlük hafıza sentezi gibi)."""
        if self.extractor is None or not self.consolidate_enabled:
            return False
        if not self._meta("portrait") and (self.facts(1) or self.summaries(1)):
            every_hours = min(every_hours, 6.0)  # tanıtım hiç yazılamadıysa (model/kota) bir gün bekleme
        if time.time() - float(self._meta("consolidated_at") or 0) < every_hours * 3600:
            return False
        self.consolidate()
        return True

    def consolidate(self) -> list[str]:
        """Bilgileri toparla: tekrarları birleştir, kurala uymayanları (bir turun sonucu, yol, test) sil, eskiyenleri
        güncelle. Kullanıcının kendi "hatırla" dediklerine dokunulmaz. Yapılanları döndürür."""
        if self.extractor is None:
            return []
        self._meta("consolidated_at", repr(time.time()))
        done = self._tidy_facts(self.facts())
        try:
            self.write_portrait()
        except Exception:  # noqa: BLE001 - profil yazılamazsa eski profil (ya da madde listesi) kalır
            log.debug("profil yazılamadı", exc_info=True)
        return done

    def write_portrait(self) -> str:
        """Kullanıcının kısa, başlıklı tanıtımı (Claude'un günlük hafıza sentezi gibi): kalıcı bilgiler + son 30 günün
        konuşma özetlerinden. Sistem istemine madde listesi yerine bu girer."""
        if self.extractor is None:
            return ""
        facts = self.facts()
        sums = self.summaries(30, within_days=30)
        if not facts and not sums:
            return ""
        prompt = PORTRAIT_PROMPT.format(
            facts="\n".join(f"- ({KINDS.get(f.kind, f.kind)}) {f.text}" for f in facts) or "(yok)",
            summaries="\n".join(f"- {s.line()}" for s in sums) or "(yok)")
        text = parse_json(self._synth(prompt)).get("portrait")
        if not isinstance(text, str) or not text.strip():
            return ""
        text = _clip(text.strip(), self.profile_chars)
        self._meta("portrait", text)
        self._meta("portrait_at", repr(time.time()))
        self._bump()
        return text

    def _tidy_facts(self, facts: list[Fact]) -> list[str]:
        """Günlük toparlama. Bilgilerle birlikte son 30 günün konuşma özetleri de gider: tek konuşmadan bilgi
        çıkmıyor (kural gereği), ama birkaç konuşmada tekrar eden şey (oynadığı oyunlar, sevdiği müzik) kalıcıdır;
        Claude'un sohbet geçmişinden günlük sentezi gibi buradan eklenir. Değersiz özetler (ALICE'i denemek, olmayan
        komutlar, yanlış duyulan sesler) de silinir, profilin "son zamanlarda"sını kirletmesin."""
        sums = self.summaries(40, within_days=30)
        if len(facts) < 2 and len(sums) < 2:
            return []
        keep = {f.id for f in facts if self._source(f.id) in ("kullanıcı", "araç", "memory.md")}
        # Kalıcı talimatlar ("oyunu kapatmamasını ister") en değerli bilgiler: model "bir konuşmalık" sanıp
        # siliyordu. Birleştirilip düzeltilebilir ama silinmez.
        keep |= {f.id for f in facts if _INSTRUCTION.search(f.text)}
        now = time.time()
        listing = "\n".join(f"{f.id} [{f.kind}, {_ago(f.created, now)}, used {f.hits}x"
                            f"{', user asked to remember' if f.id in keep else ''}]: {f.text}" for f in facts)
        convs = "\n".join(f"{x.id} [{datetime.fromtimestamp(x.end):%Y-%m-%d}]: {x.text}" for x in sums)
        data = parse_json(self._synth(CONSOLIDATE_PROMPT.format(n=self.max_facts, facts=listing or "(yok)",
                                                                   summaries=convs or "(yok)")))
        done: list[str] = []
        valid = {f.id for f in facts}

        def ids(xs: Any, among: set[int] = valid) -> list[int]:
            out = []
            for x in xs or []:
                try:
                    if int(x) in among:
                        out.append(int(x))
                except (TypeError, ValueError):
                    continue
            return out

        for m in (data.get("merge") or [])[:20]:
            if not isinstance(m, dict) or not isinstance(m.get("text"), str) or len(ids(m.get("ids"))) < 2:
                continue
            group = ids(m.get("ids"))
            self.update(group[0], m["text"])
            done.append(f"birleştirildi #{group[0]} ← " + ", ".join(f"#{i}" for i in group[1:]))
            for i in group[1:]:
                if i not in keep:
                    self.forget(i)
        for u in (data.get("update") or [])[:20]:
            if isinstance(u, dict) and isinstance(u.get("text"), str) and ids([u.get("id")]):
                if self.update(int(u["id"]), u["text"]):
                    done.append(f"güncellendi #{u['id']}")
        # Tek seferde en fazla dörtte biri: zayıf bir model bir günde bütün hafızayı silmesin.
        for i in [i for i in ids(data.get("remove")) if i not in keep][:max(2, len(facts) // 4)]:
            if self.forget(i):
                done.append(f"silindi #{i}")
        for a in (data.get("add") or [])[:3]:
            text = a.get("text") if isinstance(a, dict) else a
            if isinstance(text, str) and 3 <= len(text) <= 300 and not ephemeral(text) and not past_event(text):
                fid, new = self.remember(text, (a.get("kind") if isinstance(a, dict) else None) or "fact", "toparlama")
                if new:
                    done.append(f"eklendi #{fid}")
        # Özetlerin en fazla üçte biri: model bütün özetleri "değersiz" sayıp profili boşaltmıştı.
        for i in ids(data.get("drop_summaries"), {x.id for x in sums})[:max(1, len(sums) // 3)]:
            if self.forget_summary(i):
                done.append(f"özet silindi #{i}")
        return done

    def forget_summary(self, sid: int) -> bool:
        with self._lock:
            cur = self.db.execute("DELETE FROM summaries WHERE id = ?", (int(sid),))
            self.db.execute("DELETE FROM vectors WHERE kind = 'summary' AND ref = ?", (int(sid),))
            self.db.commit()
            if cur.rowcount:
                self._bump()
        if cur.rowcount:
            self._stale_portrait()
        return bool(cur.rowcount)

    def _synth(self, prompt: str) -> str:
        """Günlük toparlama/tanıtım: güçlü model; o yoksa ya da hata verirse hızlı model."""
        if self.synthesizer is not None:
            try:
                out = self.synthesizer(prompt)
                if parse_json(out):
                    return out
            except Exception:  # noqa: BLE001 - beyin zinciri düşükse hızlı modelle dene
                log.debug("güçlü model toparlayamadı", exc_info=True)
        return self.extractor(prompt) if self.extractor else ""

    def _source(self, fid: int) -> str:
        with self._lock:
            row = self.db.execute("SELECT source FROM facts WHERE id = ?", (int(fid),)).fetchone()
        return row[0] if row else ""


# Neyin kalıcı bilgi olduğu: ChatGPT'nin "saved memories"i ve Claude'un hafızası gibi az ve öz. Konuşmanın kendisi
# zaten episodes/summaries'te; buraya sadece aylar sonra da işe yarayacak, kullanıcıya dair şeyler girer.
DURABLE_RULES = """A memory is worth saving ONLY if it will still be true and useful a month from now in an unrelated \
conversation: who the user is (name, city, school/job, age), people in their life, lasting preferences and dislikes, \
habits, long-running projects/goals, and standing instructions for how ALICE should behave ("always ask before \
closing a game"). Test each one: "Would a human assistant write this in their notebook about their boss?"
NEVER save (these belong to the conversation log, which is kept separately):
- what happened in this conversation or a task's result: "wanted to play X on Spotify but it failed", "confirmed the \
value in us.txt", "started the League of Legends installer", "is looking at project Y"
- file paths, folder names, test folders, window titles, one-off settings
- the user testing/trying ALICE's features, or facts about ALICE's own internals ("the reflex engine is called Jev")
- passing moods and one-time worries ("thinks ALICE's voice changed")
- contacts the user messaged once (a person is worth saving only with their relation: "Que Mal is the user's friend")
- guesses: only save what the user actually said or clearly showed.
Prefer one rich memory over several thin ones; if nothing qualifies (the usual case), save nothing."""

EXTRACT_PROMPT = """You maintain the long-term memory of ALICE, a personal assistant. From the exchange below, extract \
durable facts about the user.
""" + DURABLE_RULES.replace("{", "{{").replace("}", "}}") + """
Write each fact as one short Turkish sentence in the third person, starting with "Kullanıcı" when natural \
(e.g. "Kullanıcı kahveyi şekersiz içer."). kind: preference | fact | person | routine | note.
Existing related memories (id: text):
{known}
If the user contradicts or refines an existing memory, put it in "update" (same id, new text) or "remove" instead of \
adding a duplicate. If there is nothing durable, return empty lists.
Answer ONLY with JSON: {{"add": [{{"text": "...", "kind": "..."}}], "update": [{{"id": 1, "text": "..."}}], "remove": [2]}}

User: {user}
ALICE: {reply}"""


DIGEST_PROMPT = """You maintain the long-term memory of ALICE, a personal assistant (like Jarvis) for one user. Below is \
one finished conversation (it started {date}). Produce:
1. "summary": 1-3 short Turkish sentences: what was talked about, what got done or decided, what is left. Concrete \
(names, projects, numbers). Write only what happened: never sentences like "Hiçbir işlem yapılmadı", "karar \
alınmadı", "devam eden iş kalmadı". Empty string if it was only trivial commands, small talk, testing ALICE's \
features, or misheard noise (fragments that make no sense, other languages).
2. "add": durable facts about the user. The summary already records what happened, so facts are rare: \
usually zero, at most two. """ + DURABLE_RULES.replace("{", "{{").replace("}", "}}") + """
One short Turkish sentence each, third person ("Kullanıcı ..."). kind: preference | fact | person | routine | note. \
Use "update"/"remove" with the existing ids below instead of duplicating; also "remove" existing ones that break the \
rules above.
3. "followups": things a good assistant should bring up later. kind "ask": an upcoming event in the user's life to \
ask about afterwards (exam, match, trip, interview) with "due" = the day after it happens; kind "remind": something \
the user asked to be reminded of on a day; kind "open": only when the user said they will continue something later \
(no due). A command that failed or a task ALICE was doing is NOT a follow-up. Turkish, written as an instruction to \
ALICE ("Matematik sınavının nasıl geçtiğini sor."). Usually zero.
4. "done": ids of the open follow-ups below that this conversation already resolved.
Dates are YYYY-MM-DD; resolve "yarın", "cuma" etc. relative to the conversation date.
Existing related memories (id: text):
{known}
Open follow-ups:
{open}
Answer ONLY with JSON: {{"summary": "...", "add": [{{"text": "...", "kind": "..."}}], "update": [{{"id": 1, "text": \
"..."}}], "remove": [], "followups": [{{"text": "...", "kind": "ask", "due": "YYYY-MM-DD"}}], "done": []}}

Conversation:
{transcript}"""


CONSOLIDATE_PROMPT = """You curate the long-term memory of ALICE, a personal assistant, the way a good human assistant \
tidies their notebook about their boss once a day. Below is every saved memory (id [kind, age, how often it was \
useful]: text) and the summaries of the conversations of the last 30 days (id [date]: text).
""" + DURABLE_RULES.replace("{", "{{").replace("}", "}}") + """
Do this:
- "remove": memory ids that break the rules above (results of single conversations, paths, tests, ALICE internals, \
passing moods, stale plans whose time has passed). Never remove ones marked "user asked to remember", and never \
remove standing instructions for ALICE: they are the most valuable memories.
- "merge": groups of memory ids that say the same or overlapping things, with one combined Turkish sentence (first id \
is kept).
- "update": rewrite vague or outdated memories into one clear sentence (same id).
- "add": at most 3 durable things the summaries show in TWO OR MORE different conversations and no memory says yet: \
games they play, music they like, ongoing projects, people around them, recurring interests. Write only what was \
observed, never a guess ("Kullanıcı sık sık Ankara'nın hava durumunu sorar", not "Kullanıcı Ankara'da yaşar").
- "drop_summaries": summary ids that are worthless later because they say NOTHING about the user's life, work or \
interests: only testing ALICE's features, commands that failed or got no answer, misheard noise. Most summaries \
stay; if a summary also mentions something real (a song they played, a game, a person, a plan), keep it.
Aim for at most {n} memories that really describe the user. When unsure, keep it. Turkish, third person ("Kullanıcı ...").
Answer ONLY with JSON: {{"remove": [1], "merge": [{{"ids": [2, 3], "text": "..."}}], "update": [{{"id": 4, "text": \
"..."}}], "add": [{{"text": "...", "kind": "preference"}}], "drop_summaries": [7]}}

Memories:
{facts}

Conversation summaries:
{summaries}"""


PORTRAIT_PROMPT = """Write ALICE's short portrait of the one user she assists, from her saved memories and the summaries \
of the last 30 days of conversations. It goes into her system prompt so she knows who she is talking to.
Turkish, in the third person, short markdown sections, only those that have content, in this order:
**Kim** (name, age, city, school/job), **Projeler ve hedefler**, **Tercihler**, **ALICE'ten istedikleri** (standing \
instructions, keep their exact meaning), **Son zamanlarda** (what is going on in their life these weeks).
Rules:
- Only what the memories or summaries actually say. Never guess a name, age or city (a username, a city whose \
weather they asked about, a language they spoke are not evidence). Leave a section out rather than guess.
- Every item appears once, in the one section where it fits best. "ALICE'ten istedikleri" is only for instructions \
about how ALICE should behave (not in "Tercihler" too).
- Never write that something is unknown; leave the section out.
- "Son zamanlarda" is about the user's life and work (projects they worked on, events, things they care about), \
2-4 bullets about themes, not individual moves or clicks ("Unciv'de yeni bir oyuna başladı", not which unit it \
built). Never list testing ALICE, file names/paths, single commands, messages sent, failed or unanswered requests, \
or ALICE's own behaviour.
Bullet points, concrete, no filler; at most ~900 characters.
Answer ONLY with JSON: {{"portrait": "..."}}

Saved memories:
{facts}

Conversation summaries:
{summaries}"""


# Özet modelinin her özete eklediği boş cümleler ("Hiçbir işlem tamamlanmadı veya karar alınmadı."): aramayı ve
# profili dolduruyor, bir şey söylemiyordu.
_FILLER_SENTENCE = re.compile(
    r"[^.!?;]*\b(?:hiçbir|herhangi bir) (?:işlem|karar|iş|eylem|şey)\w*[^.!?;]*?(?:yapılmadı|alınmadı|gerçekleş\w*|"
    r"tamamlanmadı|kalmadı|yok)\w*[^.!?;]*[.!?;]?|[^.!?;]*\bdevam eden (?:bir )?(?:iş|konu) (?:kalmadı|yok)[^.!?;]*[.!?;]?|"
    r"[^.!?;]*\b(?:henüz )?(?:yanıt|cevap) (?:vermedi|alınmadı|verilmedi)[^.!?;]*[.!?;]?", re.I)


def strip_filler(text: str) -> str:
    out = re.sub(r"\s{2,}", " ", _FILLER_SENTENCE.sub("", text)).strip(" ;,")
    return out + "." if out and out[-1] not in ".!?" else out


def _parse_day(value: Any) -> float | None:
    """'2026-10-23' -> o günün sabahı 09:00 (yerel saat); geçersizse None."""
    if not isinstance(value, str):
        return None
    try:
        return datetime.strptime(value.strip()[:10], "%Y-%m-%d").replace(hour=9).timestamp()
    except ValueError:
        return None


def _day_start(value: Any) -> float | None:
    """'2026-10-23' -> o günün başı (00:00, yerel saat); boş ya da geçersizse None."""
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return datetime.strptime(value.strip()[:10], "%Y-%m-%d").timestamp()
    except ValueError:
        return None


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


def make_extractor(options: dict[str, Any], router, role: str | None = None) -> Callable[[str], str] | None:
    """Bilgi çıkarımı için mevcut ücretsiz model zincirini (varsayılan "fast" rolü) kullanan fonksiyon."""
    if router is None or not options.get("auto_extract", True):
        return None
    role = role or str(options.get("extract_role", "fast"))

    def extract(prompt: str) -> str:
        msgs = [{"role": "user", "content": prompt}]
        try:
            res = router.chat(role, msgs, temperature=0, max_tokens=1600, response_format={"type": "json_object"})
        except Exception:  # noqa: BLE001 - bazı modeller response_format'ı reddediyor
            res = router.chat(role, msgs, temperature=0, max_tokens=1600)
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
            o = dict((cfg.data.get("memory") if cfg is not None else None) or {})
            mem.extractor = make_extractor(o, router)
            mem.synthesizer = make_extractor(o, router, str(o.get("synthesis_role", "brain")))
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
        mem.maybe_digest()  # dünkü konuşma hâlâ özetlenmediyse arka planda
        mem.index_soon()
    return mem


def profile_text(home: Path | None = None) -> str:
    """Sistem istemi için profil (hafıza dosyası yoksa boş; dosya yaratmaz)."""
    home = home or alice_home()
    if not (home / "memory.db").is_file() and not (home / "memory.md").is_file():
        return ""
    mem = shared_memory()
    return mem.profile() if mem is not None else ""
