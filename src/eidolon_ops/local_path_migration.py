"""One-time, explicit macOS state-layout cutover into the Host path contract."""

from __future__ import annotations

import fcntl
import json
import os
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from eidolon_ops.controller import OperationsError
from eidolon_ops.paths import HostProfile


@dataclass(frozen=True, slots=True)
class PathMove:
    move_id: str
    source: Path
    target: Path


class LocalPathMigrator:
    def __init__(self, profile: HostProfile) -> None:
        if profile.driver != "local-supervisord":
            raise OperationsError("local path migration is available on the macOS adapter only")
        self.profile = profile
        self.moves = _moves(profile)
        self.lock_path = profile.paths.runtime_root / "ops/path-migration.lock"
        self.evidence_path = profile.paths.state_root / "ops/path-migration-v1.json"

    def plan(self) -> dict[str, object]:
        entries = [self._entry(move) for move in self.moves]
        conflicts = [entry["move_id"] for entry in entries if entry["state"] == "conflict"]
        pending = [entry["move_id"] for entry in entries if entry["state"] == "pending"]
        live_processes = self._live_processes()
        return {
            "status": "conflict" if conflicts else "migration_required" if pending else "clean",
            "host_id": self.profile.host_id,
            "moves": entries,
            "conflicts": conflicts,
            "pending": pending,
            "live_processes": live_processes,
            "apply_ready": not conflicts and not live_processes,
        }

    def apply(self) -> dict[str, object]:
        self._require_stopped()
        with _exclusive(self.lock_path):
            before = self.plan()
            if before["status"] == "conflict":
                raise OperationsError(
                    "local path migration refuses source/target conflicts: "
                    + ", ".join(before["conflicts"])
                )
            for move in self.moves:
                entry = self._entry(move)
                if entry["state"] != "pending":
                    continue
                _safe_parent(move.target.parent)
                os.replace(move.source, move.target)
            after = self.plan()
            if after["status"] != "clean":
                raise OperationsError("local path migration did not converge to a clean layout")
            evidence = {
                "schema_version": 1,
                "host_id": self.profile.host_id,
                "status": "completed",
                "moves": after["moves"],
            }
            _atomic_json(self.evidence_path, evidence)
            return {**after, "status": "migrated", "evidence": str(self.evidence_path)}

    def require_clean(self) -> None:
        plan = self.plan()
        if plan["status"] == "conflict":
            raise OperationsError(
                "legacy and canonical Mac paths both exist: " + ", ".join(plan["conflicts"])
            )
        if plan["status"] == "migration_required":
            raise OperationsError(
                "Mac state paths require an explicit cutover; run "
                "eidolon-ops --config <host.toml> migrate-paths --apply"
            )

    def _entry(self, move: PathMove) -> dict[str, object]:
        source_exists = move.source.exists() or move.source.is_symlink()
        target_exists = move.target.exists() or move.target.is_symlink()
        if source_exists and target_exists:
            state = "conflict"
        elif source_exists:
            state = "pending"
        elif target_exists:
            state = "migrated"
        else:
            state = "absent"
        return {
            "move_id": move.move_id,
            "source": str(move.source),
            "target": str(move.target),
            "state": state,
        }

    def _require_stopped(self) -> None:
        live = self._live_processes()
        if live:
            details = ", ".join(f"{item['pid_file']}:{item['pid']}" for item in live)
            raise OperationsError(
                "stop the local Eidolon stack before migrating state paths: " + details
            )

    def _live_processes(self) -> list[dict[str, object]]:
        candidates = list((self.profile.paths.runtime_root / "ops").glob("*.pid"))
        candidates.extend(self.profile.paths.runtime_root.glob("eidolon-admin*.pid"))
        # An external-foundation profile intentionally reuses a pre-existing
        # infrastructure supervisor (currently NATS/client-web). It owns no
        # product SQLite path in this migration, so only the Ops-owned product
        # supervisor must be stopped.
        # Other profiles retain the one-time guard for the executor location
        # being retired from Admin.
        if self.profile.foundation_mode != "external":
            candidates.extend((self.profile.paths.current_root / "eidolon_admin/var").glob("*.pid"))
        live: list[dict[str, object]] = []
        for path in candidates:
            try:
                pid = int(path.read_text(encoding="utf-8").strip())
            except (OSError, ValueError):
                continue
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                continue
            except PermissionError:
                # EPERM proves that the PID exists but this observer cannot
                # signal it.  A state cutover must fail closed in that case.
                pass
            live.append({"pid": pid, "pid_file": str(path)})
        return sorted(live, key=lambda item: str(item["pid_file"]))


def _moves(profile: HostProfile) -> tuple[PathMove, ...]:
    state = profile.paths.state_root
    legacy_root = state.parent
    return (
        PathMove(
            "agent-db", state / "eidolon-agent.sqlite3", state / "agent/eidolon-agent.sqlite3"
        ),
        PathMove(
            "agent-db-wal",
            state / "eidolon-agent.sqlite3-wal",
            state / "agent/eidolon-agent.sqlite3-wal",
        ),
        PathMove(
            "agent-db-shm",
            state / "eidolon-agent.sqlite3-shm",
            state / "agent/eidolon-agent.sqlite3-shm",
        ),
        PathMove("audit-db", state / "audit-index.sqlite3", state / "audit/audit-index.sqlite3"),
        PathMove(
            "audit-db-wal",
            state / "audit-index.sqlite3-wal",
            state / "audit/audit-index.sqlite3-wal",
        ),
        PathMove(
            "audit-db-shm",
            state / "audit-index.sqlite3-shm",
            state / "audit/audit-index.sqlite3-shm",
        ),
        PathMove("hub-db", state / "eidolon-hub.sqlite3", state / "hub/eidolon-hub.sqlite3"),
        PathMove(
            "hub-db-wal",
            state / "eidolon-hub.sqlite3-wal",
            state / "hub/eidolon-hub.sqlite3-wal",
        ),
        PathMove(
            "hub-db-shm",
            state / "eidolon-hub.sqlite3-shm",
            state / "hub/eidolon-hub.sqlite3-shm",
        ),
        PathMove("nats-jetstream", state / "nats-jetstream", state / "nats/jetstream"),
        PathMove("voiceprints", legacy_root / "voiceprints", state / "voiceprints"),
        PathMove("debug-cache", legacy_root / "debug", profile.paths.cache_root / "debug"),
    )


def _safe_parent(path: Path) -> None:
    if path.exists() and (path.is_symlink() or not path.is_dir()):
        raise OperationsError(f"migration target parent is unsafe: {path}")
    path.mkdir(parents=True, exist_ok=True)


@contextmanager
def _exclusive(path: Path) -> Iterator[None]:
    _safe_parent(path.parent)
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, 0o600)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise OperationsError("another local path migration is running") from exc
        yield
    finally:
        os.close(descriptor)


def _atomic_json(path: Path, value: object) -> None:
    _safe_parent(path.parent)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
