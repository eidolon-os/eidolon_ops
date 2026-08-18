"""Attest every fact the operator's readiness contract declares.

Each probe reads a surface a component publishes about itself, or a fact about
this machine. None of them reconstructs a component's internal state.
"""

from __future__ import annotations

import json
import re
import ssl
import time
from collections.abc import Mapping
from pathlib import Path
from urllib.parse import urlparse

from . import app_contract, contract, host_application, primitives
from .primitives import TargetError

#: The facts this Host attests, in the order the operator's readiness contract
#: declares them. Sent in every payload rather than compiled in; this copy
#: exists only so the agent can refuse a payload that does not match what it
#: knows how to attest, and a drift test keeps the two identical.
READINESS_FACTS = (
    "backend_healthy",
    "lan_address_observed",
    "lan_name_resolves",
    "host_identity_material",
    "hub_tls_identity",
    "hub_settings_bound",
    "hub_lan_reachable",
    "local_api_reachable",
    "local_api_targets_hub",
    "livekit_client_origin",
    "livekit_lan_reachable",
    "channel_worker_healthy",
    "channel_worker_dispatch_identity",
    "channel_worker_livekit_link",
    "bootstrap_preflight",
    "bootstrap_control_socket",
    "foundation_services",
    "hub_descriptor_published",
    "hub_mdns_service",
    "local_api_mdns_service",
)

_FOUNDATION_READINESS_UNITS = (
    "bluetooth.service",
    "NetworkManager.service",
    "avahi-daemon.service",
)

APP_PREFLIGHT = Path("/opt/eidolon/current/eidolon_admin/.venv/bin/eidolon-bootstrap-preflight")

HOST_IDENTITY = Path("/var/lib/eidolon-bootstrap/host_identity.ed25519")

COMMISSIONING_TLS = Path("/var/lib/eidolon-bootstrap/commissioning_tls.pem")

BOOTSTRAP_SOCKET = Path("/run/eidolon-bootstrap/control.sock")

MDNS_DEFINITION = Path("/etc/avahi/services/eidolon-local-api.service")

HUB_SETTINGS = Path("/etc/eidolon/generated/hub.yaml")

HUB_CERTIFICATE = Path("/etc/eidolon/tls/hub.crt")

HUB_PRIVATE_KEY = Path("/etc/eidolon/tls/hub.key")

OWNER_DESCRIPTOR = Path("/var/lib/eidolon-bootstrap/owner_domain_descriptor.json")

OWNER_ROOT_CERTIFICATE = Path("/var/lib/eidolon-bootstrap/owner_domain_root_ca.pem")

AUTHORITY_SIGNING_CERTIFICATE = Path(
    "/var/lib/eidolon-bootstrap/authority_signing_certificate.pem"
)

LOCAL_API_ENV = Path("/etc/eidolon/local-api.env")

CHANNEL_ENV = Path("/etc/eidolon/channel.env")

_LOCAL_API_PORT = 9002

_HUB_SERVICE_TYPE = "_eidolon-owner._tcp"

_LOCAL_API_SERVICE_TYPE = "_eidolon-local-api._tcp"

_CGROUP_ROOT = Path("/sys/fs/cgroup/system.slice")

_SS_PROCESS_ID = re.compile(r"\bpid=(\d+)\b")

def local_api_json(path: str) -> dict[str, object]:
    return primitives.https_json_endpoint("127.0.0.1", 9002, path, label="Local API")

def fixed_readiness(payload: Mapping[str, object]) -> dict[str, object]:
    """The check set this Host was asked to attest, refused rather than assumed.

    The operator owns the contract. A Host that quietly attested a different
    set would still report ``app_ready``, and that verdict is what a release
    rollback is decided by.
    """

    value = payload.get("readiness")
    if not isinstance(value, dict) or set(value) != {"facts", "channel_worker"}:
        raise TargetError("readiness contract is missing from the operation payload")
    if value["facts"] != list(READINESS_FACTS):
        raise TargetError("readiness contract differs from the reviewed check set")
    worker = value["channel_worker"]
    if (
        not isinstance(worker, dict)
        or set(worker) != {"port", "agent_name", "livekit_port", "settle_seconds"}
        or not primitives.is_port(worker.get("port"))
        or not primitives.is_port(worker.get("livekit_port"))
        or not isinstance(worker.get("agent_name"), str)
        or not worker["agent_name"]
        or type(worker.get("settle_seconds")) is not int
        or not 0 <= worker["settle_seconds"] <= 300
    ):
        raise TargetError("Channel worker readiness contract is invalid")
    return worker

