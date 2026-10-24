"""System One karar motoru (Jev ve yedeği).

Jev metin üretmez; bir durumu (state) ve 1-8 adet tipli soruyu alır, her soru için
etiket + olasılık döndürür. Bilgisayar kullanımında işe yarayan kısım şu: ekrandaki
tıklanabilir şeyleri ID'li aday listesine çevirirsin, Jev ~100 ms'de hangisinin
seçileceğini söyler, sen de o adayı uygularsın. Uzun metin üretimi ve planlama
yine büyük modelde (System 2) kalır.

Soru tipleri (Jev ile aynı isimler):
  Choice(instructions, criteria={etiket: açıklama})  -> tek etiket
  Noul(instructions)                                   -> 0..1 arası "evet" olasılığı
  Score(instructions, levels={1: "...", 5: "..."})     -> seviye (beklenen değer)

Zincir: config [decider].chain, örn. ["jev", "jev-openrouter", "llm:fast"]. Jev anahtarı yoksa ya da
hata verirse aynı soru ücretsiz hızlı bir LLM'e JSON formatında sorulur.
"""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass, field
from typing import Any, Protocol

import httpx

from alice.config import Config
from alice.providers.router import AllProvidersFailed, Router


@dataclass
class Choice:
    instructions: str
    criteria: dict[str, str]


@dataclass
class Noul:
    instructions: str


@dataclass
class Score:
    instructions: str
    levels: dict[int, str]


Question = Choice | Noul | Score


@dataclass
class Answer:
    type: str
    value: Any
    confidence: float
    probabilities: dict[str, float] = field(default_factory=dict)


@dataclass
class Decision:
    answers: dict[str, Answer]
    engine: str
    latency: float

    def __getitem__(self, key: str) -> Answer:
        return self.answers[key]


class DeciderError(Exception):
    def __init__(self, message: str, cooldown: float = 60.0):
        super().__init__(message)
        self.cooldown = cooldown


class Engine(Protocol):
    name: str

    def decide(self, state: str, questions: dict[str, Question]) -> Decision: ...


def _validate(questions: dict[str, Question]) -> None:
    if not 1 <= len(questions) <= 8:
        raise ValueError("System One 1 ile 8 arasında soru alır")
    for k, q in questions.items():
        if isinstance(q, Choice) and len(q.criteria) < 2:
            raise ValueError(f"{k}: Choice en az 2 seçenek ister")


def _score_as_choice(q: Score) -> Choice:
    return Choice(q.instructions, {str(k): v for k, v in sorted(q.levels.items())})


def _expected(probs: dict[str, float]) -> float:
    total = sum(probs.values()) or 1.0
    return sum(float(k) * v for k, v in probs.items()) / total


