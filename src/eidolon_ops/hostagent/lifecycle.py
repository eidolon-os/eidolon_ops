"""Observe the Host, and take the product across a boundary as one action."""

from __future__ import annotations

import json
import os
import platform
import shutil
from collections.abc import Mapping
from pathlib import Path

from . import app_contract, contract, host_application, primitives
from .primitives import TargetError

BOOTSTRAP_CTL = Path("/opt/eidolon/current/eidolon_admin/.venv/bin/eidolon-bootstrapctl")

def status(payload: Mapping[str, object]) -> dict[str, object]:
    units = contract.fixed_units(payload)
    links: dict[str, str | None] = {}
    for component_id, path in contract.CURRENT_LINKS.items():
        links[component_id] = os.readlink(path) if path.is_symlink() else None
    evidence = contract.FIXED_DATA["deployment_evidence"]
    receipts: list[dict[str, object]] = []
    installations: list[dict[str, object]] = []
    if evidence.is_dir():
        for receipt in sorted(
            evidence.glob("*/receipt.json"), key=lambda item: item.stat().st_mtime
        )[-10:]:
            try:
                document = json.loads(receipt.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if isinstance(document, dict):
                receipts.append(
                    {
                        "path": str(receipt),
                        "release_id": document.get("release_id"),
                        "status": document.get("status"),
                        "transaction_id": document.get("transaction_id"),
                    }
                )
        for journal in sorted(
            evidence.glob("install-*/install.json"), key=lambda item: item.stat().st_mtime
        )[-10:]:
            try:
                document = json.loads(journal.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if isinstance(document, dict):
                installations.append(
                    {
                        "path": str(journal),
                        "release_id": document.get("release_id"),
                        "status": document.get("status"),
                        "phase": document.get("phase"),
                    }
                )
    return {
        "status": "observed",
        "host": platform.node(),
        "system": platform.system().lower(),
        "machine": platform.machine().lower(),
        "network": _network_status(),
        "units": {
            **{unit: primitives.unit_status(unit) for unit in units},
            **(
                {"eidolon-hub-ingress.service": primitives.unit_status("eidolon-hub-ingress.service")}
                if app_contract.optional_app(payload) is not None
                else {}
            ),
        },
        "current_links": links,
        "recent_receipts": receipts,
        "installations": installations,
        # Which commits each recent release was built from. A record no command
        # printed was a record that did not exist in practice: "which eight
        # commits was that release" got answered by looking at sibling
        # checkouts, which is how a release that never contained the change it
        # was made for reported success four times running.
        "release_sources": _release_provenance(evidence),
    }


def host_addresses(payload: Mapping[str, object]) -> dict[str, object]:
    """Every non-loopback IPv4 this Host answers on, as the Host observes them.

    The workstation's resolver cannot be trusted to name them all — a Host on
    both Wi-Fi and a point-to-point cable resolves to the Wi-Fi record alone
    once the cable's mDNS announcement has aged out — and the address it then
    fails to offer is the one a release has to travel over. Which addresses
    exist is a fact only this machine holds; ranking them by link quality
    remains the workstation's job, so this reports and does not choose.

    The same reading `status` already publishes, without the units, receipts
    and installation history that make that report expensive to ask for.
    """

    del payload
    return _network_status()


def _network_status() -> dict[str, object]:
    """Addresses observed on the product Host without making status fragile."""

    addresses = sorted(
        address for address in app_contract.host_addresses() if not address.startswith("127.")
    )
    try:
        lan_ipv4: str | None = str(app_contract.observed_lan_address())
    except TargetError:
        lan_ipv4 = None
    if lan_ipv4 is not None and lan_ipv4 not in addresses:
        addresses.insert(0, lan_ipv4)
    return {"lan_ipv4": lan_ipv4, "addresses": addresses}


def _release_provenance(evidence: Path) -> list[dict[str, object]]:
    """The source facts of recent cutovers, newest last, without their payload.

    Only the four fields worth reading at a glance. A cutover document also
    holds file digests, ownership and the previous component graph; copying
    those into a status report would bury the answer in the recovery data.
    """

    if not evidence.is_dir():
        return []
    recorded: list[dict[str, object]] = []
    for document in sorted(
        evidence.glob("*/cutover.json"), key=lambda item: item.stat().st_mtime
    )[-10:]:
        try:
            value = json.loads(document.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(value, dict):
            recorded.append(
                {
                    "path": str(document),
                    "release_id": value.get("release_id"),
                    "status": value.get("status"),
                    "sources": value.get("sources"),
                }
            )
    return recorded


def release_sources(payload: Mapping[str, object]) -> dict[str, object]:
    """The commits of releases that actually activated, newest first.

    Read on the failure path of a deploy, so it says nothing about the release
    being attempted and never raises for the ordinary case of a Host that has
    no history yet: a diagnostic that can fail is a diagnostic that replaces the
    error it was supposed to explain.
    """

    evidence = contract.FIXED_DATA["deployment_evidence"]
    activated = [
        {
            "release_id": item["release_id"],
            "status": item["status"],
            "sources": item["sources"],
        }
        for item in _release_provenance(evidence)
        if item["status"] == "activated" and isinstance(item["sources"], dict)
    ]
    return {"status": "observed", "releases": list(reversed(activated))}

def doctor_host(payload: Mapping[str, object]) -> dict[str, object]:
    contract.fixed_units(payload)
    contract.fixed_data(payload)
    remote_uv = payload.get("remote_uv")
    if not isinstance(remote_uv, str) or not Path(remote_uv).is_absolute():
        raise TargetError("remote uv path is invalid")
    host_env = contract.HOST_ENV_PATH
    checks = {
        "system": platform.system().lower() == "linux",
        "machine": platform.machine().lower() == "aarch64",
        "root": os.geteuid() == 0,
        "systemctl": Path("/usr/bin/systemctl").is_file(),
        "systemd_analyze": Path("/usr/bin/systemd-analyze").is_file(),
        "python3": Path("/usr/bin/python3").is_file(),
        "uv": Path(remote_uv).is_file() and os.access(remote_uv, os.X_OK),
        # Against the value for *this* Host's declaration, not the module
        # constant. host.env carries the capability line, so comparing it to
        # the baseline reported every Host that declares anything as failing
        # its own path contract — a false alarm that arrived with the first
        # capability and said nothing true about the Host.
        "host_path_contract": host_env.is_file()
        and host_env.read_text(encoding="utf-8")
        == contract.host_env_value(contract.declared_capabilities(payload)),
        "port_registry": contract.HOST_PORTS_PATH.is_file()
        and contract.HOST_PORTS_PATH.read_text(encoding="utf-8") == contract.fixed_port_registry(payload),
    }
    release_id = contract.fixed_release_id(payload, required=False)
    release_doctor: object = None
    if release_id is not None:
        descriptor = contract.RELEASES / release_id / "release.json"
        cli = contract.RELEASES / release_id / contract.RELEASE_ACTIVATOR
        result = primitives.run((str(cli), "doctor", str(descriptor)), timeout=180)
        if result.returncode != 0:
            release_doctor = {
                "healthy": False,
                "error": result.stderr.strip() or result.stdout.strip(),
            }
        else:
            try:
                release_doctor = {"healthy": True, "result": json.loads(result.stdout)}
            except json.JSONDecodeError:
                release_doctor = {"healthy": False, "error": "doctor output is not JSON"}
    return {
        "status": "healthy"
        if all(checks.values())
        and not (isinstance(release_doctor, dict) and not release_doctor.get("healthy"))
        else "degraded",
        "checks": checks,
        "release": release_doctor,
    }

def active_release(payload: Mapping[str, object]) -> dict[str, object]:
    """Resolve the active release's operator entries on the target itself.

    The deployer must not derive these from a component directory name; the
    target owns its own layout and reports the published, component-neutral
    entries here.
    """

    contract.fixed_units(payload)
    if not contract.CURRENT_KERNEL.is_symlink():
        raise TargetError("no Eidolon release is currently active")
    release_root = contract.CURRENT_KERNEL.resolve().parent
    if release_root.parent != contract.RELEASES:
        raise TargetError("the active release link points outside the release root")
    entries = {
        "activator": release_root / contract.RELEASE_ACTIVATOR,
        "interpreter": release_root / contract.RELEASE_INTERPRETER,
    }
    for name, path in entries.items():
        if not path.is_file() or not os.access(path, os.X_OK):
            raise TargetError(f"the active release does not publish its {name}")
    return {
        "status": "observed",
        "release_id": release_root.name,
        "release_root": str(release_root),
        **{name: str(path) for name, path in entries.items()},
    }

def lifecycle(action: str, payload: Mapping[str, object]) -> dict[str, object]:
    contract.fixed_units(payload)
    app = app_contract.optional_app(payload)
    try:
        from eidolon_deploy.linux import LinuxDeploymentHost
        from eidolon_deploy.manifest import load_release_descriptor
    except ImportError as exc:
        raise TargetError("active release deployment package is unavailable") from exc
    active_kernel = contract.CURRENT_KERNEL.resolve()
    descriptor = active_kernel.parent / "release.json"
    release = load_release_descriptor(descriptor)
    host = LinuxDeploymentHost(readiness_timeout_seconds=contract.release_readiness_seconds(payload))
    with host.exclusive_activation():
        host.preflight(release)
        if action in {"stop", "restart"}:
            host.quiesce(release)
        if action in {"start", "restart"}:
            host.start_release(release)
            host.wait_ready(release)
            if app is not None:
                host_application.await_host_application(primitives.run)
    return {
        "status": action + "ed" if action != "stop" else "stopped",
        "release_id": release.release_id,
    }

def rollback_plan(payload: Mapping[str, object]) -> dict[str, object]:
    release_id = contract.fixed_release_id(payload)
    snapshot_value = payload.get("snapshot")
    if not isinstance(snapshot_value, str):
        raise TargetError("snapshot path is invalid")
    snapshot = Path(snapshot_value)
    expected_parent = contract.FIXED_DATA["deployment_evidence"]
    if not snapshot.is_absolute() or snapshot.parent != expected_parent or not snapshot.is_dir():
        raise TargetError("snapshot is outside the fixed deployment evidence root or missing")
    descriptor = contract.RELEASES / release_id / "release.json"
    if not descriptor.is_file():
        raise TargetError("release descriptor is missing")
    return {
        "status": "rollback_planned",
        "release_id": release_id,
        "snapshot": str(snapshot),
        "descriptor": str(descriptor),
    }

def logs(payload: Mapping[str, object]) -> dict[str, object]:
    units = contract.fixed_units(payload)
    requested = payload.get("unit")
    selectable_units = (
        (*units, "eidolon-hub-ingress.service") if app_contract.optional_app(payload) is not None else units
    )
    if requested is not None and requested not in selectable_units:
        raise TargetError("requested log unit is outside the product topology")
    lines = payload.get("lines", 200)
    if type(lines) is not int or not 1 <= lines <= 5000:
        raise TargetError("log line count is invalid")
    since = payload.get("since")
    if since is not None and (
        not isinstance(since, str)
        or not since
        or len(since) > 80
        or any(ord(char) < 32 for char in since)
    ):
        raise TargetError("journal since value is invalid")
    selected = (requested,) if isinstance(requested, str) else selectable_units
    entries: dict[str, str] = {}
    for unit in selected:
        command = [
            "/usr/bin/journalctl",
            "--no-pager",
            "--output=short-iso",
            f"--lines={lines}",
            f"--unit={unit}",
        ]
        if since is not None:
            command.append(f"--since={since}")
        result = primitives.run(command, timeout=60)
        entries[unit] = (
            result.stdout
            if result.returncode == 0
            else (result.stderr.strip() or "journalctl failed")
        )
    return {"status": "collected", "entries": entries}

def diagnose(payload: Mapping[str, object]) -> dict[str, object]:
    observed = status(payload)
    root_usage = shutil.disk_usage("/")
    journal = logs({**payload, "lines": 100, "since": None})
    return {
        "status": "diagnosed",
        "observed": observed,
        "platform": platform.platform(),
        "boot_id": primitives.read_text(Path("/proc/sys/kernel/random/boot_id")),
        "uptime_seconds": primitives.read_uptime(),
        "disk": {
            "total_bytes": root_usage.total,
            "used_bytes": root_usage.used,
            "free_bytes": root_usage.free,
        },
        "journal": journal["entries"],
        "redaction": "No env content, private key content, database content, or process environment collected.",
    }

def controller_reset(payload: Mapping[str, object]) -> dict[str, object]:
    """Revoke every Controller Grant so a new phone can claim this Host again.

    Recovery for an Owner who lost every managing phone. Bootstrap keeps the
    Host identity, the Owner binding, saved Wi-Fi and all component data; only
    the authority to manage this Host is withdrawn.
    """

    contract.fixed_units(payload)
    if not BOOTSTRAP_CTL.is_file() or not os.access(BOOTSTRAP_CTL, os.X_OK):
        raise TargetError("bootstrap control CLI is unavailable on this Host")
    result = primitives.checked("controller reset", (str(BOOTSTRAP_CTL), "controller-reset"), timeout=120)
    try:
        document = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise TargetError("controller reset did not return one JSON document") from exc
    if not isinstance(document, dict) or "revoked_controllers" not in document:
        raise TargetError("controller reset returned invalid evidence")
    return {"status": "reset", "controller_reset": document}


#: What the Host's line report shows for a window that has no expiry. It
#: renders the field with an f-string, so an absent deadline arrives here as
#: the four characters Python spells ``None`` with, not as nothing at all.
_NO_EXPIRY = frozenset({"", "None", "none", "null"})


def _reported_expiry(reported: str) -> str | None:
    """The deadline the Host published, or nothing when it published none.

    A commissioning window stopped having a clock on it (``eidolon_admin``
    ADR-0007): it closes by being consumed or by the next issuance superseding
    it, and every session minted now carries ``expires_at: null``. Read
    verbatim, that absence went out as ``"expires_at": "None"`` — a
    timestamp-shaped answer to a question with no timestamp, which reads as a
    deadline nobody can parse rather than as the deadline nobody set.

    A real value still passes through untouched. The field is not vestigial
    everywhere: a controller recovery window does expire, and a Host from
    before that ADR put a time here too.
    """

    return None if reported in _NO_EXPIRY else reported


def commissioning_code(payload: Mapping[str, object]) -> dict[str, object]:
    """Mint the one-time Setup code a phone types to claim this Host.

    The authority is the Host's own root-owned control socket, which this
    reaches by having arrived here at all. Before this, issuance was refused
    unless the build was a development one, and nothing else could create a
    commissioning session — so a shipped Host could not be claimed by any
    phone.
    """

    contract.fixed_units(payload)
    setup_code = payload.get("setup_code")
    if setup_code is not None and not isinstance(setup_code, str):
        raise TargetError("commissioning setup_code must be a string")
    if not BOOTSTRAP_CTL.is_file() or not os.access(BOOTSTRAP_CTL, os.X_OK):
        raise TargetError("bootstrap control CLI is unavailable on this Host")
    # Passed straight through rather than checked here. The Host owns the rule
    # about what a usable code is, and a second opinion on this hop could only
    # ever disagree with it.
    # No `--ttl`: the flag is optional on the Host's own CLI and the window it
    # would bound has no clock (ADR-0007). Passing one meant this hop asked for
    # something the Host discards, and then reported a null deadline for it.
    command = [str(BOOTSTRAP_CTL), "commissioning-code"]
    if setup_code is not None:
        command += ["--code", setup_code]
    result = primitives.checked(
        "commissioning code",
        tuple(command),
        timeout=120,
    )
    setup_code = ""
    commissioning_id = ""
    expires_at: str | None = None
    for line in result.stdout.splitlines():
        label, separator, value = line.partition(":")
        if not separator:
            continue
        if label.strip() == "Setup code":
            setup_code = value.strip()
        elif label.strip() == "Commissioning":
            commissioning_id = value.strip()
        elif label.strip() == "Expires":
            expires_at = _reported_expiry(value.strip())
    if not setup_code:
        raise TargetError("commissioning code issuance returned no Setup code")
    return {
        "status": "issued",
        "setup_code": setup_code,
        "expires_at": expires_at,
        "commissioning_id": commissioning_id,
    }
