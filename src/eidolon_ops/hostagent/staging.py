"""Claim, resume and finalize the directories an operator uploads into."""

from __future__ import annotations

import json
import os
import shutil
import stat
from collections.abc import Mapping

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
