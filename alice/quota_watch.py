"""Abonelik kotası uyarısı: cliproxy hesaplarından biri (Claude 5 saatlik / haftalık, ChatGPT pencereleri) dolmak
üzereyken kullanıcıya bir kez haber ver. Hesaplar arası geçişi proxy ve router zaten yapıyor; eksik olan, kota
bitmeden önce görünür olmasıydı (iş yarıda yavaş ya da ücretsiz modele düşünce fark ediliyordu).

Her pencere (hesap + pencere + sıfırlanma zamanı) için her eşik bir kez bildirilir; pencere sıfırlanınca yeniden.
Proxy'ye en fazla `interval` saniyede bir sorulur ve bu tur bitişinde arka planda yapılır."""
from __future__ import annotations

import threading
import time
from datetime import datetime
from typing import Any, Callable

THRESHOLDS = (80, 95)
INTERVAL = 300.0  # sn


def proxy_accounts() -> list[dict[str, Any]]:
    """cliproxy hesapları (kart özetiyle aynı biçim). Proxy kapalıysa boş; başlatmaya çalışmaz."""
    from alice.app import cliproxy

    if not cliproxy.settings().get("installed"):
        return []
    data = cliproxy._req("GET", "/v8/management/credentials")  # noqa: SLF001
    files = data.get("files") if isinstance(data, dict) else None
    return [cliproxy._account(f) for f in files or [] if isinstance(f, dict)]  # noqa: SLF001


class QuotaWatch:
    def __init__(self, notify: Callable[[str], None], fetch: Callable[[], list[dict[str, Any]]] = proxy_accounts,
                 interval: float = INTERVAL, thresholds: tuple[int, ...] = THRESHOLDS):
        self.notify, self.fetch, self.interval, self.thresholds = notify, fetch, interval, tuple(sorted(thresholds))
        self._seen: dict[tuple[str, str, int], int] = {}
        self._last = 0.0
        self._lock = threading.Lock()

    def maybe_check(self, now: float | None = None) -> bool:
        """Zamanı geldiyse arka planda kontrol et. Başlatıldıysa True."""
        now = time.monotonic() if now is None else now
        with self._lock:
            if self._last and now - self._last < self.interval:
                return False
            self._last = now
        threading.Thread(target=self._run, daemon=True, name="alice-quota-watch").start()
        return True

    def _run(self) -> None:
        try:
            for msg in self.check(self.fetch()):
                self.notify(msg)
        except Exception:  # noqa: BLE001 - proxy kapalı / cevap bozuk: uyarı yok, ALICE etkilenmez
            pass

    def check(self, accounts: list[dict[str, Any]]) -> list[str]:
        """Yeni geçilen eşikler için uyarı metinleri."""
        out: list[str] = []
        for acc in accounts:
            if acc.get("disabled"):
                continue
            for w in (acc.get("quota") or {}).get("windows") or []:
                pct = int(w.get("percent") or 0)
                level = max((t for t in self.thresholds if pct >= t), default=0)
                key = (str(acc.get("name") or acc.get("email")), str(w.get("label")), int(w.get("reset") or 0))
                if not level or self._seen.get(key, 0) >= level:
                    continue
                self._seen[key] = level
                out.append(self._message(acc, w, pct, accounts))
        return out

    @staticmethod
    def _message(acc: dict[str, Any], w: dict[str, Any], pct: int, accounts: list[dict[str, Any]]) -> str:
        service = acc.get("provider_name") or acc.get("provider") or "hesap"
        reset = int(w.get("reset") or 0)
        when = f", {datetime.fromtimestamp(reset).strftime('%d.%m %H:%M')}'de sıfırlanır" if reset else ""
        others = [a for a in accounts if a is not acc and a.get("provider") == acc.get("provider") and a.get("ready")
                  and int((a.get("quota") or {}).get("percent") or 0) < 80]
        tail = (f" Diğer {service} hesabı hazır, dolunca istekler ona geçer." if others else
                f" Başka hazır {service} hesabı yok; dolunca istekler sıradaki modele düşer.")
        return f"Kota uyarısı: {service} hesabı {acc.get('email') or acc.get('name')} {w.get('label')} kotasının %{pct}'ini kullandı{when}.{tail}"
