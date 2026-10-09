from __future__ import annotations

import pytest

from alice.providers.openai_compat import ProviderError, classify_error
from alice.providers.router import AllProvidersFailed, Router, estimate_tokens, strip_images
from tests.conftest import FakeProvider, make_config


def router_with(cfg, providers: dict[str, FakeProvider], **kw) -> Router:
    return Router(cfg, provider_factory=lambda pc: providers[pc.name], sleep=lambda s: None, **kw)


def test_falls_back_on_rate_limit_and_cools_down():
    cfg = make_config({"brain": ["a:m1", "b:m2"]})
    a = FakeProvider("a", [ProviderError("rate_limit", "429", 429, retry_after=30), "unused"])
    b = FakeProvider("b", ["from b", "again b"])
    r = router_with(cfg, {"a": a, "b": b})
    assert r.chat("brain", [{"role": "user", "content": "hi"}]).text == "from b"
    # a soğumada, ikinci istek a'ya hiç gitmemeli
    assert r.chat("brain", [{"role": "user", "content": "hi"}]).text == "again b"
    assert len(a.calls) == 1
    assert not r.available("a:m1")[0]


def test_not_found_kills_model_auth_kills_provider():
    cfg = make_config({"brain": ["a:gone", "a:other", "b:ok"]})
    a = FakeProvider("a", [ProviderError("not_found", "x", 404), ProviderError("auth", "bad key", 401)])
    b = FakeProvider("b", ["ok", "ok"])
    r = router_with(cfg, {"a": a, "b": b})
    assert r.chat("brain", [{"role": "user", "content": "x"}]).provider == "b"
    assert "a:gone" in r.dead_models and "a" in r.dead_providers
    r.chat("brain", [{"role": "user", "content": "x"}])
    assert len(a.calls) == 2


def test_too_large_skips_provider_by_budget():
    cfg = make_config({"brain": ["a:small", "b:big"]}, providers={
        "a": {"base_url": "http://a", "api_key_env": "", "max_input_tokens": 50},
        "b": {"base_url": "http://b", "api_key_env": "", "max_input_tokens": 100000},
    })
    a = FakeProvider("a", [])
    b = FakeProvider("b", ["big ok"])
    r = router_with(cfg, {"a": a, "b": b})
    assert r.chat("brain", [{"role": "user", "content": "x" * 2000}]).text == "big ok"
    assert a.calls == []


def test_missing_key_provider_skipped(monkeypatch):
    cfg = make_config({"brain": ["a:m", "b:m"]}, providers={
        "a": {"base_url": "http://a", "api_key_env": "NOPE_KEY"},
        "b": {"base_url": "http://b", "api_key_env": ""},
    })
    b = FakeProvider("b", ["ok"])
    r = router_with(cfg, {"a": FakeProvider("a", []), "b": b})
    assert r.chat("brain", [{"role": "user", "content": "x"}]).provider == "b"


def test_all_fail_waits_for_short_cooldown_then_retries():
    cfg = make_config({"brain": ["a:m"]})
    a = FakeProvider("a", [ProviderError("rate_limit", "slow down", 429, retry_after=2), "after wait"])
    slept = []
    r = Router(cfg, provider_factory=lambda pc: a, sleep=slept.append)
    # sleep sahte olduğu için cooldown'u elle geçmişe çekiyoruz
    orig = r.available

    def avail(ref, now=None):
        if slept:
            r.cooldown_until.clear()
        return orig(ref, now)

    r.available = avail
    assert r.chat("brain", [{"role": "user", "content": "x"}]).text == "after wait"
    assert slept and slept[0] <= 3


def test_all_fail_raises_with_reasons():
    cfg = make_config({"brain": ["a:m"]})
    a = FakeProvider("a", [ProviderError("quota", "daily limit", 429)])
    r = router_with(cfg, {"a": a})
    with pytest.raises(AllProvidersFailed) as ei:
        r.chat("brain", [{"role": "user", "content": "x"}])
    assert "quota" in str(ei.value)


