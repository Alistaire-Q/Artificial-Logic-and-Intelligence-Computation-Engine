from __future__ import annotations

from alice.agent import Agent, NullUI, parse_args
from alice.permissions import Permissions
from alice.providers.router import Router
from alice.tools import ToolContext, all_tools
from alice.tools.base import Tool, ToolResult
from tests.conftest import FakeProvider, make_config, rm_rf, tool_call


def make_agent(script, workdir, mode="auto", approve="y", providers=None, **agent_cfg):
    cfg = make_config({"brain": ["a:m"], "fast": ["a:m"], "vision": ["b:v"]}, providers=providers, **agent_cfg)
    provs = {"a": FakeProvider("a", script), "b": FakeProvider("b", ["bir buton var (100,200)"])}
    router = Router(cfg, provider_factory=lambda pc: provs[pc.name], sleep=lambda s: None)
    ctx = ToolContext(cwd=workdir, config=cfg, router=router)
    ui = NullUI(approve)
    agent = Agent(cfg, router, all_tools(), ctx, Permissions(mode), ui, system_prompt="sys")
    return agent, provs, ui


def test_tool_loop_writes_file_and_finishes(workdir):
    import sys

    # Testi çalıştıran Python: PATH'te python olmayabilir; Windows'ta komut PowerShell'de çalışır ("||" yok, & gerekir).
    run = f'& "{sys.executable}" hello.py' if sys.platform == "win32" else f'"{sys.executable}" hello.py'
    script = [tool_call("write_file", {"path": "hello.py", "content": "print('selam')\n"}),
              tool_call("run_command", {"command": run}, cid="c2"),
              "Bitti, dosya çalışıyor."]
    agent, provs, ui = make_agent(script, workdir)
    out = agent.run_turn("hello.py yaz ve çalıştır")
    assert out == "Bitti, dosya çalışıyor."
    assert (workdir / "hello.py").read_text(encoding="utf-8") == "print('selam')\n"
    tool_msgs = [m for m in agent.messages if m["role"] == "tool"]
    assert "selam" in tool_msgs[1]["content"]
    # Her tool_call'un cevabı geçmişte olmalı
    assert [m["tool_call_id"] for m in tool_msgs] == ["c1", "c2"]


def test_edit_file_roundtrip(workdir):
    (workdir / "a.txt").write_text("alpha\nbeta\n", encoding="utf-8")
    script = [tool_call("edit_file", {"path": "a.txt", "old_string": "beta", "new_string": "gamma"}), "ok"]
    agent, _, _ = make_agent(script, workdir)
    agent.run_turn("düzenle")
    assert (workdir / "a.txt").read_text(encoding="utf-8") == "alpha\ngamma\n"


def test_dangerous_command_asks_and_denial_is_reported(workdir):
    (workdir / "keep").mkdir()
    script = [tool_call("run_command", {"command": "rm -rf keep"}), "tamam, silmedim"]
    agent, provs, ui = make_agent(script, workdir, approve="n")
    agent.run_turn("klasörü sil")
    assert ("ask", "run_command") in ui.events
    assert (workdir / "keep").exists()
    denied = [m for m in agent.messages if m["role"] == "tool"][0]["content"]
    assert "reddetti" in denied


def test_yolo_never_asks(workdir):
    (workdir / "gone").mkdir()
    script = [tool_call("run_command", {"command": rm_rf("gone")}), "silindi"]
    agent, _, ui = make_agent(script, workdir, mode="yolo")
    agent.run_turn("sil")
    assert not any(e[0] == "ask" for e in ui.events)
    assert not (workdir / "gone").exists()


def test_bad_json_and_unknown_tool_are_returned_to_model(workdir):
    bad = {"content": "", "tool_calls": [
        {"id": "x1", "type": "function", "function": {"name": "read_file", "arguments": "{not json"}},
        {"id": "x2", "type": "function", "function": {"name": "nope", "arguments": "{}"}}]}
    agent, _, _ = make_agent([bad, "ok"], workdir)
    agent.run_turn("dene")
    res = [m["content"] for m in agent.messages if m["role"] == "tool"]
    assert "JSON" in res[0] and "bilinmeyen araç" in res[1]


def test_images_described_when_brain_lacks_vision(workdir):
    class Shot(Tool):
        name = "shot"
        description = "fake screenshot"

        def run(self, ctx, **a):
            return ToolResult("ekran", images=["AAAA"])

    agent, provs, _ = make_agent([tool_call("shot", {}), "gördüm"], workdir)
    agent.tools["shot"] = Shot()
    agent.run_turn("ekrana bak")
    tool_msg = [m for m in agent.messages if m["role"] == "tool"][0]["content"]
    assert "bir buton var" in tool_msg  # vision rolü açıkladı
    assert provs["b"].calls, "vision rolü çağrılmalıydı"


