"""State round trips through the same executor on every Unix source Host."""

import json
import os
import sqlite3
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_component_contract_drift import _read
from test_host_controller import Runner, _profile, _with_product
from test_target_agent import _FakeMemory

from eidolon_ops.errors import OperationsError
from eidolon_ops.host_controller import HostController
from eidolon_ops.host_identity import derive_host_lan_identity
from eidolon_ops.hostagent import authorities, memory_realms
from eidolon_ops.hostagent.primitives import TargetError
from eidolon_ops.model import Capability
from eidolon_ops.paths import HostPlatform


def _host(tmp_path, platform, monkeypatch):
    controller = HostController(replace(_profile(tmp_path), platform=platform), Runner())
    topology = _read(frozenset())
    config = SimpleNamespace(
        sources={entry.component_id: SimpleNamespace(path=entry.source.parent.parent)
                 for entry in topology.contracts},
        capabilities=frozenset(), units=topology.systemd_units,
    )
    _with_product(controller, SimpleNamespace(config=config, health=lambda **_: {"status": "healthy"}))
    identity = controller.profile.paths.bootstrap_state_root / "host_identity.ed25519"
    identity.parent.mkdir(parents=True)
    identity.write_bytes(b"i" * 32)
    payload, runtime = controller.adapter.state._bindings()
    databases = []
    for entry in payload["authority_inventory"]:
        if entry["backup"] != "sqlite-online":
            continue
        path = Path(entry["path"])
        path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(path) as connection:
            connection.execute("CREATE TABLE truth (value TEXT)")
            connection.execute("INSERT INTO truth VALUES ('before')")
        _hub_lineage(path, entry)
        databases.append(path)
    monkeypatch.setattr(memory_realms, "_request", _FakeMemory())
    return controller, payload, runtime, databases, identity


def _hub_lineage(path, entry):
    if entry["declared_path"] != "/var/lib/eidolon/hub/eidolon-hub.sqlite3":
        return
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE hub_authority_state (singleton_id INT, owner_domain_id TEXT, owner_domain_generation INT, state_id TEXT)")
        connection.execute("INSERT INTO hub_authority_state VALUES (1, 'owner-test', 1, 'authority-state_test')")
    (path.parent / "authority-lineage.json").write_text(json.dumps({
        "contract_version": 1, "owner_domain_id": "owner-test",
        "owner_domain_generation": 1, "state_id": "authority-state_test",
    }))


@pytest.mark.parametrize("platform", [HostPlatform.MACOS, HostPlatform.LINUX])
def test_source_host_round_trip_covers_contract_state_without_admin_calls(tmp_path, platform, monkeypatch):
    controller, payload, _runtime, databases, identity = _host(tmp_path, platform, monkeypatch)
    assert {Capability.BACKUP, Capability.RESTORE} <= controller.adapter.capabilities
    captured = controller.backup(output=tmp_path / "backups").report
    assert captured["host_id"] == derive_host_lan_identity(identity.read_bytes()).host_id
    destination = Path(captured["local_directory"])
    assert any(entry["source"].endswith("hub/smarthome.sqlite3") for entry in captured["authorities"])
    assert {entry["state"] for entry in captured["not_covered"]} == {
        entry["id"] for entry in payload["authority_inventory"] if entry["backup"] == "none"
    }
    assert controller.runner.calls == []  # Online snapshots need no lifecycle mutation.
    for path in databases:
        with sqlite3.connect(path) as connection:
            connection.execute("UPDATE truth SET value='after'")
    before_plan = len(controller.runner.calls)
    plan = controller.restore(source=destination, apply=False).report
    assert plan["status"] == "planned"
    assert len(controller.runner.calls) == before_plan
    restored = controller.restore(source=destination, apply=True).report
    assert restored["status"] == "restored"
    assert [call[0][-1] for call in controller.runner.calls] == ["preflight", "stop", "start"]
    assert identity.read_bytes() == b"i" * 32
    for path in databases:
        assert path.stat().st_uid == os.getuid()
        with sqlite3.connect(path) as connection:
            assert connection.execute("SELECT value FROM truth").fetchone() == ("before",)


@pytest.mark.parametrize("damage", ["digest", "missing", "duplicate", "symlink", "memory", "identity"])
def test_invalid_packages_are_refused_before_stopping_a_source_host(tmp_path, monkeypatch, damage):
    controller, _payload, _runtime, databases, _identity = _host(tmp_path, HostPlatform.MACOS, monkeypatch)
    destination = Path(controller.backup(output=tmp_path / "backups").report["local_directory"])
    manifest_path = destination / "backup.json"
    manifest = json.loads(manifest_path.read_text())
    record = manifest["authorities"][0]
    if damage == "digest":
        (destination / record["file"]).write_bytes(b"damaged")
    elif damage == "missing":
        manifest["authorities"].pop()
    elif damage == "duplicate":
        manifest["authorities"].append(record)
    elif damage == "identity":
        manifest["host_id"] = "ehost-other"
    elif damage == "symlink":
        copy = destination / record["file"]
        copy.unlink()
        copy.symlink_to(databases[0])
    else:
        (destination / "memory/r_owner_one/palace/chroma.sqlite3").write_bytes(b"damaged")
    manifest_path.write_text(json.dumps(manifest))
    with pytest.raises(OperationsError):
        controller.restore(source=destination, apply=True)
    assert controller.runner.calls == []
    for path in databases:
        with sqlite3.connect(path) as connection:
            assert connection.execute("SELECT value FROM truth").fetchone() == ("before",)


