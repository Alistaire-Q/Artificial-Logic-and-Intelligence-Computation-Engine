"""BUDDY: ALICE'in köşesinde yaşayan küçük arkadaş (bir tür Tamagotchi).

İki parçası var:
* Kemikler (bones): türü, nadirliği, gözleri, şapkası, parlaklığı ve özellik puanları. Kullanıcıya özel bir
  tohumdan HER SEFERİNDE yeniden hesaplanır, dosyaya yazılmaz: dosyayı düzenleyip "efsanevi" yapmak mümkün olmasın.
* Ruh (soul): adı ve kişiliği. İlk açılışta bir kez (hızlı modelle, yoksa listeden) seçilir ve saklanır.

Bakım durumu (tokluk, enerji, keyif, deneyim) ``~/.alice/buddy.json`` dosyasında durur ve zamanla değişir:
konuştukça doyar ve deneyim kazanır, okşanınca keyiflenir, gece uyuyup enerji toplar, uzun süre ilgisiz kalırsa
acıkır ve keyfi kaçar. Ara sıra konuşmaya kısa bir yorum yapar (seyrek, hızlı modelle).

config.toml:
  [buddy]
  enabled = true
  chatter = true      # konuşmalara ara sıra yorum yapsın
"""
from __future__ import annotations

import getpass
import hashlib
import json
import math
import platform
import random
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

SALT = "alice-buddy/1"
DEFAULTS: dict[str, Any] = {"enabled": True, "chatter": True}

RARITIES = [("yaygın", 60, 5), ("sıradışı", 25, 15), ("nadir", 10, 25), ("destansı", 4, 35), ("efsanevi", 1, 50)]
STARS = {"yaygın": "★", "sıradışı": "★★", "nadir": "★★★", "destansı": "★★★★", "efsanevi": "★★★★★"}
STATS = ("ODAK", "MERAK", "SABIR", "ŞAKA", "KAOS")
EYES = ("o", "•", "^", "◉", "°", "@", "ø")
HATS = {"yok": "", "taç": " ♔ ", "silindir": " ▄█▄ ", "anten": "  ¦  ", "hale": " ◯ ", "kurdele": " ⋈ ", "yaprak": " ❦ "}

# Türler: ad -> 3-4 satırlık çizim. {e} göz yerine geçer.
SPECIES: dict[str, list[str]] = {
    "baykuş": [" ,___, ", " ({e},{e}) ", " /)  ) ", '  "  " '],
    "tilki": [" /\\_/\\ ", "( {e}.{e} )", " > ^ < ", "  ~~~ "],
    "kaplumbağa": ["  ____ ", " /####\\{e}", " \\____/ ", "  ‾  ‾ "],
    "ahtapot": ["  .--. ", " ( {e}{e} )", " /||||\\ ", " ‾‾‾‾‾ "],
    "kirpi": [" ,/\\/\\, ", "<  {e} {e}>", " `----' ", "  ' '  "],
    "penguen": ["  .-.  ", " ({e} {e}) ", " /( )\\ ", "  ^ ^  "],
    "ejder": [" /\\ /\\ ", "<({e} {e})>", "  )~~( ", " /    \\"],
    "robot": [" [===] ", " |{e} {e}| ", " |_=_| ", " d   b "],
    "hayalet": ["  .-.  ", " ({e} {e}) ", " |   | ", " ‾v‾v‾ "],
    "mantar": [" .-^-. ", "(_o_o_)", "  |{e}{e}| ", "  |__| "],
    "kurbağa": [" ({e})({e}) ", "(  __  )", " \\    / ", " ‾‾  ‾‾ "],
    "yarasa": ["/\\   /\\", "\\ ({e}{e}) /", " \\ vv / ", "   ‾‾   "],
    "salyangoz": ["    @  ", " {e}{e} @@ ", "(____@)", "  ‾‾‾‾ "],
    "kaktüs": ["  _|_  ", " |{e} {e}| ", "-|   |-", "  |_|  "],
    "bulut": ["  .--. ", ".({e}  {e}).", "(______)", "  ' '  "],
    "yengeç": ["v  _  v", " \\({e}{e})/", " /|  |\\", "  ‾  ‾ "],
}
NAMES = ("Pıtır", "Fıstık", "Bıcır", "Zıpzıp", "Mırnav", "Köpük", "Tarçın", "Kıvılcım", "Bulgur", "Pofuduk", "Limon",
         "Şeker", "Boncuk", "Duman", "Mısır", "Zeytin")
_lock = threading.Lock()


def settings(cfg: Any) -> dict[str, Any]:
    data = getattr(cfg, "data", None) or {}
    return {**DEFAULTS, **(data.get("buddy") or {})}


def user_seed() -> str:
    try:
        who = getpass.getuser()
    except Exception:  # noqa: BLE001
        who = "kullanıcı"
    return f"{who}@{platform.node()}"


