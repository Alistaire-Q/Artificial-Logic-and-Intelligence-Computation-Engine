"""Yerel CLIProxyAPI: hesap listesi, yeni hesap bağlama (OAuth linki), hesap silme.

CLIProxyAPI `~/.alice/cliproxy` klasöründe çalışır (config.yaml, auths/, keys.txt). ALICE arayüzündeki
"Claude Code proxy" kartı buradan beslenir: kullanıcı uygulamada bir servis seçip giriş linkine basar,
hesap tarayıcıda eklenir. Birden çok hesap eklenirse proxy istekleri aralarında paylaştırır; biri limite
takılınca otomatik sıradakine geçer (CLIProxyAPI'nin varsayılan round-robin + failover davranışı).
"""

from __future__ import annotations

import os
import re
import subprocess
import threading
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


def binary_name() -> str:
    """Platforma göre çalıştırılabilir dosya adı."""
    return "cli-proxy-api.exe" if os.name == "nt" else "cli-proxy-api"


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
            "installed": (h / binary_name()).exists()}


MODELS_TTL = 120.0  # sn
_SKIP_MODELS = ("image", "embedding", "codex-auto-review", "tts")
_models_cache: dict[str, Any] = {"at": 0.0, "ids": []}


def live_models(fetch: Any = None) -> list[str]:
    """Proxy'nin şu an sunduğu sohbet modelleri (/v1/models; bağlı hesaplara göre değişir). config'teki sabit
    liste sadece Claude/GPT içeriyordu: Kimi ya da sonradan bağlanan hesapların modelleri menüde hiç görünmüyordu.
    Proxy kapalıysa ya da cevap vermezse son bilinen liste (ilk seferde boş) döner."""
    now = time.monotonic()
    if _models_cache["at"] and now - _models_cache["at"] < MODELS_TTL:
        return list(_models_cache["ids"])
    _models_cache["at"] = now  # hata da olsa her menü açılışında yeniden denenmesin
    try:
        if fetch is None:
            s = settings()
            key = ""
            for line in (s["dir"] / "keys.txt").read_text(encoding="utf-8", errors="replace").splitlines():
                if line.startswith("api_key="):
                    key = line.split("=", 1)[1].strip()
            with httpx.Client(timeout=2.5) as c:
                r = c.get(f"http://127.0.0.1:{s['port']}/v1/models", headers={"Authorization": f"Bearer {key}"})
            r.raise_for_status()
            data = r.json().get("data") or []
        else:
            data = fetch()
        ids = sorted({str(m.get("id") or "") for m in data if isinstance(m, dict)} - {""})
        _models_cache["ids"] = [i for i in ids if not any(k in i.lower() for k in _SKIP_MODELS)]
    except Exception:  # noqa: BLE001 - proxy kapalı: menü yine açılsın
        pass
    return list(_models_cache["ids"])


# Proxy modellerinin arayüzdeki grupları: (önek, grup kimliği, etiket). Kimlik gizleme deseni olarak da kullanılır:
# "cliproxy:kimi-*" gizlenince bütün Kimi modelleri gizlenir.
GROUPS: tuple[tuple[tuple[str, ...], str, str], ...] = (
    (("claude-",), "claude", "Claude (abonelik)"),
    (("gpt-", "codex", "o3", "o4"), "codex", "ChatGPT (abonelik)"),
    (("kimi-",), "kimi", "Kimi"),
    (("gemini-",), "antigravity", "Gemini (Antigravity)"),
    (("grok-",), "xai", "xAI Grok"),
)


def model_group(model: str) -> tuple[str, str, str]:
    """(grup kimliği, etiket, gizleme deseni) - ör. kimi-k3 -> ("kimi", "Kimi", "cliproxy:kimi-*")."""
    low = model.lower()
    for prefixes, gid, label in GROUPS:
        if low.startswith(prefixes):
            return gid, label, f"cliproxy:{prefixes[0]}*"
    return "other", "Diğer (proxy)", ""


