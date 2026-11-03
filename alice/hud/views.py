"""HUD'da ne, nasıl görünür: bildirim kartı, konuşmanın yazılı özeti, tıklamadan önce hedef çerçevesi,
tarayıcıda üzerinde çalışılan ya da kullanıcıya gösterilen öğenin çerçevesi.

Çağıranlar (bildirimler, ses, bilgisayar kullanımı, tarayıcı) yalnızca bunları çağırır. HUD açık değilse hiçbiri bir şey
yapmaz ve beklemez.
"""

from __future__ import annotations

import re
import time

from alice.hud import client

CARD_TTL = {"critical": 12.0, "warn": 9.0, "info": 7.0}
CLICK_PREVIEW = 0.4  # sn: ALICE'in imleci hedefe kayıp oturur, sonra gerçek tıklama (ne yapacağını önceden gör)
_LIST = re.compile(r"^\s*(?:[-*•]|\d+[.)])\s+(.+)$")


def notification(n) -> bool:
    """Windows balonu yerine ALICE'in kartı. Uçuş anonsları zaten ses ve okla geliyor: kart açılmaz."""
    if not client.active or n.kind == "flight" or n.topic == "flight":
        return False
    sev = n.severity if n.severity in CARD_TTL else "info"
    body = " ".join((n.body or "").split())
    return client.show("card", title=n.title[:60], text=body[:240], severity=sev, ttl=CARD_TTL[sev],
                       id=f"note:{n.title[:40]}")


def summarize(text: str, limit: int = 260) -> str | None:
    """Sesli cevabın köşede gösterilecek kısa yazılı hali; tek cümlelik kısa cevaba kart gerekmez (None).
    Liste maddeleri ve sayılar korunur: sesli cevap uzun listeyi okumaz, gösterir."""
    lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip()]
    items = [m.group(1) for ln in lines if (m := _LIST.match(ln))]
    if len(items) >= 2:
        out = " · ".join(items)
    else:
        flat = " ".join(lines)
        sentences = [x for x in re.split(r"(?<=[.!?])\s+", flat) if x]
        if len(flat) < 120 and len(sentences) <= 1 and not re.search(r"\d", flat):
            return None
        out = flat
    out = re.sub(r"[*_`#]+", "", out)
    return out if len(out) <= limit else out[: limit - 1].rstrip() + "…"


def speech(text: str) -> bool:
    if not client.active:
        return False
    short = summarize(text)
    if short is None:
        return False
    return client.show("card", title="ALICE", text=short, severity="info", ttl=min(15.0, max(5.0, len(short) / 18)),
                       id="speech")


def click_target(x: float, y: float, action: str = "click", wait: bool = True) -> bool:
    """Bilgisayar kullanımında ALICE'in imleci tıklanacak yere kayar, tıklamada halka yayılır (fiziksel piksel)."""
    if not client.active:
        return False
    label = {"double_click": "çift tıklıyorum", "right_click": "sağ tıklıyorum", "drag": "sürüklüyorum",
             "move": "buraya gidiyorum", "scroll": "kaydırıyorum", "type": "yazıyorum"}.get(action, "tıklıyorum")
    ok = client.show("cursor", x=round(x), y=round(y), action=action, text=label, severity="info", ttl=1.5,
                     id="computer")
    if ok and wait:
        time.sleep(CLICK_PREVIEW)
    return ok


TASK_TTL = 45.0  # sn: ekran görevi her adımda yeniler; görev takılıp ölürse durum kendiliğinden kaybolur


def task_step(step: int) -> bool:
    """Ekran görevi sürerken Adada durum: model adımlar arasında 5-15 sn düşünüyor, HUD boş kalmasın."""
    if not client.active:
        return False
    text = f"Ekran görevi · {step}. adım" if step else "Ekran görevi · başlıyor"
    return client.show("label", text=text, icon="cursor", severity="info", ttl=TASK_TTL, id="task")


