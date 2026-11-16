from __future__ import annotations

import os
import time

from alice.core import clipwatch
from alice.tools import findfiles as ff


def test_search_downloads_and_tidy(tmp_path):
    dl = tmp_path / "Downloads"
    dl.mkdir()
    old = time.time() - 3 * 86400
    for name in ("staj_belgesi.pdf", "fatura.xlsx", "foto.jpg", "setup.exe", "notlar.bin"):
        (dl / name).write_bytes(b"x")
        os.utime(dl / name, (old, old))
    (dl / "yeni.pdf").write_bytes(b"x")  # bugün indi: tidy dokunmaz
    hits = ff.downloads("staj", "", 7, folder=dl)
    assert [p.name for p, _ in hits] == ["staj_belgesi.pdf"]
    assert [p.name for p, _ in ff.downloads("", "pdf", None, folder=dl)] == ["yeni.pdf", "staj_belgesi.pdf"]
    assert ff.downloads("", "pdf", 1, folder=dl)[0][0].name == "yeni.pdf" and len(ff.downloads("", "pdf", 1, folder=dl)) == 1
    deep = tmp_path / "Documents" / "okul" / "2026"
    deep.mkdir(parents=True)
    (deep / "Staj Raporu.docx").write_bytes(b"x")
    assert [p.name for p, _ in ff.search("staj rapor", "", None, roots=[tmp_path / "Documents"])] == ["Staj Raporu.docx"]
    plan = {src.name: dest.parent.name for src, dest in ff.tidy_plan(dl)}
    assert plan == {"staj_belgesi.pdf": "Belgeler", "fatura.xlsx": "Belgeler", "foto.jpg": "Resimler",
                    "setup.exe": "Kurulumlar"}


def test_recent_reads_shortcut_targets(tmp_path):
    rec = tmp_path / "Recent"
    rec.mkdir()
    (rec / "Bütçe.xlsx.lnk").write_bytes(b"junk\x00C:\\Users\\me\\Documents\\Butce.xlsx\x00more")
    (rec / "klasor.lnk").write_bytes(b"junk")
    hits = ff.recent("", "excel", None, folder=rec)
    assert [str(p) for p, _ in hits] == ["C:\\Users\\me\\Documents\\Butce.xlsx"]


def test_clipboard_history_skips_secrets(tmp_path):
    path = tmp_path / "h.json"
    assert clipwatch.add("https://example.com/staj?id=1", path, now=100)
    assert not clipwatch.add("Xk9#mPq2vL!", path, now=101)  # şifreye benziyor
    assert not clipwatch.add("ghp_abcdefghijklmnopqrstuvwx", path, now=102)
    assert clipwatch.add("Merhaba hocam, ödevi yarın teslim edeceğim.", path, now=103)
    assert [i["text"][:5] for i in clipwatch.load(path, now=200)] == ["https", "Merha"]
    assert clipwatch.load(path, now=100 + 13 * 3600) == []  # 12 saat sonra silinir
    w = clipwatch.ClipWatch(read=iter(["eski", "eski", "yeni metin"]).__next__)
    assert not w.step() and not w.step()  # açılıştaki pano kaydedilmez


def test_no_tool_schema_has_empty_enum_value():
    # Gemini Live boş enum değeri ("") gören bütün araç listesini reddediyordu: Live hiç açılmadı, cevaplar 10 sn sürdü
    from alice.tools import builtin_tools

    def walk(node, path):
        if isinstance(node, dict):
            assert "" not in node.get("enum", []), path
            for k, v in node.items():
                walk(v, f"{path}/{k}")
        elif isinstance(node, list):
            for i, v in enumerate(node):
                walk(v, f"{path}[{i}]")

    for t in builtin_tools():
        walk(t.parameters, t.name)


def test_open_resolves_relative_path_against_chat_cwd(tmp_path, monkeypatch):
    # Göreli yol sohbetin çalışma klasörüne göre çözülmeli, sürecin cwd'sine göre değil.
    from unittest.mock import MagicMock

    from alice.tools.base import ToolContext
    (tmp_path / "rapor.pdf").write_bytes(b"x")
    elsewhere = tmp_path / "baska"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    opened = []
    monkeypatch.setattr(ff.os, "startfile", lambda p: opened.append(p), raising=False)
    monkeypatch.setattr(ff.sys, "platform", "win32")
    res = ff.FindFiles().run(ToolContext(cwd=tmp_path, config=MagicMock()), action="open", path="rapor.pdf")
    assert "rapor.pdf" in res.text
    assert opened == [str((tmp_path / "rapor.pdf").resolve())]