def tidy_models(ids: list[str], keep: int = 2) -> list[str]:
    """Menü boğulmasın: tarihli sabit sürümler (…-20250514) ve eski kuşaklar atılır, her aileden en yeni `keep`
    sürüm kalır. Aile = sürüm numaraları çıkarılmış ad (claude-opus-#, kimi-k#-code, gpt-#-terra)."""
    import re as _re

    def nums(m: str) -> tuple[int, ...]:
        return tuple(int(x) for x in _re.findall(r"\d+", m) if len(x) < 4)

    fresh = [m for m in ids if not _re.search(r"-\d{8}$", m)]
    top: dict[str, int] = {}
    for m in fresh:  # satıcı başına en yeni ana sürüm (claude 5, kimi 3); gpt-oss-120b gibi adlar hesaba girmez
        n = nums(m)
        if n and n[0] < 50:
            v = m.split("-", 1)[0]
            top[v] = max(top.get(v, 0), n[0])
    fams: dict[str, list[str]] = {}
    for m in fresh:
        n = nums(m)
        v = m.split("-", 1)[0]
        if n and n[0] < 50 and n[0] < top.get(v, 0) - 1:
            continue  # iki kuşak eski (claude-3-7-sonnet)
        fams.setdefault(_re.sub(r"\d+(?:[.-]\d+)*", "#", m), []).append(m)
    out: list[str] = []
    for members in fams.values():
        out += sorted(members, key=nums, reverse=True)[:keep]
    return sorted(out)


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


# ------------------------------------------------------------------ başlatma
_start_lock = threading.Lock()
_last_start = 0.0
START_RETRY_SECONDS = 60.0


def running() -> bool:
    """Yönetim API'si cevap veriyorsa proxy çalışıyor demektir."""
    try:
        _req("GET", "/v8/management/credentials")
        return True
    except Exception:  # noqa: BLE001 - kapalı/erişilemez
        return False


def start(wait_seconds: float = 8.0) -> dict[str, Any]:
    """Proxy kapalıysa arka planda (ALICE'den bağımsız) başlat ve uç cevap verene kadar bekle."""
    global _last_start
    s = settings()
    if not s["installed"]:
        return {"ok": False, "error": "CLIProxyAPI kurulu değil"}
    if running():
        return {"ok": True, "already": True}
    with _start_lock:
        if running():
            return {"ok": True, "already": True}
        now = time.monotonic()
        if now - _last_start < START_RETRY_SECONDS and _last_start:
            return {"ok": False, "error": "az önce denendi; biraz sonra yeniden denenecek"}
        _last_start = now
        exe = s["dir"] / binary_name()
        if not exe.exists():
            return {"ok": False, "error": f"{binary_name()} bulunamadı"}
        try:
            log = open(s["dir"] / "server.out.log", "ab")  # noqa: SIM115 - çocuk süreç yazacak
        except OSError:
            log = subprocess.DEVNULL
        kwargs: dict[str, Any] = {"cwd": str(s["dir"]), "stdout": log, "stderr": subprocess.STDOUT,
                                  "stdin": subprocess.DEVNULL, "close_fds": True}
        if os.name == "nt":
            # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW: ALICE kapansa da çalışsın,
            # konsol penceresi açılmasın.
            kwargs["creationflags"] = 0x00000008 | 0x00000200 | 0x08000000
        try:
            subprocess.Popen([str(exe), "-config", str(s["dir"] / "config.yaml")], **kwargs)
        except OSError as e:
            return {"ok": False, "error": f"başlatılamadı: {e}"}
        finally:
            if log is not subprocess.DEVNULL:
                log.close()
        end = time.time() + wait_seconds
        while time.time() < end:
            if running():
                return {"ok": True, "started": True}
            time.sleep(0.3)
    return {"ok": False, "error": "proxy başladı ama cevap vermedi (server.out.log)"}


def ensure_started() -> dict[str, Any]:
    """Kuruluysa ve kapalıysa başlat; kurulu değilse sessizce atla."""
    if not settings()["installed"]:
        return {"ok": True, "skipped": "kurulu değil"}
    if running():
        return {"ok": True, "already": True}
    return start()


def start_in_background(install: bool = False, version: str = "") -> None:
    """ALICE açılışını bekletmeden proxy'yi (gerekirse önce kurup) başlatmayı dene."""

    def run() -> None:
        if install:
            try:
                from alice.app import cliproxy_setup

                cliproxy_setup.ensure_installed(version)
            except Exception:  # noqa: BLE001 - indirme başarısızsa proxy'siz devam
                pass
        ensure_started()

    threading.Thread(target=run, daemon=True, name="alice-cliproxy").start()


def _pct(value: Any) -> int | None:
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return round(f * 100) if f <= 1 else round(f)