# --------------------------------------------------------------------------- Jev
class JevEngine:
    """TypeSafe Jev REST istemcisi: POST {jev_url} {model, state, questions}.

    Score soruları güvenli olsun diye Jev'e Choice olarak gönderilir (seviyeler
    etiket olur) ve beklenen değer olasılıklardan hesaplanır; Choice ve Noul
    formatı resmi quickstart ile aynıdır. Cevap ayrıştırıcı toleranslıdır çünkü
    API hâlâ erken erişimde ve alan adları değişebilir.
    """

    def __init__(self, url: str, model: str, api_key: str, client: httpx.Client | None = None, name: str = "jev"):
        self.name = name
        self.url = url
        self.model = model
        self.api_key = api_key
        self._client = client or httpx.Client(timeout=httpx.Timeout(20.0, connect=5.0))

    def close(self) -> None:
        self._client.close()

    def decide(self, state: str, questions: dict[str, Question]) -> Decision:
        _validate(questions)
        wire: dict[str, Any] = {}
        for k, q in questions.items():
            if isinstance(q, Noul):
                wire[k] = {"type": "noul", "instructions": q.instructions}
            else:
                c = _score_as_choice(q) if isinstance(q, Score) else q
                wire[k] = {"type": "choice", "instructions": c.instructions, "criteria": c.criteria}
        t0 = time.monotonic()
        try:
            r = self._client.post(
                self.url,
                headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
                json={"model": self.model, "state": state, "questions": wire},
            )
        except httpx.HTTPError as e:
            raise DeciderError(f"jev ağ hatası: {e}") from e
        if r.status_code != 200:
            # 402 = kredi yok (OpenRouter), 401/403 = anahtar/erişim yok: bunlar bir dakikada düzelmez.
            cooldown = 3600.0 if r.status_code in (401, 402, 403) else 60.0
            raise DeciderError(f"{self.name} HTTP {r.status_code}: {r.text[:300]}", cooldown)
        data = r.json()
        raw = data.get("answers", data)
        answers = {k: self._parse(q, raw.get(k)) for k, q in questions.items()}
        return Decision(answers, self.name, time.monotonic() - t0)

    @staticmethod
    def _parse(q: Question, a: Any) -> Answer:
        if a is None:
            raise DeciderError("jev cevabında soru eksik")
        if isinstance(q, Noul):
            if isinstance(a, (int, float)):
                p = float(a)
            else:
                p = float(next(a[k] for k in ("value", "probability", "answer", "noul") if k in a))
            return Answer("noul", p, max(p, 1 - p), {"yes": p, "no": 1 - p})
        probs = {str(k): float(v) for k, v in (a.get("probabilities") or {}).items()} if isinstance(a, dict) else {}
        label = None
        if isinstance(a, dict):
            for key in ("choice", "label", "value", "answer", "selected"):
                if key in a and not isinstance(a[key], (dict, list)):
                    label = str(a[key])
                    break
        elif isinstance(a, str):
            label = a
        if label is None and probs:
            label = max(probs, key=probs.get)
        if label is None:
            raise DeciderError(f"jev cevabı çözülemedi: {a!r}"[:200])
        conf = float(a.get("confidence", probs.get(label, 0.0))) if isinstance(a, dict) else probs.get(label, 0.0)
        if isinstance(q, Score):
            value = _expected(probs) if probs else float(label)
            return Answer("score", value, conf, probs)
        return Answer("choice", label, conf, probs)


# ------------------------------------------------------------------ LLM emülasyonu
_SYSTEM = (
    "You are a System One decision engine. You never explain, you only decide. "
    "Read the STATE, answer every QUESTION and reply with ONE JSON object only, no prose, no code fences."
)


class LLMEngine:
    """Jev'in ücretsiz yedeği: hızlı bir LLM'e aynı soruları JSON olarak sorar."""

    def __init__(self, router: Router, role: str = "fast"):
        self.router = router
        self.role = role
        self.name = f"llm:{role}"

    def decide(self, state: str, questions: dict[str, Question]) -> Decision:
        _validate(questions)
        lines = []
        shape = {}
        for k, q in questions.items():
            if isinstance(q, Choice):
                opts = "\n".join(f'    "{lab}": {desc}' for lab, desc in q.criteria.items())
                lines.append(f'- "{k}" (choice): {q.instructions}\n  Options:\n{opts}')
                shape[k] = {"choice": "<one option key>", "confidence": "<0..1>"}
            elif isinstance(q, Score):
                lv = "\n".join(f"    {lev}: {desc}" for lev, desc in sorted(q.levels.items()))
                lines.append(f'- "{k}" (score): {q.instructions}\n  Levels:\n{lv}')
                shape[k] = {"score": "<level number>", "confidence": "<0..1>"}
            else:
                lines.append(f'- "{k}" (yes/no): {q.instructions}')
                shape[k] = {"yes_probability": "<0..1>"}
        user = (
            f"STATE:\n{state}\n\nQUESTIONS:\n" + "\n".join(lines)
            + f"\n\nReply exactly in this JSON shape:\n{json.dumps(shape)}"
        )
        msgs = [{"role": "system", "content": _SYSTEM}, {"role": "user", "content": user}]
        t0 = time.monotonic()
        try:
            res = self.router.chat(self.role, msgs, temperature=0, max_tokens=600,
                                   response_format={"type": "json_object"})
        except AllProvidersFailed:
            # Bazı modeller response_format'ı reddediyor, bir de onsuz dene.
            res = self.router.chat(self.role, msgs, temperature=0, max_tokens=600)
        data = extract_json(res.text)
        if not isinstance(data, dict):
            raise DeciderError(f"LLM decider JSON döndürmedi: {res.text[:200]}")
        answers = {k: self._parse(q, data.get(k)) for k, q in questions.items()}
        return Decision(answers, f"{self.name} ({res.provider}:{res.model})", time.monotonic() - t0)

    @staticmethod
    def _parse(q: Question, a: Any) -> Answer:
        if isinstance(q, Noul):
            p = a.get("yes_probability", a.get("value")) if isinstance(a, dict) else a
            if isinstance(p, bool):
                p = 1.0 if p else 0.0
            p = _num(p, 0.5)
            return Answer("noul", p, max(p, 1 - p), {"yes": p, "no": 1 - p})
        if isinstance(a, dict):
            raw = a.get("choice", a.get("score", a.get("value")))
            conf = _num(a.get("confidence"), 0.6)
        else:
            raw, conf = a, 0.6
        if isinstance(q, Score):
            v = _num(raw, None)
            if v is None:
                raise DeciderError(f"score çözülemedi: {a!r}")
            return Answer("score", v, conf, {})
        label = str(raw).strip() if raw is not None else ""
        if label not in q.criteria:
            # Model açıklamayı ya da tırnaklı hali döndürmüş olabilir.
            norm = {k.lower(): k for k in q.criteria}
            label = norm.get(label.lower().strip('"\''), "")
            if not label:
                raise DeciderError(f"geçersiz seçenek: {raw!r}")
        return Answer("choice", label, conf, {label: conf})


