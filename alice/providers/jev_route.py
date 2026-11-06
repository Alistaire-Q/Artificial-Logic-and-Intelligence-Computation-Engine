"""Jev ile otomatik model yönlendirme: her mesajda hangi model katmanı?

Ölçüm sıralaması tek bir "en iyi model" listesi çıkarıyor; ağır işte de "naber"de de aynı modeller deneniyordu ve
abonelikle gelen güçlü modeller (Claude Code proxy'sindeki Opus 5.5) kota harcamasın diye hiç sıraya girmiyordu.
Burada Jev (TypeSafe System One) mesaj hakkında birkaç küçük yargı veriyor; hangi katmana gidileceğine kod karar
veriyor (aşağıdaki doğruluk tablosu):

  strong  kod yazma/düzeltme, derin düşünme ya da çok adımlı bilgisayar işi -> [routing] strong + beyin zinciri
  light   sohbet ya da kısa soru, bilgisayarda en fazla tek iş             -> hızlı (fast) zincir + beyin zinciri
  normal  geri kalan her şey ve Jev'e ulaşılamayan her durum                -> beyin zinciri (bugünkü davranış)

Sorular, ölçütler ve eşikler sadece bu dosyada. Jev çağrısı başarısız olursa bu bir cevap sayılmaz: "normal"e
düşülür ve olay günlüğüne yazılır.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from alice.decider import Decision, JevEngine, Noul, Score

ASSISTANT = ("ALICE: the user's personal assistant on their Windows PC. It chats in Turkish and can use tools "
             "on the computer (open apps and websites, play music, read and edit files, run commands).")

QUESTIONS: dict[str, Any] = {
    "code": Noul(
        "Does `request` ask the assistant to write, change, fix or review program code?",
        {"true": "The user wants code written, a script changed, or a bug in a program found and fixed.",
         "false": "The user wants something that is not programming, such as opening a website, chatting, or a "
                  "general question."}),
    "thinking": Score(
        "How much careful thinking does a good answer to `request` need?",
        {1: "A greeting, thanks, or small talk.",
         2: "A quick fact or a short everyday answer.",
         3: "Explaining or comparing a known topic, such as how a tool works or how two products differ.",
         4: "Making a plan or a decision that depends on the user's own situation, deep analysis, hard math, "
            "or working out a tricky problem."}),
    "steps": Score(
        "How many separate actions on the computer does the assistant need to finish `request`?",
        {1: "No computer action; an answer in words is enough.",
         2: "One simple action, such as opening one app, one website, or one song.",
         3: "A few actions in a row, such as finding a file and then editing it.",
         4: "A long task with many actions, such as setting up a project or researching and writing a report."}),
    "chat": Noul(
        "Is `request` small talk with the assistant?",
        {"true": "A greeting, thanks, a joke, or asking how the assistant is.",
         "false": "The user asks for information or for something to be done."}),
}

# Doğruluk tablosunun eşikleri (Noul: 0.7 üstü evet, 0.3 altı hayır; Score: beklenen seviye 1-4).
YES, NO = 0.7, 0.3
STRONG_THINKING = 3.2   # bundan çok düşünme isterse güçlü model
STRONG_STEPS = 3.2      # bundan çok adımlıysa güçlü model
# Seviye 2 ("kısa bilgi / gündelik cevap") ve 2 ("tek basit iş") hafif modele yeter: eşikler 2 ile 3'ün ortası.
# (İlk denemede 1.8/2.3 idi; "Spotify'ı aç", "sesi kıs" gibi tek adımlık işler gereksiz yere beyne gidiyordu.)
LIGHT_THINKING = 2.5    # bundan az düşünme isterse hafif model yeter
LIGHT_STEPS = 2.5       # en fazla tek basit iş

TIMEOUT = 4.0  # sn: yönlendirme cevabı geciktirmesin; aşılırsa "normal"


@dataclass
class Route:
    tier: str  # strong | light | normal
    reason: str
    signals: dict[str, float] = field(default_factory=dict)
    latency: float = 0.0


def build_state(request: str, previous_reply: str = "") -> dict[str, Any]:
    state: dict[str, Any] = {"assistant": ASSISTANT, "request": request[:4000]}
    if previous_reply.strip():
        # "Evet, yap" gibi kısa cevaplar ancak bir önceki cevapla anlaşılır.
        state["previous_reply"] = previous_reply.strip()[:800]
    return state


def decide_tier(code: float, thinking: float, steps: float, chat: float) -> Route:
    """Jev'in yargılarından katman. Her birleşimin bir sonucu var; arada kalanlar "normal"."""
    signals = {"code": round(code, 2), "thinking": round(thinking, 2), "steps": round(steps, 2),
               "chat": round(chat, 2)}
    if code >= YES:
        return Route("strong", "kod işi", signals)
    if thinking >= STRONG_THINKING:
        return Route("strong", "derin düşünme", signals)
    if steps >= STRONG_STEPS:
        return Route("strong", "çok adımlı iş", signals)
    if code <= NO and steps <= LIGHT_STEPS and (chat >= YES or thinking <= LIGHT_THINKING):
        return Route("light", "sohbet" if chat >= YES else "kısa soru", signals)
    return Route("normal", "orta", signals)


