"""MagicDocs: '# MAGIC DOC:' başlıklı belgeler konuşmaya göre kendiliğinden güncellenir."""

from __future__ import annotations

from types import SimpleNamespace

from alice import magicdocs
from tests.conftest import tool_call
from tests.test_agent import make_agent


class Router:
    def __init__(self, reply):
        self.reply, self.prompts = reply, []

    def chat(self, role, messages, **kw):
        self.prompts.append(messages[-1]["content"])
        return SimpleNamespace(text=self.reply)


TALK = [{"role": "system", "content": "s"}, {"role": "user", "content": "Sunucu portu artık 9000."},
        {"role": "assistant", "content": "Tamam, 9000'e taşıdım."}]


def test_read_file_registers_only_magic_docs(workdir):
    (workdir / "notlar.md").write_text("# MAGIC DOC: Kurulum\n_Kurulum adımları_\nPort 8000.\n", encoding="utf-8")
    (workdir / "duz.md").write_text("# Sıradan\n", encoding="utf-8")
    agent, _, _ = make_agent([tool_call("read_file", {"path": "notlar.md"}),
                              tool_call("read_file", {"path": "duz.md"}, "c2"), "okudum"], workdir)
    agent.run_turn("oku")
    docs = agent.ctx.state.get("magic_docs") or {}
    assert [k.endswith("notlar.md") for k in docs] == [True] and list(docs.values())[0]["title"] == "Kurulum"


def test_update_rewrites_doc_and_respects_rules(workdir):
    p = workdir / "notlar.md"
    p.write_text("# MAGIC DOC: Kurulum\n_Kurulum adımları_\nPort 8000.\n", encoding="utf-8")
    state = {}
    magicdocs.note_read(state, p)
    r = Router("```markdown\n# MAGIC DOC: Kurulum\n_Kurulum adımları_\nPort 9000.\n```")
    assert magicdocs.update(state, r, TALK, now=1000.0) == [str(p)]
    assert p.read_text(encoding="utf-8") == "# MAGIC DOC: Kurulum\n_Kurulum adımları_\nPort 9000.\n"
    assert "Belgenin amacı: Kurulum adımları" in r.prompts[0]
    assert magicdocs.update(state, r, TALK, now=1100.0) == []  # 10 dakika dolmadı
    r.reply = magicdocs.NO_CHANGE
    assert magicdocs.update(state, r, TALK, now=5000.0) == [] and "9000" in p.read_text(encoding="utf-8")
    r.reply = "başlıksız saçma çıktı"
    assert magicdocs.update(state, r, TALK, now=9000.0) == []  # başlığı bozan cevap yazılmaz


def test_update_keeps_line_endings_and_skips_non_utf8_docs(workdir):
    lf, crlf, bad = workdir / "lf.md", workdir / "crlf.md", workdir / "bad.md"
    lf.write_bytes(b"# MAGIC DOC: Kurulum\nPort 8000.\n")
    crlf.write_bytes(b"# MAGIC DOC: Kurulum\r\nPort 8000.\r\n")
    bad.write_bytes("# MAGIC DOC: Kurulum\nŞifre değil, port 8000.\n".encode("cp1254"))
    state = {}
    for p in (bad, lf, crlf):
        assert magicdocs.note_read(state, p)
    r = Router("# MAGIC DOC: Kurulum\nPort 9000.")
    # UTF-8 olmayan belge atlanır (eskiden UnicodeDecodeError bütün güncellemeyi çökertiyordu).
    assert magicdocs.update(state, r, TALK, now=1000.0) == [str(lf), str(crlf)]
    assert lf.read_bytes() == b"# MAGIC DOC: Kurulum\nPort 9000.\n"  # LF dosya CRLF'e dönmedi
    assert crlf.read_bytes() == b"# MAGIC DOC: Kurulum\r\nPort 9000.\r\n"
