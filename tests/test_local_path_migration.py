from __future__ import annotations

import os
from pathlib import Path

import pytest

from eidolon_ops.controller import OperationsError
from eidolon_ops.local_path_migration import LocalPathMigrator
from eidolon_ops.paths import HostPaths, HostProfile


def _profile(tmp_path: Path) -> HostProfile:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    script = workspace / "eidolon_ops/deploy/dev/run_all.sh"
    script.parent.mkdir(parents=True)
    script.write_text("#!/bin/sh\n", encoding="utf-8")
    script.chmod(0o755)
    return HostProfile(
        path=tmp_path / "host.toml",
        host_id="mac-test",
        platform="macos",
        driver="local-supervisord",
        paths=HostPaths(
            install_root=workspace,
            current_root=workspace,
            config_root=workspace / "eidolon_ops/config",
            state_root=tmp_path / "eidolon/data",
            runtime_root=tmp_path / "eidolon/run",
            log_root=tmp_path / "eidolon/logs",
            cache_root=tmp_path / "eidolon/cache",
            bootstrap_state_root=tmp_path / "eidolon/bootstrap",
            bootstrap_runtime_root=tmp_path / "eidolon/run/bootstrap",
        ),
        lifecycle_script=script,
        operations_config=None,
    )


def test_local_path_migration_is_explicit_atomic_and_preserves_state(
    tmp_path: Path,
) -> None:
    profile = _profile(tmp_path)
    state = profile.paths.state_root
    state.mkdir(parents=True)
    agent = state / "eidolon-agent.sqlite3"
    agent.write_text("agent-db", encoding="utf-8")
    nats = state / "nats-jetstream"
    nats.mkdir()
    (nats / "stream").write_text("nats", encoding="utf-8")
    debug = state.parent / "debug"
    debug.mkdir()
    (debug / "trace").write_text("diagnostic", encoding="utf-8")
    system = state / "eidolon-system.sqlite3"
    system.write_text("system-db", encoding="utf-8")
    migrator = LocalPathMigrator(profile)

    assert migrator.plan()["status"] == "migration_required"
    result = migrator.apply()

    assert result["status"] == "migrated"
    assert (state / "agent/eidolon-agent.sqlite3").read_text(encoding="utf-8") == ("agent-db")
    assert (state / "nats/jetstream/stream").read_text(encoding="utf-8") == "nats"
    assert (profile.paths.cache_root / "debug/trace").read_text(encoding="utf-8") == "diagnostic"
    assert system.read_text(encoding="utf-8") == "system-db"
    assert migrator.plan()["status"] == "clean"
    assert migrator.evidence_path.is_file()


def test_local_path_migration_refuses_conflicts_and_live_stack(tmp_path: Path) -> None:
    profile = _profile(tmp_path)
    source = profile.paths.state_root / "eidolon-agent.sqlite3"
    target = profile.paths.state_root / "agent/eidolon-agent.sqlite3"
    source.parent.mkdir(parents=True)
    target.parent.mkdir(parents=True)
    source.write_text("old", encoding="utf-8")
    target.write_text("new", encoding="utf-8")
    migrator = LocalPathMigrator(profile)

    with pytest.raises(OperationsError, match="source/target conflicts"):
        migrator.apply()

    target.unlink()
    pid = profile.paths.runtime_root / "ops/supervisord.pid"
    pid.parent.mkdir(parents=True, exist_ok=True)
    pid.write_text(str(os.getpid()), encoding="utf-8")
    with pytest.raises(OperationsError, match="stop the local Eidolon stack"):
        migrator.apply()
