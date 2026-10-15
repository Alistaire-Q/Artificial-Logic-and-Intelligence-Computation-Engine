"""@dosya bahsetmeleri: mesajdaki @yol/dosya.py ifadelerini dosya içeriğiyle modele ekler."""

from __future__ import annotations

import os
import re
from pathlib import Path

from alice.tools.files import SKIP_DIRS

MENTION = re.compile(r"(?<![\w@])@((?:\"[^\"]+\")|(?:[^\s,;:!?)]+))")
MAX_FILE = 100_000


def expand_mentions(text: str, cwd: Path) -> str:
    """Var olan ve okunabilen dosyaları mesajın sonuna <file> blokları olarak ekle.
    Klasörler içerik listesiyle eklenir. Bulunamayan @ifadeler olduğu gibi kalır (e-posta, kullanıcı adı vs.)."""
    blocks = []
    seen = set()
    for m in MENTION.finditer(text):
        raw = m.group(1).strip('"').rstrip(".")
        p = Path(raw).expanduser()
        if not p.is_absolute():
            p = cwd / p
        try:
            p = p.resolve()
        except OSError:
            continue
        if p in seen or not p.exists():
            continue
        seen.add(p)
        if p.is_dir():
            try:
                names = sorted(e.name + ("/" if e.is_dir() else "") for e in p.iterdir() if e.name not in SKIP_DIRS)[:200]
            except OSError:  # izin yok: önceden bütün tur çöküyordu
                continue
            blocks.append(f'<folder path="{p}">\n' + "\n".join(names) + "\n</folder>")
            continue
        try:
            size = p.stat().st_size
            with p.open("rb") as fh:  # dev dosyanın (ISO, video) tamamı belleğe okunmasın
                data = fh.read(MAX_FILE)
        except OSError:
            continue
        if b"\0" in data[:4096]:
            blocks.append(f'<file path="{p}">(binary dosya, {size} byte)</file>')
            continue
        body = data.decode("utf-8", errors="replace")
        more = f"\n... ({size - MAX_FILE} byte kesildi)" if size > MAX_FILE else ""
        blocks.append(f'<file path="{p}">\n{body}{more}\n</file>')
    if not blocks:
        return text
    return text + "\n\n" + "\n\n".join(blocks)


def complete_paths(cwd: Path, query: str, limit: int = 30) -> list[str]:
    """@ yazınca açılan öneri listesi için: cwd altında adı query'yi içeren dosya/klasörler."""
    q = query.lower().replace("\\", "/")
    out: list[tuple[int, str]] = []
    scanned = 0
    for root, dirs, files in os.walk(cwd):
        dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS and not d.startswith("."))
        rel_root = os.path.relpath(root, cwd).replace("\\", "/")
        rel_root = "" if rel_root == "." else rel_root + "/"
        for name in dirs + sorted(files):
            rel = rel_root + name + ("/" if name in dirs else "")
            low = rel.lower()
            if not q or q in low:
                # Dosya adının kendisi eşleşirse öne al, sonra kısa yollar.
                score = (0 if name.lower().startswith(q.split("/")[-1]) else 1, len(rel))
                out.append((score, rel))
            scanned += 1
        if scanned > 20000:
            break
    out.sort()
    return [r for _, r in out[:limit]]
