"""Snapshot and restore declared component state through execution bindings."""

from __future__ import annotations

import os
import re
import shutil
import sqlite3
import stat
import uuid
from collections.abc import Mapping
from pathlib import Path

from . import authority_state, contract, deployment_identity, memory_realms, primitives
from .primitives import TargetError
from .state_runtime import StateRuntime, system_runtime


def host_id_or_none() -> str | None:
    # The authenticated, installed directory already names the public Host.
    # Hashing private key bytes here used to invent a second, incompatible ID.
    try:
        return deployment_identity.observe({})["host_id"]
    except TargetError:
        return None


def inventory(payload: Mapping[str, object]) -> dict[str, dict]:
    """Validate the controller's selected contracts, with no built-in fallback."""
    entries = payload.get("authority_inventory")
    if not isinstance(entries, list) or not entries:
        raise TargetError("backup requires declared authority inventory")
    result: dict[str, dict] = {}
    paths: set[Path] = set()
    for entry in entries:
        if not isinstance(entry, dict):
            raise TargetError("authority inventory entry is malformed")
        name = entry.get("id")
        path = Path(str(entry.get("path", "")))
        declared = Path(str(entry.get("declared_path", "")))
        if (
            not isinstance(name, str) or re.fullmatch(r"[a-zA-Z0-9_.-]+", name) is None
            or name.startswith(".") or name in result
            or not path.is_absolute() or ".." in path.parts or path in paths
            or not declared.is_absolute() or ".." in declared.parts
            or any(not isinstance(entry.get(key), str) or not entry[key] for key in ("owner", "group", "component"))
        ):
            raise TargetError("authority inventory has unsafe or duplicate entries")
        kind = entry.get("backup")
        if kind not in {"sqlite-online", "none", "component-action"}:
            raise TargetError(f"authority has no implemented backup method: {name}: {kind}")
        if kind == "none" and not entry.get("reason"):
            raise TargetError("uncovered authority must explain why")
        if kind == "component-action" and (
            entry["component"] != "eidolon_memory"
            or entry.get("snapshot_action") != memory_realms.SNAPSHOT_ACTION
            or entry.get("restore_action") != memory_realms.RESTORE_ACTION
            or any(value["backup"] == "component-action" for value in result.values())
        ):
            raise TargetError("authority has no implemented component snapshot binding")
        result[name] = {**entry, "path": path}
        paths.add(path)
    return result


def _memory(entries: Mapping[str, dict]) -> dict | None:
    return next((entry for entry in entries.values() if entry["backup"] == "component-action"), None)


def _memory_bindings(entry: dict, runtime: StateRuntime) -> dict:
    return {"service_account": (entry["owner"], entry["group"]), "own": runtime.own,
            "hand_to_operator": runtime.hand_to_operator}


def prepare_restore_stage(payload: Mapping[str, object]) -> dict[str, object]:
    """Clear only an owned, private staging directory within the fixed namespace."""
    contract.fixed_units(payload)
    inventory(payload)
    release_id = contract.fixed_release_id(payload)
    destination = contract.VAR_TMP / f"eidolon-backup-{release_id}"
    if destination.exists() or destination.is_symlink():
        operator = int(os.environ.get("SUDO_UID", os.geteuid()))
        if (destination.is_symlink() or not destination.is_dir()
            or destination.stat().st_uid not in {operator, os.geteuid()}
            or stat.S_IMODE(destination.stat().st_mode) != 0o700):
            raise TargetError("backup staging is not an owned private directory")
        shutil.rmtree(destination)
    return {"status": "prepared", "directory": str(destination)}


