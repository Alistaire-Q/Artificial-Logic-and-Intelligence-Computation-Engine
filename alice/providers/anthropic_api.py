"""Resmi Claude API (console.anthropic.com anahtarı) — resmi `anthropic` paketiyle.

ALICE'in geri kalanı OpenAI biçiminde mesajlarla çalışıyor (role/content/tool_calls/tool). Bu istemci o mesajları
Claude'un Messages biçimine çevirir, cevabı geri çevirir; router için diğer sağlayıcılarla aynı arayüzü verir
(chat, chat_stream, list_models). Tekrar deneme, soğuma ve anahtar değiştirme router'da ve anahtar havuzunda
yapıldığı için paketin kendi tekrar denemesi kapalı.

Notlar:
- Düşünme her zaman uyarlamalı (adaptive); özeti on_reasoning'e akar. Düşünme blokları sonraki turlara geri
  gönderilmez: ALICE geçmişi kısaltıp düzenlediği için düzenlenmiş geçmişteki blok isteği reddettirebilirdi.
- Güvenlik sınıflandırıcısı isteği geri çevirirse API aynı isteği başka bir Claude modeliyle sürdürür
  (fallbacks="default"); o da reddederse hata router'a gider, sıradaki modele geçilir.
"""

from __future__ import annotations

import base64
import json
import time
from typing import Any, Callable

from alice.config import ProviderConfig
from alice.providers.openai_compat import ChatResult, ProviderError, StreamCancelled, classify_error, redact

FALLBACK_BETA = "server-side-fallback-2026-07-01"
# Sunucu tarafı yedeği destekleyen modeller (Haiku'da yok).
FALLBACK_MODELS = ("claude-opus-5-5", "claude-opus-5", "claude-fable-5-1", "claude-sonnet-5-5")
EFFORTS = ("low", "medium", "high", "xhigh", "max")
# Akış içi "error" olayının türü -> HTTP karşılığı (sınıflandırma için).
_STREAM_ERROR_STATUS = {"overloaded_error": 529, "rate_limit_error": 429, "request_too_large": 413,
                        "authentication_error": 401, "permission_error": 403, "not_found_error": 404,
                        "billing_error": 402, "invalid_request_error": 400, "api_error": 500}


def _sdk() -> Any:
    try:
        import anthropic
    except ImportError as e:
        raise ProviderError("auth", "Claude API için anthropic paketi gerekli: pip install anthropic") from e
    return anthropic


# ------------------------------------------------------------------------------------------- mesaj çevirisi
def _image_block(url: str) -> dict[str, Any] | None:
    if url.startswith("data:"):
        head, _, data = url.partition(",")
        media = head[5:].split(";")[0] or "image/png"
        if ";base64" not in head:
            data = base64.b64encode(data.encode("utf-8")).decode("ascii")
        return {"type": "image", "source": {"type": "base64", "media_type": media, "data": data}}
    if url.startswith(("http://", "https://")):
        return {"type": "image", "source": {"type": "url", "url": url}}
    return None


def _user_content(content: Any) -> list[dict[str, Any]]:
    if isinstance(content, str):
        return [{"type": "text", "text": content}] if content else []
    out: list[dict[str, Any]] = []
    for p in content or []:
        if not isinstance(p, dict):
            continue
        if p.get("type") == "text" and p.get("text"):
            out.append({"type": "text", "text": p["text"]})
        elif p.get("type") == "image_url":
            iu = p.get("image_url")
            block = _image_block(iu.get("url", "") if isinstance(iu, dict) else str(iu or ""))
            if block:
                out.append(block)
    return out


def _text(content: Any) -> str:
    if isinstance(content, str):
        return content
    return "".join(p.get("text", "") for p in content or [] if isinstance(p, dict) and p.get("type") == "text")


