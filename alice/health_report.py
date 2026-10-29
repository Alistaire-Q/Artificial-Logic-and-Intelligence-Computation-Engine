"""Günlük sağlık raporu: ALICE'in kendi bileşenlerinden bozulanı kendisi söylesin.

Takvim iki hafta her saat 404 verdi; hata sadece olay kaydına "hafıza işi hata verdi" diye yazıldığı için kimse fark
etmedi. Günde bir (sabah 9'dan sonra) şunlara bakılır:
  * `alice doctor` kontrolleri (takvim gerçekten okunur, Gmail token'ı yenilenir ...): bozuk ya da eksik olanlar
  * son 24 saatte en az 3 kez tekrarlanan hata kayıtları
  * çoğu isteği hata veren modeller (usage.json: dün ve bugün)
  * dolmak üzere olan diskler, şişen veritabanı
Sadece yeni çıkan sorunlar bildirilir; süren sorun 3 günde bir hatırlatılır, düzelen sorun bir kez "düzeldi" diye
söylenir. Sorun yoksa susar. Bildirim mod yüzünden susturulursa (uçuş) sonraki turda yeniden denenir.
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable

from alice.config import alice_home

RUN_EVERY = 20 * 3600
REMIND_EVERY = 3 * 86400
START_HOUR = 9
ERROR_REPEAT = 3       # 24 saatte bu kadar tekrarlanan hata sorun sayılır
MODEL_MIN_ERRORS = 5


def state_path() -> Path:
    return alice_home() / "health-report.json"


def _load(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _save(path: Path, st: dict[str, Any]) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(st, ensure_ascii=False, indent=1), encoding="utf-8")
    except OSError:
        pass


def collect(cfg, store, now: float | None = None, network: bool = True,
            doctor_checks: Callable[[], list] | None = None, usage: dict[str, Any] | None = None) -> dict[str, str]:
    """Şu anki sorunlar: {anahtar: tek satır açıklama}."""
    now = time.time() if now is None else now
    found: dict[str, str] = {}
    try:
        if doctor_checks is None:
            from alice import doctor

            checks = doctor.run_all(cfg, network=network)
        else:
            checks = doctor_checks()
        for c in checks:
            if c.status == "fail" or (c.status == "warn" and "sorun değil" not in c.fix):  # yedeği çalışan uyarı değil
                found[f"doctor:{c.name}"] = f"{c.name}: {c.detail}" + (f" (çözüm: {c.fix})" if c.fix else "")
    except Exception as e:  # noqa: BLE001 - kontrol listesi çökse de diğerleri sürsün
        found["doctor"] = f"kontroller çalışmadı: {str(e)[:120]}"
    if store is not None:
        counts: dict[str, list] = {}
        for ev in store.events(since=now - 86400, kind="error", limit=1000):
            counts.setdefault(ev.title, []).append(ev)
        for title, evs in counts.items():
            if len(evs) >= ERROR_REPEAT:
                found[f"error:{title}"] = f"{title} ({len(evs)} kez, son: {evs[0].body[:100]})"
        for key in store.keys("pc.disk.*.percent", since=now - 3600):
            last = store.last(key)
            if last and last[1] is not None and last[1] >= 90:
                disk = key.split(".")[2].upper()
                found[f"disk:{disk}"] = f"{disk}: diski %{last[1]:.0f} dolu"
        try:
            size = Path(store.path).stat().st_size / 2**20
            if size > 500:
                found["db"] = f"core.db {size:.0f} MB oldu"
        except (OSError, TypeError):
            pass
    if usage is None:
        try:
            usage = json.loads((alice_home() / "usage.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            usage = {}
    today = datetime.fromtimestamp(now).date()
    totals: dict[str, list[int]] = {}
    for day in (today - timedelta(days=1), today):
        for ref, row in (usage.get(day.isoformat()) or {}).items():
            t = totals.setdefault(ref, [0, 0])
            t[0] += int(row.get("requests") or 0)
            t[1] += int(row.get("errors") or 0)
    for ref, (ok, bad) in totals.items():
        if bad >= MODEL_MIN_ERRORS and bad > ok:
            found[f"model:{ref}"] = f"{ref}: son iki günde {ok + bad} isteğin {bad}'i hata verdi (zincirde atlanıyor)"
    return found


def report(cfg, store, send: Callable[[str, str], bool], now: float | None = None, force: bool = False,
           path: Path | None = None, **kw) -> str | None:
    """Vakti geldiyse kontrol et, yeni/düzelen sorun varsa `send(başlık, metin)` ile söyle. Söylenen metni döndürür."""
    now = time.time() if now is None else now
    path = path or state_path()
    st = _load(path)
    pending = st.get("pending")
    if pending and not force:  # önceki rapor susturulmuştu (uçuş modu): kontrolü tekrarlamadan yeniden dene
        if send(pending["title"], pending["body"]):
            st.pop("pending")
            _save(path, st)
        return pending["body"]
    if not force and (now - float(st.get("last_run") or 0) < RUN_EVERY
                      or datetime.fromtimestamp(now).hour < START_HOUR):
        return None
    found = collect(cfg, store, now, **kw)
    seen: dict[str, float] = st.get("said") or {}
    new = [k for k in found if now - float(seen.get(k) or 0) >= REMIND_EVERY]
    fixed = [k for k in seen if k not in found]
    st["last_run"] = now
    for k in fixed:
        seen.pop(k)
    if not new and not fixed:
        st["said"] = seen
        _save(path, st)
        return None
    lines = ["• " + found[k] for k in new]
    if fixed:
        lines.append("Düzelenler: " + ", ".join(k.split(":", 1)[-1] for k in fixed))
    title = f"ALICE sağlık raporu: {len(new)} sorun" if new else "ALICE sağlık raporu: sorunlar düzeldi"
    body = "\n".join(lines)
    for k in new:
        seen[k] = now
    st["said"] = seen
    if not send(title, body):
        st["pending"] = {"title": title, "body": body}
    _save(path, st)
    return body


def summary_now(cfg, store, **kw) -> str:
    """Sohbetten "sağlığın nasıl?" için: bildirim durumuna bakmadan şu anki sorunlar."""
    found = collect(cfg, store, **kw)
    if not found:
        return "Her şey çalışıyor: kontrollerde, hata kayıtlarında ve modellerde sorun yok."
    return f"{len(found)} sorun:\n" + "\n".join("• " + v for v in found.values())

