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
