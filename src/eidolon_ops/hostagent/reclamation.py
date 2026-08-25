"""Fail-closed release and transfer reclamation on a Host.

Only direct children of the reviewed release and staging roots are eligible.
The active graph is derived from every link in ``/opt/eidolon/current``; no
component table can make a live release invisible to this collector.
"""

from __future__ import annotations

import fcntl
import json
import os
import shutil
import stat
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import Protocol

from . import contract, primitives
from .primitives import TargetError

_PHASES = {"prepare", "retain", "commit", "abort"}
_STATE_SCHEMA_VERSION = 1


class _DiskUsage(Protocol):
    total: int
    used: int
    free: int


def reclaim(
    payload: Mapping[str, object],
    *,
    root: Path = Path("/"),
    disk_usage: Callable[[Path], _DiskUsage] = shutil.disk_usage,
) -> dict[str, object]:
    """Reclaim obsolete deployment artifacts and report capacity evidence."""

    release_id = contract.fixed_release_id(payload)
    phase = payload.get("phase")
    if phase not in _PHASES:
        raise TargetError("release reclamation phase is invalid")
    required_bytes = _byte_count(payload.get("required_bytes"), "required capacity")
    reserve_bytes = _byte_count(payload.get("reserve_bytes"), "capacity reserve")
    host_root = root.resolve()
    paths = _Paths(host_root)

    with _release_lock(paths.lock), _release_lock(paths.prepare_lock):
        active_targets = _active_targets(paths)
        active_ids = {str(item["release_id"]) for item in active_targets.values()}
        _validate_state(paths.state, release_id, phase)
        if phase == "commit":
            if not active_ids or active_ids != {release_id}:
                raise TargetError(
                    "release commit requires every current link to reference the candidate"
                )
            protected_releases = {release_id}
            protected_staging: set[str] = set()
        elif phase == "abort":
            if release_id in active_ids:
                raise TargetError("failed release candidate is still referenced by current")
            protected_releases = active_ids
            protected_staging = set()
        else:
            protected_releases = active_ids | {release_id}
            protected_staging = {release_id}

        candidate = paths.releases / release_id
        candidate_prepared = (
            False
            if phase == "abort"
            else _candidate_prepared(candidate, paths.releases)
        )
        if phase == "commit" and not candidate_prepared:
            raise TargetError("release commit requires a sealed candidate release")

        before = disk_usage(paths.capacity_root)
        removed_releases, release_bytes = _clean_releases(paths, protected_releases)
        removed_uploads, upload_bytes = _clean_staging(
            paths, "eidolon-release-", protected_staging
        )
        removed_secrets, secret_bytes = _clean_staging(
            paths, "eidolon-secrets-", protected_staging
        )
        after = disk_usage(paths.capacity_root)

        effective_required = 0 if candidate_prepared else required_bytes
        sufficient = after.free >= effective_required + reserve_bytes
        status = {
            "prepare": "ready" if sufficient else "insufficient_capacity",
            "retain": "retained",
            "commit": "committed",
            "abort": "aborted",
        }[str(phase)]
        evidence: dict[str, object] = {
            "status": status,
            "phase": phase,
            "candidate_release_id": release_id,
            "active_release_ids": sorted(active_ids),
            "active_targets": active_targets,
            "protected_release_ids": sorted(protected_releases),
            "removed": {
                "releases": removed_releases,
                "uploads": removed_uploads,
                "secrets": removed_secrets,
            },
            "bytes_reclaimed": release_bytes + upload_bytes + secret_bytes,
            "scope": {
                "releases": str(contract.RELEASES),
                "current": str(contract.CURRENT_ROOT),
                "staging": str(contract.VAR_TMP),
                "excluded": [
                    "/var/lib/eidolon",
                    "/var/lib/eidolon-bootstrap",
                    "/var/lib/eidolon-lifecycle",
                    "/var/log/eidolon",
                ],
            },
            "capacity": {
                "path": str(contract.RELEASES),
                "total_bytes": after.total,
                "free_bytes_before": before.free,
                "free_bytes_after": after.free,
                "requested_bytes": required_bytes,
                "effective_required_bytes": effective_required,
                "reserve_bytes": reserve_bytes,
                "sufficient": sufficient,
            },
        }
        if phase == "prepare" and sufficient:
            primitives.atomic_json(
                paths.state,
                {
                    "schema_version": _STATE_SCHEMA_VERSION,
                    "candidate_release_id": release_id,
                },
            )
        elif phase in {"retain", "commit", "abort"}:
            paths.state.unlink(missing_ok=True)
        return evidence


