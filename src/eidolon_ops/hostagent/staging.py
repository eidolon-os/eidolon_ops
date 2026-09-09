"""Claim, resume and finalize the directories an operator uploads into."""

from __future__ import annotations

import json
import os
import shutil
import stat
import uuid
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


def _release_artifacts(payload: Mapping[str, object]) -> list[dict[str, object]]:
    values = payload.get("artifacts")
    if not isinstance(values, list) or not 1 <= len(values) <= 256:
        raise TargetError("release artifact list is invalid")
    result: list[dict[str, object]] = []
    seen: set[str] = set()
    for value in values:
        if (
            not isinstance(value, dict)
            or set(value) != {"sha256", "size"}
            or not isinstance(value.get("sha256"), str)
            or contract.SHA256.fullmatch(value["sha256"]) is None
            or value["sha256"] in seen
            or not isinstance(value.get("size"), int)
            or isinstance(value.get("size"), bool)
            or value["size"] < 0
        ):
            raise TargetError("release artifact identity is invalid")
        seen.add(value["sha256"])
        result.append({"sha256": value["sha256"], "size": value["size"]})
    return sorted(result, key=lambda item: str(item["sha256"]))


def release_artifact_state(payload: Mapping[str, object]) -> dict[str, object]:
    artifacts = _release_artifacts(payload)
    missing: list[str] = []
    corrupt: list[str] = []
    for value in artifacts:
        path = contract.RELEASE_ARTIFACT_STORE_ROOT / str(value["sha256"])
        if not path.exists() and not path.is_symlink():
            missing.append(str(value["sha256"]))
            continue
        if (
            path.is_symlink()
            or not path.is_file()
            or path.stat().st_size != value["size"]
            or primitives.file_sha256(path) != value["sha256"]
        ):
            missing.append(str(value["sha256"]))
            corrupt.append(str(value["sha256"]))
    return {
        "status": "complete" if not missing else "missing",
        "missing": missing,
        "corrupt": corrupt,
        "objects": len(artifacts),
    }


def guard_release_artifacts(payload: Mapping[str, object]) -> dict[str, object]:
    transfer_id = payload.get("transfer_id")
    if not isinstance(transfer_id, str) or contract.SHA256.fullmatch(transfer_id) is None:
        raise TargetError("artifact transfer identity is invalid")
    artifacts = _release_artifacts(payload)
    path = contract.VAR_TMP / f"eidolon-artifacts-{transfer_id[:16]}"
    marker = path / ".eidolon-artifacts.json"
    expected = {
        "schema_version": 1,
        "transfer_id": transfer_id,
        "artifacts": artifacts,
    }
    if not path.exists() and not path.is_symlink():
        path.mkdir(mode=0o700)
        primitives.atomic_json(marker, expected)
        return {"status": "ready_for_upload", "path": str(path)}
    try:
        metadata = path.stat(follow_symlinks=False)
        document = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise TargetError("artifact staging directory is not resumable") from exc
    if (
        path.is_symlink()
        or not stat.S_ISDIR(metadata.st_mode)
        or stat.S_IMODE(metadata.st_mode) != 0o700
        or metadata.st_uid != os.geteuid()
        or document != expected
    ):
        raise TargetError("artifact staging identity or ownership drifted")
    return {"status": "resume_upload", "path": str(path)}