@dataclass
class Bones:
    species: str
    rarity: str
    eye: str
    hat: str
    shiny: bool
    stats: dict[str, int] = field(default_factory=dict)

    def art(self, eye: str | None = None) -> list[str]:
        e = eye or self.eye
        rows = [r.replace("{e}", e) for r in SPECIES[self.species]]
        hat = HATS.get(self.hat, "")
        return ([hat.center(max(len(r) for r in rows))] if hat else []) + rows


def roll(seed: str | None = None) -> Bones:
    """Tohumdan kemikler: aynı kullanıcı her zaman aynı arkadaşı alır."""
    digest = hashlib.sha256(f"{seed or user_seed()}|{SALT}".encode()).digest()
    rng = random.Random(int.from_bytes(digest[:8], "big"))
    pick = rng.uniform(0, sum(w for _, w, _ in RARITIES))
    rarity, floor = RARITIES[-1][0], RARITIES[-1][2]
    for name, weight, fl in RARITIES:
        if pick < weight:
            rarity, floor = name, fl
            break
        pick -= weight
    species = rng.choice(sorted(SPECIES))
    eye = rng.choice(EYES)
    hat = "yok" if rarity == "yaygın" else rng.choice(sorted(h for h in HATS if h != "yok"))
    shiny = rng.random() < 0.01
    peak, dump = rng.sample(STATS, 2)
    stats = {}
    for s in STATS:
        if s == peak:
            stats[s] = min(100, floor + 50 + rng.randint(0, 30))
        elif s == dump:
            stats[s] = max(1, floor - 10 + rng.randint(0, 15))
        else:
            stats[s] = min(100, floor + rng.randint(0, 40))
    return Bones(species, rarity, eye, hat, shiny, stats)


# ------------------------------------------------------------------------------------------ durum
def state_path() -> Path:
    from alice.config import alice_home

    return alice_home() / "buddy.json"