def test_images_attached_when_brain_has_vision(workdir):
    class Shot(Tool):
        name = "shot"
        description = "fake"

        def run(self, ctx, **a):
            return ToolResult("ekran", images=["AAAA"])

    providers = {"a": {"base_url": "http://a", "api_key_env": "", "vision_models": ["*"]},
                 "b": {"base_url": "http://b", "api_key_env": ""}}
    agent, provs, _ = make_agent([tool_call("shot", {}), tool_call("shot", {}, "c2"), tool_call("shot", {}, "c3"), "ok"],
                                 workdir, providers=providers, keep_images=1)
    agent.tools["shot"] = Shot()
    agent.run_turn("bak")
    imgs = [m for m in agent.messages if m.get("_image")]
    assert len(imgs) == 1  # eski görüntüler budandı
    assert not provs["b"].calls


def test_compaction_summarizes_old_turns(workdir):
    def script(model, messages, tools, **kw):
        if messages[0]["content"].startswith("Summarize"):
            return "ÖZET: kullanıcı dosyalarla uğraşıyor"
        return "cevap " + "x" * 400

    agent, _, ui = make_agent(script, workdir, compact_at_tokens=400)
    for i in range(6):
        agent.run_turn(f"mesaj {i} " + "y" * 300)
    joined = " ".join(str(m.get("content")) for m in agent.messages)
    assert "ÖZET" in joined
    assert len(agent.messages) < 14


def test_interrupt_repairs_history(workdir):
    class Boom(Tool):
        name = "boom"
        description = "x"

        def run(self, ctx, **a):
            raise KeyboardInterrupt

    agent, _, _ = make_agent([tool_call("boom", {})], workdir)
    agent.tools["boom"] = Boom()
    agent.run_turn("patlat")
    last = agent.messages[-1]
    assert last["role"] == "tool" and "durduruldu" in last["content"]


def test_subagent_task(workdir):
    def script(model, messages, tools, **kw):
        last = messages[-1]
        if last["role"] == "user" and last["content"] == "alt görev":
            return "alt sonuç"
        if last["role"] == "tool":
            return "ana bitti"
        return tool_call("task", {"prompt": "alt görev"})

    agent, _, _ = make_agent(script, workdir)
    assert agent.run_turn("başla") == "ana bitti"
    assert "alt sonuç" in [m for m in agent.messages if m["role"] == "tool"][0]["content"]


def test_parse_args_tolerant():
    assert parse_args('```json\n{"a": 1}\n```') == {"a": 1}
    assert parse_args("") == {}
    assert parse_args({"b": 2}) == {"b": 2}


def test_stop_returns_quickly_during_slow_model_call(workdir):
    import threading
    import time

    release = threading.Event()

    def slow(model, messages, tools, **kw):
        release.wait(10)  # cevap vermeyen model
        return "geç cevap"

    agent, _, ui = make_agent(slow, workdir, stream=False)
    threading.Timer(0.3, agent.stop).start()
    t0 = time.monotonic()
    agent.run_turn("merhaba")
    assert time.monotonic() - t0 < 3
    assert ("info", "durduruldu") in ui.events
    release.set()


def test_stop_kills_running_command(workdir):
    import threading
    import time

    script = [tool_call("run_command", {"command": "sleep 30", "timeout": 60}), "bitti"]
    agent, _, _ = make_agent(script, workdir, mode="yolo")
    threading.Timer(0.8, agent.stop).start()
    t0 = time.monotonic()
    agent.run_turn("bekle")
    assert time.monotonic() - t0 < 5
    assert "durduruldu" in agent.messages[-1]["content"] or "HATA" in agent.messages[-1]["content"]


def test_repeated_failing_call_gets_a_nudge(workdir):
    bad = {"path": "yok.txt"}
    script = [tool_call("read_file", bad, cid="c1"), tool_call("read_file", bad, cid="c2"), "pes"]
    agent, _, _ = make_agent(script, workdir)
    agent.run_turn("oku")
    tool_msgs = [m["content"] for m in agent.messages if m["role"] == "tool"]
    assert "ALICE notu" not in tool_msgs[0]
    assert "2. kez başarısız" in tool_msgs[1]


def test_tool_groups_keep_requests_small(workdir):
    import json

    seen = []

    def script(model, messages, tools, **kw):
        seen.append({t["function"]["name"] for t in tools or []})
        if len(seen) == 2:
            return tool_call("load_tools", {"groups": ["system"]}, cid="c9")
        return "tamam"

    agent, _, _ = make_agent(script, workdir)
    agent.run_turn("merhaba")
    assert "read_file" in seen[0] and "browser" not in seen[0] and "blender" not in seen[0]
    agent.run_turn("blender'da bir ev yap")
    assert "blender" in seen[1] and "computer" not in seen[1]
    assert "system_info" in seen[2]  # load_tools ile açıldı
    full = len(json.dumps([t.spec() for t in agent.tools.values()]))
    small = len(json.dumps(agent.tool_specs()))
    assert small < full


