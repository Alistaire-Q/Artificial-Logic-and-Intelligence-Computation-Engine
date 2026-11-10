from __future__ import annotations

import json
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

from alice.config import alice_home
from alice.providers.router import strip_images


def sessions_dir() -> Path:
    d = alice_home() / "sessions"
    d.mkdir(parents=True, exist_ok=True)
    return d


_issued: set[str] = set()
_issued_lock = threading.Lock()


def new_session_id() -> str:
    """Saniyeye kadar zaman. Aynı saniyede açılan sohbetler (biri arkada çalışırken yenisi) aynı dosyaya yazmasın
    diye gerekirse sonuna -2, -3 eklenir."""
    base = datetime.now().strftime("%Y%m%d-%H%M%S")
    with _issued_lock:
        sid, n = base, 1
        while sid in _issued or (sessions_dir() / f"{sid}.json").exists():
            n += 1
            sid = f"{base}-{n}"
        _issued.add(sid)
    return sid


def save(session_id: str, messages: list[dict[str, Any]], cwd: Path) -> Path:
    path = sessions_dir() / f"{session_id}.json"
    # Görüntüler diske yazılmaz, oturum dosyaları küçük kalsın.
    body = {"cwd": str(cwd), "saved": datetime.now().isoformat(timespec="seconds"),
            "messages": strip_images(messages[1:])}
    # Önce geçici dosyaya yaz: her adımda kaydederken yarıda kalan bir yazma oturumu bozmasın.
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(body, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(path)
    return path


ARCHIVE_TOOL_CHARS = 4000  # arşivde araç çıktısı bu kadar tutulur (yalnız ekranda göstermek için)


def archive_path(session_id: str) -> Path:
    # .jsonl: oturumları listeleyen "*.json" taramalarına girmez.
    return sessions_dir() / f"{session_id}.archive.jsonl"


def archive(session_id: str, messages: list[dict[str, Any]]) -> None:
    """Özetleme sırasında bağlamdan çıkan mesajları oturumun arşivine ekle. Model artık bunları görmez (özetini
    görür) ama sohbet ekranında durmaya devam ederler: önceden özetlenince bütün eski mesajlar ekrandan ve
    oturum dosyasından siliniyordu."""
    if not messages:
        return
    lines = []
    for m in strip_images(messages):
        m = {k: v for k, v in m.items() if k != "reasoning_content"}
        if m.get("role") == "tool" and isinstance(m.get("content"), str) and len(m["content"]) > ARCHIVE_TOOL_CHARS:
            m["content"] = m["content"][:ARCHIVE_TOOL_CHARS] + "\n…(kısaltıldı)"
        lines.append(json.dumps(m, ensure_ascii=False))
    with open(archive_path(session_id), "a", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


def load_archive(session_id: str) -> list[dict[str, Any]]:
    try:
        text = archive_path(session_id).read_text(encoding="utf-8")
    except OSError:
        return []
    out = []
    for line in text.splitlines():
        try:
            m = json.loads(line)
        except json.JSONDecodeError:  # yarım yazılmış son satır
            continue
        if isinstance(m, dict):
            out.append(m)
    return out


def list_sessions(limit: int = 15) -> list[tuple[str, str, str]]:
    out = []
    for p in sorted(sessions_dir().glob("*.json"), reverse=True)[:limit]:
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        first = next((m["content"] for m in data.get("messages", [])
                      if m.get("role") == "user" and isinstance(m.get("content"), str)), "")
        out.append((p.stem, data.get("cwd", ""), first[:70].replace("\n", " ")))
    return out


def load(session_id: str | None = None) -> tuple[str, list[dict[str, Any]]] | None:
    files = sorted(sessions_dir().glob("*.json"), reverse=True)
    if session_id:
        files = [f for f in files if f.stem == session_id]
    # Bozuk ya da yarım kalmış bir dosya (list_sessions'ın da atladığı) devam etmeyi çökertmesin: sıradakine geç.
    for f in files:
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        return f.stem, data.get("messages", [])
    return None


# Kullanıcının verdiği ad ve sabitleme. Oturum dosyası her adımda baştan yazıldığı için ayrı tutulur:
# {"<id>": {"title": "...", "pinned": true}}
def _meta_path() -> Path:
    return alice_home() / "session_meta.json"


def load_meta() -> dict[str, dict[str, Any]]:
    try:
        data = json.loads(_meta_path().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def set_meta(session_id: str, **fields: Any) -> dict[str, Any]:
    """Alanları güncelle; boş başlık ya da False sabitleme alanı siler."""
    meta = load_meta()
    row = dict(meta.get(session_id) or {})
    for k, v in fields.items():
        if v in ("", None, False):
            row.pop(k, None)
        else:
            row[k] = v
    if row:
        meta[session_id] = row
    else:
        meta.pop(session_id, None)
    _meta_path().parent.mkdir(parents=True, exist_ok=True)
    tmp = _meta_path().with_suffix(".json.tmp")
    tmp.write_text(json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(_meta_path())
    return row
