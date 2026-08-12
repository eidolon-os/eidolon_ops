"""Remove exactly the fixed Eidolon deployment namespace, and nothing else."""

from __future__ import annotations

import os
import shutil
import subprocess
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path

from . import contract, primitives
from .primitives import TargetError


def refuse_nested_mounts(path: Path) -> None:
    """Never let recursive cleanup cross a mounted filesystem boundary."""

    for directory, names, _files in os.walk(path, followlinks=False):
        parent = Path(directory)
        for name in names:
            candidate = parent / name
            if not candidate.is_symlink() and os.path.ismount(candidate):
                raise TargetError(f"removal root contains a mount: {candidate}")

def reset_wipes_authority_data(payload: Mapping[str, object]) -> bool:
    value = payload.get("wipe_authority_data", False)
    if type(value) is not bool:
        raise TargetError("wipe_authority_data must be a boolean")
    return value

def reset_paths(*, wipe_authority_data: bool) -> tuple[Path, ...]:
    paths = set(contract.MANAGED_SYSTEM_ASSETS) | set(contract.RESET_DEPLOYMENT_ROOTS)
    if wipe_authority_data:
        paths.update(contract.RESET_AUTHORITY_ROOTS)
    return tuple(sorted(paths, key=str))

def reset_plan(
    payload: Mapping[str, object],
    *,
    root: Path = Path("/"),
) -> dict[str, object]:
    """Describe a clean reinstall boundary without changing the Host."""

    contract.fixed_units(payload)
    contract.fixed_data(payload)
    wipe_authority_data = reset_wipes_authority_data(payload)
    root = root.resolve()
    detected = [
        str(path)
        for path in reset_paths(wipe_authority_data=wipe_authority_data)
        if (primitives.host_path(root, path).exists() or primitives.host_path(root, path).is_symlink())
    ]
    staging = primitives.host_path(root, contract.VAR_TMP)
    staged = []
    if staging.is_dir() and not staging.is_symlink():
        staged = sorted(
            str(contract.VAR_TMP / path.name)
            for path in staging.iterdir()
            if contract.STAGING_NAME.fullmatch(path.name) is not None
        )
    return {
        "status": "planned",
        "wipe_authority_data": wipe_authority_data,
        "detected": detected,
        "staging": staged,
        "preserved": [
            "foundation packages and pinned NATS/LiveKit/Node/uv installations",
            "service identities",
            *(
                []
                if wipe_authority_data
                else ["/var/lib/eidolon and /var/lib/eidolon-bootstrap authority data"]
            ),
        ],
    }

def remove_reset_path(path: Path, *, display: Path) -> bool:
    if not (path.exists() or path.is_symlink()):
        return False
    if path.is_symlink() or path.is_file():
        path.unlink()
        return True
    if not path.is_dir() or os.path.ismount(path):
        raise TargetError(f"reset target is not a removable owned path: {display}")
    refuse_nested_mounts(path)
    shutil.rmtree(path)
    return True

def reset_host(
    payload: Mapping[str, object],
    *,
    root: Path = Path("/"),
    command: Callable[..., subprocess.CompletedProcess[str]] = primitives.run,
    manage_services: bool = True,
) -> dict[str, object]:
    """Remove the fixed Eidolon deployment namespace for a clean reinstall."""

    if os.geteuid() != 0 and root == Path("/"):
        raise TargetError("Host reset requires root")
    plan = reset_plan(payload, root=root)
    wipe_authority_data = bool(plan["wipe_authority_data"])
    root = root.resolve()
    removed: list[str] = []
    service_results: list[dict[str, object]] = []
    lock_path = primitives.host_path(root, Path("/run/lock/eidolon-install.lock"))
    with primitives.exclusive(lock_path):
        if manage_services:
            present: list[str] = []
            for unit in contract.RESET_STOP_UNITS:
                observed = command(
                    (
                        "/usr/bin/systemctl",
                        "show",
                        "--property",
                        "LoadState",
                        "--value",
                        unit,
                    ),
                    timeout=20,
                )
                if observed.returncode != 0:
                    detail = observed.stderr.strip() or observed.stdout.strip() or unit
                    raise TargetError(f"reset could not inspect product unit: {detail}")
                if observed.stdout.strip() == "not-found":
                    service_results.append({"unit": unit, "state": "absent"})
                    continue
                present.append(unit)
            if present:
                # One transaction per phase, not one call per unit: a unit
                # still in its restart loop re-enqueues start jobs for what it
                # depends on, which cancels a pending stop job for a unit
                # already handled. Two phases, because the manager has to be
                # gone before the workers it would otherwise put back.
                phases = (
                    [unit for unit in present if unit in contract.RESET_RECONCILER_UNITS],
                    [unit for unit in present if unit not in contract.RESET_RECONCILER_UNITS],
                )
                reports: list[str] = []
                returncode = 0
                for phase in phases:
                    if not phase:
                        continue
                    stopped = command(
                        ("/usr/bin/systemctl", "disable", "--now", *phase), timeout=300
                    )
                    returncode = returncode or stopped.returncode
                    if stopped.stderr.strip():
                        reports.append(stopped.stderr.strip())
                lingering = [
                    unit
                    for unit in present
                    if command(("/usr/bin/systemctl", "is-active", unit), timeout=20)
                    .stdout.strip()
                    not in {"inactive", "failed", "unknown"}
                ]
                if lingering:
                    detail = ", ".join(lingering) + (
                        f" ({'; '.join(reports)})" if reports else ""
                    )
                    raise TargetError(f"reset could not stop product unit: {detail}")
                service_results.extend(
                    {"unit": unit, "state": "stopped", "returncode": returncode}
                    for unit in present
                )
        for value in reset_paths(wipe_authority_data=wipe_authority_data):
            if remove_reset_path(primitives.host_path(root, value), display=value):
                removed.append(str(value))
        staging = primitives.host_path(root, contract.VAR_TMP)
        if staging.is_dir() and not staging.is_symlink():
            for path in sorted(staging.iterdir()):
                if contract.STAGING_NAME.fullmatch(path.name) is None:
                    continue
                display = contract.VAR_TMP / path.name
                if remove_reset_path(path, display=display):
                    removed.append(str(display))
        if manage_services:
            reloaded = command(("/usr/bin/systemctl", "daemon-reload"), timeout=120)
            if reloaded.returncode != 0:
                detail = reloaded.stderr.strip() or reloaded.stdout.strip() or "no output"
                raise TargetError(f"systemd daemon-reload failed after reset: {detail}")
            command(("/usr/bin/systemctl", "reset-failed"), timeout=120)
    return {
        "status": "reset",
        "wipe_authority_data": wipe_authority_data,
        "removed": removed,
        "services": service_results,
    }

def command_stop_units(units: Sequence[str]) -> list[str]:
    """Take the product down in the order that survives its own manager."""

    present = [
        unit
        for unit in units
        if primitives.run(
            ("/usr/bin/systemctl", "show", "--property", "LoadState", "--value", unit),
            timeout=20,
        ).stdout.strip()
        != "not-found"
    ]
    for phase in (
        [unit for unit in present if unit in contract.RESET_RECONCILER_UNITS],
        [unit for unit in present if unit not in contract.RESET_RECONCILER_UNITS],
    ):
        if phase:
            primitives.run(("/usr/bin/systemctl", "stop", *phase), timeout=300)
    return present
