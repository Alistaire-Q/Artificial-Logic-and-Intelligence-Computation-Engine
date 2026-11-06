"""CLIProxyAPI otomatik kurulum testleri: sürüm dosyası seçimi, checksum, config üretimi ve kurulum."""

from __future__ import annotations

import hashlib
import io
import zipfile
import pytest

from alice.app import cliproxy, cliproxy_setup


def test_asset_name_per_platform():
    assert cliproxy_setup.asset_name("v8.0.57", "Windows", "AMD64") == "CLIProxyAPI_8.0.57_windows_amd64.zip"
    assert cliproxy_setup.asset_name("v8.0.57", "Linux", "aarch64") == "CLIProxyAPI_8.0.57_linux_aarch64.tar.gz"
    assert cliproxy_setup.asset_name("v8.0.57", "Darwin", "arm64") == "CLIProxyAPI_8.0.57_darwin_aarch64.tar.gz"


def test_checksum_parse_and_verify(tmp_path):
    data = tmp_path / "a.bin"
    data.write_bytes(b"abc")
    digest = hashlib.sha256(b"abc").hexdigest()
    text = f"{digest}  a.bin\n{'0' * 64}  b.bin\n"
    assert cliproxy_setup.checksum_for(text, "a.bin") == digest
    with pytest.raises(ValueError):
        cliproxy_setup.checksum_for(text, "c.bin")


def test_ensure_config_creates_keys_and_env(tmp_path, monkeypatch):
    home = tmp_path / "cliproxy"
    monkeypatch.setattr(cliproxy, "home", lambda: home)
    keys = cliproxy_setup.ensure_config()
    assert (home / "config.yaml").exists() and (home / "keys.txt").exists()
    assert keys["api_key"] in (home / "config.yaml").read_text(encoding="utf-8")
    # ikinci çağrı dokunmaz
    again = cliproxy_setup.ensure_config()
    assert again["api_key"] == keys["api_key"]


def test_install_downloads_verifies_and_extracts(tmp_path, monkeypatch):
    home = tmp_path / "cliproxy"
    monkeypatch.setattr(cliproxy, "home", lambda: home)
    name = cliproxy_setup.asset_name("v9.9.9")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr(cliproxy.binary_name(), b"MZfake")
        z.writestr("README.md", "readme")
    blob = buf.getvalue()
    digest = hashlib.sha256(blob).hexdigest()

    monkeypatch.setattr(cliproxy_setup, "_release_json",
                        lambda version: {"assets": [{"name": name, "browser_download_url": "https://example/" + name}]})
    monkeypatch.setattr(cliproxy_setup, "_find_checksums", lambda release: f"{digest}  {name}\n")
    def fake_download(url, dest):
        dest.write_bytes(blob)
        return dest

    monkeypatch.setattr(cliproxy_setup, "_download", fake_download)
    r = cliproxy_setup.install("v9.9.9")
    assert r["ok"] and (home / cliproxy.binary_name()).read_bytes() == b"MZfake"
    assert (home / "config.yaml").exists()


def test_install_rejects_bad_checksum(tmp_path, monkeypatch):
    home = tmp_path / "cliproxy"
    monkeypatch.setattr(cliproxy, "home", lambda: home)
    name = cliproxy_setup.asset_name("v9.9.9")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr(cliproxy.binary_name(), b"MZfake")
    blob = buf.getvalue()
    monkeypatch.setattr(cliproxy_setup, "_release_json",
                        lambda version: {"assets": [{"name": name, "browser_download_url": "https://example/" + name}]})
    monkeypatch.setattr(cliproxy_setup, "_find_checksums", lambda release: f"{'0' * 64}  {name}\n")
    def fake_download(url, dest):
        dest.write_bytes(blob)
        return dest

    monkeypatch.setattr(cliproxy_setup, "_download", fake_download)
    r = cliproxy_setup.install("v9.9.9")
    assert not r["ok"] and "checksum" in r["error"]
