"""The Host layer Ops derives on top of a release, delivered and waited for."""

from __future__ import annotations

import os
import shutil
import stat
import time
import uuid
from collections.abc import Callable, Mapping
from pathlib import Path

from . import contract, primitives
from .primitives import TargetError

#: The Host layer Ops owns on top of a release: it fronts the Hub on the LAN,
#: so the Hub declares Wants= on it and nothing here starts it by hand.
HOST_APPLICATION_UNIT = "eidolon-hub-ingress.service"

HOST_APPLICATION_READY_SECONDS = 30.0

def await_host_application(run: Callable[..., object], root: Path = Path("/")) -> None:
    """Wait for the Host layer, the way the release waits for its components.

    Starting it is the Hub's job now: the Hub declares ``Wants=`` on the
    ingress, so every path that brings the Hub up brings the LAN up with it.
    What no unit can express is the barrier — a release's readiness set covers
    release components only, and the App gate downstream reads the Hub port
    once, without retrying. So this waits, and never starts.
    """

    if not primitives.host_path(root, contract.HOST_APPLICATION_INPUTS["hub-ingress.service"][0]).is_file():
        return
    deadline = time.monotonic() + HOST_APPLICATION_READY_SECONDS
    while True:
        result = run(("/usr/bin/systemctl", "is-active", HOST_APPLICATION_UNIT), timeout=30)
        if result.returncode == 0:
            return
        if time.monotonic() >= deadline:
            state = (result.stdout or "").strip() or "unknown"
            raise TargetError(f"Host application ingress is not active: {state}")
        time.sleep(0.5)

def refresh_host_application(payload: Mapping[str, object]) -> dict[str, object]:
    """Deliver the Host layer the operator derived, without a reinstall.

    An activation replaces components and leaves this layer alone, so a fix to
    the ingress unit or the rendered Hub settings could reach a Host no way but
    by installing it again. The Host TLS pair is not among these: private Host
    material is written once. Public Owner trust material is refreshed
    atomically with the signed directory so an existing Host can acquire or
    repair it.
    """

    contract.fixed_units(payload)
    release_id = contract.fixed_release_id(payload)
    stage = contract.VAR_TMP / f"eidolon-secrets-{release_id}"
    if stage.parent != contract.VAR_TMP or contract.STAGING_NAME.fullmatch(stage.name) is None:
        raise TargetError("secret staging path is unsafe")
    if not stage.is_dir() or stage.is_symlink():
        raise TargetError("Host application staging directory is missing")
    changed: list[str] = []
    for name in contract.REFRESHABLE_HOST_APPLICATION_INPUTS:
        source = stage / name
        if not source.is_file():
            raise TargetError(f"Host application asset was not staged: {name}")
        destination_value, user, group, mode = contract.HOST_APPLICATION_INPUTS[name]
        destination = primitives.host_path(Path("/"), destination_value)
        if (
            destination.is_file()
            and not destination.is_symlink()
            and destination.read_bytes() == source.read_bytes()
            and stat.S_IMODE(destination.stat().st_mode) == mode
        ):
            continue
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.tmp")
        try:
            shutil.copyfile(source, temporary)
            os.chmod(temporary, mode)
            primitives.chown_path(temporary, user, group)
            os.replace(temporary, destination)
        finally:
            temporary.unlink(missing_ok=True)
        changed.append(str(destination_value))
    if changed:
        primitives.checked("systemd reload", ("/usr/bin/systemctl", "daemon-reload"), timeout=120)
    return {"status": "refreshed", "changed": changed, "removed": remove_legacy_system_assets()}

def remove_legacy_system_assets(root: Path = Path("/")) -> list[str]:
    """Take away what a release used to install and no longer does.

    A release only ever writes the assets its descriptor names; dropping one
    from that set stops it being written but does not take the old copy off a
    Host that already has it. So the paths are named in the contract and
    removed here, on the same pass that delivers this layer — otherwise a Host
    keeps the file until someone reinstalls it, which for the Hub settings copy
    means keeping the misleading placeholder ``hub_id`` an operator reads first.

    Nothing needs the file back on a rollback: the Hub settings path is also
    named by the systemd drop-in this layer installs, which outlives a release
    rollback and points a restored older unit at the rendered settings too.
    """

    removed: list[str] = []
    for value in contract.LEGACY_SYSTEM_ASSETS:
        path = primitives.host_path(root, value)
        if not (path.is_file() and not path.is_symlink()):
            continue
        path.unlink()
        removed.append(str(value))
    return removed
