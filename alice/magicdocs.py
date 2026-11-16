"""MagicDocs: kendiliğinden güncel kalan belgeler.

İlk satırı ``# MAGIC DOC: <başlık>`` olan bir Markdown dosyası ALICE tarafından okununca o sohbet için kaydedilir. Her
tur bittikten sonra (belge başına en sık 10 dakikada bir) arka planda model, konuşmadan çıkan kalıcı bilgileri
(kararlar, yeni öğrenilen gerçekler, değişen adımlar) belgeye işler. Yalnız bu başlıkla işaretlenmiş dosyalara dokunulur;
başlık satırı korunur, model "değişiklik yok" derse dosya yazılmaz. Başlığın altındaki ilk satır italikse (``_..._``)
belgenin amacı sayılır ve modele yön olarak verilir.
"""
from __future__ import annotations

import re
import threading
import time
from pathlib import Path
from typing import Any

MAGIC_RX = re.compile(r"^#\s*MAGIC DOC:\s*(.+?)\s*$", re.I)
MIN_GAP = 600  # sn: aynı belge en sık bu aralıkla güncellenir
MAX_DOC = 40_000
NO_CHANGE = "DEĞİŞİKLİK YOK"
_lock = threading.Lock()

PROMPT = """Bir "sihirli belge"yi güncel tutuyorsun. Belge başlığı: {title}
{purpose}
Aşağıda belgenin şu anki hâli ve son konuşma var. Konuşmada bu belgenin konusuyla ilgili KALICI yeni bilgi varsa
(karar, öğrenilen gerçek, değişen adım, çözülen sorun) belgeyi güncelle: yerine yaz, eskiyen bilgiyi düzelt, tekrar
etme, belgenin düzenini koru, kısa ve net yaz. Geçici ayrıntıları, sohbeti ve tahminleri ekleme.
Güncelleme gerekmiyorsa yalnız şunu yaz: {no_change}
Gerekiyorsa belgenin TAM yeni hâlini yaz (ilk satır aynen "# MAGIC DOC: {title}" olmalı), başka hiçbir şey yazma.

# Belgenin şu anki hâli
{doc}

# Son konuşma
{talk}"""


def title_of(path: Path) -> str | None:
    """Dosya sihirli belgeyse başlığı, değilse None (yalnız ilk satıra bakılır)."""
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            first = fh.readline(400)
    except OSError:
        return None
    m = MAGIC_RX.match(first.strip())
    return m.group(1) if m else None


def note_read(state: dict[str, Any], path: Path) -> bool:
    """read_file bir dosyayı okudu: sihirli belgeyse sohbete kaydet."""
    if path.suffix.lower() not in (".md", ".markdown", ".txt") or not path.is_file():
        return False
    title = title_of(path)
    if not title:
        return False
    docs: dict[str, dict[str, Any]] = state.setdefault("magic_docs", {})
    docs.setdefault(str(path), {"title": title, "at": 0.0})
    return True


def _read(path: Path) -> str | None:
    """Belgenin metni; okunamıyor ya da UTF-8 değilse None (title_of hatalı baytları yutuyor, burada
    UnicodeDecodeError tur sonundaki güncellemeyi çökertiyordu)."""
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None


def _talk(messages: list[dict[str, Any]], n: int = 24) -> str:
    rows = []
    for m in messages[1:]:
        c = m.get("content")
        if m.get("role") in ("user", "assistant") and isinstance(c, str) and c.strip() and not m.get("_nudge"):
            rows.append(f"{'Kullanıcı' if m['role'] == 'user' else 'ALICE'}: {' '.join(c.split())[:1200]}")
    return "\n".join(rows[-n:])


def update(state: dict[str, Any], router: Any, messages: list[dict[str, Any]], now: float | None = None,
           role: str = "fast") -> list[str]:
    """Vakti gelmiş sihirli belgeleri güncelle. Güncellenen dosyaların yolları."""
    now = time.time() if now is None else now
    done: list[str] = []
    talk = _talk(messages)
    if not talk:
        return done
    for path_s, info in list((state.get("magic_docs") or {}).items()):
        if now - float(info.get("at") or 0) < MIN_GAP:
            continue
        path = Path(path_s)
        doc = _read(path)
        if doc is None:
            continue
        title = title_of(path)
        if not title or len(doc) > MAX_DOC:
            continue  # başlık silindiyse artık sihirli değil
        info["at"] = now
        lines = doc.splitlines()
        purpose = ""
        if len(lines) > 1 and re.fullmatch(r"\s*[_*].+[_*]\s*", lines[1] or ""):
            purpose = "Belgenin amacı: " + lines[1].strip(" _*")
        prompt = PROMPT.format(title=title, purpose=purpose, no_change=NO_CHANGE, doc=doc, talk=talk)
        try:
            res = router.chat(role, [{"role": "user", "content": prompt}], max_tokens=6000)
            new = (res.text or "").strip()
        except Exception:  # noqa: BLE001 - güncellenemezse belge olduğu gibi kalır
            continue
        new = re.sub(r"^```(?:markdown|md)?\s*\n|\n```\s*$", "", new)
        if not new or NO_CHANGE in new[:60] or not MAGIC_RX.match(new.splitlines()[0].strip()):
            continue
        with _lock:
            try:
                if _read(path) != doc:
                    continue  # bu arada kullanıcı (ya da ALICE) değiştirdi: üzerine yazma
                # Dosyanın satır sonu korunur: Windows'ta metin kipi LF dosyayı baştan sona CRLF yapıyordu.
                eol = "\r\n" if b"\r\n" in path.read_bytes() else "\n"
                with open(path, "w", encoding="utf-8", newline=eol) as fh:
                    fh.write(new.replace("\r\n", "\n").rstrip() + "\n")
            except OSError:
                continue
        done.append(path_s)
    return done
