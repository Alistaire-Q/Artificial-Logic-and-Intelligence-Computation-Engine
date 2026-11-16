"""Arayüz betikleri aynı global kapsamı paylaşır: bir dosyada `const x` varken başka dosyada `function x` tanımlamak
o dosyanın TAMAMINI yüklenmez yapar (ör. showUsage çakışması bütün yan paneli bozmuştu). Betikler index.html'deki
sırayla birleştirilip tek betik olarak ayrıştırılır."""

from __future__ import annotations

import re
import shutil
import subprocess

import pytest

from alice.app.server import STATIC


@pytest.mark.skipif(shutil.which("node") is None, reason="node yok")
def test_app_scripts_share_scope_without_conflicts(tmp_path):
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    names = [n for n in re.findall(r'src="/static/([^"]+\.js)', html) if not n.endswith(".min.js")]
    assert "ui.js" in names and "app.js" in names
    bundle = tmp_path / "all.js"
    bundle.write_text("\n;\n".join((STATIC / n).read_text(encoding="utf-8") for n in names), encoding="utf-8")
    p = subprocess.run(["node", "--check", str(bundle)], capture_output=True, text=True, encoding="utf-8")
    assert p.returncode == 0, p.stderr[-800:]


@pytest.mark.skipif(shutil.which("node") is None, reason="node yok")
def test_highlighter_survives_unclosed_shell_variable():
    """Akış sırasında yarım gelen "${" (kabuk/PowerShell) renklendiriciyi TypeError ile düşürüyordu."""
    js = ("global.window = {}; require(process.argv[1]); const H = window.AliceHL;"
          "console.log(H.highlight('echo ${HO', 'bash') + '|' + H.highlight('$x ${a} <b>', 'ps1'));")
    p = subprocess.run(["node", "-e", js, str(STATIC / "highlight.js")], capture_output=True, text=True,
                       encoding="utf-8")
    assert p.returncode == 0, p.stderr[-800:]
    assert "${HO" in p.stdout and '<span class="tk-v">${a}</span>' in p.stdout and "&lt;b&gt;" in p.stdout