def task_end() -> bool:
    return client.clear("task") if client.active else False


VOICE = {"listen": ("Dinliyorum", "wave"), "think": ("Düşünüyorum", "sparkle"), "speak": ("Konuşuyorum", "wave"),
         "work": ("Çalışıyorum", "sparkle")}
VOICE_TTL, VOICE_REFRESH = 30.0, 20.0  # takılıp kalırsa kendiliğinden söner; uzun dinlemede tazelenir
_voice: dict = {"state": None, "detail": "", "at": 0.0}


def voice_state(state: str | None, detail: str = "") -> bool:
    """ALICE'in sesli oturumunun hali Adada: dinliyor (dalga + mavi parıltı), düşünüyor, konuşuyor, bir iş yapıyor.
    Aynı hal tekrar gelirse göndermez (her döngüde çağrılabilir)."""
    if not client.active:
        return False
    now = time.monotonic()
    if state == _voice["state"] and detail == _voice["detail"] and (state is None or now - _voice["at"] < VOICE_REFRESH):
        return False
    _voice.update(state=state, detail=detail, at=now)
    if state not in VOICE:
        return client.clear("voice")
    text, icon = VOICE[state]
    return client.show("label", text=f"{text} · {detail}" if detail else text, icon=icon, severity="info",
                       ttl=VOICE_TTL, id="voice")


MUSIC_TTL = 5.0


def now_playing(n: dict) -> bool:
    """Parça değişince Adada kısa süre şarkı · sanatçı (oyun modunda bilgi olduğu için görünmez)."""
    if not client.active or not n.get("title"):
        return False
    text = f"{n['title'][:40]} · {n['artist'][:30]}" if n.get("artist") else n["title"][:60]
    return client.show("label", text=text, icon="music", severity="info", ttl=MUSIC_TTL, id="music")


def game_context(in_game: bool) -> bool:
    """Oyundayken HUD yalnız tehdit ve uyarıları gösterir (ayar: [hud] mode)."""
    return client.send({"kind": "context", "game": bool(in_game)}) if client.active else False


def system_gauge(name: str, value: float, limit: float, unit: str, warn: float | None = None,
                 ttl: float = 25.0) -> bool:
    """Kritik sınıra yaklaşan bilgisayar değeri köşede küçük bir çubuk: "GPU 89°C". Sınırın altında (varsayılan
    %90'ı) hiçbir şey göndermez; değer normale dönünce gönderim durur ve çubuk kısa ttl ile kendiliğinden kaybolur.
    Aynı ad hep aynı kimlikle gider: HUD her turda yeni çubuk açmaz, mevcut olanı yeniler."""
    if not client.active or limit <= 0:
        return False
    warn = 0.9 * limit if warn is None else warn
    if value < warn:
        return False
    sev = "critical" if value >= limit else "warn"
    text = f"{name} %{value:.0f}" if unit == "%" else f"{name} {value:.0f}{unit}"
    return client.show("progress", value=round(min(1.0, value / limit), 3), text=text, severity=sev, ttl=ttl,
                       id=f"sys:{name}")


PAGE_TTL = 1.5  # sn: tarayıcıda tıklanan/yazılan öğenin çerçevesi
SHOW_TTL = 6.0  # sn: kullanıcıya gösterilen öğe (browser action=highlight) daha uzun kalır


def page_target(x: float, y: float, w: float, h: float, label: str = "", ttl: float = PAGE_TTL,
                wait: bool = False) -> bool:
    """Tarayıcıdaki bir öğeyi ekranda çerçevele (fiziksel ekran pikseli, x/y sol üst köşe)."""
    if not client.active or w < 2 or h < 2:
        return False
    ok = client.show("box", x=x, y=y, w=w, h=h, text=label[:40], severity="info", ttl=ttl, id="browser")
    if ok and wait:
        time.sleep(CLICK_PREVIEW)
    return ok
