"""OpenAI uyumlu /chat/completions istemcisi.

Gemini, OpenRouter, Groq, NVIDIA NIM, Cerebras ve Ollama hepsi bu formatı konuşuyor,
o yüzden tek bir istemci yeterli. Asıl iş hataları doğru sınıflandırmak: router
hangi hatada bekleyeceğine, hangisinde modeli/sağlayıcıyı tamamen atlayacağına
buna göre karar veriyor.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

from alice.config import ProviderConfig


class ProviderError(Exception):
    """kind: rate_limit | quota | auth | not_found | too_large | bad_request | server | network | unreachable"""

    def __init__(self, kind: str, message: str, status: int | None = None, retry_after: float | None = None):
        super().__init__(f"[{kind}] {message}")
        self.kind = kind
        self.status = status
        self.retry_after = retry_after


@dataclass
class ChatResult:
    message: dict[str, Any]
    provider: str
    model: str
    usage: dict[str, Any] = field(default_factory=dict)
    latency: float = 0.0

    @property
    def text(self) -> str:
        c = self.message.get("content")
        if isinstance(c, list):
            return "".join(p.get("text", "") for p in c if isinstance(p, dict))
        return c or ""

    @property
    def tool_calls(self) -> list[dict[str, Any]]:
        return self.message.get("tool_calls") or []


_QUOTA_HINTS = ("per day", "daily", "quota", "rpd", "tokens per day", "free-models-per-day", "exhausted")
_TOO_LARGE_HINTS = ("context length", "context_length", "maximum context", "too long", "too large",
                    "reduce the length", "request_too_large", "tokens per minute", "max_tokens")
_NOT_FOUND_HINTS = ("model not found", "does not exist", "no endpoints found", "not a valid model",
                    "model_not_found", "unknown model", "is not supported", "decommissioned", "deprecated")


def classify_error(status: int, body: str, headers: dict[str, str] | None = None) -> ProviderError:
    low = body.lower()
    retry_after = None
    if headers:
        ra = headers.get("retry-after") or headers.get("Retry-After")
        if ra:
            try:
                retry_after = float(ra)
            except ValueError:
                retry_after = None
    short = body[:400]
    if status in (401, 403):
        # OpenRouter bazı moderasyon/quota hatalarında da 403 döner.
        if any(h in low for h in _QUOTA_HINTS):
            return ProviderError("quota", short, status, retry_after)
        return ProviderError("auth", short, status)
    if status == 404:
        return ProviderError("not_found", short, status)
    if status == 413:
        return ProviderError("too_large", short, status)
    if status == 429:
        if any(h in low for h in _TOO_LARGE_HINTS) and "request" in low and "too large" in low:
            return ProviderError("too_large", short, status)
        if any(h in low for h in _QUOTA_HINTS):
            return ProviderError("quota", short, status, retry_after)
        return ProviderError("rate_limit", short, status, retry_after)
    if status in (400, 422):
        if any(h in low for h in _NOT_FOUND_HINTS):
            return ProviderError("not_found", short, status)
        if any(h in low for h in _TOO_LARGE_HINTS):
            return ProviderError("too_large", short, status)
        return ProviderError("bad_request", short, status)
    if status >= 500:
        return ProviderError("server", short, status, retry_after)
    return ProviderError("bad_request", short, status)


class OpenAICompatProvider:
    def __init__(self, cfg: ProviderConfig, timeout: float = 180.0, client: httpx.Client | None = None):
        self.cfg = cfg
        self.name = cfg.name
        self._client = client or httpx.Client(timeout=httpx.Timeout(timeout, connect=15.0))

    def _headers(self) -> dict[str, str]:
        h = {"Content-Type": "application/json", **self.cfg.headers}
        if self.cfg.api_key:
            h["Authorization"] = f"Bearer {self.cfg.api_key}"
        return h

    def chat(
        self,
        model: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        response_format: dict[str, Any] | None = None,
        extra: dict[str, Any] | None = None,
    ) -> ChatResult:
        body: dict[str, Any] = {"model": model, "messages": messages}
        if tools:
            body["tools"] = tools
            body["tool_choice"] = "auto"
        if temperature is not None:
            body["temperature"] = temperature
        if max_tokens is not None:
            body["max_tokens"] = max_tokens
        if response_format is not None:
            body["response_format"] = response_format
        if extra:
            body.update(extra)

        t0 = time.monotonic()
        try:
            r = self._client.post(f"{self.cfg.base_url}/chat/completions", headers=self._headers(), json=body)
        except httpx.TimeoutException as e:
            raise ProviderError("network", f"timeout: {e}") from e
        except httpx.ConnectError as e:
            # Sunucu hiç yok (ör. Ollama kapalı): uzun süre tekrar deneme.
            raise ProviderError("unreachable", str(e)) from e
        except httpx.HTTPError as e:
            raise ProviderError("network", str(e)) from e
        latency = time.monotonic() - t0

        if r.status_code != 200:
            raise classify_error(r.status_code, r.text, dict(r.headers))
        try:
            data = r.json()
        except json.JSONDecodeError as e:
            raise ProviderError("server", f"geçersiz JSON: {r.text[:200]}") from e

        # Bazı sağlayıcılar (OpenRouter) hatayı 200 içinde döndürür.
        if isinstance(data, dict) and data.get("error") and not data.get("choices"):
            err = data["error"]
            code = err.get("code") if isinstance(err, dict) else None
            msg = json.dumps(err)[:400]
            status = code if isinstance(code, int) else 500
            raise classify_error(status, msg)

        choices = data.get("choices") or []
        if not choices:
            raise ProviderError("server", f"boş cevap: {str(data)[:200]}")
        msg = choices[0].get("message") or {}
        if not msg.get("content") and not msg.get("tool_calls"):
            if choices[0].get("finish_reason") == "length":
                raise ProviderError("too_large", "cevap max_tokens'a takıldı, içerik yok")
        return ChatResult(
            message=normalize_message(msg),
            provider=self.name,
            model=data.get("model") or model,
            usage=data.get("usage") or {},
            latency=latency,
        )

    def list_models(self) -> list[dict[str, Any]]:
        r = self._client.get(f"{self.cfg.base_url}/models", headers=self._headers())
        if r.status_code != 200:
            raise classify_error(r.status_code, r.text, dict(r.headers))
        data = r.json()
        return data.get("data", data if isinstance(data, list) else [])


def normalize_message(msg: dict[str, Any]) -> dict[str, Any]:
    """Sağlayıcıya özel alanları atıp geçmişte güvenle tutulabilecek bir assistant mesajı üret."""
    out: dict[str, Any] = {"role": "assistant", "content": msg.get("content") or ""}
    calls = []
    for i, tc in enumerate(msg.get("tool_calls") or []):
        fn = tc.get("function") or {}
        args = fn.get("arguments")
        if isinstance(args, dict):
            args = json.dumps(args, ensure_ascii=False)
        calls.append({
            "id": tc.get("id") or f"call_{int(time.time() * 1000)}_{i}",
            "type": "function",
            "function": {"name": fn.get("name", ""), "arguments": args or "{}"},
        })
    if calls:
        out["tool_calls"] = calls
    return out
