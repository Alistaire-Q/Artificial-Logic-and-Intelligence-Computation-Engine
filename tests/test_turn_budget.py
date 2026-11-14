"""Tur token bütçesi (Agent SDK'daki task_budget gibi): sınır dolunca tur durur, iş yarıda olduğu söylenir."""

from __future__ import annotations

from tests.conftest import tool_call
from tests.test_agent import make_agent


def _script(n: int) -> list:
    return [tool_call("list_dir", {"path": "."}, cid=f"c{i}") for i in range(n)] + ["bitti"]


def test_turn_halts_when_budget_spent(workdir):
    # FakeProvider her çağrıda 10 girdi + 5 çıktı = 15 token harcar: 20'lik bütçe ikinci adımda dolar.
    agent, provs, _ = make_agent(_script(5), workdir, turn_token_budget=20)
    out = agent.run_turn("klasöre bak")
    assert "token bütçesini" in out and "devam et" in out
    assert sum(1 for m in agent.messages if m["role"] == "tool") == 2
    assert agent.messages[-1].get("_notice")  # sohbet kaydında da görünür, sonraki tur işin yarıda kaldığını bilir


def test_no_budget_by_default_and_counter_resets_each_turn(workdir):
    agent, provs, _ = make_agent(_script(3) + _script(3), workdir)
    assert agent.run_turn("bak") == "bitti"
    agent.config.agent["turn_token_budget"] = 100  # tur başına: önceki turun harcaması sayılmaz
    assert agent.run_turn("yine bak") == "bitti"


def test_cached_input_does_not_count(workdir):
    agent, provs, _ = make_agent(_script(1), workdir, turn_token_budget=1000)
    from alice.providers.openai_compat import ChatResult

    agent._turn_tokens = 0
    res = ChatResult({"role": "assistant", "content": "x"}, "a", "m",
                     {"prompt_tokens": 50_000, "completion_tokens": 100,
                      "prompt_tokens_details": {"cached_tokens": 49_900}})
    agent._count_call(res, 0.0, 0.0)
    assert agent._turn_tokens == 200  # 100 önbelleksiz girdi + 100 çıktı
