"""Exact, explicit reset of Hub-owned Owner Authority state.

This is deliberately not a factory reset.  It advances one Owner Domain
lineage while preserving the Host identity, release, network configuration,
and every authority outside Hub.  The controller first installs a signed
descriptor and one-shot bootstrap capability for the next generation; this
operation then performs the destructive cutover under a Host-local lock.
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import time
from collections.abc import Callable, Mapping
from pathlib import Path

from . import contract, primitives
from .primitives import TargetError

HUB_DATABASE = Path("/var/lib/eidolon/hub/eidolon-hub.sqlite3")
AUTHORITY_ANCHOR = Path("/var/lib/eidolon/hub/authority-lineage.json")
AUTHORITY_BOOTSTRAP = Path("/var/lib/eidolon/hub/authority-bootstrap.json")
OWNER_DESCRIPTOR = Path(
    "/etc/eidolon/owner-domain/owner_domain_descriptor.json"
)
HUB_UNIT = "eidolon-hub.service"
HUB_INGRESS_UNIT = "eidolon-hub-ingress.service"
_SQLITE_SIDECARS = ("-wal", "-shm", "-journal")


def _request(payload: Mapping[str, object]) -> dict[str, object]:
    contract.fixed_units(payload)
    value = payload.get("authority_reset")
    keys = {
        "owner_domain_id",
        "previous_generation",
        "next_generation",
        "state_id",
    }
    if not isinstance(value, dict) or set(value) != keys:
        raise TargetError("Owner Authority reset request is invalid")
    owner_domain_id = value.get("owner_domain_id")
    previous_generation = value.get("previous_generation")
    next_generation = value.get("next_generation")
    state_id = value.get("state_id")
    if (
        not isinstance(owner_domain_id, str)
        or not owner_domain_id.startswith("owner-")
        or not isinstance(previous_generation, int)
        or isinstance(previous_generation, bool)
        or previous_generation < 1
        or not isinstance(next_generation, int)
        or isinstance(next_generation, bool)
        or next_generation != previous_generation + 1
        or not isinstance(state_id, str)
        or not state_id.startswith("authority-state_")
    ):
        raise TargetError("Owner Authority reset request is invalid")
    return dict(value)


def _expected(request: Mapping[str, object]) -> dict[str, object]:
    return {
        "contract_version": 1,
        "owner_domain_id": request["owner_domain_id"],
        "owner_domain_generation": request["next_generation"],
        "state_id": request["state_id"],
    }


def _read_json(path: Path, *, label: str) -> dict[str, object] | None:
    if not path.exists():
        return None
    if path.is_symlink() or not path.is_file():
        raise TargetError(f"{label} is not a safe regular file")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise TargetError(f"{label} is invalid") from exc
    if not isinstance(value, dict):
        raise TargetError(f"{label} is invalid")
    return value


def _marker(database: Path) -> dict[str, object] | None:
    if not database.exists():
        return None
    if database.is_symlink() or not database.is_file():
        raise TargetError("Hub database is not a safe regular file")
    try:
        connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
        try:
            present = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                ("hub_authority_state",),
            ).fetchone()
            if present is None:
                return None
            row = connection.execute(
                "SELECT owner_domain_id, owner_domain_generation, state_id "
                "FROM hub_authority_state WHERE singleton_id=1"
            ).fetchone()
        finally:
            connection.close()
    except sqlite3.Error as exc:
        raise TargetError("Hub Authority marker is unreadable") from exc
    if row is None:
        raise TargetError("Hub Authority marker is missing")
    return {
        "contract_version": 1,
        "owner_domain_id": row[0],
        "owner_domain_generation": row[1],
        "state_id": row[2],
    }


def _validate_new_inputs(root: Path, expected: Mapping[str, object]) -> None:
    descriptor = _read_json(
        primitives.host_path(root, OWNER_DESCRIPTOR), label="Owner Domain descriptor"
    )
    if descriptor is None or (
        descriptor.get("owner_domain_id") != expected["owner_domain_id"]
        or descriptor.get("owner_domain_generation")
        != expected["owner_domain_generation"]
    ):
        raise TargetError("installed Owner Domain descriptor is not the reset generation")
    bootstrap = _read_json(
        primitives.host_path(root, AUTHORITY_BOOTSTRAP),
        label="Owner Authority bootstrap capability",
    )
    if bootstrap != {"operation": "owner-authority.bootstrap", **expected}:
        raise TargetError("Owner Authority bootstrap capability does not match reset")


def established_lineage(*, root: Path = Path("/")) -> dict[str, object]:
    """Report the Owner Authority lineage this Host actually holds.

    Read-only, and deliberately separate from the reset request shape: the
    controller has to know what a Host holds *before* it decides which
    capability to ship, and on the path that matters most the answer is
    "nothing at all" — an empty namespace has no request to validate against.

    ``established`` is the lineage the Host can prove twice over. ``marker``
    is reported on its own because a database whose external anchor is missing
    is a recovery case, not an empty Host, and a caller that only looked at
    ``established`` would mistake one for the other and mint a generation over
    a database that is still there.
    """

    root = root.resolve()
    marker = _marker(primitives.host_path(root, HUB_DATABASE))
    anchor = _read_json(
        primitives.host_path(root, AUTHORITY_ANCHOR),
        label="Owner Authority lineage anchor",
    )
    return {
        "marker": marker,
        "anchor": anchor,
        "established": marker if marker is not None and marker == anchor else None,
    }


def authority_lineage(
    payload: Mapping[str, object], *, root: Path = Path("/")
) -> dict[str, object]:
    """The read-only op behind ``established_lineage``."""

    contract.fixed_units(payload)
    return {"status": "observed", **established_lineage(root=root)}


def authority_reset_plan(
    payload: Mapping[str, object], *, root: Path = Path("/")
) -> dict[str, object]:
    """Prove the exact next lineage and whether the destructive step remains."""

    request = _request(payload)
    expected = _expected(request)
    root = root.resolve()
    database = primitives.host_path(root, HUB_DATABASE)
    anchor_path = primitives.host_path(root, AUTHORITY_ANCHOR)
    marker = _marker(database)
    anchor = _read_json(anchor_path, label="Owner Authority lineage anchor")
    if marker == expected and anchor == expected:
        return {
            "status": "already_reset",
            "authority": expected,
            "destructive_work_required": False,
        }
    if marker == expected or anchor == expected:
        raise TargetError("Owner Authority reset is partially committed")
    _validate_new_inputs(root, expected)
    return {
        "status": "planned",
        "authority": expected,
        "destructive_work_required": True,
        "removes": [
            str(HUB_DATABASE),
            *(str(HUB_DATABASE) + suffix for suffix in _SQLITE_SIDECARS),
            str(AUTHORITY_ANCHOR),
        ],
        "preserves": [
            "Host identity and active release",
            "network and commissioning transport configuration",
            "Kernel, Admin, Channel, Agent, Memory and Controller authorities",
        ],
    }


def _checked(
    command: Callable[..., subprocess.CompletedProcess[str]],
    value: tuple[str, ...],
    *,
    operation: str,
    timeout: int,
) -> None:
    result = command(value, timeout=timeout)
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or "no output"
        raise TargetError(f"{operation} failed: {detail}")


def _remove_exact(path: Path, *, label: str) -> bool:
    if not path.exists():
        return False
    if path.is_symlink() or not path.is_file():
        raise TargetError(f"{label} is not a safe regular file")
    path.unlink()
    return True


def reset_owner_authority(
    payload: Mapping[str, object],
    *,
    root: Path = Path("/"),
    command: Callable[..., subprocess.CompletedProcess[str]] = primitives.run,
    manage_services: bool = True,
    ready_timeout_seconds: float = 120.0,
) -> dict[str, object]:
    """Replace only Hub Authority state and prove the new external lineage."""

    if os.geteuid() != 0 and root == Path("/"):
        raise TargetError("Owner Authority reset requires root")
    request = _request(payload)
    expected = _expected(request)
    root = root.resolve()
    plan = authority_reset_plan(payload, root=root)
    if plan["status"] == "already_reset":
        return plan
    lock_path = primitives.host_path(root, Path("/run/lock/eidolon-install.lock"))
    database = primitives.host_path(root, HUB_DATABASE)
    anchor = primitives.host_path(root, AUTHORITY_ANCHOR)
    removed: list[str] = []
    with primitives.exclusive(lock_path):
        # Re-prove after taking the same lock as install/reset.  No file is
        # removed based on a plan observed before a concurrent transition.
        authority_reset_plan(payload, root=root)
        if manage_services:
            _checked(
                command,
                ("/usr/bin/systemctl", "stop", HUB_INGRESS_UNIT, HUB_UNIT),
                operation="Hub quiesce for Owner Authority reset",
                timeout=180,
            )
        for display in (
            HUB_DATABASE,
            *(Path(str(HUB_DATABASE) + suffix) for suffix in _SQLITE_SIDECARS),
            AUTHORITY_ANCHOR,
        ):
            if _remove_exact(
                primitives.host_path(root, display), label=f"reset target {display}"
            ):
                removed.append(str(display))
        if manage_services:
            _checked(
                command,
                ("/usr/bin/systemctl", "start", HUB_UNIT),
                operation="Hub start after Owner Authority reset",
                timeout=180,
            )
            deadline = time.monotonic() + ready_timeout_seconds
            while True:
                active = command(
                    ("/usr/bin/systemctl", "is-active", HUB_UNIT), timeout=20
                )
                if active.returncode == 0 and active.stdout.strip() == "active":
                    marker = _marker(database)
                    lineage = _read_json(
                        anchor, label="Owner Authority lineage anchor"
                    )
                    bootstrap = primitives.host_path(root, AUTHORITY_BOOTSTRAP)
                    if (
                        marker == expected
                        and lineage == expected
                        and not bootstrap.exists()
                    ):
                        break
                if time.monotonic() >= deadline:
                    detail = active.stderr.strip() or active.stdout.strip() or "unknown"
                    raise TargetError(
                        "Hub did not establish the requested Owner Authority lineage: "
                        f"{detail}"
                    )
                time.sleep(0.5)
        marker = _marker(database)
        lineage = _read_json(anchor, label="Owner Authority lineage anchor")
        bootstrap = primitives.host_path(root, AUTHORITY_BOOTSTRAP)
        if marker != expected or lineage != expected:
            raise TargetError("Hub did not establish the requested Owner Authority lineage")
        if bootstrap.exists():
            raise TargetError("Hub did not consume the Owner Authority bootstrap capability")
    return {
        "status": "authority_reset",
        "authority": expected,
        "removed": removed,
        "bootstrap_consumed": True,
    }
