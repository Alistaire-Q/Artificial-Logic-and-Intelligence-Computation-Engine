"""Yerel CLIProxyAPI: hesap listesi, yeni hesap bağlama (OAuth linki), hesap silme.

CLIProxyAPI `~/.alice/cliproxy` klasöründe çalışır (config.yaml, auths/, keys.txt). ALICE arayüzündeki
"Claude Code proxy" kartı buradan beslenir: kullanıcı uygulamada bir servis seçip giriş linkine basar,
hesap tarayıcıda eklenir. Birden çok hesap eklenirse proxy istekleri aralarında paylaştırır; biri limite
takılınca otomatik sıradakine geçer (CLIProxyAPI'nin varsayılan round-robin + failover davranışı).
"""

from __future__ import annotations

import re
import time
from typing import Any
from urllib.parse import quote

import httpx

from alice.config import alice_home

TIMEOUT = 6.0

# Arayüzde "hesap ekle" için görünen servisler (CLIProxyAPI'nin OAuth destekledikleri).
PROVIDERS: tuple[tuple[str, str], ...] = (
    ("claude", "Claude (Pro / Max)"),
    ("codex", "ChatGPT (Codex)"),
    ("antigravity", "Google Antigravity"),
    ("kimi", "Kimi"),
    ("xai", "xAI (Grok)"),
)
PROVIDER_NAMES = dict(PROVIDERS)
FILE_PREFIXES = (("claude-", "claude"), ("codex-", "codex"), ("antigravity-", "antigravity"),
                 ("kimi-", "kimi"), ("xai-", "xai"), ("meta-", "meta"), ("devin-", "devin"),
                 ("vertex-", "vertex"))


def home() -> Any:
    """CLIProxyAPI klasörü (`~/.alice/cliproxy`)."""
    return alice_home() / "cliproxy"


def settings() -> dict[str, Any]:
    """Klasör, port ve yönetim anahtarı. Kurulu değilse installed=False, anahtar yoksa key=""."""
    h = home()
    port = 8317
    try:
        text = (h / "config.yaml").read_text(encoding="utf-8", errors="replace")
        m = re.search(r"(?m)^\s*port:\s*(\d+)", text)
        if m:
            port = int(m.group(1))
    except OSError:
        pass
    key = ""
    try:
        for line in (h / "keys.txt").read_text(encoding="utf-8", errors="replace").splitlines():
            if line.startswith("management_key="):
                key = line.split("=", 1)[1].strip()
    except OSError:
        pass
    return {"dir": h, "port": port, "key": key,
            "installed": (h / "cli-proxy-api.exe").exists() or (h / "config.yaml").exists()}


def _req(method: str, path: str) -> dict[str, Any]:
    s = settings()
    if not s["key"]:
        raise RuntimeError("CLIProxyAPI yönetim anahtarı yok (~/.alice/cliproxy/keys.txt)")
    url = f"http://127.0.0.1:{s['port']}{path}"
    with httpx.Client(timeout=TIMEOUT) as c:
        r = c.request(method, url, headers={"Authorization": f"Bearer {s['key']}"})
    if r.status_code >= 400:
        raise RuntimeError(f"proxy {r.status_code}: {r.text[:160]}")
    try:
        return r.json()
    except ValueError:
        return {}


def _pct(value: Any) -> int | None:
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return round(f * 100) if f <= 1 else round(f)


def _quota_summary(quota: Any) -> dict[str, Any]:
    """Kotanın en anlamlı satırı: etiket, yüzde ve sıfırlanma zamanı (unix). Sağlayıcıya göre değişir."""
    sig = (quota or {}).get("signals") if isinstance(quota, dict) else None
    if not isinstance(sig, dict):
        return {}
    checks = (("Anthropic-Ratelimit-Unified-5h", "5 saat"), ("Anthropic-Ratelimit-Unified-7d", "7 gün"),
              ("X-Codex-Primary", "hafta"), ("X-Codex-Secondary", "hafta"))
    for prefix, label in checks:
        pct = _pct(sig.get(f"{prefix}-Utilization")) if f"{prefix}-Utilization" in sig else _pct(sig.get(f"{prefix}-Used-Percent"))
        if pct is None:
            continue
        reset = sig.get(f"{prefix}-Reset") or sig.get(f"{prefix}-Reset-At") or 0
        try:
            reset = int(float(reset))
        except (TypeError, ValueError):
            reset = 0
        return {"label": label, "percent": pct, "reset": reset}
    return {}