def test_tools_bad_request_marks_no_tools():
    cfg = make_config({"brain": ["a:m", "b:m"]})
    a = FakeProvider("a", [ProviderError("bad_request", "tools not supported", 400)])
    b = FakeProvider("b", ["ok"])
    r = router_with(cfg, {"a": a, "b": b})
    r.chat("brain", [{"role": "user", "content": "x"}], tools=[{"type": "function", "function": {"name": "t"}}])
    assert "a:m" in r.no_tools


def test_images_stripped_for_non_vision_and_private_keys_removed():
    cfg = make_config({"brain": ["a:m"]})
    a = FakeProvider("a", ["ok"])
    r = router_with(cfg, {"a": a})
    msgs = [{"role": "user", "_image": True, "content": [
        {"type": "text", "text": "bak"}, {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAA"}}]}]
    r.chat("brain", msgs)
    sent = a.calls[0]["messages"][0]
    assert isinstance(sent["content"], str) and "atlandı" in sent["content"]
    assert "_image" not in sent


def test_vision_provider_keeps_images():
    cfg = make_config({"brain": ["b:m"]})
    b = FakeProvider("b", ["ok"])
    r = router_with(cfg, {"b": b})
    msgs = [{"role": "user", "content": [{"type": "image_url", "image_url": {"url": "data:x"}}]}]
    r.chat("brain", msgs)
    assert isinstance(b.calls[0]["messages"][0]["content"], list)


def test_classify_error():
    assert classify_error(429, "Rate limit reached for requests per day (RPD)").kind == "quota"
    assert classify_error(429, "Rate limit reached, try again in 2s").kind == "rate_limit"
    assert classify_error(413, "Request too large for model").kind == "too_large"
    assert classify_error(400, "The model `x` does not exist").kind == "not_found"
    assert classify_error(400, "This model's maximum context length is 8192").kind == "too_large"
    assert classify_error(401, "invalid api key").kind == "auth"
    assert classify_error(503, "overloaded").kind == "server"
    assert classify_error(429, "slow", {"retry-after": "7"}).retry_after == 7.0


def test_estimate_and_strip():
    msgs = [{"role": "user", "content": "x" * 350}]
    assert 90 <= estimate_tokens(msgs) <= 110
    assert strip_images([{"role": "user", "content": "plain"}])[0]["content"] == "plain"


def test_usage_recorded(isolated_home):
    cfg = make_config({"brain": ["a:m"]})
    r = router_with(cfg, {"a": FakeProvider("a", ["ok"])})
    r.chat("brain", [{"role": "user", "content": "x"}])
    assert r.usage.today()["a:m"]["requests"] == 1


def test_gemini_gets_thought_signatures_and_others_dont():
    from alice.providers.openai_compat import _merge_tool_delta, normalize_message
    from alice.providers.router import GEMINI_DUMMY_SIGNATURE, clean_messages

    calls: dict = {}
    _merge_tool_delta(calls, {"id": "c1", "function": {"name": "run_command", "arguments": "{}"},
                              "extra_content": {"google": {"thought_signature": "SIG"}}})
    own = normalize_message({"content": "", "tool_calls": [calls[0]]})
    other = normalize_message({"content": "", "tool_calls": [{"id": "c2", "function": {"name": "x", "arguments": "{}"}}]})
    msgs = [own, other]
    g = clean_messages(msgs, gemini=True)
    assert g[0]["tool_calls"][0]["extra_content"]["google"]["thought_signature"] == "SIG"
    assert g[1]["tool_calls"][0]["extra_content"]["google"]["thought_signature"] == GEMINI_DUMMY_SIGNATURE
    plain = clean_messages(msgs)
    assert all("extra_content" not in tc and "_extra_content" not in tc for m in plain for tc in m["tool_calls"])


def test_signature_error_does_not_disable_tools_and_too_large_learns_limit():
    from alice.providers.openai_compat import ProviderError
    from alice.providers.router import Router
    from tests.conftest import make_config

    r = Router(make_config({"brain": ["a:m"]}), provider_factory=lambda pc: None, sleep=lambda s: None)
    r._penalize("a:m", "a", ProviderError("bad_request", "Function call is missing a thought_signature"), True, False)
    assert "a:m" not in r.no_tools
    r._penalize("a:m", "a", ProviderError("too_large", "TPM limit 8000"), True, False, est=10000)
    assert r.size_limit["a:m"] == 9000
