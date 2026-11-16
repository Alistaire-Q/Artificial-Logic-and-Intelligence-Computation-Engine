"""Büyük araç çıktıları kaybolmasın: sınırı aşan çıktının tamamı diske yazılır, modele başı-sonu ve dosya yolu gider.

Önceden 12 bin karakteri aşan çıktının ortası atılıyordu (yalnız run_command tamamını saklıyordu): uzun bir CI logu,
web sayfası, grep ya da MCP çıktısında kesilen kısım bir daha görülemiyordu. Model artık gerekirse read_file
(offset/limit) ya da grep ile dosyadan okur. Dosyalar ``~/.alice/tool-results/GGAAYY/`` altında; 7 günden eskiler
silinir.
"""
from __future__ import annotations

import re
import shutil
import time
import uuid
from datetime import datetime
from pathlib import Path

KEEP_DAYS = 7
NO_PERSIST = {"read_file"}  # kendi sınırı var; dosyaya yazıp tekrar okutmak döngü olur
_MARK = "[tam çıktı dosyada:"
_last_sweep = 0.0


def root() -> Path:
    from alice.config import alice_home

    return alice_home() / "tool-results"


def _sweep(now: float) -> None:
    global _last_sweep
    if now - _last_sweep < 3600:
        return
    _last_sweep = now
    try:
        for d in root().iterdir():
            if d.is_dir() and now - d.stat().st_mtime > KEEP_DAYS * 86400:
                shutil.rmtree(d, ignore_errors=True)
    except OSError:
        pass


def save(text: str, call_id: str, tool: str) -> Path | None:
    now = time.time()
    folder = root() / datetime.fromtimestamp(now).strftime("%Y%m%d")
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", f"{tool}-{call_id}")[:120] or "cikti"
    # Çağrı kimliği tekil değil: bazı sağlayıcılar boş ya da sıra numarası ("call_0") veriyor. Ad yalnız kimlikten
    # olunca sonraki çıktı öncekinin dosyasını eziyor, eski sonuçtaki yol başka bir çıktıyı gösteriyordu.
    path = folder / f"{safe}-{uuid.uuid4().hex[:8]}.txt"
    try:
        folder.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8", errors="replace")
    except OSError:
        return None
    _sweep(now)
    return path


def shape(text: str, limit: int, call_id: str, tool: str) -> str:
    """Sınırı aşıyorsa tamamını sakla, başı-sonu ve dosya yolunu döndür; aşmıyorsa metnin kendisi."""
    from alice.tools.base import truncate

    if len(text) <= limit or tool in NO_PERSIST or _MARK in text:
        return truncate(text, limit)
    path = save(text, call_id, tool)
    if path is None:
        return truncate(text, limit)
    note = (f"\n{_MARK} {path} ({len(text)} karakter) - kesilen kısmı read_file (offset/limit) ya da grep ile "
            "oku; aracı tekrar çalıştırma]")
    return truncate(text, max(1000, limit - len(note))) + note