def _num(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


# (sinyal öneki, etiket). Claude: 5 saatlik ve haftalık pencere; ChatGPT (Codex): birincil ve ikincil pencere.
_WINDOWS = (("Anthropic-Ratelimit-Unified-5h", "5 saat"), ("Anthropic-Ratelimit-Unified-7d", "7 gün"),
            ("X-Codex-Primary", "kısa dönem"), ("X-Codex-Secondary", "hafta"))


def _window_label(sig: dict[str, Any], prefix: str, label: str) -> str:
    mins = _num(sig.get(f"{prefix}-Window-Minutes"))
    if not mins:
        return label
    return f"{round(mins / 1440)} gün" if mins >= 1440 else f"{round(mins / 60)} saat" if mins >= 60 else f"{round(mins)} dk"


def _iso_ts(value: Any) -> float:
    """ISO zaman damgası (Go'nun 7-9 haneli kesirli saniyesiyle) -> unix; okunamazsa 0."""
    if not value:
        return 0.0
    from datetime import datetime

    text = re.sub(r"(\.\d{6})\d+", lambda m: m.group(1), str(value).strip()).replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(text).timestamp()
    except ValueError:
        return 0.0


def _quota_summary(quota: Any, now: float | None = None) -> dict[str, Any]:
    """Hesabın kota pencereleri: {"windows": [{label, percent, reset, stale}], label, percent, reset}. Üstteki
    label/percent/reset en dolu penceredir (çubuk onu gösterir). Önceden yalnız ilk pencere (5 saat) gösteriliyordu:
    haftalığı %91 olan hesap "%13" görünüyordu. Sinyaller hesabın son isteğinden kalmadır; sıfırlanma zamanı geçmiş
    pencere artık boştur (%0, stale): dün dolan 5 saatlik pencere bugün hâlâ "%100" diye gösteriliyordu."""
    sig = (quota or {}).get("signals") if isinstance(quota, dict) else None
    if not isinstance(sig, dict):
        return {}
    now = time.time() if now is None else now
    observed = _iso_ts((quota or {}).get("observed_at"))
    windows: list[dict[str, Any]] = []
    for prefix, label in _WINDOWS:
        pct = _pct(sig.get(f"{prefix}-Utilization")) if f"{prefix}-Utilization" in sig else _pct(sig.get(f"{prefix}-Used-Percent"))
        if pct is None:
            continue
        reset = _num(sig.get(f"{prefix}-Reset") or sig.get(f"{prefix}-Reset-At")) or 0
        after = _num(sig.get(f"{prefix}-Reset-After-Seconds"))
        if not reset and after is not None and observed:
            reset = observed + after
        stale = bool(reset and reset <= now)
        windows.append({"label": _window_label(sig, prefix, label), "percent": 0 if stale else pct,
                        "reset": 0 if stale else int(reset), "stale": stale})
    if not windows:
        return {}
    top = max(windows, key=lambda w: w["percent"])
    return {"label": top["label"], "percent": top["percent"], "reset": top["reset"], "windows": windows,
            "observed": int(observed or 0)}


def _duration(seconds: float) -> str:
    if seconds >= 86400:
        return f"{round(seconds / 86400)} gün"
    if seconds >= 3600:
        return f"{round(seconds / 3600)} sa"
    return f"{max(1, round(seconds / 60))} dk"


def _cooldown_summary(cooldowns: Any, now: float | None = None) -> str:
    """Bekleyen model varsa ne kadar kaldığı ("3 gün", "2 sa", "15 dk"), yoksa boş. Proxy süreyi `remaining_seconds`
    ve `retry_at` ile veriyor; önceden bunlar okunmuyor, kart "beklemede · beklemede" yazıyordu."""
    if not cooldowns:
        return ""
    items = cooldowns if isinstance(cooldowns, list) else [cooldowns]
    now = time.time() if now is None else now
    best = 0.0
    unknown = False  # süresi okunamayan kayıt: proxy beklemede diyor ama ne kadar bilinmiyor
    for it in items:
        if not isinstance(it, dict):
            continue
        left = _num(it.get("remaining_seconds"))
        if left is None:
            until = it.get("retry_at") or it.get("until") or it.get("until_ts") or it.get("expires_at") or it.get("reset")
            ts = _num(until) or _iso_ts(until)
            left = ts - now if ts else None
        if left is None:
            unknown = True
        elif left > 0:
            best = max(best, left)
    # Süresi geçmiş kayıtlar engel sayılmaz: proxy temizlemeden bıraksa hesap sonsuza dek "meşgul" kalıyordu.
    return _duration(best) if best > 0 else "kısa süre" if unknown else ""


_SIGN_IN_CODES = ("invalid_grant", "refresh_failed", "unauthorized", "invalid_token")


def _account_error(f: dict[str, Any]) -> tuple[bool, str]:
    """(yeniden giriş gerekli mi, kısa hata). Proxy, yenileme jetonu ölmüş hesabı `status: error, unavailable: true`
    diye bildiriyor ama `requires_sign_in` çoğu zaman false kalıyor: kart bu hesapları "hazır" gösteriyordu."""
    err = f.get("refresh_error") if isinstance(f.get("refresh_error"), dict) else {}
    code = str(err.get("code") or "").lower()
    msg = str(err.get("message") or f.get("status_message") or "")
    if bool(f.get("requires_sign_in")) or code in _SIGN_IN_CODES:
        if "restricted" in msg.lower():
            return True, "hesap kısıtlanmış (sağlayıcı girişi reddediyor)"
        return True, "giriş süresi dolmuş"
    if str(f.get("status") or "").lower() == "error" or f.get("unavailable"):
        if f.get("quota_blocked") or _cooldown_summary(f.get("cooldowns")):
            return False, ""  # kota: ayrı gösterilir
        return False, " ".join(msg.split())[:120] or "hata"
    return False, ""


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
    sign_in, error = _account_error(f)
    cooldown = _cooldown_summary(f.get("cooldowns"))
    return {
        "name": name,
        "email": str(f.get("email") or f.get("account") or f.get("label") or name),
        "provider": provider,
        "provider_name": PROVIDER_NAMES.get(provider, provider or "hesap"),
        "disabled": bool(f.get("disabled")),
        "status": str(f.get("status") or ""),
        "status_message": str(f.get("status_message") or ""),
        "needs_sign_in": sign_in,
        "error": error,
        "expires_at": str(f.get("access_token_expires_at") or ""),
        "quota_blocked": bool(f.get("quota_blocked")),
        "cooldown": cooldown,
        "quota": _quota_summary(f.get("quota")),
        "failed": int(f.get("failed") or 0),
        # Ham cooldowns listesi değil, hâlâ süren bekleme: süresi geçmiş kayıt hesabı meşgul göstermez.
        "ready": not (sign_in or error or f.get("disabled") or f.get("quota_blocked") or cooldown),
    }


READY_TTL = 60.0  # sn
_ready_cache: dict[str, Any] = {"at": 0.0, "groups": {}}


def groups_ready(fetch: Any = None) -> dict[str, bool]:
    """Servis (claude, codex, antigravity...) -> kullanılabilir en az bir hesabı var mı. Hesabı olmayan servis
    sözlükte yoktur. Router bununla bütün hesapları dolu/girişi düşmüş servisi atlar: her istekte önce o servisi
    deneyip hata beklemek yerine doğrudan sıradakine geçer. Proxy'ye ulaşılamazsa boş döner (hiçbiri atlanmaz)."""
    now = time.monotonic()
    if fetch is None and _ready_cache["at"] and now - _ready_cache["at"] < READY_TTL:
        return dict(_ready_cache["groups"])
    out: dict[str, bool] = {}
    try:
        data = fetch() if fetch is not None else _req("GET", "/v8/management/credentials")
        files = data.get("files") if isinstance(data, dict) else None
        for f in files or []:
            if isinstance(f, dict):
                acc = _account(f)
                if acc["provider"]:
                    out[acc["provider"]] = out.get(acc["provider"], False) or acc["ready"]
    except Exception:  # noqa: BLE001 - proxy kapalı: bilinmiyor
        out = {}
    if fetch is None:
        _ready_cache.update(at=now, groups=out)
    return dict(out)


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
    except Exception as e:  # noqa: BLE001 - kapalıysa başlatmayı dene, yine olmazsa "çalışmıyor" de
        started = start()
        if not started.get("ok"):
            out["error"] = str(started.get("error") or e)[:200]
            return out
        try:
            data = _req("GET", "/v8/management/credentials")
        except Exception as e2:  # noqa: BLE001
            out["error"] = str(e2)[:200]
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
        # is_webui=1: proxy tarayıcının döndüğü localhost:54545/1455 adresini dinleyip
        # yönetim callback'ine yönlendirsin. Bu bayrak olmadan tarayıcı "bağlantı reddedildi"de kalıyor
        # ve hesap hiç eklenmiyordu.
        d = _req("GET", f"/v8/management/oauth/auth-url?provider={quote(provider)}&is_webui=1")
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
