from __future__ import annotations

import json
import sqlite3
import subprocess
from pathlib import Path

import pytest

from eidolon_ops.hostagent import authority_restore, contract
from eidolon_ops.hostagent.primitives import TargetError, file_sha256

pytestmark = pytest.mark.component

_AUTHORITY = {
    "contract_version": 1,
    "owner_domain_id": "owner-restore-test",
    "owner_domain_generation": 2,
    "state_id": "authority-state_restore-test",
}


def _database(path: Path, *, generation: int = 2) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    try:
        connection.executescript(
            """
            CREATE TABLE hub_authority_state (
              singleton_id INTEGER PRIMARY KEY,
              owner_domain_id TEXT NOT NULL,
              owner_domain_generation INTEGER NOT NULL,
              state_id TEXT NOT NULL
            );
            CREATE TABLE hub_devices (device_id TEXT PRIMARY KEY, lifecycle_state TEXT NOT NULL);
            CREATE TABLE hub_claim_command_results (command_id TEXT PRIMARY KEY, outcome TEXT NOT NULL);
            CREATE TABLE hub_claim_events (event_id TEXT PRIMARY KEY, event_type TEXT NOT NULL);
            CREATE TABLE hub_device_tombstones (device_id TEXT PRIMARY KEY, generation INTEGER NOT NULL);
            CREATE TABLE hub_outbox (event_id TEXT PRIMARY KEY, published_at TEXT);
            CREATE TABLE hub_claim_event_inbox (consumer TEXT, event_id TEXT, PRIMARY KEY (consumer,event_id));
            CREATE TABLE hub_claim_consumer_cursors (consumer TEXT PRIMARY KEY, stream_position INTEGER);
            """
        )
        connection.execute(
            "INSERT INTO hub_authority_state VALUES (1,?,?,?)",
            (_AUTHORITY["owner_domain_id"], generation, _AUTHORITY["state_id"]),
        )
        connection.execute("INSERT INTO hub_devices VALUES ('device-1','active')")
        connection.execute("INSERT INTO hub_claim_command_results VALUES ('command-1','committed')")
        connection.execute("INSERT INTO hub_claim_events VALUES ('event-1','ClaimActivated')")
        connection.execute("INSERT INTO hub_device_tombstones VALUES ('device-old',1)")
        connection.execute("INSERT INTO hub_outbox VALUES ('event-1',NULL)")
        connection.execute("INSERT INTO hub_claim_event_inbox VALUES ('kernel','event-1')")
        connection.execute("INSERT INTO hub_claim_consumer_cursors VALUES ('kernel',7)")
        connection.commit()
    finally:
        connection.close()


def _anchor(path: Path, *, generation: int = 2) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    value = {**_AUTHORITY, "owner_domain_generation": generation}
    path.write_text(json.dumps(value) + "\n", encoding="utf-8")


def _payload(root: Path) -> dict[str, object]:
    stage = root / "var/tmp/eidolon-authority-restore-r1"
    return {
        "units": list(contract.PRODUCT_UNITS),
        "release_id": "r1",
        "authority_restore": {
            "owner_domain_id": _AUTHORITY["owner_domain_id"],
            "owner_domain_generation": _AUTHORITY["owner_domain_generation"],
            "state_id": _AUTHORITY["state_id"],
            "database_sha256": file_sha256(stage / "eidolon-hub.sqlite3"),
            "anchor_sha256": file_sha256(stage / "authority-lineage.json"),
        },
    }


def _stage(root: Path, *, generation: int = 2) -> dict[str, object]:
    path = root / "var/tmp/eidolon-authority-restore-r1"
    path.mkdir(parents=True, mode=0o700)
    _database(path / "eidolon-hub.sqlite3", generation=generation)
    _anchor(path / "authority-lineage.json", generation=generation)
    for item in path.iterdir():
        item.chmod(0o600)
    return _payload(root)


def test_restore_authority_preserves_complete_same_generation_state(tmp_path: Path) -> None:
    payload = _stage(tmp_path)
    live = tmp_path / authority_restore.authority_state.HUB_DATABASE.relative_to("/")
    _database(live, generation=2)
    connection = sqlite3.connect(live)
    connection.execute("DELETE FROM hub_devices")
    connection.commit()
    connection.close()
    _anchor(tmp_path / authority_restore.authority_state.AUTHORITY_ANCHOR.relative_to("/"))

    result = authority_restore.restore(
        payload, root=tmp_path, manage_services=False
    )

    assert result == {
        "status": "authority_restored",
        "authority": _AUTHORITY,
        "generation_advanced": False,
        "commissioning_required": False,
    }
    connection = sqlite3.connect(live)
    try:
        assert connection.execute("SELECT * FROM hub_devices").fetchall() == [
            ("device-1", "active")
        ]
        assert connection.execute("SELECT * FROM hub_claim_command_results").fetchall() == [
            ("command-1", "committed")
        ]
        assert connection.execute("SELECT * FROM hub_claim_events").fetchall() == [
            ("event-1", "ClaimActivated")
        ]
        assert connection.execute("SELECT * FROM hub_device_tombstones").fetchall() == [
            ("device-old", 1)
        ]
        assert connection.execute("SELECT * FROM hub_outbox").fetchall() == [
            ("event-1", None)
        ]
        assert connection.execute("SELECT * FROM hub_claim_event_inbox").fetchall() == [
            ("kernel", "event-1")
        ]
        assert connection.execute("SELECT * FROM hub_claim_consumer_cursors").fetchall() == [
            ("kernel", 7)
        ]
    finally:
        connection.close()

    # A crash/retry replays the exact restore instead of minting a generation.
    assert authority_restore.restore(payload, root=tmp_path, manage_services=False)[
        "authority"
    ] == _AUTHORITY


