"""Legacy ownership transfer preserves authority bytes and survives interruption."""

import json
import os
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_local_product import _product

from eidolon_ops.source_state import _journal, _recover, _transfer, prepare_logs


def test_transfer_preserves_identity_and_sqlite_wal_and_keeps_original(tmp_path):
    profile = _product(tmp_path, foundation_mode="external").profile
    root = profile.paths.bootstrap_state_root
    identity = (root / "host_identity.ed25519").read_bytes()
    tls = root / "commissioning-tls.pem"
    tls.write_text("preserve component-declared mode")
    tls.chmod(0o640)
    connection = sqlite3.connect(root / "authority.sqlite3")
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("CREATE TABLE authority (id TEXT)")
    connection.execute("INSERT INTO authority VALUES ('preserved')")
    connection.commit()
    backup = _transfer(root, profile, runtime=False, mode=0o700)
    connection.close()
    assert (root / "host_identity.ed25519").read_bytes() == identity
    assert (backup / "host_identity.ed25519").read_bytes() == identity
    assert (root / "commissioning-tls.pem").stat().st_mode & 0o777 == 0o640
    with sqlite3.connect(root / "authority.sqlite3") as copied:
        assert copied.execute("SELECT id FROM authority").fetchall() == [("preserved",)]
    assert all(p.stat().st_uid == os.getuid() for p in root.rglob("*"))
    assert not _journal(root).exists()


def test_failed_publish_restores_original_directory(tmp_path, monkeypatch):
    profile = _product(tmp_path, foundation_mode="external").profile
    root = profile.paths.bootstrap_state_root
    before = (root / "host_identity.ed25519").read_bytes()
    rename = Path.rename

    def fail_publish(path, destination):
        if path.name.startswith(f".{root.name}.source-") and not path.name.endswith("-original"):
            raise OSError("publish failed")
        return rename(path, destination)

    monkeypatch.setattr(Path, "rename", fail_publish)
    with pytest.raises(OSError, match="publish failed"):
        _transfer(root, profile, runtime=False, mode=0o700)
    assert (root / "host_identity.ed25519").read_bytes() == before
    _recover(root, profile)
    assert not _journal(root).exists()


def test_recovery_after_crash_between_renames_restores_original(tmp_path):
    profile = _product(tmp_path, foundation_mode="external").profile
    root = profile.paths.bootstrap_state_root
    staging = root.with_name(f".{root.name}.source-test")
    backup = staging.with_name(staging.name + "-original")
    staging.mkdir(mode=0o700)
    before = (root / "host_identity.ed25519").read_bytes()
    root.rename(backup)
    _journal(root).write_text(json.dumps({"staging": str(staging), "backup": str(backup)}))
    _recover(root, profile)
    assert (root / "host_identity.ed25519").read_bytes() == before
    assert not staging.exists()


def test_transfer_never_follows_symlink(tmp_path):
    profile = _product(tmp_path, foundation_mode="external").profile
    root = profile.paths.bootstrap_state_root
    unrelated = tmp_path / "private"
    unrelated.write_text("unchanged")
    (root / "link").symlink_to(unrelated)
    with pytest.raises(Exception, match="symlink"):
        _transfer(root, profile, runtime=False, mode=0o700)
    assert unrelated.read_text() == "unchanged"
    assert (root / "host_identity.ed25519").exists()


def test_legacy_log_is_preserved_without_chown_and_new_log_is_operator_owned(tmp_path, monkeypatch):
    profile = _product(tmp_path, foundation_mode="external").profile
    log = profile.paths.log_root / "admin/control.err.log"
    log.parent.mkdir(parents=True)
    log.write_text("legacy log")
    profile.paths.config_root.mkdir(parents=True)
    (profile.paths.config_root / "supervisor.conf").write_text(
        "[program:control]\nstderr_logfile=%(ENV_EIDOLON_LOG_ROOT)s/admin/control.err.log\n"
    )
    original = Path.stat

    def legacy_owner(path, **kwargs):
        details = original(path, **kwargs)
        if path == log and path.read_text() == "legacy log":
            return SimpleNamespace(st_uid=0, st_mode=details.st_mode)
        return details

    monkeypatch.setattr(Path, "stat", legacy_owner)
    monkeypatch.setattr(os, "chown", lambda *a, **k: pytest.fail("must never chown"))
    backups = prepare_logs(profile, ())
    assert len(backups) == 1 and backups[0].read_text() == "legacy log"
    assert log.read_bytes() == b""
    assert log.stat().st_uid == os.getuid()
    assert prepare_logs(profile, ()) == []
