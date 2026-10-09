"""Rol tabanlı model router.

Her rol (brain, fast, vision) sıralı bir "provider:model" listesine sahip. İstek
listedeki ilk uygun modele gider; hata türüne göre:

  rate_limit -> o model kısa süre soğumaya alınır (retry-after ya da 20 sn)
  quota      -> o model 1 saat soğumaya alınır (günlük limit dolmuş)
  auth       -> sağlayıcı oturum boyunca kapatılır (key yanlış/eksik)
  not_found  -> model oturum boyunca kapatılır (katalogdan kalkmış)
  too_large  -> sadece bu istek için atlanır (context o sağlayıcıya sığmıyor)
  bad_request + tools -> model bu oturumda tool'suz işlerde kullanılır
  server/network -> 30 sn soğuma
  unreachable -> 10 dk soğuma (sunucu yok, ör. Ollama kapalı)

Ücretsiz katmanlar sürekli değiştiği için tek bir sağlayıcıya güvenmek yerine
zinciri bu şekilde dolaşmak sistemin ayakta kalmasını sağlıyor.
"""

from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, Callable, Protocol

from alice.config import Config, ProviderConfig, alice_home
from alice.providers.openai_compat import ChatResult, OpenAICompatProvider, ProviderError, StreamCancelled

COOLDOWNS = {"rate_limit": 20.0, "quota": 3600.0, "server": 30.0, "network": 30.0, "unreachable": 600.0}


def parse_ref(ref: str) -> tuple[str, str]:
    provider, sep, model = ref.partition(":")
    if not sep or not model:
        raise ValueError(f"geçersiz model referansı: {ref!r} (beklenen 'provider:model')")
    return provider, model


def estimate_tokens(messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None = None) -> int:
    chars = 0
    images = 0
    for m in messages:
        c = m.get("content")
        if isinstance(c, str):
            chars += len(c)
        elif isinstance(c, list):
            for p in c:
                if p.get("type") == "text":
                    chars += len(p.get("text", ""))
                elif p.get("type") == "image_url":
                    images += 1
        for tc in m.get("tool_calls") or []:
            chars += len(tc["function"].get("arguments", "")) + 30
    if tools:
        chars += len(json.dumps(tools))
    return int(chars / 3.5) + images * 1100