def to_messages(messages: list[dict[str, Any]]) -> tuple[str, list[dict[str, Any]]]:
    """OpenAI biçimi → (system, Claude mesajları). Baştaki sistem mesajları system'e, aradakiler kullanıcı turuna
    işaretli metin olarak gider. Arka arkaya gelen araç sonuçları tek kullanıcı turunda toplanır."""
    system: list[str] = []
    out: list[dict[str, Any]] = []

    def push(role: str, blocks: list[dict[str, Any]]) -> None:
        if not blocks:
            return
        if out and out[-1]["role"] == role:
            out[-1]["content"].extend(blocks)
        else:
            out.append({"role": role, "content": list(blocks)})

    for m in messages:
        role = m.get("role")
        if role == "system":
            t = _text(m.get("content"))
            if not out:
                system.append(t)
            elif t:
                push("user", [{"type": "text", "text": f"[Sistem notu]\n{t}"}])
        elif role == "user":
            push("user", _user_content(m.get("content")))
        elif role == "assistant":
            blocks: list[dict[str, Any]] = []
            t = _text(m.get("content"))
            if t.strip():
                blocks.append({"type": "text", "text": t})
            for tc in m.get("tool_calls") or []:
                fn = tc.get("function") or {}
                try:
                    args = json.loads(fn.get("arguments") or "{}")
                except ValueError:
                    args = {}
                blocks.append({"type": "tool_use", "id": tc.get("id") or "call", "name": fn.get("name") or "",
                               "input": args if isinstance(args, dict) else {"value": args}})
            push("assistant", blocks)
        elif role == "tool":
            c = m.get("content")
            push("user", [{"type": "tool_result", "tool_use_id": m.get("tool_call_id") or "call",
                           "content": c if isinstance(c, str) else _text(c) or json.dumps(c, ensure_ascii=False)}])
    if out and out[0]["role"] != "user":  # ilk tur kullanıcıdan olmalı
        out.insert(0, {"role": "user", "content": [{"type": "text", "text": "(devam)"}]})
    return "\n\n".join(s for s in system if s), out