def test_mcp_tools_grouped_by_server():
    import os

    from alice.mcp import MCPServer
    from alice.tools.groups import groups_for_text, visible_tools

    names = ["read_file", "mcp__github__list_issues", "mcp__github__create_pr", "mcp__db__query"]
    assert "mcp:github" in groups_for_text("GitHub'daki repolarımı listele", names)
    assert groups_for_text("db tablosuna bak", names) == {"mcp:db"}
    assert groups_for_text("merhaba", names) == set()
    tools = {n: object() for n in names}
    assert len(visible_tools(tools, set())) == 1
    assert len(visible_tools(tools, {"mcp:github"})) == 3
    os.environ["ALICE_TEST_TOK"] = "abc"
    assert MCPServer("x", "cmd", env={"T": "${ALICE_TEST_TOK}"}).env == {"T": "abc"}


def test_long_tool_chain_is_compacted_to_fit_a_small_model(workdir):
    """Ajanın tek görev mesajıyla uzun araç zinciri büyüdü ve sadece dar bağlamlı model açık: hata vermek yerine
    konuşma o modele sığacak kadar özetlenip devam edilir."""
    seen = {"summary_chars": 0}

    def script(model, messages, tools, **kw):
        if messages[0]["content"].startswith("Summarize"):
            seen["summary_chars"] = len(messages[1]["content"])
            return "ÖZET: dosyalar okundu"
        return "bitti"

    providers = {"a": {"base_url": "http://a", "api_key_env": "", "max_input_tokens": 30000},
                 "b": {"base_url": "http://b", "api_key_env": "", "max_input_tokens": 30000}}
    agent, _, ui = make_agent(script, workdir, providers=providers, compact_at_tokens=10**6)
    agent.messages.append({"role": "user", "content": "Görevine başla."})
    for i in range(120):  # uzun araç zinciri: araç tanımlarıyla birlikte 30K sınırını aşar
        agent.messages.append({"role": "assistant", "content": "", "tool_calls": [
            {"id": f"t{i}", "type": "function", "function": {"name": "read_file", "arguments": "{}"}}]})
        agent.messages.append({"role": "tool", "tool_call_id": f"t{i}", "name": "read_file", "content": "z " * 600})
    assert agent.run_turn("devam et") == "bitti"
    assert any("ÖZET" in str(m.get("content")) for m in agent.messages)
    assert 0 < seen["summary_chars"] <= int(30000 * 2.4)
    for i, m in enumerate(agent.messages):  # araç çağrısı ve sonucu ayrılmamış olmalı
        if m["role"] == "tool":
            assert agent.messages[i - 1]["role"] in ("assistant", "tool")


def test_tool_specs_get_lean_when_they_alone_fill_a_tiny_model(workdir):
    from alice.agent import lean_specs
    from alice.providers.router import estimate_tokens

    specs = [t.spec() for t in all_tools()]
    assert estimate_tokens([], lean_specs(specs)) < estimate_tokens([], specs) * 0.75
    assert {s["function"]["name"] for s in lean_specs(specs)} == {s["function"]["name"] for s in specs}


def test_claim_without_evidence_is_sent_back_once(workdir):
    """Canlı kayıt: ALICE dosya inmeden "indirmeyi başlattım", sürüm sayfasında Setup.exe varken "Windows sürümü yok"
    dedi. Sonuç bildiren cevap araç sonuçlarıyla desteklenmiyorsa model bir kez doğrulamaya geri gönderilir."""
    script = [tool_call("list_dir", {"path": "."}),
              "Sonora-Setup.exe indirildi, kurulum hazır.",  # araç sonucu boş klasör: dayanağı yok
              '{"unsupported": ["Sonora-Setup.exe indirildi"]}',  # hızlı modelin kararı
              "Klasörde Sonora-Setup.exe yok; indirmeyi doğrulayamadım."]
    agent, provs, ui = make_agent(script, workdir, verify_claims=True)
    out = agent.run_turn("sonorayı indir")
    assert out == "Klasörde Sonora-Setup.exe yok; indirmeyi doğrulayamadım."
    nudge = [m for m in agent.messages if m.get("_nudge")]
    assert len(nudge) == 1 and "dayanağı yok" in nudge[0]["content"] and "Sonora-Setup.exe indirildi" in nudge[0]["content"]


def test_supported_claim_passes_and_plain_answers_are_not_checked(workdir):
    script = [tool_call("list_dir", {"path": "."}), "Klasör boş, hiçbir dosya yok.", '{"unsupported": []}']
    agent, provs, ui = make_agent(script, workdir, verify_claims=True)
    assert agent.run_turn("klasörde ne var") == "Klasör boş, hiçbir dosya yok."
    assert not any(m.get("_nudge") for m in agent.messages)
    agent2, _, _ = make_agent(["Merhaba! Nasıl yardımcı olayım?"], workdir, verify_claims=True)
    assert agent2.run_turn("selam") == "Merhaba! Nasıl yardımcı olayım?"  # araç yok, iddia yok: kontrol yok