class _Paths:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.releases = primitives.host_path(root, contract.RELEASES)
        self.current = primitives.host_path(root, contract.CURRENT_ROOT)
        self.staging = primitives.host_path(root, contract.VAR_TMP)
        self.lock = primitives.host_path(root, contract.RELEASE_LOCK)
        self.prepare_lock = primitives.host_path(root, contract.RELEASE_PREPARE_LOCK)
        self.state = primitives.host_path(root, contract.RECLAMATION_STATE)
        self.capacity_root = self.releases if self.releases.exists() else root


def _byte_count(value: object, label: str) -> int:
    if type(value) is not int or not 0 <= value <= 2**63 - 1:
        raise TargetError(f"{label} is invalid")
    return value


@contextmanager
def _release_lock(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, 0o600)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise TargetError("another release transaction is in progress") from exc
        yield
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def _validate_state(path: Path, release_id: str, phase: object) -> None:
    if not path.exists() and not path.is_symlink():
        return
    if path.is_symlink() or not path.is_file():
        raise TargetError("release reclamation state path is unsafe")
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise TargetError("release reclamation state is unreadable") from exc
    expected = {
        "schema_version": _STATE_SCHEMA_VERSION,
        "candidate_release_id": release_id,
    }
    if document != expected:
        # Name it. A driver process that died leaves this marker behind, and an
        # operator who is only told "another candidate is in flight" cannot find
        # out which one, nor that abandoning it is a supported move — the abort
        # phase below already refuses to touch anything the current links use.
        held = document.get("candidate_release_id") if isinstance(document, dict) else None
        if isinstance(held, str) and held:
            raise TargetError(
                f"release candidate {held} is in flight and this operation names "
                f"{release_id}; finish {held}, or abandon it with "
                f"`eidolon-ops abandon --release-id {held}`"
            )
        raise TargetError("release reclamation state is not a candidate marker")
    if phase not in _PHASES:
        raise TargetError("release reclamation phase is invalid")


def _active_targets(paths: _Paths) -> dict[str, dict[str, str]]:
    if not paths.current.exists() and not paths.current.is_symlink():
        return {}
    _require_real_directory(paths.current, "current root")
    releases_real = paths.releases.resolve(strict=False)
    result: dict[str, dict[str, str]] = {}
    for link in sorted(paths.current.iterdir(), key=lambda item: item.name):
        if contract.RELEASE_ID.fullmatch(link.name) is None or not link.is_symlink():
            raise TargetError(f"current entry is not a conventional symlink: {link.name}")
        try:
            target = link.resolve(strict=True)
            relative = target.relative_to(releases_real)
        except (OSError, RuntimeError, ValueError) as exc:
            raise TargetError(f"current link target is missing or outside releases: {link}") from exc
        if len(relative.parts) < 2 or contract.RELEASE_ID.fullmatch(relative.parts[0]) is None:
            raise TargetError(f"current link target has an invalid release layout: {link}")
        release_root = paths.releases / relative.parts[0]
        if release_root.is_symlink() or not release_root.is_dir():
            raise TargetError(f"current link release root is unsafe: {release_root}")
        result[link.name] = {
            "link": str(contract.CURRENT_ROOT / link.name),
            "target": str(contract.RELEASES / relative),
            "release_id": relative.parts[0],
        }
    return result


def _clean_releases(paths: _Paths, protected: set[str]) -> tuple[list[str], int]:
    if not paths.releases.exists() and not paths.releases.is_symlink():
        return [], 0
    _require_real_directory(paths.releases, "release root")
    removed: list[str] = []
    reclaimed = 0
    for path in sorted(paths.releases.iterdir(), key=lambda item: item.name):
        if contract.RELEASE_ID.fullmatch(path.name) is None:
            raise TargetError(f"release root contains a non-conventional path: {path.name}")
        if path.name in protected:
            continue
        if path.is_symlink() or not path.is_dir():
            raise TargetError(f"release deletion target is not a real directory: {path}")
        if path.name in {
            str(item["release_id"]) for item in _active_targets(paths).values()
        }:
            raise TargetError(f"release deletion target is currently referenced: {path}")
        size = _tree_bytes(path)
        try:
            shutil.rmtree(path)
        except OSError as exc:
            raise TargetError(f"release directory could not be removed safely: {path}") from exc
        removed.append(str(contract.RELEASES / path.name))
        reclaimed += size
    return removed, reclaimed


