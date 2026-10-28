from __future__ import annotations

import json
import time

from alice.memory import LongMemory, attach_memory, clean_user_text, explicit_note, has_signal, terms
from alice.prompts import MEMORY_OPEN, with_memory
from alice.tools import ToolContext
from alice.tools.meta import Memory
from tests.test_agent import make_agent


def mem(**opts) -> LongMemory:
    return LongMemory(":memory:", opts)


def test_terms_fold_turkish_suffixes():
    assert "kahv" in terms("Kahveyi şekersiz içerim")
    assert set(terms("KAHVEMİ")) == {"kahv"}
    assert terms("filmini") == terms("film")
    assert terms("ve bu bir şey") == []


def test_remember_dedupes_and_profile_orders_by_kind():
    m = mem()
    a, new_a = m.remember("Kullanıcı kahveyi şekersiz içer.", "preference")
    b, new_b = m.remember("Kullanıcı kahveyi şekersiz içer", "preference")
    assert new_a and not new_b and a == b
    m.remember("Kullanıcının kardeşinin adı Ece.", "person")
    prof = m.profile()
    assert "Long-term memory" in prof
    assert prof.index("şekersiz") < prof.index("Ece")
    assert len(m.facts()) == 2


def test_search_finds_fact_and_old_conversation():
    m = mem()
    m.remember("Kullanıcı en çok Interstellar filmini sever.", "preference")
    m.record("Hey Alice, yarın Ankara'ya trenle gidiyorum, bilet fiyatlarına bak\n\n[şu an: 01.10.2026 ...]",
             "Bilet 450 lira.", "ses")
    hits = m.search("en sevdiğim film neydi")
    assert hits and "Interstellar" in hits[0].item.text
    hits = m.search("ankara treni ne kadardı")
    assert hits and "Ankara" in hits[0].item.user and "[şu an" not in hits[0].item.user
    assert m.search("hava nasıl") == []


def test_recall_block_and_hits():
    m = mem()
    fid, _ = m.remember("Kullanıcı Opera GX kullanır.", "fact")
    block = m.recall("opera'yı aç")
    assert block.startswith("[hafızadan") and "Opera GX" in block
    assert next(f for f in m.facts() if f.id == fid).hits == 1
    assert clean_user_text("merhaba\n\n" + block) == "merhaba"


def test_explicit_note_and_signals():
    assert explicit_note("Alice, bunu hatırla: annemin doğum günü 3 Mayıs") == "Annemin doğum günü 3 Mayıs"
    assert explicit_note("arabamı B3 katına park ettim, unutma") == "Arabamı B3 katına park ettim"
    assert explicit_note("saat kaç") is None
    assert has_signal("Ben caz müziği çok severim")
    assert not has_signal("YouTube'u aç")
    m = mem()
    m.learn("bunu hatırla: annemin doğum günü 3 Mayıs")
    assert "3 Mayıs" in m.facts()[0].text


def test_learn_uses_extractor_and_updates_existing():
    seen = []

    def extractor(prompt: str) -> str:
        seen.append(prompt)
        if "artık" in prompt:
            return json.dumps({"update": [{"id": 1, "text": "Kullanıcı artık çayı tercih ediyor."}], "add": []})
        return '```json\n{"add": [{"text": "Kullanıcı kahveyi şekersiz içer.", "kind": "preference"}]}\n```'

    m = LongMemory(":memory:", {}, extractor)
    m.learn("YouTube'u aç", background=False)
    assert not seen  # sinyal yok: model çağrılmaz
    m.learn("ben kahveyi hep şekersiz içerim", "Not ettim.", background=False)
    assert m.facts()[0].text == "Kullanıcı kahveyi şekersiz içer."
    m.learn("artık kahve içmiyorum, çay seviyorum", background=False)
    assert "1: Kullanıcı kahveyi" in seen[-1]  # ilgili eski bilgi modele gösterildi
    assert [f.text for f in m.facts()] == ["Kullanıcı artık çayı tercih ediyor."]


def test_import_markdown_once(tmp_path):
    md = tmp_path / "memory.md"
    md.write_text("* Kullanıcının adı Hayat  _(2026-09-01)_\n* Projeler D:\\Unity altında  _(2026-09-02)_\n", encoding="utf-8")
    m = mem()
    assert m.import_markdown(md) == 2
    assert m.import_markdown(md) == 0
    assert {f.text for f in m.facts()} == {"Kullanıcının adı Hayat", "Projeler D:\\Unity altında"}


