"""The Host layer Ops derives on top of a release, delivered and waited for."""

from __future__ import annotations

import grp
import os
import pwd
import shutil
import stat
import time
import uuid
from collections.abc import Callable, Mapping
from pathlib import Path

from . import app_contract, contract, primitives
from .primitives import TargetError

#: Where this Host tells its own ``.local`` responder about the Hub name.
#:
#: The Hub advertises ``_eidolon-owner._tcp`` with an SRV target of
#: ``eidolon-hub-<host>.local`` — a name devices must resolve, pin TLS against,
#: and reach. Nothing published an address for it. Clients that happened to
#: hold a cached answer worked; a device booting fresh got no mDNS answer, fell
#: through to unicast DNS, and its router handed back an unrelated LAN address
#: whose port 9443 was closed. The device then reported only
#: "failed to connect", forever.
#:
#: avahi-daemon owns ``.local`` on this Host, so the name is registered there,
#: from the address this Host is observed to answer on — never from a declared
#: value carried across from the workstation, which is how it would go stale.
_AVAHI_HOSTS = Path("/etc/avahi/hosts")
_AVAHI_UNIT = "avahi-daemon.service"

#: The Host layer Ops owns on top of a release: it fronts the Hub on the LAN,
#: so the Hub declares Wants= on it and nothing here starts it by hand.
HOST_APPLICATION_UNIT = "eidolon-hub-ingress.service"

HOST_APPLICATION_READY_SECONDS = 30.0


def _expected_ids(user: str, group: str) -> tuple[int, int]:
    try:
        return pwd.getpwnam(user).pw_uid, grp.getgrnam(group).gr_gid
    except KeyError as exc:
        raise TargetError(f"required Host layer identity is missing: {user}:{group}") from exc