def test_restore_starts_and_requires_both_hub_and_ingress(tmp_path: Path) -> None:
    payload = _stage(tmp_path)
    calls: list[tuple[str, ...]] = []

    def command(value: tuple[str, ...], *, timeout: float):
        calls.append(value)
        return subprocess.CompletedProcess(value, 0, "active\nactive\n", "")

    result = authority_restore.restore(
        payload,
        root=tmp_path,
        command=command,
        manage_services=True,
        ready_timeout_seconds=1,
    )

    assert result["status"] == "authority_restored"
    assert (
        "/usr/bin/systemctl",
        "start",
        authority_restore.authority_state.HUB_UNIT,
        authority_restore.authority_state.HUB_INGRESS_UNIT,
    ) in calls
    assert (
        "/usr/bin/systemctl",
        "is-active",
        authority_restore.authority_state.HUB_UNIT,
        authority_restore.authority_state.HUB_INGRESS_UNIT,
    ) in calls


@pytest.mark.parametrize("failure", ["missing-anchor", "partial", "generation-mismatch", "digest"])
def test_restore_authority_fails_closed_before_live_state_changes(
    tmp_path: Path, failure: str
) -> None:
    payload = _stage(tmp_path)
    stage = tmp_path / "var/tmp/eidolon-authority-restore-r1"
    if failure == "missing-anchor":
        (stage / "authority-lineage.json").unlink()
    elif failure == "partial":
        (stage / "extra").write_text("unsafe", encoding="utf-8")
    elif failure == "generation-mismatch":
        _anchor(stage / "authority-lineage.json", generation=1)
        payload["authority_restore"]["anchor_sha256"] = file_sha256(
            stage / "authority-lineage.json"
        )
    else:
        payload["authority_restore"]["database_sha256"] = "0" * 64

    live = tmp_path / authority_restore.authority_state.HUB_DATABASE.relative_to("/")
    _database(live, generation=2)
    before = live.read_bytes()

    with pytest.raises(TargetError, match=r"AUTHORITY_RESTORE_(?:INCOMPLETE|INVALID|MISMATCH)"):
        authority_restore.restore(payload, root=tmp_path, manage_services=False)

    assert live.read_bytes() == before


def test_authority_backup_refuses_marker_anchor_or_requested_generation_drift(
    tmp_path: Path,
) -> None:
    database = tmp_path / authority_restore.authority_state.HUB_DATABASE.relative_to("/")
    anchor = tmp_path / authority_restore.authority_state.AUTHORITY_ANCHOR.relative_to("/")
    _database(database, generation=2)
    _anchor(anchor, generation=1)
    payload = {
        "units": list(contract.PRODUCT_UNITS),
        "release_id": "r1",
        "authority_restore": {
            "owner_domain_id": _AUTHORITY["owner_domain_id"],
            "owner_domain_generation": 2,
            "state_id": _AUTHORITY["state_id"],
        },
    }

    with pytest.raises(TargetError, match="AUTHORITY_RESTORE_MISMATCH"):
        authority_restore.backup(payload, root=tmp_path)


def test_restore_stage_reset_is_exact_safe_and_idempotent(tmp_path: Path) -> None:
    stage = tmp_path / "var/tmp/eidolon-authority-restore-release-8"
    stage.mkdir(parents=True)
    stage.chmod(0o700)
    (stage / "old").write_text("stale")
    unrelated = tmp_path / "var/tmp/eidolon-authority-restore-release-80"
    unrelated.mkdir()

    first = authority_restore.clear_restore_stage(
        {"release_id": "release-8"}, root=tmp_path
    )
    second = authority_restore.clear_restore_stage(
        {"release_id": "release-8"}, root=tmp_path
    )

    assert first["removed_previous"] is True
    assert second["removed_previous"] is False
    assert unrelated.is_dir()


def test_restore_stage_reset_rejects_symlink(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    stage = tmp_path / "var/tmp/eidolon-authority-restore-release-8"
    stage.parent.mkdir(parents=True)
    stage.symlink_to(outside, target_is_directory=True)

    with pytest.raises(TargetError, match="AUTHORITY_RESTORE_INVALID"):
        authority_restore.clear_restore_stage(
            {"release_id": "release-8"}, root=tmp_path
        )
    assert outside.is_dir()


def test_restore_stage_finalize_is_exact_safe_and_idempotent(tmp_path: Path) -> None:
    stage = tmp_path / "var/tmp/eidolon-authority-restore-release-8"
    stage.mkdir(parents=True)
    stage.chmod(0o700)
    secret = stage / "eidolon-hub.sqlite3"
    secret.write_bytes(b"private snapshot")
    secret.chmod(0o600)
    unrelated = tmp_path / "var/tmp/eidolon-authority-restore-release-80"
    unrelated.mkdir()

    first = authority_restore.finalize_restore_stage(
        {"release_id": "release-8"}, root=tmp_path
    )
    second = authority_restore.finalize_restore_stage(
        {"release_id": "release-8"}, root=tmp_path
    )

    assert first["status"] == "authority_restore_stage_absent"
    assert first["removed"] is True
    assert second["removed"] is False
    assert not stage.exists()
    assert unrelated.is_dir()


@pytest.mark.parametrize("drift", ["directory", "file"])
def test_restore_rejects_private_stage_permission_drift(
    tmp_path: Path, drift: str
) -> None:
    payload = _stage(tmp_path)
    stage = tmp_path / "var/tmp/eidolon-authority-restore-r1"
    if drift == "directory":
        stage.chmod(0o755)
    else:
        (stage / "eidolon-hub.sqlite3").chmod(0o644)

    with pytest.raises(TargetError, match="ownership or mode drifted"):
        authority_restore.restore_plan(payload, root=tmp_path)
