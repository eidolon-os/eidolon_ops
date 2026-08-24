"""Exact backup and same-generation restore of Hub Owner Authority state.

Reset and restore deliberately have different entry points.  This module never
creates a database, advances a generation, or consumes a bootstrap capability:
it only copies a proven database plus its matching external lineage anchor.
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import stat
import subprocess
import time
import uuid
from collections.abc import Callable, Mapping
from pathlib import Path

from . import authority_reset, contract, primitives
from .primitives import TargetError

_BACKUP_FILE = "eidolon-hub.sqlite3"
_ANCHOR_FILE = "authority-lineage.json"
_SQLITE_SIDECARS = ("-wal", "-shm", "-journal")


def _error(code: str, detail: str) -> TargetError:
    return TargetError(f"{code}: {detail}")


def _request(
    payload: Mapping[str, object], *, require_digests: bool = True
) -> dict[str, object]:
    contract.fixed_units(payload)
    value = payload.get("authority_restore")
    expected_keys = {
        "owner_domain_id",
        "owner_domain_generation",
        "state_id",
    }
    if require_digests:
        expected_keys |= {"database_sha256", "anchor_sha256"}
    if not isinstance(value, dict) or set(value) != expected_keys:
        raise _error("AUTHORITY_RESTORE_INVALID", "restore request shape is invalid")
    owner = value.get("owner_domain_id")
    generation = value.get("owner_domain_generation")
    state_id = value.get("state_id")
    if (
        not isinstance(owner, str)
        or not owner.startswith("owner-")
        or type(generation) is not int
        or generation < 1
        or not isinstance(state_id, str)
        or not state_id.startswith("authority-state_")
        or (
            require_digests
            and any(
            not isinstance(value.get(name), str)
            or contract.SHA256.fullmatch(str(value.get(name))) is None
            for name in ("database_sha256", "anchor_sha256")
            )
        )
    ):
        raise _error("AUTHORITY_RESTORE_INVALID", "restore request values are invalid")
    return dict(value)


def _expected(request: Mapping[str, object]) -> dict[str, object]:
    return {
        "contract_version": 1,
        "owner_domain_id": request["owner_domain_id"],
        "owner_domain_generation": request["owner_domain_generation"],
        "state_id": request["state_id"],
    }


def _safe_file(path: Path, label: str) -> None:
    if path.is_symlink() or not path.is_file():
        raise _error("AUTHORITY_RESTORE_INCOMPLETE", f"{label} is missing or unsafe")


def _operator_uid() -> int:
    value = os.environ.get("SUDO_UID")
    try:
        return os.geteuid() if value is None else int(value)
    except ValueError as exc:
        raise _error("AUTHORITY_RESTORE_INVALID", "invoking operator UID is invalid") from exc


def _private_stage(path: Path) -> None:
    if (
        path.is_symlink()
        or not path.is_dir()
        or stat.S_IMODE(path.stat().st_mode) != 0o700
        or path.stat().st_uid != _operator_uid()
    ):
        raise _error(
            "AUTHORITY_RESTORE_INVALID", "Authority staging ownership or mode drifted"
        )


def _anchor(path: Path) -> dict[str, object]:
    _safe_file(path, "Authority lineage anchor")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise _error("AUTHORITY_RESTORE_INVALID", "Authority lineage anchor is invalid") from exc
    if not isinstance(value, dict):
        raise _error("AUTHORITY_RESTORE_INVALID", "Authority lineage anchor is invalid")
    return value


def _marker(path: Path) -> dict[str, object]:
    _safe_file(path, "Hub Authority database")
    try:
        connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            quick_check = connection.execute("PRAGMA quick_check").fetchone()
            row = connection.execute(
                "SELECT owner_domain_id, owner_domain_generation, state_id "
                "FROM hub_authority_state WHERE singleton_id=1"
            ).fetchone()
        finally:
            connection.close()
    except sqlite3.Error as exc:
        raise _error("AUTHORITY_RESTORE_INVALID", "Hub Authority database is invalid") from exc
    if quick_check != ("ok",) or row is None:
        raise _error("AUTHORITY_RESTORE_INVALID", "Hub Authority database is invalid")
    return {
        "contract_version": 1,
        "owner_domain_id": row[0],
        "owner_domain_generation": row[1],
        "state_id": row[2],
    }


def _stage(root: Path, release_id: str, kind: str) -> Path:
    path = primitives.host_path(
        root, contract.VAR_TMP / f"eidolon-authority-{kind}-{release_id}"
    )
    if path.parent != primitives.host_path(root, contract.VAR_TMP):
        raise _error("AUTHORITY_RESTORE_INVALID", "Authority staging path is unsafe")
    return path


def backup(payload: Mapping[str, object], *, root: Path = Path("/")) -> dict[str, object]:
    """Capture one complete Hub database and its matching lineage anchor."""

    release_id = contract.fixed_release_id(payload)
    request = _request(payload, require_digests=False)
    expected = _expected(request)
    root = root.resolve()
    database = primitives.host_path(root, authority_reset.HUB_DATABASE)
    anchor = primitives.host_path(root, authority_reset.AUTHORITY_ANCHOR)
    destination = _stage(root, release_id, "backup")
    lock = primitives.host_path(root, Path("/run/lock/eidolon-install.lock"))
    with primitives.exclusive(lock):
        if _marker(database) != expected or _anchor(anchor) != expected:
            raise _error(
                "AUTHORITY_RESTORE_MISMATCH",
                "live Hub marker, lineage anchor and Owner recovery state differ",
            )
        if destination.exists() or destination.is_symlink():
            _private_stage(destination)
            shutil.rmtree(destination)
        destination.mkdir(parents=True, mode=0o700)
        snapshot = destination / _BACKUP_FILE
        try:
            connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
            try:
                connection.execute("VACUUM INTO ?", (str(snapshot),))
            finally:
                connection.close()
        except sqlite3.Error as exc:
            raise _error("AUTHORITY_RESTORE_INVALID", "Hub Authority snapshot failed") from exc
        lineage = destination / _ANCHOR_FILE
        shutil.copyfile(anchor, lineage)
        if _marker(snapshot) != expected or _anchor(lineage) != expected:
            raise _error(
                "AUTHORITY_RESTORE_MISMATCH", "captured Authority lineage changed"
            )
    for path in (snapshot, lineage):
        os.chmod(path, 0o600)
    primitives.give_to_invoking_operator(destination)
    primitives.give_to_invoking_operator(snapshot)
    primitives.give_to_invoking_operator(lineage)
    return {
        "status": "authority_backup_captured",
        "release_id": release_id,
        "directory": str(destination),
        "authority": expected,
        "files": {
            "database": {
                "name": _BACKUP_FILE,
                "sha256": primitives.file_sha256(snapshot),
                "bytes": snapshot.stat().st_size,
            },
            "anchor": {
                "name": _ANCHOR_FILE,
                "sha256": primitives.file_sha256(lineage),
                "bytes": lineage.stat().st_size,
            },
        },
    }


def clear_restore_stage(
    payload: Mapping[str, object], *, root: Path = Path("/")
) -> dict[str, object]:
    """Remove only the exact RestoreAuthority staging directory for a release."""

    release_id = contract.fixed_release_id(payload)
    destination = _stage(root.resolve(), release_id, "restore")
    removed = destination.exists()
    if removed:
        _private_stage(destination)
        shutil.rmtree(destination)
    elif destination.is_symlink():
        raise _error("AUTHORITY_RESTORE_INVALID", "restore staging path is unsafe")
    return {
        "status": "authority_restore_stage_ready",
        "directory": str(contract.VAR_TMP / destination.name),
        "removed_previous": removed,
    }


def _prepared(payload: Mapping[str, object], root: Path) -> tuple[dict[str, object], Path, Path]:
    release_id = contract.fixed_release_id(payload)
    request = _request(payload)
    expected = _expected(request)
    source = _stage(root, release_id, "restore")
    database = source / _BACKUP_FILE
    anchor = source / _ANCHOR_FILE
    if source.is_symlink() or not source.is_dir() or set(item.name for item in source.iterdir()) != {
        _BACKUP_FILE,
        _ANCHOR_FILE,
    }:
        raise _error("AUTHORITY_RESTORE_INCOMPLETE", "restore package is missing or partial")
    _private_stage(source)
    for path in (database, anchor):
        if (
            path.is_symlink()
            or not path.is_file()
            or stat.S_IMODE(path.stat().st_mode) != 0o600
            or path.stat().st_uid != _operator_uid()
        ):
            raise _error(
                "AUTHORITY_RESTORE_INVALID", "restore file ownership or mode drifted"
            )
    if (
        primitives.file_sha256(database) != request["database_sha256"]
        or primitives.file_sha256(anchor) != request["anchor_sha256"]
    ):
        raise _error("AUTHORITY_RESTORE_INVALID", "restore package digest mismatch")
    if _marker(database) != expected or _anchor(anchor) != expected:
        raise _error(
            "AUTHORITY_RESTORE_MISMATCH",
            "backup marker, anchor and requested Owner generation differ",
        )
    return expected, database, anchor


def restore_plan(
    payload: Mapping[str, object], *, root: Path = Path("/")
) -> dict[str, object]:
    expected, _database, _anchor_path = _prepared(payload, root.resolve())
    return {
        "status": "authority_restore_planned",
        "authority": expected,
        "replaces": [str(authority_reset.HUB_DATABASE), str(authority_reset.AUTHORITY_ANCHOR)],
        "preserves": [
            "owner_domain_generation",
            "Host identity and release targets",
            "all non-Hub authorities",
        ],
    }


def _checked(
    command: Callable[..., subprocess.CompletedProcess[str]],
    value: tuple[str, ...],
    *,
    operation: str,
) -> None:
    result = command(value, timeout=180)
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or "no output"
        raise _error("AUTHORITY_RESTORE_FAILED", f"{operation} failed: {detail}")


def restore(
    payload: Mapping[str, object],
    *,
    root: Path = Path("/"),
    command: Callable[..., subprocess.CompletedProcess[str]] = primitives.run,
    manage_services: bool = True,
    ready_timeout_seconds: float = 120.0,
) -> dict[str, object]:
    """Atomically replace Hub state with a fully validated same-generation copy."""

    if os.geteuid() != 0 and root == Path("/"):
        raise _error("AUTHORITY_RESTORE_FORBIDDEN", "Owner Authority restore requires root")
    root = root.resolve()
    expected, source_database, source_anchor = _prepared(payload, root)
    lock = primitives.host_path(root, Path("/run/lock/eidolon-install.lock"))
    database = primitives.host_path(root, authority_reset.HUB_DATABASE)
    anchor = primitives.host_path(root, authority_reset.AUTHORITY_ANCHOR)
    with primitives.exclusive(lock):
        expected, source_database, source_anchor = _prepared(payload, root)
        if manage_services:
            _checked(
                command,
                ("/usr/bin/systemctl", "stop", authority_reset.HUB_INGRESS_UNIT, authority_reset.HUB_UNIT),
                operation="Hub quiesce for Owner Authority restore",
            )
        database.parent.mkdir(parents=True, exist_ok=True)
        for suffix in _SQLITE_SIDECARS:
            database.with_name(database.name + suffix).unlink(missing_ok=True)
        for source, destination in ((source_database, database), (source_anchor, anchor)):
            temporary = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.tmp")
            try:
                shutil.copyfile(source, temporary)
                os.chmod(temporary, 0o640 if destination == database else 0o600)
                if root == Path("/"):
                    primitives.chown_path(temporary, "eidolon", "eidolon")
                os.replace(temporary, destination)
            finally:
                temporary.unlink(missing_ok=True)
        if manage_services:
            _checked(
                command,
                ("/usr/bin/systemctl", "start", authority_reset.HUB_UNIT),
                operation="Hub start after Owner Authority restore",
            )
            deadline = time.monotonic() + ready_timeout_seconds
            while time.monotonic() < deadline:
                active = command(("/usr/bin/systemctl", "is-active", authority_reset.HUB_UNIT), timeout=20)
                if active.returncode == 0 and _marker(database) == expected and _anchor(anchor) == expected:
                    break
                time.sleep(0.5)
            else:
                raise _error("AUTHORITY_RESTORE_FAILED", "Hub did not become ready on restored lineage")
        if _marker(database) != expected or _anchor(anchor) != expected:
            raise _error("AUTHORITY_RESTORE_FAILED", "restored Authority proof is incomplete")
    return {
        "status": "authority_restored",
        "authority": expected,
        "generation_advanced": False,
        "commissioning_required": False,
    }
