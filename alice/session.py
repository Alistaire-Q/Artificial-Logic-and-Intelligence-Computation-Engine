from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from alice.config import alice_home
from alice.providers.router import strip_images


def sessions_dir() -> Path:
    d = alice_home() / "sessions"
    d.mkdir(parents=True, exist_ok=True)
    return d


def new_session_id() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S")


def save(session_id: str, messages: list[dict[str, Any]], cwd: Path) -> Path:
    path = sessions_dir() / f"{session_id}.json"
    # Görüntüler diske yazılmaz, oturum dosyaları küçük kalsın.
    body = {"cwd": str(cwd), "saved": datetime.now().isoformat(timespec="seconds"),
            "messages": strip_images(messages[1:])}
    path.write_text(json.dumps(body, ensure_ascii=False, indent=1), encoding="utf-8")
    return path


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
    if not files:
        return None
    data = json.loads(files[0].read_text(encoding="utf-8"))
    return files[0].stem, data.get("messages", [])