def channel_worker_report(worker: Mapping[str, object]) -> dict[str, object]:
    """Read what the Channel worker publishes about itself.

    Two facts, both the worker's own. ``/`` is its verdict on whether it can
    serve: it answers only while its inference executor is alive and it has not
    given up on LiveKit, and it answers from the same event loop that answers
    LiveKit's job requests — so a wedged worker fails this while its listening
    socket still accepts, which is precisely what a port probe cannot see.
    ``/worker`` names the agent it registered as; a worker serving some other
    name cannot take this product's jobs however healthy it is.

    Nothing here reconstructs the worker's internal state. What this cannot
    know is LiveKit's own view of the registration, and that belongs in the
    Channel component's contract rather than in a guess made here.
    """

    port = int(worker["port"])
    expected = str(worker["agent_name"])
    status, _ = primitives.http_json("127.0.0.1", port, "/")
    _, identity = primitives.http_json("127.0.0.1", port, "/worker")
    return {
        "healthy": status == 200,
        "http_status": status,
        "agent_name": None if identity is None else identity.get("agent_name"),
        "worker_type": None if identity is None else identity.get("worker_type"),
        "dispatch_identity": identity is not None and identity.get("agent_name") == expected,
        "expected_agent_name": expected,
    }

def channel_worker_livekit_link(worker: Mapping[str, object]) -> dict[str, object]:
    """Whether the worker still holds the connection it is registered over.

    A worker registers by holding one connection to LiveKit and answering job
    requests on it. Both ends are on this Host, so that connection existing is
    an observation about this machine — not an inference about a library — and
    its absence is the difference between "the unit is running" and "LiveKit
    can reach it", which is the gap a unit state and a port probe both missed.
    """

    processes = unit_processes("eidolon-channel.service")
    peers = established_peers(int(worker["livekit_port"]))
    linked = sorted(processes & peers)
    return {
        "healthy": bool(linked),
        "linked_processes": linked,
        "unit_processes": sorted(processes),
    }

def unit_processes(unit: str) -> set[int]:
    try:
        content = (_CGROUP_ROOT / unit / "cgroup.procs").read_text(encoding="utf-8")
    except OSError:
        return set()
    return {int(value) for value in content.split() if value.isdecimal()}

def established_peers(port: int) -> set[int]:
    result = primitives.run(
        (
            "/usr/bin/ss",
            "-H",
            "-tn",
            "-p",
            "state",
            "established",
            f"( dport = :{port} )",
        ),
        timeout=20,
    )
    if result.returncode != 0:
        return set()
    return {int(value) for value in _SS_PROCESS_ID.findall(result.stdout)}

def hub_tls_matches(hostname: str) -> bool:
    try:
        decoded = ssl._ssl._test_decode_cert(str(HUB_CERTIFICATE))
        sans = decoded.get("subjectAltName", ())
        starts = ssl.cert_time_to_seconds(decoded["notBefore"])
        expires = ssl.cert_time_to_seconds(decoded["notAfter"])
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(HUB_CERTIFICATE, HUB_PRIVATE_KEY)
    except (KeyError, OSError, ssl.SSLError, ValueError):
        return False
    chain = primitives.run(
        (
            "/usr/bin/openssl",
            "verify",
            "-CAfile",
            str(OWNER_ROOT_CERTIFICATE),
            str(HUB_CERTIFICATE),
        ),
        timeout=15,
    )
    instant = time.time()
    return (
        sans == (("DNS", hostname),)
        and starts <= instant
        and expires > instant
        and chain.returncode == 0
    )