def finalize_release_artifacts(payload: Mapping[str, object]) -> dict[str, object]:
    transfer_id = payload.get("transfer_id")
    if not isinstance(transfer_id, str) or contract.SHA256.fullmatch(transfer_id) is None:
        raise TargetError("artifact transfer identity is invalid")
    artifacts = _release_artifacts(payload)
    path = contract.VAR_TMP / f"eidolon-artifacts-{transfer_id[:16]}"
    marker = path / ".eidolon-artifacts.json"
    object_root = path / "sha256"
    try:
        metadata = path.stat(follow_symlinks=False)
        document = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise TargetError("artifact staging directory is incomplete") from exc
    expected = {
        "schema_version": 1,
        "transfer_id": transfer_id,
        "artifacts": artifacts,
    }
    expected_names = {str(value["sha256"]) for value in artifacts}
    if (
        path.is_symlink()
        or not stat.S_ISDIR(metadata.st_mode)
        or stat.S_IMODE(metadata.st_mode) != 0o700
        or document != expected
        or {item.name for item in path.iterdir()} != {marker.name, "sha256"}
        or object_root.is_symlink()
        or not object_root.is_dir()
        or {item.name for item in object_root.iterdir()} != expected_names
    ):
        raise TargetError("artifact staging shape or identity drifted")
    for value in artifacts:
        source = object_root / str(value["sha256"])
        if (
            source.is_symlink()
            or not source.is_file()
            or source.stat().st_size != value["size"]
            or primitives.file_sha256(source) != value["sha256"]
        ):
            raise TargetError(f"staged release artifact checksum drifted: {value['sha256']}")

    contract.RELEASE_ARTIFACT_STORE_ROOT.mkdir(parents=True, exist_ok=True, mode=0o755)
    if (
        contract.RELEASE_ARTIFACT_STORE_ROOT.is_symlink()
        or not contract.RELEASE_ARTIFACT_STORE_ROOT.is_dir()
    ):
        raise TargetError("release artifact store is unsafe")
    installed = 0
    for value in artifacts:
        digest = str(value["sha256"])
        source = object_root / digest
        destination = contract.RELEASE_ARTIFACT_STORE_ROOT / digest
        if destination.is_file() and not destination.is_symlink():
            if (
                destination.stat().st_size == value["size"]
                and primitives.file_sha256(destination) == digest
            ):
                source.unlink()
                continue
        elif destination.exists() or destination.is_symlink():
            raise TargetError(f"release artifact destination is unsafe: {digest}")
        _install_release_artifact(
            source,
            destination,
            digest=digest,
            size=int(value["size"]),
        )
        installed += 1
    shutil.rmtree(path)
    return {
        "status": "installed" if installed else "already_installed",
        "objects": len(artifacts),
        "installed": installed,
    }


def _install_release_artifact(
    source: Path, destination: Path, *, digest: str, size: int
) -> None:
    """Copy into the destination filesystem, prove it, then publish atomically.

    ``/var/tmp`` and ``/var/cache`` normally share a filesystem on the Pi, but
    that is not a deployment contract. Copying to a private temporary beside
    the destination avoids an EXDEV failure and also prevents the unprivileged
    upload owner from mutating a hard-linked object after its final checksum.
    """

    temporary = destination.parent / f".{digest}.{uuid.uuid4().hex}.tmp"
    try:
        shutil.copyfile(source, temporary)
        os.chown(temporary, 0, 0)
        os.chmod(temporary, 0o444)
        if temporary.stat().st_size != size or primitives.file_sha256(temporary) != digest:
            raise TargetError(f"release artifact changed during installation: {digest}")
        with temporary.open("rb") as stream:
            os.fsync(stream.fileno())
        os.replace(temporary, destination)
        source.unlink()
    finally:
        if temporary.exists() or temporary.is_symlink():
            temporary.unlink()


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


def component_artifact_state(payload: Mapping[str, object]) -> dict[str, object]:
    """What this Host already holds at a destination, if anything.

    Asked before carrying a gigabyte across, and answered from the digest the
    Host recorded rather than from the directory existing: a partial copy is
    not a copy.
    """

    destination = payload.get("destination")
    if not isinstance(destination, str):
        raise TargetError("artifact destination is required")
    record = Path(destination) / contract.EMBEDDING_DIGEST_RECORD
    if not record.is_file():
        return {"status": "absent", "destination": destination}
    return {
        "status": "held",
        "destination": destination,
        "digest": record.read_text(encoding="utf-8").strip(),
    }


def install_component_artifact(payload: Mapping[str, object]) -> dict[str, object]:
    """Move carried files into the Host's durable model root.

    Root-owned and read-only afterwards: every service reads these and none of
    them writes any, and a palace already built against one encoder must not
    find another there later. Each artifact has its own directory under the
    model root, named by the component that declared it, so a second pin adds a
    directory rather than replacing one something was built against.

    The destination is checked rather than trusted. It arrives from a component
    contract, which is a file in a repository, and the one thing this agent can
    say about it is that nothing may write outside the root it owns.
    """

    staging = payload.get("staging")
    destination = payload.get("destination")
    if not isinstance(staging, str) or not isinstance(destination, str):
        raise TargetError("artifact staging and destination are required")
    source = Path(staging)
    target = Path(destination)
    if target.parent != contract.HOST_MODEL_ROOT or target.name in {"", ".", ".."}:
        raise TargetError(f"artifact destination is outside the model root: {target}")
    if not source.is_dir() or source.is_symlink():
        raise TargetError("carried artifact is missing")
    record = source / contract.EMBEDDING_DIGEST_RECORD
    if not record.is_file():
        raise TargetError("carried artifact has no digest record")

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