def _num(v: Any, default: float | None) -> float | None:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return default
    return min(max(f, 0.0), 1.0) if default is not None and default <= 1 else f


def extract_json(text: str) -> Any:
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    m = re.search(r"\{.*\}", text, re.S)
    if m:
        try:
            return json.loads(m.group(0))
        except json.JSONDecodeError:
            return None
    return None


# ------------------------------------------------------------------------ zincir
class Decider:
    def __init__(self, engines: list[Engine], on_event=None):
        if not engines:
            raise ValueError("en az bir karar motoru gerekli")
        self.engines = engines
        self.on_event = on_event or (lambda s: None)
        self._broken: dict[str, float] = {}

    @classmethod
    def from_config(cls, config: Config, router: Router, on_event=None) -> "Decider":
        dc = config.decider
        engines: list[Engine] = []
        for item in dc.get("chain", ["llm:fast"]):
            if item == "jev":
                key = os.environ.get(dc.get("jev_api_key_env", "TYPESAFE_API_KEY"))
                if key:
                    engines.append(JevEngine(dc.get("jev_url"), dc.get("jev_model", "jev-latest"), key))
            elif item == "jev-openrouter":
                # Aynı Jev, OpenRouter hesabından faturalanır. Ücretsiz (:free) sürümü yok, kredi gerekir.
                key = os.environ.get(dc.get("openrouter_jev_api_key_env", "OPENROUTER_API_KEY"))
                if key:
                    engines.append(JevEngine(dc.get("openrouter_jev_url", "https://openrouter.ai/api/v1/systemone"),
                                             dc.get("openrouter_jev_model", "~typesafe/jev-latest"), key,
                                             name="jev-openrouter"))
            elif item.startswith("llm:"):
                engines.append(LLMEngine(router, item.split(":", 1)[1]))
        if not engines:
            engines.append(LLMEngine(router, "fast"))
        return cls(engines, on_event)

    @property
    def names(self) -> list[str]:
        return [e.name for e in self.engines]

    def close(self) -> None:
        """Kendi HTTP istemcisi olan motorları kapat (yönlendirici paylaşımlı; onu sahibi kapatır)."""
        for e in self.engines:
            close = getattr(e, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:  # noqa: BLE001
                    pass

    def decide(self, state: str, questions: dict[str, Question]) -> Decision:
        # Geçersiz soru çağıranın hatası: motorlara gitmeden düşsün. Önceden her motor bu yüzden "bozuk" sayılıp
        # soğumaya alınıyordu ve sonraki geçerli sorular da bir dakika boyunca cevapsız kalıyordu.
        _validate(questions)
        errors = []
        for e in self.engines:
            if self._broken.get(e.name, 0) > time.monotonic():
                continue
            try:
                return e.decide(state, questions)
            except Exception as err:  # noqa: BLE001 - beklenmeyen cevap (JSON değil, eksik anahtar) da sıradaki motora geçsin
                errors.append(f"{e.name}: {err}")
                self.on_event(f"decider {e.name} başarısız, sıradakine geçiliyor")
                self._broken[e.name] = time.monotonic() + getattr(err, "cooldown", 60.0)
        raise DeciderError("; ".join(errors) or "kullanılabilir karar motoru yok")
