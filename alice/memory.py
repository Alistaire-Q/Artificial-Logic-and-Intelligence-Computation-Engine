"""Uzun süreli hafıza: kalıcı notlar, geçmiş konuşmalar, özetler ve takipler.

İki katman var:
- Notlar (alice/memdir.py): kullanıcıya ve işlerine dair kalıcı bilgi, her biri ayrı bir markdown dosyası
  (~/.alice/memory/*.md, projeye özgü olanlar ~/.alice/projects/<depo>/memory/). Türleri user / feedback / project /
  reference; MEMORY.md dizini sistem istemine girer, ilgili notların tamamı her mesaja eklenir. Arka planda model
  konuşmadan not çıkarır; Dream (günde bir, yeterince konuşma birikince) notları toparlar.
- Konuşma kaydı (~/.alice/memory.db, SQLite): her tur (episodes), bitmiş konuşmaların özetleri (summaries), ileride
  sorulacak/hatırlatılacak takipler (followups). Eski tek cümlelik "facts" tablosu yalnız ilk açılışta notlara
  taşınmak için okunur.

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
import tempfile
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
CREATE TABLE IF NOT EXISTS forgotten (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, text TEXT NOT NULL, terms TEXT NOT NULL DEFAULT '');
"""
KEEP_FORGOTTEN = 200  # silinen notların izi (otomatik çıkarım ve Dream aynı şeyi geri yazmasın)
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
_RECALL = re.compile(r"\s*\[hafızadan.*\]\s*$", re.S)  # not gövdelerinde ']' olabilir: sona kadar

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


# Kullanıcının ALICE'in çalışma biçimini düzelttiği ya da beğendiği anlar (feedback notları buradan doğar). Sadece
# düzeltmeler kaydedilirse ALICE işe yaradığı onaylanmış yollardan da uzaklaşır; onaylar da sinyal.
_FEEDBACK = re.compile(
    r"\b(hayır|hayir|öyle değil|oyle degil|yanlış|yanlis|yapma|yapmasana|bırak|birak|olmamış|olmamis|olmadı|olmadi|"
    r"bir daha|asla|her zaman|hep böyle|bundan sonra|aynen|tam olarak|mükemmel|mukemmel|harika|süper|super|"
    r"güzel olmuş|guzel olmus|eline sağlık|eline saglik|böyle devam|boyle devam|doğru yaptın|dogru yaptin|"
    r"beğendim|begendim|istemiyorum|isterim|tercih|don'?t|stop|exactly|perfect|keep doing)\b", re.I)

RECALL_ITEM_CHARS, RECALL_TOTAL_CHARS = 1800, 6000
RECENT_TURNS_HOURS = 3  # sistem istemine giren "son turlar" bundan eskiyse girmez (yeni sohbet devam sanılmasın)


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


def has_feedback(text: str) -> bool:
    return bool(_FEEDBACK.search(clean_user_text(text)))


_KIND_TYPE = {"preference": "user", "fact": "user", "person": "user", "routine": "user", "note": "user"}