def service_records(service_type: str, instance: str | None = None) -> list[list[str]]:
    """Resolve one mDNS service type, the way a device on the LAN would."""

    browse = primitives.run(("/usr/bin/avahi-browse", "-rtp", service_type), timeout=15)
    if browse.returncode != 0:
        return []
    records = []
    for line in browse.stdout.splitlines():
        fields = line.split(";")
        if len(fields) < 10 or fields[0] != "=" or fields[4] != service_type:
            continue
        if instance is not None and fields[3] != instance:
            continue
        records.append(fields)
    return records

def resolved_addresses(hostname: str) -> tuple[bool, set[str]]:
    resolution = primitives.run(("/usr/bin/avahi-resolve-host-name", "-4", hostname), timeout=10)
    addresses = {
        line.split("\t", 1)[1].strip() for line in resolution.stdout.splitlines() if "\t" in line
    }
    return resolution.returncode == 0, addresses

def bootstrap_preflight() -> dict[str, object]:
    try:
        result = primitives.run((str(APP_PREFLIGHT),), timeout=60)
    except TargetError as exc:
        return {"ok": False, "error": str(exc)}
    if result.returncode not in {0, 1}:
        return {
            "ok": False,
            "error": result.stderr.strip() or "Bootstrap preflight could not run",
        }
    try:
        document = json.loads(result.stdout)
    except json.JSONDecodeError:
        return {"ok": False, "error": "Bootstrap preflight output is invalid"}
    return document if isinstance(document, dict) else {"ok": False, "error": "not an object"}

def local_api_report() -> dict[str, object]:
    try:
        health = local_api_json("/healthz")
        descriptor = local_api_json("/api/local/v1/descriptor")
    except TargetError as exc:
        return {"healthy": False, "error": str(exc)}
    host_id = descriptor.get("host_id")
    fingerprint = descriptor.get("host_public_key_fingerprint")
    valid = (
        health.get("status") == "ok"
        and descriptor.get("contract_version") == "1"
        and isinstance(host_id, str)
        and re.fullmatch(r"ehost-[0-9a-f]{20}", host_id) is not None
        and isinstance(fingerprint, str)
        and fingerprint.startswith("sha256:")
        and isinstance(descriptor.get("ble_service_uuid"), str)
    )
    return {
        "healthy": valid,
        "health_status": health.get("status"),
        "bootstrap_status": health.get("bootstrap"),
        "host_id": host_id,
        "ble_service_uuid": descriptor.get("ble_service_uuid"),
        "tls": "loopback self-check only; App pins the BLE-advertised TLS SPKI",
    }

