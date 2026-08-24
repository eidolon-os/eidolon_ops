"""Snapshot and replace the authorities that declare how they are copied."""

from __future__ import annotations

import hashlib
import os
import shutil
import sqlite3
import uuid
from collections.abc import Mapping
from pathlib import Path

from . import contract, lifecycle, memory_realms, primitives, reset
from .primitives import TargetError


def host_id_or_none() -> str | None:
    identity = Path("/var/lib/eidolon-bootstrap/host_identity.ed25519")
    if not identity.is_file():
        return None
    return "ehost-" + hashlib.sha256(identity.read_bytes()).hexdigest()[:20]

def backup(payload: Mapping[str, object]) -> dict[str, object]:
    """Snapshot every authority that can say how it is snapshotted.

    SQLite can hand out a consistent copy of a database another process is
    writing, so nothing is stopped for this. Each authority is copied on its
    own, which means each carries its own instant: this is a set of backups,
    not one moment of the whole Host, and it does not pretend otherwise.

    Memory is the exception that proves the rule: its spaces are not a file this
    agent understands, so it asks the component that owns them for a snapshot
    and checks the manifest that comes back. That is what took memory off the
    uncovered list — a declaration by its owner, not an exception here.

    What has no declared snapshot is named in the result rather than skipped
    quietly, because a backup believed to be complete is worse than one known
    to be partial. So is memory, on a Host where the supervisor could not
    answer: the authorities are still worth copying, and the operator is told
    which state this particular copy went without.
    """

    contract.fixed_units(payload)
    release_id = contract.fixed_release_id(payload)
    destination = contract.VAR_TMP / f"eidolon-backup-{release_id}"
    if destination.parent != contract.VAR_TMP or contract.STAGING_NAME.fullmatch(destination.name) is None:
        raise TargetError("backup staging path is unsafe")
    if destination.exists():
        shutil.rmtree(destination)
    destination.mkdir(mode=0o700, parents=True)
    authorities: list[dict[str, object]] = []
    # These carry the Owner, the Companions and every Controller grant, so the
    # copy stays as closed as the original. It is handed to the account that
    # invoked this, which already holds root here, rather than opened up so a
    # transfer can read it.
    primitives.give_to_invoking_operator(destination)
    for name, (source, _user, _group) in sorted(contract.BACKED_UP_AUTHORITIES.items()):
        if not source.is_file() or source.is_symlink():
            raise TargetError(f"authority database is missing: {source}")
        copy = destination / f"{name}.sqlite3"
        connection = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
        try:
            # VACUUM INTO takes a consistent snapshot of a live database
            # without holding the writer out of it.
            connection.execute("VACUUM INTO ?", (str(copy),))
        except sqlite3.Error as exc:
            raise TargetError(f"authority snapshot failed: {name}: {exc}") from exc
        finally:
            connection.close()
        os.chmod(copy, 0o600)
        primitives.give_to_invoking_operator(copy)
        authorities.append(
            {
                "authority": name,
                "source": str(source),
                "file": copy.name,
                "sha256": primitives.file_sha256(copy),
                "bytes": copy.stat().st_size,
                "taken_at": primitives.now_timestamp(),
            }
        )
    spaces: list[dict[str, object]] = []
    uncovered = [
        {"state": name, "path": str(path), "reason": reason}
        for name, (path, reason) in sorted(contract.UNCOVERED_STATE.items())
    ]
    # Resolved before the attempt, and outside it: a payload with no memory
    # address is this deployer's bug and should fail loudly, while a supervisor
    # that does not answer is a fact about the Host and belongs in the report.
    memory_admin_url = contract.fixed_memory_admin_url(payload)
    try:
        spaces = memory_realms.capture(
            memory_admin_url,
            destination / memory_realms.SPACES_DIRECTORY,
        )
    except TargetError as exc:
        # Whatever was copied before the failure goes with it. A directory
        # holding three of four spaces while the report says memory was not
        # carried is an invitation to restore from it anyway.
        shutil.rmtree(destination / memory_realms.SPACES_DIRECTORY, ignore_errors=True)
        uncovered.append(memory_realms.unavailable(str(exc)))
        uncovered.sort(key=lambda entry: str(entry["state"]))
    return {
        "status": "captured",
        "release_id": release_id,
        "host_id": host_id_or_none(),
        "directory": str(destination),
        "authorities": authorities,
        "memory_spaces": spaces,
        "not_covered": uncovered,
        "consistency": (
            "each authority is snapshotted on its own instant; this is not a "
            "point-in-time image of the whole Host"
        ),
    }