def kind_to_type(kind: str, text: str) -> str:
    """Eski tür adlarını (tercih, bilgi, kişi...) not türüne çevir; ALICE'e verilen kalıcı talimat feedback'tir."""
    if kind in ("user", "feedback", "project", "reference"):
        return kind
    return "feedback" if _INSTRUCTION.search(text) else _KIND_TYPE.get(kind, "user")


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
                 extractor: Callable[[str], str] | None = None, embedder: Any = None,
                 notes_root: Path | str | None = None):
        o = options or {}
        from alice.memdir import NoteBook

        if notes_root is None:
            # ":memory:" (testler, geçici kullanım): notlar da geçici bir klasörde, kimseninkine karışmaz.
            notes_root = (Path(path).parent / "memory" if str(path) != ":memory:"
                          else Path(tempfile.mkdtemp(prefix="notes-", dir=_scratch_dir())))
        self.notes = NoteBook(notes_root)
        self.recall_model = bool(o.get("recall_model", True))  # ilgili notları hızlı modelle seç
        self.extract_every = max(1, int(o.get("extract_every", 4)))  # sinyal yoksa da her N turda bir bak
        self.dream_enabled = bool(o.get("dream", o.get("consolidate", True)))
        self.dream_hours = float(o.get("dream_hours", 24))
        self.dream_sessions = int(o.get("dream_sessions", 5))
        self._turns_since_extract = 0
        self._last_dream_scan: float | None = None  # monotonic; None = hiç taranmadı (açılıştan hemen sonra monotonic < 600 olabilir)
        self._dream_lock = threading.Lock()
        self._in_profile: dict[str, set[str]] = {}  # cwd -> sistem isteminde tamamı duran notlar (anahtar@zaman)
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
        cols = {r[1] for r in self.db.execute("PRAGMA table_info(episodes)")}
        if "sid" not in cols:  # tur hangi sohbetten: sohbet silinince hafızadaki turları da silinebilsin
            self.db.execute("ALTER TABLE episodes ADD COLUMN sid TEXT NOT NULL DEFAULT ''")
        self.db.execute("CREATE INDEX IF NOT EXISTS ix_episodes_sid ON episodes(sid)")
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
        mem = cls(alice_home() / "memory.db", o, make_extractor(o, router), embedder,
                  notes_root=o.get("dir") or alice_home() / "memory")
        mem.synthesizer = make_extractor(o, router, str(o.get("synthesis_role", "brain")))
        mem.import_markdown(alice_home() / "memory.md")
        try:
            mem.migrate_facts()
        except Exception:  # noqa: BLE001 - taşıma bir sonraki açılışta yeniden denenir
            log.debug("eski bilgiler notlara taşınamadı", exc_info=True)
        return mem

    # ------------------------------------------------------------------ notlar
    def books(self, cwd: Path | str | None = None) -> list[Any]:
        """Genel defter + cwd bir git deposundaysa o projenin defteri."""
        from alice.memdir import books_for

        return books_for(self.notes, cwd)

    def all_books(self) -> list[Any]:
        """Genel defter + notu ya da günlüğü olan bütün proje defterleri (Dream ve Hafıza sekmesi için)."""
        from alice.memdir import NoteBook

        out = [self.notes]
        base = self.notes.root.parent / "projects"
        if base.is_dir():
            for d in sorted(base.iterdir()):
                m = d / "memory"
                if m.is_dir() and (any(m.glob("*.md")) or (m / "logs").is_dir()):
                    out.append(NoteBook(m, scope="proje"))
        return out

    def book(self, scope: str = "genel", cwd: Path | str | None = None) -> Any:
        if scope in ("proje", "project"):
            books = self.books(cwd)
            if len(books) < 2:
                raise ValueError("proje kapsamı için çalışma klasörü bir git deposu olmalı; genel kapsamı kullan")
            return books[1]
        return self.notes

    def all_notes(self, cwd: Path | str | None = None) -> list[Any]:
        return [n for b in self.books(cwd) for n in b.scan()]

    def notes_stamp(self, cwd: Path | str | None = None) -> tuple[float, ...]:
        return tuple(b.stamp() for b in self.books(cwd))

    def find_note(self, key: str, cwd: Path | str | None = None) -> tuple[Any, Any] | tuple[None, None]:
        """'genel:ad', 'proje:ad' ya da yalnız 'ad' -> (defter, not)."""
        key = str(key or "").strip()
        scope = key.split(":", 1)[0] if ":" in key else ""
        books = self.books(cwd) if cwd else []
        roots = {b.root for b in books}
        books += [b for b in self.all_books() if b.root not in roots]
        for b in books:
            if scope and b.scope != scope:
                continue
            n = b.get(key)
            if n is not None:
                return b, n
        return None, None

    def _similar_note(self, text: str, book: Any) -> Any:
        mine = set(terms(text))
        if not mine:
            return None
        best, best_score = None, 0.0
        for n in book.scan():
            other = set(terms(n.description)) | set(terms(n.body[:300]))
            if not other:
                continue
            j = len(mine & other) / len(mine | other)
            if j > best_score:
                best, best_score = n, j
        return best if best_score >= 0.75 else None

    def save_note(self, name: str, description: str, type: str, content: str, title: str = "", source: str = "",
                  scope: str = "genel", cwd: Path | str | None = None) -> tuple[Any, bool]:
        b = self.book(scope, cwd)
        note, new = b.save(name, description, type, content, title=title, source=source)
        if source in ("kullanıcı", "araç"):  # konuşmada açıkça yeniden kaydedildi
            self.unforget(f"{title} {description} {content}")
        self._bump()
        return note, new

    def delete_note(self, key: str, cwd: Path | str | None = None) -> Any:
        """Notu sil (kullanıcı arayüzden, sesle ya da ALICE kullanıcının isteğiyle). Silinen notun izi tutulur: konuşma
        özetleri durduğu için tur sonu çıkarımı ve Dream aynı bilgiyi yeniden not yapıyor, silinen not geri
        geliyordu. Dream'in birleştirme silmeleri buradan geçmez (iz bırakmaz)."""
        b, n = self.find_note(key, cwd)
        if b is None:
            return None
        gone = b.delete(n.name)
        if gone is not None:
            self._tombstone(f"{n.title}. {n.description}. {_clip(n.body, 400)}")
        self._bump()
        return gone

    def _tombstone(self, text: str) -> None:
        text = " ".join(text.split())
        with self._lock:
            self.db.execute("INSERT INTO forgotten(ts, text, terms) VALUES (?, ?, ?)",
                            (time.time(), text[:800], " ".join(sorted(set(terms(text))))))
            self.db.execute("DELETE FROM forgotten WHERE id NOT IN (SELECT id FROM forgotten ORDER BY id DESC "
                            "LIMIT ?)", (KEEP_FORGOTTEN,))
            self.db.commit()

    def forgotten(self, limit: int = 20) -> list[str]:
        with self._lock:
            return [r[0] for r in self.db.execute("SELECT text FROM forgotten ORDER BY id DESC LIMIT ?", (int(limit),))]

    def _forgotten_like(self, text: str) -> list[int]:
        """Metinle aynı şeyi söyleyen silinmiş notların kimlikleri (kelime örtüşmesi: kısa olanın en az %60'ı)."""
        mine = set(terms(text))
        if len(mine) < 2:
            return []
        out = []
        with self._lock:
            rows = self.db.execute("SELECT id, terms FROM forgotten").fetchall()
        for fid, t in rows:
            other = set(t.split())
            if len(other) >= 2 and len(mine & other) / min(len(mine), len(other)) >= 0.6:
                out.append(fid)
        return out

    def unforget(self, text: str) -> int:
        """Kullanıcı aynı şeyi yeniden açıkça kaydettirdi: izi kaldır."""
        ids = self._forgotten_like(text)
        if ids:
            with self._lock:
                self.db.execute(f"DELETE FROM forgotten WHERE id IN ({','.join('?' * len(ids))})", ids)
                self.db.commit()
        return len(ids)

    def migrate_facts(self) -> int:
        """Eski tek cümlelik bilgileri (facts tablosu) bir kez notlara taşı. Tablo silinmez (yedek)."""
        if self._meta("notes_migrated"):
            return 0
        n = 0
        for f in reversed(self.facts()):
            src = self._source(f.id)
            try:
                self.remember(f.text, f.kind, source="kullanıcı" if src in ("kullanıcı", "araç", "memory.md") else src,
                              when=f.updated)
                n += 1
            except Exception:  # noqa: BLE001 - dizin doluysa kalanı sonra
                log.debug("bilgi nota taşınamadı: %s", f.text, exc_info=True)
        self._meta("notes_migrated", datetime.now().isoformat(timespec="seconds"))
        return n

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

    def remember(self, text: str, kind: str = "fact", source: str = "", cwd: Path | str | None = None,
                 scope: str = "genel", when: float | None = None) -> tuple[str, bool]:
        """Tek cümlelik bilgiyi not olarak kaydet (eski arayüz: 'bunu hatırla', eski bilgilerin taşınması).
        Neredeyse aynısını söyleyen not varsa onun yerine geçer. (not adı, yeni_mi) döner."""
        from alice.memdir import slugify

        text = " ".join(str(text or "").split())
        if not text:
            raise ValueError("boş bilgi")
        typ = kind_to_type(kind, text)
        book = self.book(scope, cwd)
        old = self._similar_note(text, book)
        if old is not None:
            name, title = old.name, old.title
        else:
            words = re.sub(r"^(kullanıcı(nın|ya|yı)?|kullanici)\s+", "", text, flags=re.I)
            name = slugify(f"{typ}-{' '.join(words.split()[:5])}")
            base, i = name, 2
            while book.get(name) is not None:
                name, i = f"{base}-{i}", i + 1
            title = _clip(words.rstrip("."), 60)
        note, new = book.save(name, text, typ, text, title=title,
                              source=source if old is None else (old.source or source), when=when)
        if source in ("kullanıcı", "araç"):  # konuşmada açıkça yeniden kaydedildi
            self.unforget(text)
        self._bump()
        return note.name, new

    def update(self, key: str | int, text: str, cwd: Path | str | None = None) -> bool:
        """Notun açıklamasını ve gövdesini tek cümleyle değiştir (eski arayüz)."""
        text = " ".join(str(text or "").split())
        b, n = self.find_note(str(key), cwd)
        if n is None or not text:
            return False
        b.save(n.name, text, n.type, text, title=n.title, source=n.source)
        self._bump()
        return True

    def forget(self, key: str | int, cwd: Path | str | None = None) -> Any:
        """Notu sil (çöpe taşınır). Silinen not ya da None."""
        return self.delete_note(str(key), cwd)

    def old_role(self) -> dict[str, list[Any]]:
        """Eski rolden kalan izler: notlar, özetler ve o dönemin cevapları."""
        notes = [n for b in self.all_books() for n in b.scan()
                 if OLD_ROLE_RX.search(f"{n.description}\n{n.body}")]
        with self._lock:
            sums = [Summary(*r) for r in self.db.execute(
                "SELECT id, start, end, channel, text FROM summaries").fetchall() if OLD_ROLE_RX.search(r[4])]
            eps = [Episode(*r) for r in self.db.execute(
                "SELECT id, ts, channel, user, reply FROM episodes").fetchall() if OLD_ROLE_RX.search(r[4])]
        return {"facts": notes, "summaries": sums, "episodes": eps}

    def purge_old_role(self) -> dict[str, int]:
        """Eski rol izlerini sil: notlar (çöpe) ve özetler gider, konuşmaların sadece eski cevabı silinir
        (kullanıcının söyledikleri kalır, "geçen hafta ne konuşmuştuk" aramaları bozulmasın)."""
        found = self.old_role()
        for n in found["facts"]:
            self.delete_note(n.key)
        with self._lock:
            for s in found["summaries"]:
                self.db.execute("DELETE FROM summaries WHERE id = ?", (s.id,))
                self.db.execute("DELETE FROM vectors WHERE kind = 'summary' AND ref = ?", (s.id,))
            for e in found["episodes"]:
                self.db.execute("UPDATE episodes SET reply = '' WHERE id = ?", (e.id,))
                self.db.execute("DELETE FROM vectors WHERE kind = 'episode' AND ref = ?", (e.id,))  # yeniden dizinlensin
            self.db.commit()
            self._bump()
        return {k: len(v) for k, v in found.items()}

    def search_notes(self, query: str, cwd: Path | str | None = None, limit: int = 8) -> list[Any]:
        """Notlarda kelime araması (başlık, açıklama ve gövde)."""
        from alice.memdir import keyword_relevant

        notes = self.all_notes(cwd) if cwd else [n for b in self.all_books() for n in b.scan()]
        hits = keyword_relevant(query, notes, limit=limit, min_score=0.25)
        q = fold(query).strip()
        if q and len(q) >= 3:  # birebir geçen ifade (ad, sayı, kısaltma) kelime köküne takılmasın
            for n in notes:
                if n not in hits and q in fold(f"{n.name} {n.description} {n.body}"):
                    hits.append(n)
        return hits[:limit]

    def forget_matching(self, query: str, cwd: Path | str | None = None) -> tuple[list[Any], list[Any]]:
        """Sorguya açıkça uyan tek notu sil. Emin olunamazsa hiçbirini silmez, adayları döndürür."""
        hits = self.search_notes(query, cwd, limit=5)
        if len(hits) == 1:
            gone = self.delete_note(hits[0].key, cwd)
            return ([gone] if gone else []), hits
        return [], hits

    # ------------------------------------------------------------------ konuşmalar
    def record(self, user: str, reply: str, channel: str = "", sid: str = "") -> int | None:
        user = clean_user_text(user)
        reply = (reply or "").strip()
        if not user or noise_turn(user):
            return None
        with self._lock:
            cur = self.db.execute("INSERT INTO episodes(ts, channel, user, reply, terms, sid) VALUES (?, ?, ?, ?, ?, ?)",
                                  (time.time(), channel, user[:2000], reply[:2000], " ".join(terms(user + " " + reply)),
                                   str(sid or "")))
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
        frows: list[Any] = []  # eski "facts" tablosu notlara taşındı; notlarda arama: search_notes
        with self._lock:
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

    def recall(self, query: str, exclude_after: float | None = None, cwd: Path | str | None = None,
               surfaced: set[str] | None = None, use_model: bool | None = None,
               recent_tools: tuple[str, ...] = ()) -> str:
        """Kullanıcının sözüne eklenecek hafıza notu (boş olabilir): bu mesaja gerçekten yarayacak notların tamamı
        (en fazla 5; bu sohbette zaten gösterilenler tekrar gösterilmez) ve güçlü eşleşen konuşma özetleri. Tek tek
        eski turlar eklenmez; onları model search_chats / recent_chats ile kendisi bulur."""
        from alice.memdir import select_relevant

        clean = clean_user_text(query)
        if not clean.strip():
            return ""
        seen = surfaced if surfaced is not None else set()
        stamp = lambda n: f"{n.key}@{int(n.modified)}"  # noqa: E731 - not değişince yeniden gösterilsin
        in_profile = self._in_profile.get(str(cwd or ""), set())
        notes = [n for n in self.all_notes(cwd) if stamp(n) not in seen and stamp(n) not in in_profile]
        use = self.recall_model if use_model is None else use_model
        chooser = self.extractor if use and self.extractor is not None else None
        if chooser is not None and notes and not _worth_asking(clean, notes):
            # "selam naber", "hava nasıl" gibi kısa mesajlar: hiçbir notla kelime ortaklığı yoksa model çağrısı (her
            # mesajın başında ~1 sn bekleme) boşuna; zaten hiçbir not seçilmiyordu.
            chooser = None
        picked = select_relevant(clean, notes, chooser, limit=5, recent_tools=recent_tools) if notes else []
        sums = [h for h in self.search(clean, limit=4, facts=False, exclude_after=exclude_after)
                if isinstance(h.item, Summary) and h.score >= 0.6][:2]
        if not picked and not sums:
            return ""
        for n in picked:
            seen.add(stamp(n))
        now = time.time()
        parts = [n.render(now, RECALL_ITEM_CHARS) for n in picked]
        if sums:
            parts.append("Geçmiş konuşmalardan:\n" + "\n".join(f"- {h.item.line(now)}" for h in sums))
        body = _clip("\n\n".join(parts), RECALL_TOTAL_CHARS)
        return ("[hafızadan — bu mesajla ilgili olabilecek notların; işine yararsa kullan, hafızadan okuduğunu "
                f"söyleme:\n{body}]")

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
        open_items = "\n".join(f.line() for f in self.followups()) or "(yok)"
        start = datetime.fromtimestamp(group[0].ts)
        prompt = DIGEST_PROMPT.format(date=f"{start:%Y-%m-%d %A}", open=open_items, transcript=transcript)
        data = parse_json(self.extractor(prompt) if self.extractor else "")
        if not data:
            raise ValueError("özet cevabı boş")
        # Kalıcı notları özet değil, tur sonu çıkarımı ve Dream yazar (Dream bu özetleri okuyup damıtır).
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
    def profile(self, chars: int | None = None, recent: int = 3, cwd: Path | str | None = None) -> str:
        """Sistem istemine giren blok: hafıza kuralları, MEMORY.md dizin(ler)i, her zaman geçerli kurallar
        (feedback notları) ve kullanıcıyı tanıtan notlar, son günlerin özeti, günü gelen takipler, son konuşmalar."""
        from alice.memdir import MEMORY_RULES, TYPE_TR

        chars = chars or self.profile_chars
        books = self.books(cwd)
        parts = ["# Long-term memory", MEMORY_RULES]
        for b in books:
            b.ensure()
            where = "Genel hafıza" if b.scope == "genel" else f"Bu projenin hafızası ({project_label(cwd)})"
            idx = b.index_for_prompt()
            parts.append(f"## {where}: MEMORY.md (`{b.root}`)\n"
                         + (idx or "(boş: kaydettiğin notlar burada listelenir)"))
            if b.near_limit():
                parts.append(f"(Not: {b.scope} dizini sınıra yaklaştı; benzer notları birleştir ya da memory "
                             "action=dream ile toparla.)")
        # Davranış kuralları ve kullanıcıyı tanıtan notlar kısa olur ve her konuşmada gerekir: tamamı istemde. Diğer
        # notlar dizinden okunur ya da ilgili olduklarında mesaja kendiliğinden eklenir.
        always, used = [], 0
        shown: set[str] = set()  # tamamı istemde olan notlar: hatırlama bunları mesaja bir daha eklemesin
        for typ in ("feedback", "user"):
            for n in sorted((n for b in books for n in b.scan() if n.type == typ), key=lambda n: -n.modified):
                if " ".join(n.body.split()) == " ".join(n.description.split()):
                    continue  # tek cümlelik not: dizin satırında zaten tamamı var
                block = n.render(limit=700)
                if used + len(block) > chars:
                    continue
                always.append(block)
                used += len(block)
                shown.add(f"{n.key}@{int(n.modified)}")
        self._in_profile[str(cwd or "")] = shown
        if always:
            parts.append(f"## Notların tamamı: {TYPE_TR['feedback']} ve {TYPE_TR['user']} (her konuşmada geçerli)\n"
                         + "\n\n".join(always))
        # Son turlar yalnız yakın zamandansa girer: dünkü sohbetin son cümleleri yeni açılan sohbete "kaldığımız yer"
        # gibi giriyor, ALICE kullanıcının yeni (alakasız) mesajını dünkü konuşmanın devamı sanıyordu.
        cutoff = time.time() - RECENT_TURNS_HOURS * 3600
        eps = [e for e in self.recent(recent) if e.ts >= cutoff]
        sums = self.summaries(4, within_days=10)
        due = self.due_followups()
        parts.append("This chat is a NEW, separate conversation. The past conversations below are background only: do "
                     "not assume the user is continuing one of them or reacting to something said there, unless they "
                     "say so or it is obvious from their message. Older conversations are not shown here: when the "
                     "user refers to one, look it up with memory action=search_chats (by topic) or recent_chats (by "
                     "time) before answering.")
        if sums:
            parts.append("Earlier conversations (separate chats), summarized; bring one up briefly only when it fits, "
                         "e.g. ask how a thing went, and never treat them as this chat's history:\n"
                         + "\n".join(f"- {s.line()}" for s in sums))
        if due:
            parts.append("Follow-ups due now: bring each up ONCE at a natural moment (a greeting or a pause is fine), "
                         "briefly, in your own words; then call memory action=done id=<n>. Unfinished threads: offer "
                         "to continue.\n" + "\n".join(f"- {f.line()}" for f in due))
        if eps:
            parts.append("Last turns of earlier chats from the past few hours (other chats or voice, not this "
                         "conversation):\n" + "\n".join(f"- {e.line(width=160)}" for e in eps))
        return "\n\n".join(parts)

    def export_markdown(self, cwd: Path | str | None = None) -> str:
        books = self.books(cwd) if cwd else self.all_books()
        lines: list[str] = []
        for b in books:
            notes = b.scan()
            if not notes:
                continue
            lines += [f"# Notlar ({b.scope}: {b.root})"]
            lines += [f"* {n.manifest_line()[2:]}" for n in notes]
        eps = self.recent(10)
        sums = self.summaries(5)
        if sums:
            lines += ["", "# Konuşma özetleri"] + [f"* {s.line()}" for s in sums]
        fups = self.followups()
        if fups:
            lines += ["", "# Takipler"] + [f"* {f.line()}" for f in fups]
        if eps:
            lines += ["", "# Son konuşmalar"] + [f"* {e.line()}" for e in eps]
        return "\n".join(lines).strip() or "(hafıza boş)"

    # ------------------------------------------------------------------ öğrenme
    def learn(self, user: str, reply: str = "", background: bool = True, cwd: Path | str | None = None,
              wrote: bool = False) -> None:
        """Konuşma turundan kalıcı not çıkar. Açık "hatırla" isteği hemen kaydedilir; diğerleri arka planda modelle.
        wrote: ALICE bu turda hafızaya kendisi yazdı; o zaman arka plandaki çıkarım aynı şeyi bir daha yazmasın."""
        user = clean_user_text(user)
        self.maybe_digest()  # önceki (bitmiş) konuşma henüz özetlenmediyse şimdi arka planda
        if wrote:
            self._turns_since_extract = 0
            self._advance_cursor()
            return
        note = explicit_note(user)
        if note:
            try:
                self.remember(note, "note", source="kullanıcı", cwd=cwd)
            except Exception:  # noqa: BLE001 - dizin dolu: model memory aracıyla toparlayıp kaydeder
                log.debug("açık not kaydedilemedi", exc_info=True)
            self._advance_cursor()
            return
        if self.extractor is None or not self.auto_extract:
            return
        self._turns_since_extract += 1
        if not (has_signal(user) or has_feedback(user) or self._turns_since_extract >= self.extract_every):
            return
        self._turns_since_extract = 0
        if background:
            threading.Thread(target=self._extract, args=(cwd,), daemon=True, name="alice-memory").start()
        else:
            self._extract(cwd)

    def _advance_cursor(self) -> None:
        """Buraya kadarki turlar not için incelendi (ya da ALICE kendisi yazdı): çıkarım bir daha bakmasın."""
        with self._lock:
            row = self.db.execute("SELECT MAX(id) FROM episodes").fetchone()
        self._meta("notes_cursor", str(int(row[0] or 0)))

    def _recent_turns(self, n: int = 8) -> list[Episode]:
        """İmleçten sonraki (en fazla son 3 saatin) turları, eskiden yeniye."""
        after = int(float(self._meta("notes_cursor") or 0))
        with self._lock:
            rows = self.db.execute("SELECT id, ts, channel, user, reply FROM episodes WHERE id > ? AND ts > ? "
                                   "ORDER BY id DESC LIMIT ?", (after, time.time() - 3 * 3600, int(n))).fetchall()
        return [Episode(*r) for r in reversed(rows)]

    def _extract(self, cwd: Path | str | None = None) -> list[str]:
        """Son turlara bakıp kalıcı not yaz/güncelle/sil (yalnız hafıza notlarına dokunur)."""
        from alice.memdir import EXTRACT_PROMPT, MEMORY_RULES, keyword_relevant, project_root

        if not self._learn_lock.acquire(timeout=60):
            return []
        try:
            turns = self._recent_turns()
            if not turns:
                return []
            transcript = "\n".join(f"Kullanıcı: {_clip(e.user, 1200)}\nALICE: {_clip(e.reply, 700)}" for e in turns)
            notes = self.all_notes(cwd)
            related = keyword_relevant(" ".join(e.user for e in turns), notes, limit=4, min_score=0.2)
            root = project_root(cwd) if cwd else None
            prompt = EXTRACT_PROMPT.format(
                rules=MEMORY_RULES, manifest="\n".join(n.manifest_line() for n in notes) or "(yok)",
                related="\n\n".join(f"--- {n.key}\n{n.body}" for n in related) or "(yok)", explicit="",
                repo=f" ({root.name})" if root else " (şu an bir depoda değilsin: hep genel)", transcript=transcript)
            gone = self.forgotten(15)
            if gone:
                prompt += ("\n\nThe user DELETED these notes; never save them (or anything saying the same) again:\n"
                           + "\n".join(f"- {_clip(g, 200)}" for g in gone))
            data = parse_json(self.extractor(prompt) if self.extractor else "")
            done = self.apply_notes(data, cwd=cwd, source="otomatik", max_save=3, max_delete=2)
            self._meta("notes_cursor", str(turns[-1].id))
            if done:
                log.info("hafıza: %s", ", ".join(done))
            return done
        except Exception:  # noqa: BLE001 - öğrenme hatası konuşmayı etkilemesin
            log.debug("konuşmadan not çıkarılamadı", exc_info=True)
            return []
        finally:
            self._learn_lock.release()

    def apply_notes(self, data: dict[str, Any], cwd: Path | str | None = None, source: str = "otomatik",
                    max_save: int = 3, max_delete: int = 2, book: Any = None, protect: bool = True) -> list[str]:
        """Modelin {"save": [...], "delete": [...]} cevabını uygula. Kullanıcının açıkça istediği notlar (source
        kullanıcı) silinmez; tek seferde en fazla max_delete not silinir."""
        from alice.memdir import TYPES, MemoryFull

        done: list[str] = []
        for s in (data.get("save") or [])[:max_save]:
            if not isinstance(s, dict):
                continue
            content = str(s.get("content") or s.get("body") or "").strip()
            desc = str(s.get("description") or "").strip()
            typ = str(s.get("type") or "")
            if typ not in TYPES or not (content or desc) or len(content) > 6000:
                continue
            if typ == "user" and ephemeral(desc):  # yol, deneme, tek seferlik iş: kullanıcıya dair bilgi değil
                continue
            try:
                target = book or self.book(str(s.get("scope") or "genel"), cwd)
            except ValueError:
                target = self.notes
            old = target.get(str(s.get("name") or ""))
            if old is None and source in ("otomatik", "dream") and \
                    self._forgotten_like(f"{s.get('title') or ''} {desc} {content}"):
                done.append(f"atlandı (kullanıcı silmişti): {desc[:60]}")
                continue
            try:
                note, new = target.save(str(s.get("name") or s.get("title") or desc), desc, typ, content,
                                        title=str(s.get("title") or ""),
                                        source=(old.source if old is not None and old.source else source))
            except (MemoryFull, ValueError) as e:
                done.append(f"kaydedilemedi ({e})"[:160])
                continue
            done.append(("eklendi " if new else "güncellendi ") + note.key)
        removed = 0
        for key in data.get("delete") or []:
            if removed >= max_delete:
                break
            b, n = (book, book.get(str(key))) if book is not None else self.find_note(str(key), cwd)
            if n is None or (protect and n.source == "kullanıcı"):
                continue
            if b.delete(n.name):
                removed += 1
                done.append(f"silindi {n.key}")
        if done:
            self._bump()
        return done

    # ------------------------------------------------------------------ Dream
    def maybe_dream(self) -> bool:
        """Ucuz kontrol (çekirdek dakikada bir çağırır): son Dream'den beri dream_hours geçtiyse ve en az
        dream_sessions konuşma biriktiyse hafızayı toparla. Kapı sırası: zaman (dosya zamanı) -> 10 dakikada bir
        konuşma sayımı -> kilit."""
        if not self.dream_enabled or (self.synthesizer is None and self.extractor is None):
            return False
        last = self.notes.last_dream()
        if time.time() - last < self.dream_hours * 3600:
            return False
        if self._last_dream_scan is not None and time.monotonic() - self._last_dream_scan < 600:
            return False
        self._last_dream_scan = time.monotonic()
        groups = self.sessions(after=last or time.time() - 7 * 86400)
        if groups and time.time() - groups[-1][-1].ts < self.session_gap:
            groups = groups[:-1]  # süren konuşma sayılmaz
        if len(groups) < self.dream_sessions:
            return False
        self.dream()
        return True

    def dream(self, force: bool = False, cwd: Path | str | None = None) -> list[str]:
        """Hafızanın üstünden geç: tekrarları birleştir, eskiyeni düzelt, göreli tarihleri mutlak yap, son
        konuşmalardan kalıcı olanı ekle, dizini toparla. Yapılanları döndürür. Önce yedek alınır."""
        from alice.memdir import DREAM_PROMPT, MEMORY_RULES, MemoryFull

        if not self._dream_lock.acquire(blocking=False):
            return ["başka bir Dream sürüyor"]
        try:
            books = self.all_books()
            if cwd:
                books += [b for b in self.books(cwd) if b.root not in {x.root for x in books}]
            report: list[str] = []
            for b in books:
                prior = b.try_lock()
                if prior is None:
                    report.append(f"{b.scope}: başka bir süreç toparlıyor")
                    continue
                ok = False
                try:
                    report += self._dream_book(b, prior, DREAM_PROMPT, MEMORY_RULES, MemoryFull)
                    ok = True
                except Exception as e:  # noqa: BLE001 - model yok/kota/bozuk cevap: zaman kapısı yine açık kalsın
                    log.debug("Dream başarısız", exc_info=True)
                    report.append(f"{b.scope}: toparlanamadı ({str(e)[:120]})")
                finally:
                    b.release(prior, ok=ok)
            summary = "; ".join(report) or "değişiklik yok"
            self._meta("dream_summary", summary[:2000])
            self._meta("dream_at", repr(time.time()))
            return report
        finally:
            self._dream_lock.release()

    def _dream_book(self, b: Any, prior: float, prompt_tpl: str, rules: str, full: type) -> list[str]:
        notes = b.scan()
        since = prior or time.time() - 14 * 86400
        signal: list[str] = []
        logs = b.logs_since(since)
        if logs:
            signal.append("Daily logs:\n" + logs)
        if b.scope == "genel":
            sums = [s for s in self.summaries(60) if s.end > since]
            if sums:
                signal.append("Conversation summaries:\n" + "\n".join(f"- {s.line()}" for s in sums))
            fups = self.followups()
            if fups:
                signal.append("Open follow-ups:\n" + "\n".join(f"- {f.line()}" for f in fups))
        if not notes and not signal:
            return []
        gone = self.forgotten(20)
        if gone and signal:
            signal.append("Notes the user DELETED (do NOT re-create them or anything saying the same):\n"
                          + "\n".join(f"- {_clip(g, 200)}" for g in gone))
        backup = b.backup()
        listing = "\n\n".join(
            f"--- {n.name}.md [{n.type or '?'}] ({age_text_iso(n.modified)})"
            f"{' (kullanıcı istedi)' if n.source == 'kullanıcı' else ''}\ndescription: {n.description}\n"
            f"{_clip(n.body, 2500)}" for n in notes) or "(yok)"
        prompt = prompt_tpl.format(today=f"{datetime.now():%Y-%m-%d %A}", scope=f"{b.scope} ({b.root})", rules=rules,
                                   index=b.index_raw().strip() or "(boş)", notes=listing,
                                   since=datetime.fromtimestamp(since).strftime("%Y-%m-%d %H:%M"),
                                   signal="\n\n".join(signal) or "(yeni bir şey yok)")
        data = parse_json(self._synth(prompt))
        if not data:
            raise ValueError("Dream cevabı boş")
        done = self.apply_notes(data, source="dream", max_save=15, max_delete=max(2, len(notes) // 4), book=b)
        index = data.get("index")
        if isinstance(index, str) and index.strip():
            try:
                b.write_index(index)
                done.append("dizin yeniden düzenlendi")
            except full as e:
                b.repair_index()
                done.append(f"yeni dizin sığmadı ({e})"[:160])
        elif b.repair_index():
            done.append("dizin onarıldı")
        summary = data.get("summary")
        if isinstance(summary, str) and summary.strip():
            b.append_log(f"Dream: {summary.strip()}")
            done.insert(0, summary.strip())
        if backup is not None and done:
            done.append(f"yedek: {backup.name}")
        return [f"{b.scope}: {x}" for x in done]

    # Eski adlar (çekirdek ve arayüz bunlarla çağırıyordu).
    def maybe_consolidate(self, every_hours: float | None = None) -> bool:
        return self.maybe_dream()

    def consolidate(self) -> list[str]:
        return self.dream(force=True)

    def forget_session(self, sid: str, users: list[str] | None = None, start: float | None = None,
                       end: float | None = None) -> dict[str, int]:
        """Silinen sohbetin hafızadaki izleri: turları (vektörleriyle) ve yalnız o turları kapsayan özetler.

        Sohbet kimliğiyle kaydedilen turlar kimlikten bulunur. Kimlik eklenmeden önceki eski turlar için sohbetin
        zaman aralığı (start..end) ve kullanıcının yazdıkları eşleştirilir; başka sohbetin turu silinmesin diye
        ikisi birden tutmalı. Sohbet silindiği hâlde ALICE onu "Son konuşmalar"dan hatırlamaya devam ediyordu."""
        ids: set[int] = set()
        with self._lock:
            if sid:
                ids |= {r[0] for r in self.db.execute("SELECT id FROM episodes WHERE sid = ?", (str(sid),))}
            wanted = {fold(" ".join(clean_user_text(u).split()))[:2000] for u in users or [] if u and u.strip()}
            if wanted and start is not None and end is not None:
                for eid, user in self.db.execute("SELECT id, user FROM episodes WHERE sid = '' AND ts BETWEEN ? AND ?",
                                                 (start - 120, end + 120)):
                    if fold(" ".join(user.split())) in wanted:
                        ids.add(eid)
            if not ids:
                return {"episodes": 0, "summaries": 0}
            marks = ",".join("?" * len(ids))
            lo, hi = self.db.execute(f"SELECT MIN(ts), MAX(ts) FROM episodes WHERE id IN ({marks})", tuple(ids)).fetchone()
            self.db.execute(f"DELETE FROM episodes WHERE id IN ({marks})", tuple(ids))
            self.db.execute(f"DELETE FROM vectors WHERE kind = 'episode' AND ref IN ({marks})", tuple(ids))
            # Özet bir konuşmanın tamamını anlatır: aralığında başka tur kalmadıysa o sohbetindir, gider.
            gone_sums = []
            for s_id, s_start, s_end in self.db.execute("SELECT id, start, end FROM summaries WHERE end >= ? AND "
                                                        "start <= ?", (lo - 1, hi + 1)).fetchall():
                left = self.db.execute("SELECT COUNT(*) FROM episodes WHERE ts BETWEEN ? AND ?",
                                       (s_start - 1, s_end + 1)).fetchone()[0]
                if not left:
                    gone_sums.append(s_id)
            for s_id in gone_sums:
                self.db.execute("DELETE FROM summaries WHERE id = ?", (s_id,))
                self.db.execute("DELETE FROM vectors WHERE kind = 'summary' AND ref = ?", (s_id,))
            self.db.commit()
        self._bump()
        return {"episodes": len(ids), "summaries": len(gone_sums)}

    def forget_history(self) -> dict[str, int]:
        """Bütün konuşma geçmişi (turlar, özetler, vektörleri). Notlar ve takipler kalır."""
        with self._lock:
            n_eps = self.db.execute("DELETE FROM episodes").rowcount
            n_sums = self.db.execute("DELETE FROM summaries").rowcount
            self.db.execute("DELETE FROM vectors WHERE kind IN ('episode', 'summary')")
            self.db.commit()
        self._bump()
        return {"episodes": n_eps, "summaries": n_sums}

    def forget_episode(self, eid: int) -> bool:
        with self._lock:
            cur = self.db.execute("DELETE FROM episodes WHERE id = ?", (int(eid),))
            self.db.execute("DELETE FROM vectors WHERE kind = 'episode' AND ref = ?", (int(eid),))
            self.db.commit()
        if cur.rowcount:
            self._bump()
        return bool(cur.rowcount)

    def forget_summary(self, sid: int) -> bool:
        with self._lock:
            cur = self.db.execute("DELETE FROM summaries WHERE id = ?", (int(sid),))
            self.db.execute("DELETE FROM vectors WHERE kind = 'summary' AND ref = ?", (int(sid),))
            self.db.commit()
            if cur.rowcount:
                self._bump()
        return bool(cur.rowcount)

    def _synth(self, prompt: str) -> str:
        """Dream: güçlü model; o yoksa ya da hata verirse hızlı model."""
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


def _scratch_dir() -> Path:
    """Geçici (":memory:") hafızanın notları: ALICE evinin altında (testlerde her testin kendi geçici evi)."""
    d = alice_home() / "tmp"
    d.mkdir(parents=True, exist_ok=True)
    return d


def age_text_iso(ts: float) -> str:
    from alice.memdir import age_text

    return f"{datetime.fromtimestamp(ts):%Y-%m-%d}, {age_text(ts)}"


def project_label(cwd: Path | str | None) -> str:
    from alice.memdir import project_root

    root = project_root(cwd) if cwd else None
    return root.name if root else "?"


# Konuşma özeti: konuşma kaydının (günlüğün) kendisi. Kalıcı notlar buradan değil, tur sonu çıkarımından ve Dream'den
# gelir; Dream bu özetleri okuyup birkaç konuşmada tekrar edenleri nota damıtır.
DIGEST_PROMPT = """You keep the conversation log of ALICE, a personal assistant (like Jarvis) for one user. Below is \
one finished conversation (it started {date}). Produce:
1. "summary": 1-3 short Turkish sentences: what was talked about, what got done or decided, what is left. Concrete \
(names, projects, numbers, dates). Write only what happened: never sentences like "Hiçbir işlem yapılmadı", "karar \
alınmadı", "devam eden iş kalmadı". Empty string if it was only trivial commands, small talk, testing ALICE's \
features, or misheard noise (fragments that make no sense, other languages). ALICE replies starting with \
[doğrulanmadı] are claims no tool confirmed: do not report them as done or as confirmed by the user.
2. "followups": things a good assistant should bring up later. kind "ask": an upcoming event in the user's life to \
ask about afterwards (exam, match, trip, interview) with "due" = the day after it happens; kind "remind": something \
the user asked to be reminded of on a day; kind "open": only when the user said they will continue something later \
(no due). A command that failed or a task ALICE was doing is NOT a follow-up. Turkish, written as an instruction to \
ALICE ("Matematik sınavının nasıl geçtiğini sor."). Usually zero.
3. "done": ids of the open follow-ups below that this conversation already resolved.
Dates are YYYY-MM-DD; resolve "yarın", "cuma" etc. relative to the conversation date.
Open follow-ups:
{open}
Answer ONLY with JSON: {{"summary": "...", "followups": [{{"text": "...", "kind": "ask", "due": "YYYY-MM-DD"}}], \
"done": []}}

Conversation:
{transcript}"""


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


def _worth_asking(text: str, notes: list[Any]) -> bool:
    """Not seçimini modele sormaya değer mi: mesajda en az 3 anlamlı kelime var ya da bir notla ortak kelime var."""
    q = set(terms(text))
    if len(q) >= 3:
        return True
    if not q:
        return False
    for n in notes:
        if q & set(terms(f"{n.title} {n.name.replace('-', ' ')} {n.description} {n.body[:600]}")):
            return True
    return False


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


def profile_text(home: Path | None = None, cwd: Path | str | None = None) -> str:
    """Sistem istemi için profil (hafıza dosyası yoksa boş; dosya yaratmaz)."""
    home = home or alice_home()
    if not any((home / x).exists() for x in ("memory.db", "memory.md", "memory")):
        return ""
    mem = shared_memory()
    return mem.profile(cwd=cwd) if mem is not None else ""