def test_generic_restore_cannot_replace_a_different_owner_generation(tmp_path, monkeypatch):
    controller, _payload, _runtime, _databases, _identity = _host(tmp_path, HostPlatform.LINUX, monkeypatch)
    destination = Path(controller.backup(output=tmp_path / "backups").report["local_directory"])
    anchor = controller.profile.paths.state_root / "hub/authority-lineage.json"
    lineage = json.loads(anchor.read_text())
    lineage["owner_domain_generation"] = 2
    anchor.write_text(json.dumps(lineage))
    with pytest.raises(OperationsError, match="Owner Authority lineage differs"):
        controller.restore(source=destination, apply=True)
    assert controller.runner.calls == []


def test_failed_replacement_rolls_back_and_resumes_the_source_host(tmp_path, monkeypatch):
    controller, _payload, _runtime, databases, _identity = _host(tmp_path, HostPlatform.LINUX, monkeypatch)
    destination = Path(controller.backup(output=tmp_path / "backups").report["local_directory"])
    for path in databases:
        with sqlite3.connect(path) as connection:
            connection.execute("UPDATE truth SET value='live'")
    original = authorities.os.replace
    count = 0

    def replace_file(source, target):
        nonlocal count
        if str(source).endswith(".tmp"):
            count += 1
            if count == 2:
                raise OSError("write failure")
        return original(source, target)

    monkeypatch.setattr(authorities.os, "replace", replace_file)
    with pytest.raises(OSError, match="write failure"):
        controller.restore(source=destination, apply=True)
    assert controller.runner.calls[-1][0][-1] == "start"
    for path in databases:
        with sqlite3.connect(path) as connection:
            assert connection.execute("SELECT value FROM truth").fetchone() == ("live",)


def test_partial_stop_failure_resumes_without_replacing_state(tmp_path, monkeypatch):
    _controller, payload, runtime, databases, _identity = _host(tmp_path, HostPlatform.LINUX, monkeypatch)
    captured = authorities.backup({**payload, "release_id": "r1"}, runtime=runtime)
    calls = []

    def stop():
        calls.append("stop")
        raise TargetError("only some services stopped")

    runtime = replace(runtime, prepare=None, stop=stop,
                      start=lambda: calls.append("start") or {"status": "started"})
    with pytest.raises(TargetError, match="only some services stopped"):
        authorities.restore({**payload, "release_id": "r1", "manifest": captured},
                            runtime=runtime, directory=Path(captured["directory"]))
    assert calls == ["stop", "start"]
    for path in databases:
        with sqlite3.connect(path) as connection:
            assert connection.execute("SELECT value FROM truth").fetchone() == ("before",)


def test_lifecycle_preflight_failure_does_not_stop_or_start(tmp_path, monkeypatch):
    _controller, payload, runtime, _databases, _identity = _host(tmp_path, HostPlatform.MACOS, monkeypatch)
    captured = authorities.backup({**payload, "release_id": "r1"}, runtime=runtime)
    calls = []

    def prepare():
        raise TargetError("unsafe execution profile")

    runtime = replace(runtime, prepare=prepare,
                      stop=lambda: calls.append("stop"), start=lambda: calls.append("start"))
    with pytest.raises(TargetError, match="unsafe execution profile"):
        authorities.restore({**payload, "release_id": "r1", "manifest": captured},
                            runtime=runtime, directory=Path(captured["directory"]))
    assert calls == []


def test_new_declared_database_needs_no_executor_inventory_change(tmp_path, monkeypatch):
    controller, payload, runtime, _databases, _identity = _host(tmp_path, HostPlatform.LINUX, monkeypatch)
    entry = next(entry for entry in payload["authority_inventory"] if entry["backup"] == "sqlite-online")
    path = controller.profile.paths.state_root / "new-component.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE new_authority (value TEXT)")
    new = {**entry, "id": "new-component-authority", "path": str(path)}
    payload["authority_inventory"].append(new)
    captured = authorities.backup({**payload, "release_id": "r1"}, runtime=runtime)
    assert new["id"] in {entry["authority"] for entry in captured["authorities"]}


def test_repeated_backups_of_one_revision_preserve_the_first_copy(tmp_path, monkeypatch):
    _controller, payload, runtime, databases, _identity = _host(tmp_path, HostPlatform.LINUX, monkeypatch)
    first = authorities.backup({**payload, "release_id": "r1"}, runtime=runtime)
    digests = {entry["file"]: entry["sha256"] for entry in first["authorities"]}
    with sqlite3.connect(databases[0]) as connection:
        connection.execute("UPDATE truth SET value='changed'")
    second = authorities.backup({**payload, "release_id": "r1"}, runtime=runtime)
    assert first["directory"] != second["directory"]
    from eidolon_ops.hostagent.primitives import file_sha256

    assert all(file_sha256(Path(first["directory"]) / name) == digest for name, digest in digests.items())


def test_missing_component_declaration_is_not_a_smaller_backup(tmp_path):
    from eidolon_ops.component_contract import ContractTopology

    with pytest.raises(OperationsError, match="every component"):
        ContractTopology(silent=("eidolon_hub",)).authority_payload()
    with pytest.raises(TargetError, match="declared authority inventory"):
        authorities.inventory({})


def test_cli_and_console_offer_only_operations_the_composed_host_implements():
    from eidolon_ops.console.catalog import CATALOG
    from eidolon_ops.host_cli import operation_capability

    for operation in CATALOG:
        name = "service" if operation.name == "service-restart" else operation.name
        assert operation_capability(name) is operation.capability
