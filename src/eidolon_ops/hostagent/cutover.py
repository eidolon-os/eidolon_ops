"""Host-layer half of one release activation transaction.

The sealed activator owns component links and system assets.  Ops owns the
Host-bound configuration installed immediately before it.  These operations
join both halves with an immutable snapshot and a final receipt, so restoring
links can never silently leave candidate Host configuration behind.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import stat
import uuid
from collections.abc import Mapping
from pathlib import Path

from . import authority_reset, contract, primitives
from .primitives import TargetError

_SCHEMA_VERSION = 1
_MODES = {"reversible", "forward-only"}
_SNAPSHOT_NAME = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}-host-[0-9a-f]{32}$"
)


def _mode(payload: Mapping[str, object]) -> str:
    value = payload.get("cutover_mode")
    if value not in _MODES:
        raise TargetError("release cutover mode is invalid")
    return str(value)


def _evidence_root(root: Path) -> Path:
    return primitives.host_path(root, contract.FIXED_DATA["deployment_evidence"])


def _snapshot_path(payload: Mapping[str, object], root: Path) -> Path:
    value = payload.get("host_snapshot")
    if not isinstance(value, str):
        raise TargetError("Host cutover snapshot is invalid")
    path = Path(value)
    evidence = _evidence_root(root)
    if (not path.is_absolute() or path.parent != contract.FIXED_DATA["deployment_evidence"]
            or _SNAPSHOT_NAME.fullmatch(path.name) is None):
        raise TargetError("Host cutover snapshot is outside deployment evidence")
    return path if root == Path("/") else evidence / path.name


def _authority(root: Path) -> dict[str, object]:
    """The Owner Authority lineage this Host holds, read from durable evidence.

    Deliberately not ``authority-bootstrap.json``.  That file is a one-shot
    capability: Hub accepts it only into an empty database and deletes it on
    use, so on any Host whose Hub has ever started it is simply gone.  Reading
    it here meant a Host could be installed exactly once and then never
    deployed to again — the only way to ship newer code was a wipe that
    destroyed the Owner, the Companions and the Claims.

    《设备生命周期状态机与恢复边》§3.6.1 names the Host's own evidence for a
    lineage as the Hub database marker together with the external lineage
    anchor, and that is what ``controller.authority_capability`` already
    decides against.  A spent capability was never evidence of anything, and a
    cutover could not have used it as such in any case: the capability file is
    a refreshable Host layer input, so this transaction re-ships that very
    file between the ``before`` and ``after`` readings.
    """

    observed = authority_reset.established_lineage(root=root)
    established = observed["established"]
    if not isinstance(established, dict):
        raise TargetError(
            "Host holds no established Owner Authority lineage; a release cutover "
            "requires a Hub database marker and lineage anchor that agree"
        )
    return established


def _current_targets(root: Path) -> dict[str, str]:
    targets: dict[str, str] = {}
    for component, absolute in contract.CURRENT_LINKS.items():
        path = primitives.host_path(root, absolute)
        if path.is_symlink():
            targets[component] = os.readlink(path)
        elif path.exists():
            raise TargetError(f"current component target is not a symlink: {component}")
    return targets


def _host_files(root: Path, files: Path, *, copy: bool) -> dict[str, object]:
    result: dict[str, object] = {}
    for name in contract.REFRESHABLE_HOST_LAYER_INPUTS:
        absolute, _user, _group, _mode = contract.INSTALL_INPUTS[name]
        path = primitives.host_path(root, absolute)
        if not path.exists() and not path.is_symlink():
            result[name] = {"path": str(absolute), "exists": False}
            continue
        if path.is_symlink() or not path.is_file():
            raise TargetError(f"Host layer input is unsafe: {absolute}")
        metadata = path.stat()
        record = {
            "path": str(absolute),
            "exists": True,
            "sha256": primitives.file_sha256(path),
            "mode": stat.S_IMODE(metadata.st_mode),
            "uid": metadata.st_uid,
            "gid": metadata.st_gid,
        }
        if copy:
            destination = files / name
            shutil.copyfile(path, destination)
            os.chmod(destination, 0o600)
        result[name] = record
    return result


def snapshot(payload: Mapping[str, object], *, root: Path = Path("/")) -> dict[str, object]:
    release_id = contract.fixed_release_id(payload)
    mode = _mode(payload)
    root = root.resolve()
    evidence = _evidence_root(root)
    if evidence.is_symlink():
        raise TargetError("deployment evidence root is unsafe")
    evidence.mkdir(parents=True, exist_ok=True, mode=0o700)
    transaction_id = uuid.uuid4().hex
    destination = evidence / f"{release_id}-host-{transaction_id}"
    destination.mkdir(mode=0o700)
    files = destination / "host-files"
    files.mkdir(mode=0o700)
    previous_targets = _current_targets(root)
    if set(previous_targets) != set(contract.CURRENT_LINKS):
        raise TargetError("release cutover requires a complete previous component graph")
    document = {
        "schema_version": _SCHEMA_VERSION,
        "release_id": release_id,
        "cutover_mode": mode,
        "host_transaction_id": transaction_id,
        "status": "snapshotted",
        "previous_targets": previous_targets,
        "authority_before": _authority(root),
        "host_files_before": _host_files(root, files, copy=True),
        "schema_migration": {"state": "not_started"},
        # Which commit of each repository this release was built from. Recorded
        # here because this directory is the one release artifact reclamation
        # never touches: the descriptor under /opt/eidolon/releases carried the
        # same facts and went away with the directory at commit, which is why
        # "what was in the release from three weeks ago" had no answer at all.
        "sources": contract.optional_source_provenance(payload),
    }
    primitives.atomic_json(destination / "cutover.json", document)
    return {
        "status": "host_cutover_snapshotted",
        "release_id": release_id,
        "cutover_mode": mode,
        "host_transaction_id": transaction_id,
        "host_snapshot": str(
            contract.FIXED_DATA["deployment_evidence"] / destination.name
        ),
        "authority": document["authority_before"],
        "previous_targets": document["previous_targets"],
    }


def _load(payload: Mapping[str, object], root: Path) -> tuple[Path, dict[str, object]]:
    path = _snapshot_path(payload, root)
    manifest = path / "cutover.json"
    if path.is_symlink() or not path.is_dir() or manifest.is_symlink() or not manifest.is_file():
        raise TargetError("Host cutover snapshot is missing or unsafe")
    try:
        document = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise TargetError("Host cutover receipt is invalid") from exc
    if (
        not isinstance(document, dict)
        or document.get("schema_version") != _SCHEMA_VERSION
        or document.get("release_id") != contract.fixed_release_id(payload)
        or document.get("cutover_mode") != _mode(payload)
    ):
        raise TargetError("Host cutover receipt does not match the release")
    return path, document


def restore(payload: Mapping[str, object], *, root: Path = Path("/")) -> dict[str, object]:
    root = root.resolve()
    path, document = _load(payload, root)
    if document["cutover_mode"] == "forward-only":
        previous_targets = document.get("previous_targets")
        schema_migration = document.get("schema_migration")
        if (
            not isinstance(previous_targets, dict)
            or previous_targets != _current_targets(root)
            or not isinstance(schema_migration, dict)
            or schema_migration.get("state") != "not_started"
            or document.get("status") not in {"snapshotted", "host_restored"}
        ):
            raise TargetError(
                "forward-only Host configuration crossed or cannot prove the "
                "persistent-state barrier"
            )
    if _authority(root) != document.get("authority_before"):
        raise TargetError("Authority lineage changed during release cutover")
    records = document.get("host_files_before")
    if not isinstance(records, dict) or set(records) != set(contract.REFRESHABLE_HOST_LAYER_INPUTS):
        raise TargetError("Host cutover snapshot is incomplete")
    files = path / "host-files"
    if files.is_symlink() or not files.is_dir():
        raise TargetError("Host cutover snapshot content is invalid")
    actions: list[tuple[Path | None, Path, dict[str, object]]] = []
    for name in contract.REFRESHABLE_HOST_LAYER_INPUTS:
        record = records[name]
        absolute = contract.INSTALL_INPUTS[name][0]
        destination = primitives.host_path(root, absolute)
        if not isinstance(record, dict) or record.get("path") != str(absolute):
            raise TargetError("Host cutover snapshot record is invalid")
        if record.get("exists") is False:
            if destination.is_symlink() or (destination.exists() and not destination.is_file()):
                raise TargetError(f"Host layer restore target is unsafe: {absolute}")
            actions.append((None, destination, record))
            continue
        source = files / name
        if (
            record.get("exists") is not True
            or source.is_symlink()
            or not source.is_file()
            or primitives.file_sha256(source) != record.get("sha256")
            or type(record.get("mode")) is not int
            or type(record.get("uid")) is not int
            or type(record.get("gid")) is not int
            or not 0 <= record["mode"] <= 0o777
            or record["uid"] < 0
            or record["gid"] < 0
        ):
            raise TargetError("Host cutover snapshot content is invalid")
        if destination.is_symlink() or (destination.exists() and not destination.is_file()):
            raise TargetError(f"Host layer restore target is unsafe: {absolute}")
        actions.append((source, destination, record))
    expected_copies = {
        name for name, record in records.items() if record.get("exists") is True
    }
    if {item.name for item in files.iterdir()} != expected_copies:
        raise TargetError("Host cutover snapshot content is incomplete")
    # Validation above is deliberately complete before the first live path is
    # touched.  A later corrupt entry therefore cannot produce a partial Host
    # configuration rollback.
    for source, destination, record in actions:
        if source is None:
            destination.unlink(missing_ok=True)
            continue
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.tmp")
        try:
            shutil.copyfile(source, temporary)
            os.chmod(temporary, record["mode"])
            os.chown(temporary, record["uid"], record["gid"])
            os.replace(temporary, destination)
        finally:
            temporary.unlink(missing_ok=True)
    primitives.checked("Host layer rollback systemd reload", ("/usr/bin/systemctl", "daemon-reload"))
    document["status"] = "host_restored"
    primitives.atomic_json(path / "cutover.json", document)
    return {
        "status": "host_cutover_restored",
        "host_snapshot": payload["host_snapshot"],
        "persistent_state_barrier": "not_crossed",
    }


def finalize(payload: Mapping[str, object], *, root: Path = Path("/")) -> dict[str, object]:
    root = root.resolve()
    path, document = _load(payload, root)
    activation = payload.get("activation")
    if not isinstance(activation, dict):
        raise TargetError("component activation receipt is missing")
    mode = document["cutover_mode"]
    status = activation.get("status")
    if (
        activation.get("cutover_mode") != mode
        or not isinstance(activation.get("transaction_id"), str)
        or re.fullmatch(r"[0-9a-f]{32}", str(activation.get("transaction_id"))) is None
        or status not in {"activated", "forward_fix_required"}
        or (status == "forward_fix_required" and mode != "forward-only")
    ):
        raise TargetError("component activation receipt does not match Host cutover")
    authority_after = _authority(root)
    if authority_after != document.get("authority_before"):
        raise TargetError("Authority lineage changed during release cutover")
    active_targets = _current_targets(root)
    expected_prefix = f"/opt/eidolon/releases/{document['release_id']}/"
    if set(active_targets) != set(contract.CURRENT_LINKS) or any(
        not target.startswith(expected_prefix) for target in active_targets.values()
    ):
        raise TargetError("component graph does not reference the activated release")
    document.update(
        {
            "status": "activated" if status == "activated" else "forward_fix_required",
            "activation": activation,
            "authority_after": authority_after,
            "host_files_after": _host_files(root, path / "unused", copy=False),
            "active_targets": active_targets,
            "schema_migration": {
                "state": (
                    "persistent_state_barrier_crossed"
                    if activation.get("persistent_state_mutated") is True
                    else "not_mutated"
                ),
                "database_migrations": activation.get("database_migrations", []),
            },
        }
    )
    primitives.atomic_json(path / "cutover.json", document)
    return {
        "status": "cutover_recorded",
        "host_snapshot": payload["host_snapshot"],
        "cutover_mode": mode,
        "activation_status": status,
        "authority": authority_after,
        "active_targets": document["active_targets"],
        "schema_migration": document["schema_migration"],
    }