def backup(payload: Mapping[str, object], *, runtime: StateRuntime | None = None,
           directory: Path | None = None) -> dict[str, object]:
    """Online SQLite snapshots and component-owned memory snapshots on any Host."""
    release_id = contract.fixed_release_id(payload)
    entries = inventory(payload)
    runtime = runtime or system_runtime(payload, host_id_or_none)
    host_id = runtime.host_id()
    if host_id is None:
        raise TargetError("backup requires an established Host identity")
    memory = _memory(entries)
    memory_url = contract.fixed_memory_admin_url(payload) if memory else None
    destination = directory or runtime.staging_root / f"eidolon-backup-{release_id}-{uuid.uuid4().hex}"
    if destination.exists() or destination.is_symlink():
        raise TargetError(f"backup destination already exists: {destination}")
    destination.mkdir(mode=0o700, parents=True)
    runtime.hand_to_operator(destination)
    captured: list[dict[str, object]] = []
    for name, entry in entries.items():
        if entry["backup"] != "sqlite-online":
            continue
        source = entry["path"]
        if not source.is_file() or source.is_symlink():
            raise TargetError(f"authority database is missing: {source}")
        copy = destination / f"{name}.sqlite3"
        try:
            connection = sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)
            try:
                connection.execute("VACUUM INTO ?", (str(copy),))
            finally:
                connection.close()
        except sqlite3.Error as exc:
            raise TargetError(f"authority snapshot failed: {name}: {exc}") from exc
        os.chmod(copy, 0o600)
        runtime.hand_to_operator(copy)
        captured.append({"authority": name, "source": str(source), "file": copy.name,
                         "sha256": primitives.file_sha256(copy), "bytes": copy.stat().st_size,
                         "taken_at": primitives.now_timestamp()})
    uncovered = [{"state": name, "path": str(entry["path"]), "reason": entry["reason"]}
                 for name, entry in entries.items() if entry["backup"] == "none"]
    spaces: list[dict[str, object]] = []
    if memory:
        try:
            # The service must traverse the private staging parent to reach its
            # own subtree. Borrow ownership, never grant world access or ACLs.
            runtime.own(destination, memory["owner"], memory["group"])
            spaces = memory_realms.capture(memory_url, destination / memory_realms.SPACES_DIRECTORY,
                                          **_memory_bindings(memory, runtime))
        except TargetError as exc:
            shutil.rmtree(destination / memory_realms.SPACES_DIRECTORY, ignore_errors=True)
            uncovered.append(memory_realms.unavailable(str(exc), memory["path"]))
        finally:
            runtime.hand_to_operator(destination)
    return {"status": "captured", "release_id": release_id, "host_id": host_id,
            "directory": str(destination), "authorities": captured, "memory_spaces": spaces,
            "not_covered": uncovered,
            "consistency": "each authority is snapshotted on its own instant; this is not a point-in-time image of the whole Host"}


def restore_plan(payload: Mapping[str, object], *, runtime: StateRuntime | None = None,
                 directory: Path | None = None) -> tuple[dict, list[tuple[str, Path, dict]]]:
    """Validate the entire package before the adapter stops any service."""
    release_id = contract.fixed_release_id(payload)
    entries = inventory(payload)
    runtime = runtime or system_runtime(payload, host_id_or_none)
    manifest = payload.get("manifest")
    if not isinstance(manifest, dict):
        raise TargetError("restore requires the manifest its backup produced")
    host_id = runtime.host_id()
    if host_id is None or manifest.get("host_id") != host_id:
        raise TargetError("backup belongs to a different Host")
    if manifest.get("release_id") != release_id:
        raise TargetError("backup was taken from a different release")
    source = directory or runtime.staging_root / f"eidolon-backup-{release_id}"
    if source.is_symlink() or not source.is_dir():
        raise TargetError("backup directory is missing or unsafe")
    found = manifest.get("authorities")
    if not isinstance(found, list) or not found:
        raise TargetError("backup manifest names no authority")
    prepared: list[tuple[str, Path, dict]] = []
    seen: set[str] = set()
    for item in found:
        if not isinstance(item, dict):
            raise TargetError("backup manifest entry is malformed")
        name = item.get("authority")
        if not isinstance(name, str) or name not in entries or entries[name]["backup"] != "sqlite-online" or name in seen:
            raise TargetError("backup names an unknown or duplicate authority")
        filename = item.get("file")
        if not isinstance(filename, str) or Path(filename).name != filename:
            raise TargetError("backup file is missing or unsafe")
        copy = source / filename
        if copy.is_symlink() or not copy.is_file():
            raise TargetError("backup file is missing or unsafe")
        if primitives.file_sha256(copy) != item.get("sha256"):
            raise TargetError(f"backup file does not match its digest: {name}")
        try:
            connection = sqlite3.connect(copy.as_uri() + "?mode=ro", uri=True)
            try:
                if connection.execute("PRAGMA quick_check").fetchone() != ("ok",):
                    raise TargetError(f"backup database is invalid: {name}")
            finally:
                connection.close()
        except sqlite3.Error as exc:
            raise TargetError(f"backup database is invalid: {name}") from exc
        target = entries[name]["path"]
        if any(parent.is_symlink() for parent in (target, *target.parents)):
            raise TargetError("authority destination is unsafe")
        prepared.append((name, copy, {**entries[name], "expected_sha256": item["sha256"]}))
        seen.add(name)
    if seen != {name for name, entry in entries.items() if entry["backup"] == "sqlite-online"}:
        raise TargetError("backup does not cover every authority this Host keeps")
    for _name, copy, entry in prepared:
        if entry["component"] == "eidolon_hub" and Path(entry["declared_path"]) == authority_state.HUB_DATABASE:
            # Ordinary state restore cannot roll a signed Owner directory back
            # to another generation. The dedicated Authority package owns that.
            evidence = authority_state.lineage_evidence(
                database=copy, anchor=entry["path"].parent / authority_state.AUTHORITY_ANCHOR.name,
            )
            if evidence["established"] is None:
                raise TargetError("Owner Authority lineage differs; use an explicit Authority restore package")
    spaces = manifest.get("memory_spaces")
    if spaces is not None and not isinstance(spaces, list):
        raise TargetError("backup describes memory spaces as a non-list")
    for space in spaces or ():
        memory_realms.verify_backup(source / memory_realms.SPACES_DIRECTORY, space)
    return manifest, prepared