def _signals(d: Decision) -> tuple[float, float, float, float]:
    return (float(d["code"].value), float(d["thinking"].value), float(d["steps"].value), float(d["chat"].value))


_engine: JevEngine | None = None
_broken_until = 0.0


def _get_engine(config) -> JevEngine | None:
    global _engine
    if _engine is None:
        dc = config.decider
        key = os.environ.get(dc.get("jev_api_key_env", "TYPESAFE_API_KEY"))
        if not key:
            return None
        import httpx

        _engine = JevEngine(dc.get("jev_url", "https://api.typesafe.ai/v1/systemone"),
                            dc.get("jev_model", "jev-latest"), key, name="jev-route",
                            client=httpx.Client(timeout=httpx.Timeout(TIMEOUT, connect=2.0)))
    return _engine


def route(config, request: str, previous_reply: str = "", engine: Any = None,
          log: Callable[[str], None] = lambda s: None) -> Route:
    """Mesajın katmanı. Jev yoksa ya da hata verirse "normal" (bugünkü beyin zinciri)."""
    global _broken_until
    if not request.strip():
        return Route("normal", "boş mesaj")
    eng = engine or _get_engine(config)
    if eng is None:
        return Route("normal", "TypeSafe anahtarı yok")
    if engine is None and time.monotonic() < _broken_until:
        return Route("normal", "jev geçici olarak kapalı")
    t0 = time.monotonic()
    try:
        d = eng.decide(build_state(request, previous_reply), QUESTIONS)
        r = decide_tier(*_signals(d))
    except Exception as e:  # noqa: BLE001 - hata bir cevap değildir: bugünkü davranışa dön, izini bırak
        if engine is None:
            _broken_until = time.monotonic() + float(getattr(e, "cooldown", 60.0) or 60.0)
        log(f"yönlendirme: jev başarısız ({str(e)[:160]}), normal zincir")
        return Route("normal", "jev hatası")
    r.latency = time.monotonic() - t0
    return r


def _dedupe(refs: list[str]) -> list[str]:
    out: list[str] = []
    for ref in refs:
        if ref and ref != "auto" and ref not in out:
            out.append(ref)
    return out


def voice_chain(router, config, request: str, fast: list[str],
                log: Callable[[str], None] = lambda s: None) -> tuple[list[str], Route]:
    """Sesli konuşmada think'in model sırası. Sesde hız önce: sadece "strong" katmanı değişir ve orada da
    [routing] voice_strong (varsayılan Sonnet 5.5) hızlı modellerin önüne geçer. Diğer her şey `fast` sırası.
    Yönlendirme kapalıysa ya da Jev'e ulaşılamazsa `fast` olduğu gibi döner."""
    rc = config.data.get("routing", {}) or {}
    if not rc.get("enabled") or not rc.get("voice", True):
        return fast, Route("normal", "yönlendirme kapalı")
    r = route(config, request, log=log)
    if r.tier != "strong":
        return fast, r
    head = [str(x) for x in rc.get("voice_strong") or [] if not _hidden(router, str(x))]
    return _dedupe(head + fast) or fast, r


def tier_chain(router, config, tier: str) -> list[str]:
    """Katmanın model sırası: katmanın modelleri önce, sonra beyin zinciri (hiçbir iş yedeksiz kalmasın)."""
    rc = config.data.get("routing", {}) or {}
    brain = router.chain("brain")
    if tier == "strong":
        head = [str(r) for r in rc.get("strong") or []]
    elif tier == "light":
        head = [str(r) for r in rc.get("light") or []] or router.chain("fast")
    else:
        return brain
    # Arayüzden gizlenen model ya da grup (ör. "cliproxy:claude-*") yönlendirmede de kullanılmaz.
    return [r for r in _dedupe(head + brain) if not _hidden(router, r)]


def _hidden(router, ref: str) -> bool:
    check = getattr(router, "is_hidden", None)
    try:
        return bool(check(ref)) if callable(check) else False
    except Exception:  # noqa: BLE001
        return False