def _load(path: Path | None = None) -> dict[str, Any]:
    try:
        data = json.loads((path or state_path()).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save(data: dict[str, Any], path: Path | None = None) -> None:
    path = path or state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(path)


def _clamp(v: float) -> float:
    return max(0.0, min(100.0, v))


def _night(ts: float) -> bool:
    return datetime.fromtimestamp(ts).hour < 7


def _decay(st: dict[str, Any], now: float) -> None:
    """Son güncellemeden bu yana geçen zamanın etkisi (saat saat)."""
    for k in ("food", "energy", "mood"):
        st.setdefault(k, 80.0)
    last = float(st.get("updated") or now)
    hours = max(0.0, min(72.0, (now - last) / 3600))
    if hours <= 0:
        st["updated"] = now
        return
    night_h = 0.0
    for i in range(int(math.ceil(hours))):
        if _night(last + i * 3600):
            night_h += min(1.0, hours - i)
    day_h = hours - night_h
    st["food"] = _clamp(st.get("food", 80) - 4 * hours)
    st["energy"] = _clamp(st.get("energy", 80) + 10 * night_h - 3 * day_h)
    low = 1.5 if st["food"] > 30 else 4.0  # açken keyfi daha hızlı kaçar
    st["mood"] = _clamp(st.get("mood", 80) - low * hours)
    st["updated"] = now


def level(xp: int) -> int:
    return int(math.sqrt(max(0, xp) / 5)) + 1


def _soul(st: dict[str, Any], bones: Bones, router: Any = None) -> None:
    if st.get("name"):
        return
    name, persona = "", ""
    if router is not None:
        try:
            top = max(bones.stats, key=bones.stats.get)
            res = router.chat("fast", [{"role": "user", "content": (
                f"Küçük bir {bones.species} maskot için Türkçe, sevimli, tek kelimelik bir ad ve tek cümlelik kişilik "
                f"yaz. En güçlü yanı {top}. Yalnız JSON: {{\"name\": \"...\", \"persona\": \"...\"}}")}],
                temperature=0.9, max_tokens=120)
            raw = (res.message.get("content") or "").strip()
            data = json.loads(raw[raw.find("{"): raw.rfind("}") + 1])
            name = str(data.get("name") or "").strip()[:20]
            persona = str(data.get("persona") or "").strip()[:200]
        except Exception:  # noqa: BLE001 - model yoksa listeden
            pass
    rng = random.Random(hashlib.sha256((user_seed() + "|ad").encode()).digest())
    st["name"] = name or rng.choice(NAMES)
    st["persona"] = persona or f"Meraklı ve biraz {'yaramaz' if bones.stats['KAOS'] > 50 else 'sakin'} bir {bones.species}."
    st["born"] = time.time()


def mood_word(st: dict[str, Any], now: float) -> str:
    if _night(now) and st.get("energy", 100) < 90:
        return "uyuyor"
    if st.get("food", 100) < 25:
        return "aç"
    if st.get("energy", 100) < 20:
        return "yorgun"
    m = st.get("mood", 50)
    return "çok mutlu" if m > 80 else "keyifli" if m > 55 else "durgun" if m > 30 else "küs"


def _face_eye(word: str, bones: Bones) -> str:
    return {"uyuyor": "-", "küs": ";", "aç": "°", "çok mutlu": "^"}.get(word, bones.eye)


def view(cfg: Any = None, path: Path | None = None, router: Any = None, now: float | None = None) -> dict[str, Any]:
    """Şu anki hâl (uygulama ve araç için). İlk çağrıda ruhu da oluşturur."""
    now = now or time.time()
    bones = roll()
    with _lock:
        st = _load(path)
        _decay(st, now)
        _soul(st, bones, router)
        _save(st, path)
    word = mood_word(st, now)
    say = st.get("say") or {}
    return {
        "name": st["name"], "persona": st.get("persona", ""), "species": bones.species, "rarity": bones.rarity,
        "stars": STARS[bones.rarity], "shiny": bones.shiny, "hat": bones.hat, "stats": bones.stats,
        "art": bones.art(_face_eye(word, bones)), "mood": word,
        "food": round(st["food"]), "energy": round(st["energy"]), "happy": round(st["mood"]),
        "xp": int(st.get("xp", 0)), "level": level(int(st.get("xp", 0))),
        "say": say.get("text", "") if now - float(say.get("ts", 0)) < 90 else "",
        "say_ts": say.get("ts", 0), "enabled": bool(settings(cfg).get("enabled", True)) if cfg is not None else True,
    }


def act(action: str, value: str = "", path: Path | None = None, now: float | None = None) -> str:
    """pet | feed | rename. Kısa sonuç cümlesi."""
    now = now or time.time()
    with _lock:
        st = _load(path)
        _decay(st, now)
        _soul(st, roll())
        name = st["name"]
        if action == "pet":
            st["mood"] = _clamp(st["mood"] + 15)
            st["pets"] = int(st.get("pets", 0)) + 1
            out = f"{name} mırıldandı, keyfi yerine geldi."
        elif action == "feed":
            if now - float(st.get("fed", 0)) < 30 * 60:
                out = f"{name} tok, az önce yedi."
            else:
                st["food"] = _clamp(st["food"] + 30)
                st["mood"] = _clamp(st["mood"] + 5)
                st["fed"] = now
                out = f"{name} afiyetle yedi."
        elif action == "rename":
            new = " ".join(value.split())[:20]
            if not new:
                return "yeni ad gerekli"
            st["name"] = new
            out = f"Artık adı {new}."
        else:
            return f"bilinmeyen işlem: {action}"
        _save(st, path)
    return out


def on_turn(cfg: Any, router: Any = None, user: str = "", reply: str = "", path: Path | None = None,
            now: float | None = None, chance: float = 0.35) -> str:
    """Bir konuşma turu bitti: doyur, deneyim ver; ara sıra kısa bir yorum yap. Yorumu döndürür ("" = sustu)."""
    s = settings(cfg)
    if not s.get("enabled", True):
        return ""
    now = now or time.time()
    bones = roll()
    with _lock:
        st = _load(path)
        _decay(st, now)
        _soul(st, bones)
        st["food"] = _clamp(st["food"] + 6)
        st["mood"] = _clamp(st["mood"] + 2)
        st["xp"] = int(st.get("xp", 0)) + 1
        last_say = float((st.get("say") or {}).get("ts", 0))
        _save(st, path)
    if not s.get("chatter", True) or router is None or now - last_say < 8 * 60 or random.random() > chance:
        return ""
    word = mood_word(st, now)
    if word == "uyuyor":
        return ""
    try:
        res = router.chat("fast", [{"role": "user", "content": (
            f"Sen {st['name']} adında küçük bir {bones.species}sın; kişiliğin: {st.get('persona', '')}. Şu an "
            f"{word}sın. Sahibin ile asistanı arasındaki son konuşmaya en fazla 10 kelimelik, Türkçe, sevimli bir "
            "yan yorum yap. Tavsiye verme, soru sorma, emoji kullanma. Yalnız yorumu yaz.\n\n"
            f"Sahibi: {user[:400]}\nAsistan: {reply[:400]}")}], temperature=0.9, max_tokens=40)
        text = " ".join((res.message.get("content") or "").split()).strip('"')[:90]
    except Exception:  # noqa: BLE001
        return ""
    if not text:
        return ""
    with _lock:
        st = _load(path)
        st["say"] = {"text": text, "ts": now}
        _save(st, path)
    return text


def card(cfg: Any = None, path: Path | None = None) -> str:
    """Metin olarak kart (araç ve CLI için)."""
    v = view(cfg, path)
    stats = "  ".join(f"{k} {n}" for k, n in v["stats"].items())
    return "\n".join(v["art"]) + (
        f"\n{v['name']} — {v['rarity']} {v['species']} {v['stars']}{' ✨parlak' if v['shiny'] else ''}"
        f"\nSeviye {v['level']} · {v['mood']} · tokluk {v['food']} · enerji {v['energy']} · keyif {v['happy']}"
        f"\n{stats}\n{v['persona']}")