def to_tools(tools: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    out = []
    for t in tools or []:
        fn = t.get("function") if t.get("type") == "function" else t
        if not isinstance(fn, dict) or not fn.get("name"):
            continue
        schema = fn.get("parameters") or {"type": "object", "properties": {}}
        out.append({"name": fn["name"], "description": fn.get("description") or "", "input_schema": schema})
    return out


_CACHEABLE = {"text", "tool_result", "image", "document"}


def mark_cache(kw: dict[str, Any]) -> None:
    """Prompt önbelleği için iki işaret: sistem metni (araç tanımlarıyla birlikte önek) ve son mesajın son bloğu.
    Böylece her adımda yalnız yeni eklenen kısım işlenir; geçmiş önbellekten okunur (çok daha ucuz ve hızlı)."""
    eph = {"type": "ephemeral"}
    system = kw.get("system")
    if isinstance(system, str) and system.strip():
        kw["system"] = [{"type": "text", "text": system, "cache_control": eph}]
    msgs = kw.get("messages") or []
    if not msgs:
        return
    last = msgs[-1]
    content = last.get("content")
    if isinstance(content, str):
        if content.strip():
            last["content"] = [{"type": "text", "text": content, "cache_control": eph}]
    elif isinstance(content, list) and content:
        block = content[-1]
        if isinstance(block, dict) and block.get("type") in _CACHEABLE and (block.get("type") != "text"
                                                                            or str(block.get("text") or "").strip()):
            content[-1] = {**block, "cache_control": eph}


def from_message(msg: Any) -> tuple[dict[str, Any], dict[str, Any]]:
    """Claude cevabı → (OpenAI biçiminde mesaj, kullanım)."""
    texts, calls = [], []
    for b in msg.content or []:
        if b.type == "text":
            texts.append(b.text)
        elif b.type == "tool_use":
            calls.append({"id": b.id, "type": "function",
                          "function": {"name": b.name, "arguments": json.dumps(b.input, ensure_ascii=False)}})
    out: dict[str, Any] = {"role": "assistant", "content": "".join(texts)}
    if calls:
        out["tool_calls"] = calls
    u = msg.usage
    written = getattr(u, "cache_creation_input_tokens", 0) or 0
    prompt = (u.input_tokens or 0) + (getattr(u, "cache_read_input_tokens", 0) or 0) + written
    # Önbelleğe yazılan token ayrıca verilir: router'ın kullanım sayacı (cache_write_tokens) bu adı okuyor; önceden
    # düşürülüyordu ve Claude API'de önbellek yazımı (normal girdiden pahalı) hep 0 görünüyordu.
    return out, {"prompt_tokens": prompt, "completion_tokens": u.output_tokens or 0,
                 "cached_tokens": getattr(u, "cache_read_input_tokens", 0) or 0,
                 "cache_creation_input_tokens": written}


class AnthropicProvider:
    STREAM_READ = 300.0  # sn: akış bu kadar veri göndermezse kesilir (uzun düşünmede bile özet ve ping gelir)

    def __init__(self, cfg: ProviderConfig, timeout: float = 180.0, client: Any = None):
        self.cfg = cfg
        self.name = cfg.name
        self.timeout = timeout
        self._client = client

    @property
    def client(self) -> Any:
        if self._client is None:
            sdk = _sdk()
            base = self.cfg.base_url.rstrip("/")
            self._client = sdk.Anthropic(api_key=self.cfg.api_key or "", max_retries=0,
                                         base_url=base if base and base != "https://api.anthropic.com" else None,
                                         default_headers=self.cfg.headers or None,
                                         timeout=sdk.Timeout(self.timeout, connect=15.0, read=self.STREAM_READ))
        return self._client

    def close(self) -> None:
        if self._client is not None:
            try:
                self._client.close()
            except Exception:  # noqa: BLE001 - kapanışta hata önemsiz
                pass

    # ------------------------------------------------------------------------------------ istek gövdesi
    def _params(self, model, messages, tools, max_tokens, response_format, extra, stream: bool) -> dict[str, Any]:
        system, msgs = to_messages(messages)
        kw: dict[str, Any] = {"model": model, "messages": msgs,
                              "max_tokens": int(max_tokens) if max_tokens else (32000 if stream else 16000)}
        out_cfg: dict[str, Any] = {}
        if response_format and response_format.get("type") == "json_schema":
            schema = (response_format.get("json_schema") or {}).get("schema")
            if schema:
                out_cfg["format"] = {"type": "json_schema", "schema": schema}
        elif response_format and response_format.get("type") == "json_object":
            system = (system + "\n\n" if system else "") + "Cevabı yalnızca geçerli bir JSON nesnesi olarak ver."
        if system:
            kw["system"] = system
        t = to_tools(tools)
        if t:
            kw["tools"] = t
        mark_cache(kw)
        if not model.startswith(("claude-3", "claude-haiku-4")):
            kw["thinking"] = {"type": "adaptive", "display": "summarized"}
        effort = (extra or {}).get("reasoning_effort") or ((extra or {}).get("output_config") or {}).get("effort")
        if effort in EFFORTS:
            out_cfg["effort"] = effort
        if out_cfg:
            kw["output_config"] = out_cfg
        if model in FALLBACK_MODELS:
            kw["betas"] = [FALLBACK_BETA]
            kw["fallbacks"] = "default"
        return kw

    def _error(self, e: Exception) -> ProviderError:
        sdk = _sdk()
        if isinstance(e, sdk.APITimeoutError):
            err = ProviderError("network", "zaman aşımı: Claude API cevap vermedi")
        elif isinstance(e, sdk.APIConnectionError):
            err = ProviderError("network", f"Claude API'ye bağlanılamadı: {e}")
        elif isinstance(e, sdk.APIStatusError):
            body = ""
            try:
                body = e.response.text
            except Exception:  # noqa: BLE001
                body = str(e)
            status = e.status_code
            if status < 400:
                # Akış 200 ile açıldıktan sonra gelen "error" olayı (overloaded_error, api_error...): paket bunu
                # 200 durumlu APIStatusError yapıyor ve classify_error 200'ü "bad_request" sayıyordu; geçici
                # yoğunluk hatası kalıcı istek hatası gibi işleniyordu. Durum kodu hata türünden çıkarılır.
                raw = getattr(e, "body", None)
                etype = ((raw.get("error") or {}).get("type") if isinstance(raw, dict)
                         and isinstance(raw.get("error"), dict) else None) or ""
                text = f"{e} {body}"
                status = _STREAM_ERROR_STATUS.get(etype) or next(
                    (code for name, code in _STREAM_ERROR_STATUS.items() if name in text), 500)
            err = classify_error(status, body or str(e), dict(e.response.headers))
        else:
            err = ProviderError("server", str(e))
        err.args = tuple(redact(a, self.cfg.api_key) if isinstance(a, str) else a for a in err.args)
        return err

    @staticmethod
    def _check(msg: Any) -> None:
        if msg.stop_reason == "refusal":
            det = getattr(msg, "stop_details", None)
            cat = getattr(det, "category", None) if det else None
            raise ProviderError("bad_request", "Claude isteği geri çevirdi" + (f" ({cat})" if cat else ""))
        if msg.stop_reason == "max_tokens" and not any(b.type in ("text", "tool_use") and
                                                       (b.type == "tool_use" or b.text) for b in msg.content):
            raise ProviderError("too_large", "cevap max_tokens'a takıldı, içerik yok")

    # ---------------------------------------------------------------------------------------- çağrılar
    def chat(self, model: str, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None = None,
             temperature: float | None = None, max_tokens: int | None = None,
             response_format: dict[str, Any] | None = None, extra: dict[str, Any] | None = None) -> ChatResult:
        kw = self._params(model, messages, tools, max_tokens, response_format, extra, stream=False)
        t0 = time.monotonic()
        try:
            msg = self.client.beta.messages.create(**kw)
        except ProviderError:
            raise
        except Exception as e:  # noqa: BLE001 - paket hatası sağlayıcı hatasına çevrilir
            raise self._error(e) from e
        self._check(msg)
        out, usage = from_message(msg)
        return ChatResult(out, self.name, getattr(msg, "model", None) or model, usage, time.monotonic() - t0,
                          finish="length" if getattr(msg, "stop_reason", "") == "max_tokens" else "")

    def chat_stream(self, model: str, messages: list[dict[str, Any]], on_delta: Callable[[str], None],
                    tools: list[dict[str, Any]] | None = None, temperature: float | None = None,
                    max_tokens: int | None = None, response_format: dict[str, Any] | None = None,
                    extra: dict[str, Any] | None = None, cancel: Callable[[], bool] | None = None,
                    on_reasoning: Callable[[str], None] | None = None, on_first: Callable[[], None] | None = None,
                    on_tool: Callable[[str, int], None] | None = None,
                    on_call: Callable[[dict[str, Any]], None] | None = None) -> ChatResult:
        kw = self._params(model, messages, tools, max_tokens, response_format, extra, stream=True)
        t0 = time.monotonic()
        tool_name, tool_len = "", 0
        try:
            with self.client.beta.messages.stream(**kw) as stream:
                for ev in stream:
                    if cancel and cancel():
                        raise StreamCancelled()
                    if ev.type == "content_block_start":
                        if ev.content_block.type == "tool_use":
                            tool_name, tool_len = ev.content_block.name, 0
                    elif ev.type == "content_block_delta":
                        d = ev.delta
                        if on_first is not None and d.type in ("text_delta", "thinking_delta", "input_json_delta"):
                            on_first()
                            on_first = None
                        if d.type == "text_delta" and d.text:
                            on_delta(d.text)
                        elif d.type == "thinking_delta" and d.thinking and on_reasoning:
                            on_reasoning(d.thinking)
                        elif d.type == "input_json_delta" and on_tool is not None:
                            tool_len += len(d.partial_json or "")
                            on_tool(tool_name, tool_len)
                    elif ev.type == "content_block_stop" and on_call is not None:
                        block = getattr(ev, "content_block", None)
                        if getattr(block, "type", "") == "tool_use":
                            try:  # araç bloğu bitti: ajan güvenli aracı cevap bitmeden başlatabilir
                                on_call({"id": block.id, "type": "function", "function": {
                                    "name": block.name, "arguments": json.dumps(block.input or {}, ensure_ascii=False)}})
                            except Exception:  # noqa: BLE001
                                pass
                msg = stream.get_final_message()
        except (ProviderError, StreamCancelled):
            raise
        except Exception as e:  # noqa: BLE001
            raise self._error(e) from e
        self._check(msg)
        out, usage = from_message(msg)
        return ChatResult(out, self.name, getattr(msg, "model", None) or model, usage, time.monotonic() - t0,
                          finish="length" if getattr(msg, "stop_reason", "") == "max_tokens" else "")

    def list_models(self) -> list[dict[str, Any]]:
        try:
            return [{"id": m.id, "context_length": getattr(m, "max_input_tokens", None)}
                    for m in self.client.models.list()]
        except ProviderError:
            raise
        except Exception as e:  # noqa: BLE001
            raise self._error(e) from e
