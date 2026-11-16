"""Projedeki .alice/permissions.toml kendine izin veremez: allow kuralları yalnız ALICE'in yazdığı haliyle geçerli."""
from __future__ import annotations

from alice import permissions as perm


def write(workdir, text):
    p = workdir / ".alice" / "permissions.toml"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    perm._project_cache.clear()  # noqa: SLF001 - aynı saniyede yazılan dosyanın mtime'ı değişmeyebilir
    return p


def test_cloned_repo_rules_do_not_grant_permissions(workdir):
    write(workdir, '[permissions]\nallow = ["*(*)", "run_command"]\ndeny = ["run_command(rm*)"]\n')
    rules = perm.project_rules(workdir)
    assert rules["allow"] == [] and rules["deny"] == ["run_command(rm*)"]  # deny/ask sıkılaştırır: geçerli
    assert perm.project_rules(workdir, include_untrusted=True)["allow"] == ["*(*)", "run_command"]
    assert not perm.project_rules_trusted(workdir)


def test_alice_written_rules_apply_until_tampered(workdir):
    perm.persist_allow(workdir, "run_command(git status*)")
    assert perm.project_rules(workdir)["allow"] == ["run_command(git status*)"]
    p = workdir / ".alice" / "permissions.toml"
    write(workdir, p.read_text(encoding="utf-8").replace('"run_command(git status*)"', '"*(*)"'))
    assert perm.project_rules(workdir)["allow"] == []  # dışarıdan değişti: onay gerekir
    perm.persist_allow(workdir, "run_command(npm test*)")  # tek onay sahte kuralı aklamaz
    assert perm.project_rules(workdir)["allow"] == ["run_command(npm test*)"]


def test_user_can_trust_and_remove(workdir):
    write(workdir, '[permissions]\nallow = ["run_command(make*)"]\n')
    assert perm.trust_project_rules(workdir)
    assert perm.project_rules(workdir)["allow"] == ["run_command(make*)"]
    assert perm.remove_project_rule(workdir, "allow", "run_command(make*)")
    assert perm.project_rules(workdir, include_untrusted=True)["allow"] == []


def test_permissions_apply_always_asks():
    from alice.tools.base import DANGER
    from alice.tools.permshare import PermissionsTool

    assert PermissionsTool().risk_for({"action": "apply"}) == DANGER
