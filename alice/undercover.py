"""Gizli mod (undercover): GitHub'a giden yazılarda ALICE'in ve yapay zekânın izini bırakmamak.

Açıkken commit mesajları, PR başlık ve açıklamaları, issue'lar ve yorumlar kullanıcının kendi yazısı gibi gider:
* imza satırları ("Co-Authored-By: …", "Generated with …", "— ALICE") kendiliğinden silinir;
* metinde hâlâ ALICE'i ya da bir yapay zekâyı ele veren söz varsa araç yazıyı reddeder, ajan yeniden yazar;
* PR takibi kendi cevaplarını imzadan değil, yazdığı yorumların kimliğinden tanır (``own.json``);
* bağımsız inceleme bulguları PR'a yazılmaz, yalnız kullanıcıya bildirilir: depo sahibinin hesabından yazılmış
  yorumlar takip tarafından "sahibin isteği" sanılıp uygulanırdı.

Sınır: biri doğrudan ve ciddi olarak "bunu bir bot mu yazdı" diye sorarsa ALICE yalan söylemez; o konuya cevap
vermez, kullanıcıya bırakır.

Ayar: ``[github] undercover = "off" | "on" | "auto"`` ("auto": yalnız herkese açık depolarda). Ek gizli sözcükler
(şirket içi kod adları gibi): ``[github] undercover_words = ["..."]``.
"""
from __future__ import annotations

import json
import re
import threading
from pathlib import Path
from typing import Any

MODES = ("off", "on", "auto")
OWN_FILE = "github-own.json"
OWN_KEEP = 3000

# Satırın tamamı imza: silinir.
_SIGN_LINE = re.compile(
    r"(?im)^\s*(?:"
    r"co-authored-by:.*(?:claude|anthropic|alice|openai|chatgpt|gpt|gemini|copilot|cursor|\bai\b|bot\b|noreply@).*"
    r"|(?:🤖\s*)?(?:generated|written|created|made|co-?authored|assisted)\s+(?:with|by|using)\s+.*"
    r"|🤖.*"
    r"|[—–-]{1,2}\s*alice\s*"
    r"|.*\b(?:alice|yapay zek[aâ]|ai)\s+(?:ile|tarafından|yardımıyla)\s+(?:yazıldı|oluşturuldu|üretildi|hazırlandı).*"
    r")$")
_BLANKS = re.compile(r"\n{3,}")

# Silinmeden kalırsa yazıyı ele verir: ajan yeniden yazmalı.
_TELLS = [
    (re.compile(r"(?i)\bALICE\b"), "ALICE"),
    (re.compile(r"(?i)\b(?:claude|anthropic|chatgpt|openai|gemini|copilot)\b"), "model/şirket adı"),
    (re.compile(r"(?i)\b(?:as an ai|an ai assistant|language model|ai[- ]generated|llm)\b"), "yapay zekâ sözü"),
    (re.compile(r"(?i)yapay zek[aâ]|dil modeli|asistan(?:ı|ın)?\s+olarak"), "yapay zekâ sözü"),
    (re.compile(r"(?i)\b(?:otomatik olarak düzeltildi|auto-?fix(?:ed)? by|addressed by)\b"), "otomasyon izi"),
]

_lock = threading.Lock()
_public: dict[str, bool] = {}


def settings(cfg: Any) -> dict[str, Any]:
    data = getattr(cfg, "data", None) or {}
    return dict(data.get("github") or {})


def mode(cfg: Any) -> str:
    m = str(settings(cfg).get("undercover", "off")).strip().lower()
    if m in ("true", "1", "yes", "evet"):
        return "on"
    return m if m in MODES else "off"


def is_public(api: Any, repo: str) -> bool:
    """Depo herkese açık mı (oturum boyunca önbellekte). Bakılamazsa açık sayılır: emin değilken gizlemek güvenli."""
    if repo in _public:
        return _public[repo]
    try:
        val = not bool(api.get(f"/repos/{repo}").get("private"))
    except Exception:  # noqa: BLE001
        return True
    _public[repo] = val
    return val


def active(cfg: Any, repo: str | None, api: Any = None) -> bool:
    m = mode(cfg)
    if m == "on":
        return True
    if m == "off" or not repo:
        return False
    if api is None:
        from alice.tools.github import GitHubAPI, github_token

        api = GitHubAPI(github_token(cfg))
    return is_public(api, repo)


def scrub(text: str) -> str:
    """İmza satırlarını sil, fazla boş satırları topla."""
    out = _SIGN_LINE.sub("", text or "")
    return _BLANKS.sub("\n\n", out).strip()


def tells(text: str, extra: list[str] | tuple[str, ...] = ()) -> list[str]:
    """Silinmeden kalan ele veren sözler (tekrarsız)."""
    found: list[str] = []
    for rx, label in _TELLS:
        if rx.search(text or "") and label not in found:
            found.append(label)
    for w in extra:
        w = str(w).strip()
        if w and re.search(rf"(?i)\b{re.escape(w)}\b", text or "") and w not in found:
            found.append(w)
    return found


def clean(cfg: Any, text: str, what: str = "metin") -> str:
    """İmzaları sil; ele veren söz kalırsa ToolError ile reddet (ajan yazıyı yeniden yazar)."""
    from alice.tools.base import ToolError

    out = scrub(text)
    hits = tells(out, settings(cfg).get("undercover_words") or [])
    if hits:
        raise ToolError(f"gizli mod açık: {what} şunları ele veriyor: {', '.join(hits)}. Kullanıcının kendi yazısı "
                        "gibi, ALICE'ten ya da yapay zekâdan söz etmeden yeniden yaz ve tekrar dene.")
    return out


RULES = (
    "Gizli mod açık: commit, PR, issue ve yorumları depo sahibi kendisi yazmış gibi yaz. ALICE'ten, yapay zekâdan, "
    "modelden ya da otomasyondan söz etme; imza ekleme. Biri doğrudan bunu bir botun mu yazdığını sorarsa inkâr "
    "etme ve cevaplama: o konuyu kullanıcıya bırak, son mesajında söyle."
)


# --------------------------------------------------------------------------- ALICE'in yazdığı yorumlar
def _own_path() -> Path:
    from alice.config import alice_home

    return alice_home() / OWN_FILE


def _load_own() -> list[int]:
    try:
        return [int(x) for x in json.loads(_own_path().read_text(encoding="utf-8"))]
    except (OSError, ValueError, TypeError):
        return []


def remember_own(*ids: Any) -> None:
    """ALICE'in GitHub'a yazdığı yorum/inceleme kimlikleri: PR takibi bunları yeni yorum saymaz."""
    new = [int(i) for i in ids if i]
    if not new:
        return
    with _lock:
        rows = _load_own()
        rows = [i for i in rows if i not in new] + new
        path = _own_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(rows[-OWN_KEEP:]), encoding="utf-8")
        tmp.replace(path)


def is_own(cid: Any) -> bool:
    if not cid:
        return False
    try:
        return int(cid) in set(_load_own())
    except (TypeError, ValueError):
        return False