def app_ready(payload: Mapping[str, object]) -> dict[str, object]:
    """Attest every fact the operator's readiness contract declares.

    Each entry in ``checks`` is one named fact, and the set is compared against
    the contract that arrived with the payload. A probe that stops producing
    its fact fails the report instead of quietly shrinking it.
    """

    contract.fixed_units(payload)
    worker_contract = fixed_readiness(payload)
    app = app_contract.fixed_app(payload)
    hostname = str(app["hub_hostname"])
    owner_domain_id = str(app["owner_domain_id"])
    address = str(app["lan_ipv4"])
    hub_port = int(app["hub_https_port"])
    origin = str(app["hub_origin"])
    descriptor_uri = origin + "/api/device-onboarding/v1/descriptor"
    settle_seconds = int(worker_contract["settle_seconds"])

    units = {unit: primitives.unit_status(unit) for unit in (*contract.PRODUCT_UNITS, host_application.HOST_APPLICATION_UNIT)}
    foundation = {unit: primitives.unit_status(unit) for unit in _FOUNDATION_READINESS_UNITS}
    files = {
        "host_identity": primitives.private_file_check(
            HOST_IDENTITY, 0o600, "eidolon-bootstrap", "eidolon-bootstrap"
        ),
        "commissioning_tls": primitives.private_file_check(
            COMMISSIONING_TLS, 0o640, "eidolon-bootstrap", "eidolon-bootstrap"
        ),
        "hub_settings": primitives.private_file_check(HUB_SETTINGS, 0o640, "root", "eidolon"),
        "hub_certificate": primitives.private_file_check(HUB_CERTIFICATE, 0o640, "root", "eidolon"),
        "hub_private_key": primitives.private_file_check(HUB_PRIVATE_KEY, 0o640, "root", "eidolon"),
        "owner_descriptor": primitives.private_file_check(
            OWNER_DESCRIPTOR, 0o640, "root", "eidolon"
        ),
        "owner_root_certificate": primitives.private_file_check(
            OWNER_ROOT_CERTIFICATE, 0o640, "root", "eidolon"
        ),
        "authority_signing_certificate": primitives.private_file_check(
            AUTHORITY_SIGNING_CERTIFICATE, 0o640, "root", "eidolon"
        ),
    }
    settings = primitives.read_text(HUB_SETTINGS) or ""
    local_api_values = primitives.environment_values_or_empty(LOCAL_API_ENV)
    channel_values = primitives.environment_values_or_empty(CHANNEL_ENV)
    preflight = bootstrap_preflight()
    local_api = local_api_report()
    try:
        hub_health = primitives.https_json_endpoint(address, hub_port, "/health", label="Hub LAN ingress")
        hub_descriptor = primitives.https_json_endpoint(
            address, hub_port, "/api/device-onboarding/v1/descriptor", label="Hub LAN descriptor"
        )
    except TargetError as exc:
        hub_health = {"error": str(exc)}
        hub_descriptor = {}
    resolved_ok, resolved = resolved_addresses(hostname)
    hub_records = service_records(_HUB_SERVICE_TYPE, owner_domain_id)
    local_api_records = service_records(_LOCAL_API_SERVICE_TYPE)
    livekit_origin = urlparse(str(app["livekit_client_url"]))
    livekit_port = livekit_origin.port or (443 if livekit_origin.scheme == "wss" else 80)
    # One window between them, not one each. Two probes with a window apiece
    # could outlast the deadline the operator's side holds, and then a Host
    # with an unhealthy worker reports as a timed-out connection rather than
    # as degraded — which is the one thing the report exists to distinguish.
    budget = primitives.Budget(settle_seconds)
    channel = primitives.settle(
        lambda: channel_worker_report(worker_contract),
        lambda report: bool(report["healthy"]) and bool(report["dispatch_identity"]),
        seconds=budget.remaining(),
    )
    waited_on_worker = budget.spent()
    link = primitives.settle(
        lambda: channel_worker_livekit_link(worker_contract),
        lambda report: bool(report["healthy"]),
        seconds=budget.remaining(),
    )
    waiting = {
        "budget_seconds": settle_seconds,
        "spent_seconds": round(budget.spent(), 1),
        # Named so a slow app-ready points at what was slow. Without this the
        # operator sees only the total and has no way to tell a board that
        # boots slowly from a worker that never comes back.
        "spent_on": {
            "channel_worker": round(waited_on_worker, 1),
            "channel_worker_livekit_link": round(budget.spent() - waited_on_worker, 1),
        },
        "exhausted": budget.exhausted(),
    }
    checks = {
        "backend_healthy": all(
            value.get("ActiveState") == "active" and value.get("SubState") == "running"
            for value in units.values()
        ),
        "lan_address_observed": address in app_contract.host_addresses(),
        "lan_name_resolves": resolved_ok and resolved == {address},
        "host_identity_material": all(
            bool(files[name]["healthy"]) for name in ("host_identity", "commissioning_tls")
        ),
        "hub_tls_identity": (
            bool(files["hub_certificate"]["healthy"])
            and bool(files["hub_private_key"]["healthy"])
            and bool(files["owner_root_certificate"]["healthy"])
            and hub_tls_matches(hostname)
        ),
        "hub_settings_bound": (
            bool(files["hub_settings"]["healthy"])
            and f"owner_domain_id: {owner_domain_id}" in settings
            and f"descriptor_uri: {descriptor_uri}" in settings
            and "owner_domain_id: owner-local" not in settings
        ),
        "hub_lan_reachable": hub_health.get("status") == "ok",
        "local_api_reachable": bool(local_api["healthy"]),
        "local_api_targets_hub": (
            local_api_values.get("EIDOLON_LOCAL_API_OWNER_DOMAIN_ID")
            == owner_domain_id
            and local_api_values.get("EIDOLON_LOCAL_API_OWNER_DOMAIN_DESCRIPTOR_URI")
            == descriptor_uri
            and local_api_values.get("EIDOLON_LOCAL_API_OWNER_DOMAIN_DESCRIPTOR")
            == str(OWNER_DESCRIPTOR)
            and local_api_values.get("EIDOLON_LOCAL_API_OWNER_ROOT_CERTIFICATE")
            == str(OWNER_ROOT_CERTIFICATE)
            and local_api_values.get("EIDOLON_LOCAL_API_AUTHORITY_SIGNING_CERTIFICATE")
            == str(AUTHORITY_SIGNING_CERTIFICATE)
        ),
        "livekit_client_origin": (
            channel_values.get("EIDOLON_LIVEKIT_CLIENT_URL") == app["livekit_client_url"]
            and channel_values.get("EIDOLON_CHANNEL_PROVIDER_ALLOW_INSECURE_LAN_CLIENT_URL")
            == ("1" if app["allow_insecure_livekit"] else "0")
        ),
        "livekit_lan_reachable": primitives.tcp_reachable(address, livekit_port),
        "channel_worker_healthy": bool(channel["healthy"]),
        "channel_worker_dispatch_identity": bool(channel["dispatch_identity"]),
        "channel_worker_livekit_link": bool(link["healthy"]),
        "bootstrap_preflight": preflight.get("ok") is True,
        "bootstrap_control_socket": BOOTSTRAP_SOCKET.is_socket(),
        "foundation_services": all(
            value.get("ActiveState") == "active" and value.get("SubState") == "running"
            for value in foundation.values()
        ),
        "hub_descriptor_published": (
            bool(files["owner_descriptor"]["healthy"])
            and bool(files["authority_signing_certificate"]["healthy"])
            and hub_descriptor.get("owner_domain_id") == owner_domain_id
            and isinstance(hub_descriptor.get("signature"), str)
            and isinstance(hub_descriptor.get("directory_revision"), int)
        ),
        "hub_mdns_service": bool(hub_records)
        and all(
            fields[6] == hostname
            and fields[7] == address
            and fields[8] == str(hub_port)
            and descriptor_uri in ";".join(fields[9:])
            for fields in hub_records
        ),
        "local_api_mdns_service": any(
            fields[7] == address and fields[8] == str(_LOCAL_API_PORT)
            for fields in local_api_records
        ),
    }
    if tuple(checks) != READINESS_FACTS:
        raise TargetError("readiness report does not match the declared check set")
    return {
        "status": "app_ready" if all(checks.values()) else "degraded",
        "host_id": app["host_id"],
        "owner_domain_id": owner_domain_id,
        "lan_ipv4": address,
        "checks": checks,
        "units": units,
        "foundation": foundation,
        "files": files,
        "preflight": preflight,
        "local_api": local_api,
        "hub_health": hub_health,
        "channel_worker": {**channel, "livekit_link": link},
        "waiting": waiting,
        "resolution": sorted(resolved),
        "mdns": {
            "hub": [";".join(fields) for fields in hub_records],
            "local_api": [";".join(fields) for fields in local_api_records],
            "definition": MDNS_DEFINITION.is_file() and not MDNS_DEFINITION.is_symlink(),
        },
        "scope": (
            "Host-side commissioning readiness only. A real phone must still verify BLE, "
            "Host proof, TLS SPKI pinning, Controller claim, Wi-Fi checkpoint and Workspace setup."
        ),
    }