def strip_images(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = []
    for m in messages:
        c = m.get("content")
        if isinstance(c, list):
            texts = []
            for p in c:
                if p.get("type") == "text":
                    texts.append(p.get("text", ""))
                elif p.get("type") == "image_url":
                    texts.append("[görüntü: bu model görsel desteklemiyor, atlandı]")
            m = {**m, "content": "\n".join(texts)}
        out.append(m)
    return out


# Gemini'nin belgelediği yer tutucu imza: başka bir modelin yaptığı ya da imzası kaybolmuş çağrılar için.
GEMINI_DUMMY_SIGNATURE = "skip_thought_signature_validator"


def is_gemini(pc: ProviderConfig) -> bool:
    return "generativelanguage.googleapis.com" in pc.base_url or pc.name == "gemini"


def clean_messages(messages: list[dict[str, Any]], gemini: bool = False) -> list[dict[str, Any]]:
    """ALICE'in iç işaretlerini ("_" ile başlayan anahtarlar) sağlayıcıya göndermeden önce temizle.
    Gemini'ye giderken her tool_call'a thought_signature eklenir (yoksa Gemini 3 isteği 400 ile reddediyor)."""
    out = []
    for m in messages:
        m2 = {k: v for k, v in m.items() if not k.startswith("_")}
        if m.get("tool_calls"):
            calls = []
            for tc in m["tool_calls"]:
                tc2 = {k: v for k, v in tc.items() if not k.startswith("_")}
                if gemini:
                    tc2["extra_content"] = tc.get("_extra_content") or {
                        "google": {"thought_signature": GEMINI_DUMMY_SIGNATURE}}
                calls.append(tc2)
            m2["tool_calls"] = calls
        out.append(m2)
    return out


class StreamSink(Protocol):
    def on_delta(self, text: str) -> None: ...
    def on_reset(self) -> None: ...
    def cancelled(self) -> bool: ...


class AllProvidersFailed(Exception):
    def __init__(self, role: str, errors: list[str]):
        super().__init__(f"'{role}' rolündeki hiçbir model cevap veremedi:\n  " + "\n  ".join(errors))
        self.errors = errors


@dataclass
class UsageTracker:
    path: Path
    data: dict[str, Any] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def __post_init__(self) -> None:
        try:
            self.data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            self.data = {}

    def record(self, ref: str, usage: dict[str, Any], ok: bool) -> None:
        with self._lock:
            day = self.data.setdefault(date.today().isoformat(), {})
            row = day.setdefault(ref, {"requests": 0, "errors": 0, "prompt_tokens": 0, "completion_tokens": 0})
            if ok:
                row["requests"] += 1
                row["prompt_tokens"] += int(usage.get("prompt_tokens") or 0)
                row["completion_tokens"] += int(usage.get("completion_tokens") or 0)
            else:
                row["errors"] += 1
            # Sadece son 14 günü tut.
            for k in sorted(self.data)[:-14]:
                del self.data[k]
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                self.path.write_text(json.dumps(self.data, indent=1), encoding="utf-8")
            except OSError:
                pass

    def today(self) -> dict[str, Any]:
        return self.data.get(date.today().isoformat(), {})


class Router:
    def __init__(
        self,
        config: Config,
        provider_factory: Callable[[ProviderConfig], Any] | None = None,
        usage_path: Path | None = None,
        sleep: Callable[[float], None] = time.sleep,
        max_wait: float = 65.0,
        on_event: Callable[[str], None] | None = None,
    ):
        self.config = config
        self.providers_cfg = config.providers()
        self._factory = provider_factory or (lambda pc: OpenAICompatProvider(pc))
        self._clients: dict[str, Any] = {}
        self.cooldown_until: dict[str, float] = {}
        self.cooldown_kind: dict[str, str] = {}
        self.dead_models: set[str] = set()
        self.dead_providers: set[str] = set()
        self.no_tools: set[str] = set()
        self.size_limit: dict[str, int] = {}
        self.usage = UsageTracker(usage_path or alice_home() / "usage.json")
        self.sleep = sleep
        self.max_wait = max_wait
        self.on_event = on_event or (lambda s: None)
        self.last_ref: dict[str, str] = {}
        self.role_overrides: dict[str, list[str]] = {}

    # ------------------------------------------------------------------ helpers
    def chain(self, role: str) -> list[str]:
        if role in self.role_overrides:
            return self.role_overrides[role]
        if ":" in role and role.split(":", 1)[0] in self.providers_cfg:
            return [role]  # doğrudan model referansı
        chain = self.config.roles.get(role)
        if chain is None:
            raise KeyError(f"tanımsız rol: {role}")
        return list(chain)

    def client(self, provider: str) -> Any:
        if provider not in self._clients:
            self._clients[provider] = self._factory(self.providers_cfg[provider])
        return self._clients[provider]

    def supports_vision(self, ref: str) -> bool:
        p, m = parse_ref(ref)
        pc = self.providers_cfg.get(p)
        return bool(pc and pc.supports_vision(m))

    def available(self, ref: str, now: float | None = None) -> tuple[bool, str]:
        now = time.monotonic() if now is None else now
        try:
            p, _ = parse_ref(ref)
        except ValueError as e:
            return False, str(e)
        pc = self.providers_cfg.get(p)
        if pc is None:
            return False, "bilinmeyen sağlayıcı"
        if not pc.enabled:
            return False, f"{pc.api_key_env} yok"
        if p in self.dead_providers:
            return False, "sağlayıcı kapalı (auth)"
        if ref in self.dead_models:
            return False, "model bulunamadı"
        until = self.cooldown_until.get(ref, 0.0)
        if until > now:
            return False, f"soğumada ({int(until - now)} sn)"
        return True, "hazır"

    def first_available(self, role: str) -> str | None:
        for ref in self.chain(role):
            if self.available(ref)[0]:
                return ref
        return None

    # --------------------------------------------------------------------- chat
    def chat(
        self,
        role: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        response_format: dict[str, Any] | None = None,
        stream: "StreamSink | None" = None,
    ) -> ChatResult:
        """stream verilirse cevap parça parça stream.on_delta'ya akar. Bir model akışın ortasında
        düşerse stream.on_reset() çağrılır ve sıradaki model baştan dener."""
        chain = self.chain(role)
        est = estimate_tokens(messages, tools)
        attempts = 0
        while True:
            errors: list[str] = []
            now = time.monotonic()
            for ref in chain:
                if stream is not None and stream.cancelled():
                    raise StreamCancelled()
                ok, why = self.available(ref, now)
                if not ok:
                    errors.append(f"{ref}: {why}")
                    continue
                p, model = parse_ref(ref)
                pc = self.providers_cfg[p]
                if est > pc.max_input_tokens:
                    errors.append(f"{ref}: istek ~{est} token, sağlayıcı sınırı {pc.max_input_tokens}")
                    continue
                if tools and ref in self.no_tools:
                    errors.append(f"{ref}: tool desteklemiyor")
                    continue
                if est > self.size_limit.get(ref, 10**9):
                    errors.append(f"{ref}: istek ~{est} token, bu model daha önce ~{self.size_limit[ref]} üstünü reddetti")
                    continue
                msgs = clean_messages(messages if pc.supports_vision(model) else strip_images(messages),
                                      gemini=is_gemini(pc))
                client = self.client(p)
                kw = dict(tools=tools, temperature=temperature, max_tokens=max_tokens,
                          response_format=response_format, extra=self.config.model_extra(ref) or None)
                emitted = [False]
                try:
                    if stream is not None and hasattr(client, "chat_stream"):
                        def on_delta(piece: str, _flag=emitted, _sink=stream) -> None:
                            _flag[0] = True
                            _sink.on_delta(piece)

                        res = client.chat_stream(model, msgs, on_delta, cancel=stream.cancelled,
                                                 on_reasoning=getattr(stream, "on_reasoning", None), **kw)
                        if not res.usage:
                            res.usage = {"prompt_tokens": est, "completion_tokens": int(len(res.text) / 3.5)}
                    else:
                        res = client.chat(model, msgs, **kw)
                        if stream is not None and res.text:
                            stream.on_delta(res.text)
                except ProviderError as e:
                    if emitted[0]:
                        stream.on_reset()
                    self.usage.record(ref, {}, ok=False)
                    errors.append(f"{ref}: {e}")
                    self._penalize(ref, p, e, tools is not None, response_format is not None, est)
                    continue
                self.usage.record(ref, res.usage, ok=True)
                if self.last_ref.get(role) != ref:
                    self.on_event(f"{role} -> {ref}")
                self.last_ref[role] = ref
                return res

            # Hepsi başarısız. Sadece rate limit yüzünden kısa süre kapalı bir model varsa bekle.
            waits = [u - time.monotonic() for r, u in self.cooldown_until.items()
                     if r in chain and self.cooldown_kind.get(r) == "rate_limit"]
            waits = [w for w in waits if w > 0]
            attempts += 1
            if waits and min(waits) <= self.max_wait and attempts <= 3:
                w = min(waits) + 0.5
                self.on_event(f"tüm modeller meşgul, {w:.0f} sn bekleniyor")
                self.sleep(w)
                continue
            raise AllProvidersFailed(role, errors)

    def _penalize(self, ref: str, provider: str, e: ProviderError, had_tools: bool, had_format: bool,
                  est: int = 0) -> None:
        now = time.monotonic()
        low = str(e).lower()
        if e.kind == "auth":
            self.dead_providers.add(provider)
        elif e.kind == "not_found":
            self.dead_models.add(ref)
        elif e.kind == "too_large":
            # Ör. Groq ücretsiz katmanı dakikada 8K token: bu büyüklükteki istekleri bir daha bu modele yollama.
            if est:
                self.size_limit[ref] = min(self.size_limit.get(ref, 10**9), int(est * 0.9))
        elif e.kind == "bad_request":
            if had_tools and "thought_signature" not in low and ("tool" in low or "function" in low):
                self.no_tools.add(ref)
            elif not had_format or had_tools:
                self.cooldown_until[ref] = now + 60
        else:
            base = COOLDOWNS.get(e.kind, 30.0)
            wait = e.retry_after if (e.retry_after and e.kind == "rate_limit") else base
            self.cooldown_until[ref] = now + min(max(wait, 1.0), 6 * 3600)
            self.cooldown_kind[ref] = e.kind

    def status(self) -> dict[str, list[tuple[str, str]]]:
        roles = set(self.config.roles) | set(self.role_overrides)
        return {role: [(ref, self.available(ref)[1]) for ref in self.chain(role)] for role in sorted(roles)}
