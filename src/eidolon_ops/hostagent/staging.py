"""Claim, resume and finalize the directories an operator uploads into."""

from __future__ import annotations

import hashlib
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


def _artifact_contract(payload: Mapping[str, object]) -> tuple[Path, dict[str, str], str]:
    destination = payload.get("destination")
    files = payload.get("files")
    if not isinstance(destination, str) or not isinstance(files, dict) or not files:
        raise TargetError("artifact destination and pinned file manifest are required")
    target = Path(destination)
    if target.parent != contract.HOST_MODEL_ROOT or target.name in {"", ".", ".."}:
        raise TargetError("artifact destination is outside the model root")
    for name, digest in files.items():
        path = Path(name) if isinstance(name, str) else Path("/")
        if (path.is_absolute() or not path.parts or any(part in {".", ".."} for part in path.parts)
                or str(path) != name or name == contract.EMBEDDING_DIGEST_RECORD
                or not isinstance(digest, str) or contract.SHA256.fullmatch(digest) is None):
            raise TargetError("artifact file manifest is invalid")
    joined = "\n".join(f"{name} {digest}" for name, digest in sorted(files.items()))
    return target, files, hashlib.sha256(joined.encode()).hexdigest()


def _artifact_matches(root: Path, files: dict[str, str], digest: str) -> bool:
    if root.is_symlink() or not root.is_dir():
        return False
    record = root / contract.EMBEDDING_DIGEST_RECORD
    if record.is_symlink() or not record.is_file() or record.read_text().strip() != digest:
        return False
    present = set()
    for path in root.rglob("*"):
        if path.is_symlink() or not (path.is_file() or path.is_dir()):
            return False
        if path.is_file():
            present.add(path.relative_to(root).as_posix())
    if present != {*files, contract.EMBEDDING_DIGEST_RECORD}:
        return False
    return all(primitives.file_sha256(root / name) == expected for name, expected in files.items())


def component_artifact_state(payload: Mapping[str, object]) -> dict[str, object]:
    target, files, digest = _artifact_contract(payload)
    status = "held" if _artifact_matches(target, files, digest) else "absent"
    return {"status": status, "destination": str(target), "digest": digest if status == "held" else None}


def install_component_artifact(payload: Mapping[str, object]) -> dict[str, object]:
    target, files, digest = _artifact_contract(payload)
    staging = payload.get("staging")
    if not isinstance(staging, str):
        raise TargetError("artifact staging is required")
    source = Path(staging)
    if source.parent != contract.VAR_TMP or not source.name.startswith("eidolon-artifact-"):
        raise TargetError("artifact staging is outside the private upload root")
    if not _artifact_matches(source, files, digest):
        raise TargetError("carried artifact content does not match its pinned manifest")
    if target.exists() or target.is_symlink():
        if _artifact_matches(target, files, digest):
            return {"status": "installed", "destination": str(target), "digest": digest}
        raise TargetError("model destination already contains different or damaged data; choose a new model path or explicitly repair it")
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
    try:
        shutil.copytree(source, temporary)
        if not _artifact_matches(temporary, files, digest):
            raise TargetError("model content changed while copying")
        for path in (temporary, *temporary.rglob("*")):
            os.chown(path, 0, 0)
            os.chmod(path, 0o755 if path.is_dir() else 0o644)
        os.rename(temporary, target)
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)
    shutil.rmtree(source)
    return {"status": "installed", "destination": str(target), "digest": digest}