def test_episodes_pruned_by_age():
    m = mem(keep_days=1)
    m.record("eski konuşma", "cevap")
    m.db.execute("UPDATE episodes SET ts = ?", (time.time() - 3 * 86400,))
    m._prune()
    assert m.recent() == []


def test_with_memory_replaces_section():
    s = with_memory("sys", "# Long-term memory\n- a")
    assert s.count(MEMORY_OPEN) == 1 and "- a" in s
    s2 = with_memory(s + "\n\n# Voice mode", "# Long-term memory\n- b")
    assert "- a" not in s2 and "- b" in s2 and s2.endswith("# Voice mode")


def test_agent_injects_recall_records_turns_and_refreshes_profile(workdir):
    agent, provs, _ = make_agent(["Tamam.", "Interstellar dedin."], workdir)
    attach_memory(agent, "yazı")
    assert agent.memory is not None
    agent.memory.remember("Kullanıcının en sevdiği film Interstellar.", "preference")
    agent.run_turn("en sevdiğim film hangisiydi?")
    sent = provs["a"].calls[0]["messages"]
    assert "Interstellar" in sent[0]["content"]  # profil sistem isteminde
    assert "[hafızadan" in sent[-1]["content"]
    assert agent.messages[1]["_display"] == "en sevdiğim film hangisiydi?"
    ep = agent.memory.recent(1)[0]
    assert ep.user == "en sevdiğim film hangisiydi?" and ep.reply == "Tamam." and ep.channel == "yazı"


def test_memory_tool_add_search_forget(workdir):
    agent, _, _ = make_agent([], workdir)
    attach_memory(agent, "yazı")
    ctx: ToolContext = agent.ctx
    tool = Memory()
    assert "kaydedildi" in tool.run(ctx, "add", note="Kullanıcı Galatasaray tutar.", kind="preference").text
    assert "Galatasaray" in tool.run(ctx, "search", query="hangi takımı tutuyorum galatasaray").text
    assert tool.risk_for({"action": "forget"}) == "write"
    out = tool.run(ctx, "forget", query="Galatasaray tutar").text
    assert out.startswith("silindi")
    assert agent.memory.facts() == []


def test_bir_konusmalik_seyler_bilgi_diye_kaydedilmez():
    m = mem()
    m.apply({"add": [
        {"text": "Kullanıcı, Spotify'da DNA parçasını dinlemek istedi ancak oynatma başarısız oldu.", "kind": "fact"},
        {"text": r"Kullanıcının masaüstünde C:\Users\x\Desktop\Deneme adında bir klasör var.", "kind": "fact"},
        {"text": "Kullanıcı us.txt dosyasındaki değerin doğru olduğunu onayladı.", "kind": "fact"},
        {"text": "Kullanıcı kahveyi şekersiz içer.", "kind": "preference"},
    ]})
    assert [f.text for f in m.facts()] == ["Kullanıcı kahveyi şekersiz içer."]


def test_toparlama_birlestirir_siler_ama_hatirla_dediklerine_dokunmaz():
    seen = []

    def extractor(prompt):
        seen.append(prompt)
        return json.dumps({"remove": [2, 3], "merge": [{"ids": [4, 5], "text": "Kullanıcı Unciv ve Solitaire oynar."}],
                           "update": []})

    m = LongMemory(":memory:", {}, extractor)
    m.remember("Kullanıcı kahveyi şekersiz içer.", "preference", source="kullanıcı")  # 1
    m.remember("Kullanıcı reflex katmanında çalışıyor.", "fact", source="otomatik")  # 2
    m.remember("Kullanıcının adı Hayat.", "fact", source="kullanıcı")  # 3: elle kaydedildi, silinmez
    m.remember("Kullanıcı Unciv oynuyor.", "fact", source="otomatik")  # 4
    m.remember("Kullanıcı Solitaire sever.", "preference", source="otomatik")  # 5
    done = m.consolidate()
    texts = {f.id: f.text for f in m.facts()}
    assert texts == {1: "Kullanıcı kahveyi şekersiz içer.", 3: "Kullanıcının adı Hayat.",
                     4: "Kullanıcı Unciv ve Solitaire oynar."}
    assert "user asked to remember" in seen[0] and done
    assert not m.maybe_consolidate()  # günde bir
