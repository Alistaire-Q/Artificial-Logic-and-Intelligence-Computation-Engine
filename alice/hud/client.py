"""HUD'a komut gönderen istemci: show("arrow", clock=4, text="SAM 12 km", ttl=3).

UDP ile 127.0.0.1'e tek datagram yollar: bağlantı kurmaz, cevap beklemez. HUD kapalıysa paket düşer, hiçbir şey
olmaz; uçuş anonsu ya da ses asla HUD yüzünden beklemez.
"""

from __future__ import annotations

import json
import socket
import threading
from typing import Any

from alice.hud import PORT

_port = PORT
_sock: socket.socket | None = None
_lock = threading.Lock()


def configure(port: int) -> None:
    global _port
    _port = int(port)


def send(msg: dict[str, Any]) -> bool:
    """Komutu gönder. Gönderilemezse (HUD kapalı, ağ yığını hata verdi) sessizce False."""
    global _sock
    try:
        raw = json.dumps(msg, ensure_ascii=False).encode("utf-8")
        with _lock:
            if _sock is None:
                _sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                _sock.setblocking(False)
            _sock.sendto(raw, ("127.0.0.1", _port))
        return True
    except (OSError, TypeError, ValueError):
        # Windows'ta kapalı porta giden önceki paketin ICMP'si sonraki çağrıda ConnectionResetError olarak döner.
        return False


def show(kind: str, ttl: float = 3.0, **fields: Any) -> bool:
    """Bir öğe göster: arrow (clock), edge (clock), label (text, x, y), card (title, text), box (x, y, w, h),
    progress (value 0-1, text). severity: info | warn | critical. Aynı id yenisiyle değişir."""
    return send({"kind": kind, "ttl": ttl, **fields})


def clear(id: str = "") -> bool:  # noqa: A002 - protokoldeki alan adı
    """Hepsini ya da yalnızca bu kimlikteki öğeyi sil."""
    return send({"kind": "clear", "id": id} if id else {"kind": "clear"})


def toggle() -> bool:
    return send({"kind": "toggle"})
