"""Araçlardaki somut hataların düzeltmelerini sabitleyen odaklı testler."""

from __future__ import annotations

import pytest

from alice.tools import ToolContext
from alice.tools.files import EditFile, ReadFile
from tests.conftest import make_config


@pytest.fixture
def ctx(workdir):
    return ToolContext(cwd=workdir, config=make_config({"brain": ["a:m"]}), state={"checkpoint": {}})


def _edit(ctx, name, old, new):
    ReadFile().run(ctx, path=name)
    return EditFile().run(ctx, path=name, old_string=old, new_string=new)


def test_edit_keeps_utf8_bom(ctx, workdir):
    # Windows PowerShell 5.1 BOM'suz UTF-8 betiği ANSI okur: düzenleme BOM'u düşürmemeli
    f = workdir / "s.ps1"
    f.write_bytes("﻿Write-Host 'çalıştı'\n".encode("utf-8"))
    _edit(ctx, "s.ps1", "çalıştı", "bitti ğ")
    assert f.read_bytes() == "﻿Write-Host 'bitti ğ'\n".encode("utf-8")


def test_edit_keeps_cp1254(ctx, workdir):
    f = workdir / "a.bat"
    f.write_bytes("echo şişe\r\n".encode("cp1254"))
    _edit(ctx, "a.bat", "şişe", "çiçek")
    assert f.read_bytes() == "echo çiçek\r\n".encode("cp1254")


def test_edit_cp1254_falls_back_to_utf8_for_unencodable(ctx, workdir):
    f = workdir / "a.txt"
    f.write_bytes("şişe\n".encode("cp1254"))
    _edit(ctx, "a.txt", "şişe", "日本")
    assert f.read_bytes().decode("utf-8") == "日本\n"


def test_edit_in_crlf_file_inserts_crlf_lines(ctx, workdir):
    f = workdir / "w.txt"
    f.write_bytes(b"a\r\nb\r\nc\r\n")
    _edit(ctx, "w.txt", "b", "b1\nb2")
    assert f.read_bytes() == b"a\r\nb1\r\nb2\r\nc\r\n"


def test_edit_in_lf_file_left_alone(ctx, workdir):
    f = workdir / "w.txt"
    f.write_bytes(b"a\nb\n")
    _edit(ctx, "w.txt", "b", "b1\nb2")
    assert f.read_bytes() == b"a\nb1\nb2\n"


def test_read_offset_past_end_is_error_not_empty(ctx, workdir):
    from alice.tools.base import ToolError

    (workdir / "r.txt").write_text("a\nb\nc\n", encoding="utf-8")
    with pytest.raises(ToolError, match="3 satır"):
        ReadFile().run(ctx, path="r.txt", offset=50)
    (workdir / "e.txt").write_text("", encoding="utf-8")
    assert "boş dosya" in ReadFile().run(ctx, path="e.txt").text


def _case_insensitive_fs(path) -> bool:
    probe = path / "CaseProbe.tmp"
    probe.write_text("x")
    try:
        return (path / "caseprobe.tmp").exists()
    finally:
        probe.unlink()


def test_file_ops_case_only_rename(ctx, workdir):
    from alice.tools.files import FileOps

    if not _case_insensitive_fs(workdir):
        pytest.skip("büyük/küçük harf duyarlı dosya sistemi")
    (workdir / "rapor.txt").write_text("içerik", encoding="utf-8")
    FileOps().run(ctx, action="move", path="rapor.txt", destination="Rapor.txt")
    assert [p.name for p in workdir.iterdir()] == ["Rapor.txt"]
    assert (workdir / "Rapor.txt").read_text(encoding="utf-8") == "içerik"
    (workdir / "proje").mkdir()
    (workdir / "proje" / "a.txt").write_text("a")
    FileOps().run(ctx, action="move", path="proje", destination="Proje")
    assert sorted(p.name for p in workdir.iterdir()) == ["Proje", "Rapor.txt"]
    assert (workdir / "Proje" / "a.txt").read_text() == "a"