def _clean_staging(
    paths: _Paths, prefix: str, protected: set[str]
) -> tuple[list[str], int]:
    if not paths.staging.exists() and not paths.staging.is_symlink():
        return [], 0
    _require_real_directory(paths.staging, "staging root")
    candidates: list[Path] = []
    for path in sorted(paths.staging.iterdir(), key=lambda item: item.name):
        if not path.name.startswith(prefix):
            continue
        release_id = path.name.removeprefix(prefix)
        if contract.RELEASE_ID.fullmatch(release_id) is None:
            raise TargetError(f"staging root contains a non-conventional path: {path.name}")
        if release_id in protected:
            continue
        if path.is_symlink() or not path.is_dir():
            raise TargetError(f"staging deletion target is not a real directory: {path}")
        if prefix == "eidolon-secrets-":
            _require_private_secret_stage(path)
        candidates.append(path)
    # Validate the whole deletion set before removing the first path. A later
    # ownership/symlink failure must not leave an unreviewable partial cleanup.
    removed: list[str] = []
    reclaimed = 0
    for path in candidates:
        size = _tree_bytes(path)
        try:
            shutil.rmtree(path)
        except OSError as exc:
            raise TargetError(f"staging directory could not be removed safely: {path}") from exc
        removed.append(str(contract.VAR_TMP / path.name))
        reclaimed += size
    return removed, reclaimed


def _require_private_secret_stage(path: Path) -> None:
    """Prove a staging copy still has the exact private upload ownership."""

    expected_uid_value = os.environ.get("SUDO_UID")
    try:
        expected_uid = (
            int(expected_uid_value) if expected_uid_value is not None else os.geteuid()
        )
    except ValueError as exc:
        raise TargetError("secret staging operator identity is invalid") from exc
    for directory, names, files in os.walk(path, followlinks=False):
        current = Path(directory)
        metadata = current.stat(follow_symlinks=False)
        if (
            current.is_symlink()
            or not stat.S_ISDIR(metadata.st_mode)
            or stat.S_IMODE(metadata.st_mode) != 0o700
            or metadata.st_uid != expected_uid
        ):
            raise TargetError(f"secret staging ownership or mode drifted: {current}")
        for name in (*names, *files):
            child = current / name
            child_metadata = child.stat(follow_symlinks=False)
            if child.is_symlink():
                raise TargetError(f"secret staging contains a symlink: {child}")
            if stat.S_ISDIR(child_metadata.st_mode):
                continue
            if (
                not stat.S_ISREG(child_metadata.st_mode)
                or stat.S_IMODE(child_metadata.st_mode) != 0o600
                or child_metadata.st_uid != expected_uid
            ):
                raise TargetError(f"secret staging ownership or mode drifted: {child}")


def _require_real_directory(path: Path, label: str) -> None:
    try:
        metadata = path.stat(follow_symlinks=False)
    except OSError as exc:
        raise TargetError(f"{label} is unavailable") from exc
    if path.is_symlink() or not stat.S_ISDIR(metadata.st_mode):
        raise TargetError(f"{label} is not a real directory")


def _candidate_prepared(path: Path, parent: Path) -> bool:
    if not path.exists() and not path.is_symlink():
        return False
    if path.parent != parent or path.is_symlink() or not path.is_dir():
        raise TargetError(f"candidate release path is unsafe: {path}")
    markers = (path / "release.json", path / "release.json.sha256")
    if not any(marker.exists() or marker.is_symlink() for marker in markers):
        return False
    if any(marker.is_symlink() or not marker.is_file() for marker in markers):
        raise TargetError(f"candidate release markers are unsafe or incomplete: {path}")
    return True


def _tree_bytes(root: Path) -> int:
    total = 0
    for directory, _names, files in os.walk(root, followlinks=False):
        for name in files:
            path = Path(directory) / name
            try:
                total += path.lstat().st_size
            except OSError as exc:
                raise TargetError(f"reclamation candidate changed during inspection: {root}") from exc
    return total