def _cooldown_summary(cooldowns: Any) -> str:
    """Bekleyen model varsa kısa metin ("dk"), yoksa boş."""
    if not cooldowns:
        return ""
    items = cooldowns if isinstance(cooldowns, list) else [cooldowns]
    now = time.time()
    best = 0.0
    for it in items:
        if not isinstance(it, dict):
            continue
        until = it.get("until") or it.get("until_ts") or it.get("expires_at") or it.get("reset")
        try:
            ts = float(until)
        except (TypeError, ValueError):
            continue
        if ts > now:
            best = max(best, ts - now)
    if best <= 0:
        return "beklemede"
    return f"{max(1, round(best / 60))} dk"


def _provider_of(name: str, provider: str) -> str:
    provider = str(provider or "").strip().lower()
    if provider:
        return provider
    for prefix, key in FILE_PREFIXES:
        if name.startswith(prefix):
            return key
    return ""


def _account(f: dict[str, Any]) -> dict[str, Any]:
    name = str(f.get("id") or f.get("name") or "")
    provider = _provider_of(name, str(f.get("provider") or ""))
    return {
        "name": name,
        "email": str(f.get("email") or f.get("account") or f.get("label") or name),
        "provider": provider,
        "provider_name": PROVIDER_NAMES.get(provider, provider or "hesap"),
        "disabled": bool(f.get("disabled")),
        "status": str(f.get("status") or ""),
        "status_message": str(f.get("status_message") or ""),
        "needs_sign_in": bool(f.get("requires_sign_in")),
        "expires_at": str(f.get("access_token_expires_at") or ""),
        "quota_blocked": bool(f.get("quota_blocked")),
        "cooldown": _cooldown_summary(f.get("cooldowns")),
        "quota": _quota_summary(f.get("quota")),
        "failed": int(f.get("failed") or 0),
    }


def status() -> dict[str, Any]:
    """Kartın ihtiyacı olan her şey: kurulu mu, çalışıyor mu, hesaplar, eklenebilecek servisler."""
    s = settings()
    out: dict[str, Any] = {
        "installed": bool(s["installed"]),
        "running": False,
        "dir": str(s["dir"]),
        "port": s["port"],
        "accounts": [],
        "providers": [{"key": k, "name": n} for k, n in PROVIDERS],
        "error": "",
    }
    if not s["installed"]:
        out["error"] = "CLIProxyAPI kurulu değil"
        return out
    try:
        data = _req("GET", "/v8/management/credentials")
    except Exception as e:  # noqa: BLE001 - proxy kapalıysa kart "çalışmıyor" der, çökmez
        out["error"] = str(e)[:200]
        return out
    out["running"] = True
    files = data.get("files") if isinstance(data, dict) else None
    out["accounts"] = [_account(f) for f in (files or []) if isinstance(f, dict)]
    out["accounts"].sort(key=lambda a: (a["provider"], a["email"]))
    return out


def login_url(provider: str) -> dict[str, Any]:
    """Bir servis için OAuth giriş linki başlat: {ok, url, state}. Kullanıcı linke basıp kendi hesabıyla girer."""
    provider = str(provider or "").strip().lower()
    if provider not in PROVIDER_NAMES and provider not in ("meta", "devin"):
        return {"ok": False, "error": "bilinmeyen servis"}
    try:
        d = _req("GET", f"/v8/management/oauth/auth-url?provider={quote(provider)}")
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e)[:200]}
    url = str(d.get("url") or "")
    if not url:
        return {"ok": False, "error": "giriş linki alınamadı"}
    return {"ok": True, "url": url, "state": str(d.get("state") or ""), "provider": provider}


def login_state(state: str) -> dict[str, Any]:
    """Giriş tamamlandı mı: status "ok" ise hesap eklendi, "wait" ise devam ediyor."""
    state = str(state or "").strip()
    if not state:
        return {"ok": False, "error": "eksik state"}
    try:
        d = _req("GET", f"/v8/management/oauth/status?state={quote(state)}")
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e)[:200]}
    return {"ok": True, "status": str(d.get("status") or ""), "error": str(d.get("error") or "")}


def cancel_login(state: str) -> dict[str, Any]:
    """Vazgeçilen girişi iptal et."""
    state = str(state or "").strip()
    if not state:
        return {"ok": False, "error": "eksik state"}
    try:
        _req("DELETE", f"/v8/management/oauth/session?state={quote(state)}")
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e)[:200]}
    return {"ok": True}


def remove(name: str) -> dict[str, Any]:
    """Bir hesabı (auth dosyasını) kaldır. Ad dosya adı olmalı: claude-....json."""
    name = str(name or "").strip()
    if not name.endswith(".json") or "/" in name or "\\" in name or ".." in name:
        return {"ok": False, "error": "geçersiz hesap adı"}
    try:
        _req("DELETE", f"/v8/management/credentials?name={quote(name)}")
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e)[:200]}
    return {"ok": True}