def restore(payload: Mapping[str, object]) -> dict[str, object]:
    """Put a backup back on the Host it came from, or refuse.

    A restore that lands on a different Host quietly produces a machine whose
    Controller grants, Hub identity and TLS names all describe somewhere else,
    so the identity is checked before anything is written rather than after
    someone notices their phone no longer recognises the Host.

    Services stop for this. SQLite can be read consistently while it is being
    written; it cannot be replaced underneath a process that has it open.

    Memory's spaces go back last, after the product is running again, because
    only a live supervisor can take one realm's runner off its palace. The
    refusals that matter there are memory's own and it makes them before it
    writes, so a space that cannot be restored leaves that space as it was
    rather than half of two copies.
    """

    contract.fixed_units(payload)
    release_id = contract.fixed_release_id(payload)
    manifest_value = payload.get("manifest")
    if not isinstance(manifest_value, dict):
        raise TargetError("restore requires the manifest its backup produced")
    if manifest_value.get("host_id") != host_id_or_none():
        raise TargetError(
            "backup belongs to a different Host; restoring it here would "
            "describe a machine that does not exist"
        )
    if manifest_value.get("release_id") != release_id:
        raise TargetError("backup was taken from a different release")
    source_directory = contract.VAR_TMP / f"eidolon-backup-{release_id}"
    entries = manifest_value.get("authorities")
    if not isinstance(entries, list) or not entries:
        raise TargetError("backup manifest names no authority")
    prepared: list[tuple[str, Path, Path, str, str]] = []
    for entry in entries:
        if not isinstance(entry, dict):
            raise TargetError("backup manifest entry is malformed")
        name = entry.get("authority")
        if name not in contract.BACKED_UP_AUTHORITIES:
            raise TargetError(f"backup names an authority this Host does not have: {name}")
        copy = source_directory / str(entry.get("file"))
        if copy.parent != source_directory or not copy.is_file():
            raise TargetError(f"backup file is missing: {entry.get('file')}")
        if primitives.file_sha256(copy) != entry.get("sha256"):
            raise TargetError(f"backup file does not match its digest: {name}")
        destination, user, group = contract.BACKED_UP_AUTHORITIES[name]
        prepared.append((name, copy, destination, user, group))
    if {name for name, *_ in prepared} != set(contract.BACKED_UP_AUTHORITIES):
        raise TargetError("backup does not cover every authority this Host keeps")

    stopped = reset.command_stop_units(contract.RESET_STOP_UNITS)
    restored: list[str] = []
    try:
        for name, copy, destination, user, group in prepared:
            destination.parent.mkdir(parents=True, exist_ok=True)
            for suffix in ("-wal", "-shm"):
                destination.with_name(destination.name + suffix).unlink(missing_ok=True)
            temporary = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.tmp")
            try:
                shutil.copyfile(copy, temporary)
                os.chmod(temporary, 0o640 if name != "bootstrap" else 0o600)
                primitives.chown_path(temporary, user, group)
                os.replace(temporary, destination)
            finally:
                temporary.unlink(missing_ok=True)
            restored.append(name)
    finally:
        primitives.checked("systemd reload", ("/usr/bin/systemctl", "daemon-reload"), timeout=120)
    # A restore that leaves the product down is half an operation: the operator
    # would have to know which units to start and in what order, which is the
    # knowledge this tool exists to hold.
    started = lifecycle.lifecycle("start", payload)

    spaces = manifest_value.get("memory_spaces")
    if spaces is None:
        # A backup taken before memory declared a snapshot. Saying so beats
        # both silence and a failure: the authorities did go back.
        memory_result: object = "this backup carries no memory spaces"
    elif not isinstance(spaces, list):
        raise TargetError("backup manifest describes memory spaces as a non-list")
    else:
        memory_result = memory_realms.put_back(
            contract.fixed_memory_admin_url(payload),
            source_directory / memory_realms.SPACES_DIRECTORY,
            spaces,
        )
    return {
        "status": "restored",
        "release_id": release_id,
        "host_id": manifest_value.get("host_id"),
        "restored": restored,
        "stopped": stopped,
        "started": started.get("status"),
        "memory_spaces": memory_result,
        "not_restored": [name for name in sorted(contract.UNCOVERED_STATE)],
    }