def restore(payload: Mapping[str, object], *, runtime: StateRuntime | None = None,
            directory: Path | None = None) -> dict[str, object]:
    runtime = runtime or system_runtime(payload, host_id_or_none)
    manifest, prepared = restore_plan(payload, runtime=runtime, directory=directory)
    entries = inventory(payload)
    memory = _memory(entries)
    memory_url = contract.fixed_memory_admin_url(payload) if memory else None
    source = directory or runtime.staging_root / f"eidolon-backup-{manifest['release_id']}"
    if runtime.prepare is not None:
        runtime.prepare()
    saved: list[tuple[Path, Path]] = []
    created: list[Path] = []
    restored: list[str] = []
    try:
        stopped = runtime.stop()
        _, prepared = restore_plan(payload, runtime=runtime, directory=directory)
        for name, copy, entry in prepared:
            destination = entry["path"]
            destination.parent.mkdir(parents=True, exist_ok=True)
            temporary = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.tmp")
            try:
                shutil.copyfile(copy, temporary)
                if primitives.file_sha256(temporary) != entry["expected_sha256"]:
                    raise TargetError(f"backup changed during restore: {name}")
                os.chmod(temporary, 0o600)
                runtime.own(temporary, entry["owner"], entry["group"])
                for suffix in ("", "-wal", "-shm", "-journal"):
                    original = destination.with_name(destination.name + suffix)
                    if original.exists():
                        saved_path = original.with_name(f".{original.name}.{uuid.uuid4().hex}.restore-previous")
                        os.replace(original, saved_path)
                        saved.append((original, saved_path))
                os.replace(temporary, destination)
                created.append(destination)
                restored.append(name)
            finally:
                temporary.unlink(missing_ok=True)
    except Exception:
        for destination in created:
            destination.unlink(missing_ok=True)
        for original, saved_path in reversed(saved):
            os.replace(saved_path, original)
        raise
    finally:
        started = runtime.start()
    spaces = manifest.get("memory_spaces")
    memory_result: object = "this backup carries no memory spaces" if spaces is None else []
    if memory and spaces is not None:
        memory_root = source / memory_realms.SPACES_DIRECTORY
        if spaces:
            runtime.own(source, memory["owner"], memory["group"])
            runtime.own(memory_root, memory["owner"], memory["group"])
        try:
            memory_result = memory_realms.put_back(memory_url, memory_root, spaces,
                                                  **_memory_bindings(memory, runtime))
        finally:
            if spaces:
                runtime.hand_to_operator(memory_root)
                runtime.hand_to_operator(source)
    return {"status": "restored", "release_id": manifest["release_id"], "host_id": runtime.host_id(),
            "restored": restored, "stopped": stopped, "started": started.get("status"),
            "memory_spaces": memory_result,
            "previous_files": [str(path) for _original, path in saved],
            "not_restored": [name for name, entry in entries.items() if entry["backup"] == "none"]}
