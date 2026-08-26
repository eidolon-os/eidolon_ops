"""Claim, resume and finalize the directories an operator uploads into."""

from __future__ import annotations

import json
import os
import shutil
import stat
from collections.abc import Mapping
from pathlib import Path

from . import contract, primitives
from .primitives import TargetError


def guard_upload(payload: Mapping[str, object]) -> dict[str, object]:
    release_id = contract.fixed_release_id(payload)
    transfer_id = payload.get("transfer_id")
    if not isinstance(transfer_id, str) or contract.SHA256.fullmatch(transfer_id) is None:
        raise TargetError("upload transfer identity is invalid")
    prepared = contract.RELEASES / release_id
    if (prepared / "release.json").is_file() and (prepared / "release.json.sha256").is_file():
        return {
            "status": "already_prepared",
            "path": str(prepared),
            "transfer_id": transfer_id,
        }
    path = contract.VAR_TMP / f"eidolon-release-{release_id}"
    marker = path / ".eidolon-upload.json"
    expected = {
        "schema_version": 1,
        "release_id": release_id,
        "transfer_id": transfer_id,
    }
    if not path.exists() and not path.is_symlink():
        path.mkdir(mode=0o700)
        primitives.atomic_json(marker, expected)
        return {
            "status": "ready_for_upload",
            "path": str(path),
            "transfer_id": transfer_id,
        }
    try:
        metadata = path.stat(follow_symlinks=False)
    except OSError as exc:
        raise TargetError(f"remote bundle path is not resumable: {path}") from exc
    owned_directory = (
        not path.is_symlink()
        and stat.S_ISDIR(metadata.st_mode)
        and stat.S_IMODE(metadata.st_mode) == 0o700
        and metadata.st_uid == os.geteuid()
    )
    if not marker.exists():
        closed_shape = {
            "bundle.json",
            "prepare_target.py",
            "python-dependencies.tar.gz",
            "sources",
        }
        manifest = path / "bundle.json"
        if (
            not owned_directory
            or {item.name for item in path.iterdir()} != closed_shape
            or not manifest.is_file()
            or manifest.is_symlink()
            or primitives.file_sha256(manifest) != transfer_id
        ):
            raise TargetError(f"remote bundle path is not resumable: {path}")
        return {
            "status": "ready_for_prepare",
            "path": str(path),
            "transfer_id": transfer_id,
        }
    try:
        document = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise TargetError(f"remote bundle path is not resumable: {path}") from exc
    if not owned_directory or document != expected:
        raise TargetError(f"remote bundle path identity or ownership drifted: {path}")
    return {
        "status": "resume_upload",
        "path": str(path),
        "transfer_id": transfer_id,
    }

def finalize_upload(payload: Mapping[str, object]) -> dict[str, object]:
    release_id = contract.fixed_release_id(payload)
    transfer_id = payload.get("transfer_id")
    if not isinstance(transfer_id, str) or contract.SHA256.fullmatch(transfer_id) is None:
        raise TargetError("upload transfer identity is invalid")
    path = contract.VAR_TMP / f"eidolon-release-{release_id}"
    marker = path / ".eidolon-upload.json"
    try:
        metadata = path.stat(follow_symlinks=False)
    except OSError as exc:
        raise TargetError("uploaded bundle directory is missing") from exc
    if (
        path.is_symlink()
        or not stat.S_ISDIR(metadata.st_mode)
        or stat.S_IMODE(metadata.st_mode) != 0o700
        or metadata.st_uid != os.geteuid()
    ):
        raise TargetError("uploaded bundle directory ownership drifted")
    closed_shape = {
        "bundle.json",
        "prepare_target.py",
        "python-dependencies.tar.gz",
        "sources",
    }
    actual = {item.name for item in path.iterdir()}
    if marker.exists():
        try:
            document = json.loads(marker.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise TargetError("upload marker is unreadable") from exc
        expected = {
            "schema_version": 1,
            "release_id": release_id,
            "transfer_id": transfer_id,
        }
        if document != expected or actual != closed_shape | {marker.name}:
            raise TargetError("uploaded bundle shape or identity drifted")
    elif actual != closed_shape:
        raise TargetError("uploaded bundle shape or identity drifted")
    manifest = path / "bundle.json"
    if manifest.is_symlink() or not manifest.is_file() or primitives.file_sha256(manifest) != transfer_id:
        raise TargetError("uploaded bundle manifest digest drifted")
    if marker.exists():
        marker.unlink()
        status = "finalized"
    else:
        status = "already_finalized"
    return {"status": status, "path": str(path), "transfer_id": transfer_id}

def cleanup_stage(payload: Mapping[str, object]) -> dict[str, object]:
    release_id = contract.fixed_release_id(payload)
    path = contract.VAR_TMP / f"eidolon-secrets-{release_id}"
    if path.parent != contract.VAR_TMP or contract.STAGING_NAME.fullmatch(path.name) is None:
        raise TargetError("secret staging path is unsafe")
    if path.exists():
        if path.is_symlink() or not path.is_dir():
            raise TargetError("secret staging path is not a directory")
        shutil.rmtree(path)
    return {"status": "cleaned", "path": str(path)}


def embedding_model_state(payload: Mapping[str, object]) -> dict[str, object]:
    """What encoder this Host already holds at a destination, if any.

    Asked before carrying a hundred megabytes across, and answered from the
    digest the Host recorded rather than from the directory existing: a partial
    copy is not a copy.
    """

    destination = payload.get("destination")
    if not isinstance(destination, str):
        raise TargetError("embedding model destination is required")
    record = Path(destination) / contract.EMBEDDING_DIGEST_RECORD
    if not record.is_file():
        return {"status": "absent", "destination": destination}
    return {
        "status": "held",
        "destination": destination,
        "digest": record.read_text(encoding="utf-8").strip(),
    }


def install_embedding_model(payload: Mapping[str, object]) -> dict[str, object]:
    """Move a carried encoder into the Host's durable model root.

    Root-owned and read-only afterwards: every service reads this and none of
    them writes it, and a palace already built against these weights must not
    find different ones there later. The destination carries the digest of what
    it holds in its name, so installing a different pin adds a directory rather
    than replacing the one a palace was built with.
    """

    staging = payload.get("staging")
    destination = payload.get("destination")
    if not isinstance(staging, str) or not isinstance(destination, str):
        raise TargetError("embedding model staging and destination are required")
    source = Path(staging)
    target = Path(destination)
    if target.parent != contract.HOST_EMBEDDING_MODEL_ROOT:
        raise TargetError(f"embedding model destination is outside the model root: {target}")
    if not source.is_dir() or source.is_symlink():
        raise TargetError("carried embedding model is missing")
    record = source / contract.EMBEDDING_DIGEST_RECORD
    if not record.is_file():
        raise TargetError("carried embedding model has no digest record")

    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        shutil.rmtree(target)
    shutil.move(str(source), str(target))
    for path in (target, *target.rglob("*")):
        os.chown(path, 0, 0)
        os.chmod(path, 0o755 if path.is_dir() else 0o644)
    # Read from where the record now is, not from where it was. `record` points
    # into the staging directory, and the move above took that directory away —
    # reading it afterwards raised FileNotFoundError and failed an install whose
    # weights were already correctly in place. Reading the installed copy also
    # says the truer thing: this digest is what the Host now holds.
    installed_record = target / contract.EMBEDDING_DIGEST_RECORD
    return {
        "status": "installed",
        "destination": str(target),
        "digest": installed_record.read_text(encoding="utf-8").strip(),
    }