def await_host_application(run: Callable[..., object], root: Path = Path("/")) -> None:
    """Wait for the Host layer, the way the release waits for its components.

    Starting it is the Hub's job now: the Hub declares ``Wants=`` on the
    ingress, so every path that brings the Hub up brings the LAN up with it.
    What no unit can express is the barrier — a release's readiness set covers
    release components only, and the App gate downstream reads the Hub port
    once, without retrying. So this waits, and never starts.
    """

    if not primitives.host_path(
        root, contract.HOST_APPLICATION_INPUTS["hub-ingress.service"][0]
    ).is_file():
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
    by installing it again. Stable Host TLS material, public Owner trust, and
    the authoritative Host-bound environment files are refreshed atomically
    by file. Owner signing private keys are not part of this contract.
    """

    contract.fixed_units(payload)
    release_id = contract.fixed_release_id(payload)
    stage = contract.VAR_TMP / f"eidolon-secrets-{release_id}"
    if stage.parent != contract.VAR_TMP or contract.STAGING_NAME.fullmatch(stage.name) is None:
        raise TargetError("secret staging path is unsafe")
    if not stage.is_dir() or stage.is_symlink():
        raise TargetError("Host application staging directory is missing")
    _validate_hub_settings_compatibility(stage, release_id)
    _validate_product_settings_compatibility(stage, release_id)
    # A refresh is the deployment path for Host-owned contract changes, not
    # merely a byte copier.  Reconcile the path contract before replacing
    # files so a new public/private classification (including parent traversal
    # modes) takes effect before candidate services start.  Previously only a
    # first install did this, leaving updates with new file modes trapped below
    # an old, non-traversable /etc/eidolon directory.
    contract.ensure_host_path_contract(
        Path("/"),
        primitives.chown_path,
        contract.fixed_port_registry(payload),
    )
    changed: list[str] = []
    # Every Host application input is required now. What used to be placed
    # first here was a per-device commissioning registry that belonged to no
    # sealed release; nothing installs a per-device file any more.
    selected_inputs: list[str] = list(contract.REFRESHABLE_HOST_LAYER_INPUTS)
    for name in selected_inputs:
        source = stage / name
        if not source.is_file():
            raise TargetError(f"Host application asset was not staged: {name}")
        destination_value, user, group, mode = contract.INSTALL_INPUTS[name]
        destination = primitives.host_path(Path("/"), destination_value)
        if destination.exists() or destination.is_symlink():
            if destination.is_symlink() or not destination.is_file():
                raise TargetError(f"Host application destination is unsafe: {destination_value}")
            expected_uid, expected_gid = _expected_ids(user, group)
            metadata = destination.stat()
            if (
                stat.S_IMODE(metadata.st_mode) != mode
                or metadata.st_uid != expected_uid
                or metadata.st_gid != expected_gid
            ):
                raise TargetError(
                    f"Host application ownership or mode drifted: {destination_value}"
                )
        if (
            destination.is_file()
            and not destination.is_symlink()
            and destination.read_bytes() == source.read_bytes()
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
    removed: list[str] = []
    if changed:
        primitives.checked("systemd reload", ("/usr/bin/systemctl", "daemon-reload"), timeout=120)
    changed.extend(publish_hub_hostname(payload))
    removed.extend(remove_legacy_system_assets())
    return {"status": "refreshed", "changed": changed, "removed": removed}


def publish_hub_hostname(payload: Mapping[str, object], root: Path = Path("/")) -> list[str]:
    """Register the Hub's advertised name with this Host's ``.local`` responder.

    The advertisement and the address answer are two halves of one fact — where
    a device should send its first request — and they were owned by different
    components: the Hub process advertised the SRV target, and nothing at all
    answered for that name. Both halves live here now, derived from the same
    app contract, so a Host cannot advertise a name it does not answer for.

    Written from the observed address on every refresh rather than kept, so an
    address change is corrected by the next deploy instead of persisting as a
    confidently wrong answer.
    """

    app = app_contract.optional_app(payload)
    if app is None:
        return []
    hostname = str(app["hub_hostname"])
    address = str(app["lan_ipv4"])
    if not hostname.endswith(".local") or "/" in hostname or " " in hostname:
        raise TargetError(f"Hub hostname is not a bare .local name: {hostname}")
    path = primitives.host_path(root, _AVAHI_HOSTS)
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise TargetError(f"static mDNS host file is unsafe: {_AVAHI_HOSTS}")
    # Every line this Host owns is rewritten; lines for other names are kept
    # so the file stays usable by anything else the operator put there.
    kept: list[str] = []
    if path.is_file():
        for line in path.read_text(encoding="utf-8").splitlines():
            fields = line.split()
            if len(fields) >= 2 and fields[1] == hostname:
                continue
            kept.append(line)
    desired = "\n".join([*kept, f"{address} {hostname}"]).strip("\n") + "\n"
    if path.is_file() and path.read_text(encoding="utf-8") == desired:
        return []
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(desired, encoding="utf-8")
        os.chmod(temporary, 0o644)
        primitives.chown_path(temporary, "root", "root")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
    primitives.checked(
        "mDNS responder reload",
        ("/usr/bin/systemctl", "reload", _AVAHI_UNIT),
        timeout=60,
    )
    return [str(_AVAHI_HOSTS)]


def _validate_hub_settings_compatibility(stage: Path, release_id: str) -> None:
    """Require one rendered config to load in both sides of the cutover.

    The Host layer is installed before component symlinks switch.  A strict
    config understood only by the candidate can therefore strand the previous
    release during automatic rollback.  Schema expansion must first ship as
    code defaults; only a later release may require new YAML fields.
    """

    settings = stage / "hub.generated.yaml"
    if not settings.is_file() or settings.is_symlink():
        raise TargetError("rendered Hub settings were not staged safely")
    script = "from hub.config import HubConfig; HubConfig.load()"
    interpreters = (
        Path("/opt/eidolon/current/eidolon_hub/.venv/bin/python"),
        Path(f"/opt/eidolon/releases/{release_id}/eidolon_hub/.venv/bin/python"),
    )
    for interpreter in interpreters:
        primitives.checked(
            "cross-release Hub settings validation",
            (
                "/usr/bin/env",
                f"EIDOLON_HUB_SETTINGS_PATH={settings}",
                str(interpreter),
                "-c",
                script,
            ),
            timeout=120,
        )


def _validate_product_settings_compatibility(stage: Path, release_id: str) -> None:
    """Require each component config to load before replacing the live copy.

    The settings and component links switch in separate atomic operations. Both
    the current interpreter (rollback safety) and candidate interpreter (forward
    safety) must therefore understand the candidate settings before any live
    Host path is touched.
    """

    roots = {
        "EIDOLON_RUNTIME_ROOT": "/run/eidolon",
        "EIDOLON_STATE_ROOT": "/var/lib/eidolon",
        "EIDOLON_CACHE_ROOT": "/var/cache/eidolon",
        "EIDOLON_LOG_ROOT": "/var/log/eidolon",
    }
    specifications = (
        (
            "agent",
            "agent.yaml",
            "EIDOLON_AGENT_SETTINGS_YAML",
            "EIDOLON_AGENT_ENV_FILE=/etc/eidolon/agent.env",
            "from eidolon_agent.config import load_settings; load_settings()",
        ),
        (
            "channel",
            "channel.yaml",
            "EIDOLON_CHANNEL_SETTINGS_YAML",
            "EIDOLON_CHANNEL_ENV_FILE=/etc/eidolon/channel.env",
            (
                "from eidolon.livekit.common.config import load_effective_config; "
                "load_effective_config()"
            ),
        ),
        (
            "memory",
            "memory.yaml",
            "EIDOLON_MEMORY_SETTINGS_YAML",
            "EIDOLON_MEMORY_ENV_FILE=/etc/eidolon/memory.env",
            "from eidolon.memory.config import load_memory_settings; load_memory_settings()",
        ),
    )
    for component, filename, settings_variable, env_file, script in specifications:
        settings = stage / filename
        if not settings.is_file() or settings.is_symlink():
            raise TargetError(f"product settings were not staged safely: {filename}")
        interpreters = (
            Path(f"/opt/eidolon/current/eidolon_{component}/.venv/bin/python"),
            Path(f"/opt/eidolon/releases/{release_id}/eidolon_{component}/.venv/bin/python"),
        )
        for interpreter in interpreters:
            primitives.checked(
                f"cross-release {component} settings validation",
                (
                    "/usr/bin/env",
                    *(f"{key}={value}" for key, value in roots.items()),
                    f"{settings_variable}={settings}",
                    env_file,
                    str(interpreter),
                    "-c",
                    script,
                ),
                timeout=120,
            )


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
