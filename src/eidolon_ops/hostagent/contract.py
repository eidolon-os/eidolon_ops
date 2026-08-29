"""The fixed Host facts, and the refusal of any payload that differs.

Every table here is a reviewed property of the product topology, and every
validator refuses rather than defaults. What the operator sends is compared
against these; nothing is invented on the Host.
"""

from __future__ import annotations

import os
import re
from collections.abc import Callable, Mapping
from pathlib import Path
from urllib.parse import urlparse

from . import primitives
from .primitives import TargetError

PRODUCT_UNITS = (
    "eidolon-bootstrapd.service",
    "eidolond.service",
    "eidolon-data.service",
    "eidolon-data-workspace.service",
    "eidolon-hub.service",
    "eidolon-kernel.service",
    "eidolon-local-api.service",
    "eidolon-lifecycle-workflow.service",
    "eidolon-admin.service",
    "eidolon-nats.service",
    "eidolon-livekit.service",
    "eidolon-memory-embedder.service",
    "eidolon-memory-supervisor.service",
    "eidolon-memory-discovery.service",
    "eidolon-agent.service",
    "eidolon-channel-provider.service",
    "eidolon-channel.service",
)

#: Units that bring other units up. They have to be stopped in a transaction
#: of their own, before the workers they manage: systemd orders a transaction
#: by unit dependencies rather than by the order of the arguments, so naming
#: the manager first in one long call never reached it. Its own stop job was
#: cancelled, it outlived the sweep, and it reconciled every worker back up
#: while the reset was still running.
RESET_RECONCILER_UNITS = ("eidolond.service",)

# Stop control/reconciliation entry points before their managed workers.  In
# particular, an active legacy eidolond can race a later Kernel stop with a
# start transaction and make systemd cancel the reset job.
RESET_STOP_UNITS = (
    "eidolon-local-api.service",
    "eidolon-lifecycle-workflow.service",
    "eidolon-admin.service",
    "eidolond.service",
    "eidolon-bootstrapd.service",
    "eidolon-channel-provider.service",
    "eidolon-channel.service",
    "eidolon-agent.service",
    "eidolon-memory-discovery.service",
    "eidolon-memory-supervisor.service",
    "eidolon-memory-embedder.service",
    "eidolon-livekit.service",
    "eidolon-nats.service",
    "eidolon-kernel.service",
    "eidolon-hub-ingress.service",
    "eidolon-hub.service",
    "eidolon-data-workspace.service",
    "eidolon-data.service",
)

DIRECT_ENABLE_UNITS = (
    "eidolon-bootstrapd.service",
    "eidolond.service",
    "eidolon-local-api.service",
    "eidolon-lifecycle-workflow.service",
    "eidolon-admin.service",
)

#: How long a release's components may take to answer, when the operator's
#: Host profile does not say. Sized on the board rather than on a laptop: the
#: Channel worker alone spends 45s in TimeoutStopSec on the way down and then
#: loads its ONNX turn-detector on the way up, and the manager reconciles it
#: only after starting itself. At 90s a rollback reported "readiness timeout:
#: agent, channel" for services that were healthy moments later, and a
#: spurious rollback is a far worse failure than a slow one.
DEFAULT_RELEASE_READINESS_SECONDS = 240


def release_readiness_seconds(payload: Mapping[str, object]) -> int:
    """How long this Host says its own services need. A platform property."""

    value = payload.get("readiness_timeout_seconds", DEFAULT_RELEASE_READINESS_SECONDS)
    if not isinstance(value, int) or isinstance(value, bool) or not 30 <= value <= 1800:
        raise TargetError("Host readiness timeout is invalid")
    return value


CURRENT_LINKS = {
    "eidolon_kernel": Path("/opt/eidolon/current/eidolon_kernel"),
    "eidolon_data": Path("/opt/eidolon/current/eidolon_data"),
    "eidolon_hub": Path("/opt/eidolon/current/eidolon_hub"),
    "eidolon_admin": Path("/opt/eidolon/current/eidolon_admin"),
    "eidolon_agent": Path("/opt/eidolon/current/eidolon_agent"),
    "eidolon_channel": Path("/opt/eidolon/current/eidolon_channel"),
    "eidolon_memory": Path("/opt/eidolon/current/eidolon_memory"),
}

SECRET_INPUTS = {
    "data.env": (Path("/etc/eidolon/data.env"), "root", "root", 0o600),
    "hub.env": (Path("/etc/eidolon/hub.env"), "root", "root", 0o600),
    "kernel.env": (Path("/etc/eidolon/kernel.env"), "root", "root", 0o600),
    "admin.env": (Path("/etc/eidolon/admin.env"), "root", "root", 0o600),
    "local-api.env": (Path("/etc/eidolon/local-api.env"), "root", "root", 0o600),
    "bootstrap.env": (Path("/etc/eidolon/bootstrap.env"), "root", "root", 0o600),
    "host_identity.ed25519": (
        Path("/var/lib/eidolon-bootstrap/host_identity.ed25519"),
        "eidolon-bootstrap",
        "eidolon-bootstrap",
        0o600,
    ),
    "agent.env": (Path("/etc/eidolon/agent.env"), "root", "root", 0o600),
    "channel.env": (Path("/etc/eidolon/channel.env"), "root", "root", 0o600),
    "memory.env": (Path("/etc/eidolon/memory.env"), "root", "root", 0o600),
    "livekit.env": (Path("/etc/eidolon/livekit.env"), "root", "root", 0o600),
    "agent.yaml": (Path("/etc/eidolon/agent.yaml"), "root", "eidolon", 0o640),
    "channel.yaml": (Path("/etc/eidolon/channel.yaml"), "root", "eidolon", 0o640),
    "memory.yaml": (Path("/etc/eidolon/memory.yaml"), "root", "eidolon", 0o640),
}

HOST_APPLICATION_INPUTS = {
    "hub.generated.yaml": (
        Path("/etc/eidolon/generated/hub.yaml"),
        "root",
        "eidolon",
        0o640,
    ),
    "hub.crt": (Path("/etc/eidolon/tls/hub.crt"), "root", "eidolon", 0o640),
    "hub.key": (Path("/etc/eidolon/tls/hub.key"), "root", "eidolon", 0o640),
    "owner-domain-descriptor.json": (
        Path("/etc/eidolon/owner-domain/owner_domain_descriptor.json"),
        "root",
        "eidolon-owner-trust-readers",
        0o640,
    ),
    "owner-domain-root-ca.pem": (
        Path("/etc/eidolon/owner-domain/owner_domain_root_ca.pem"),
        "root",
        "eidolon-owner-trust-readers",
        0o640,
    ),
    "authority-signing-certificate.pem": (
        Path("/etc/eidolon/owner-domain/authority_signing_certificate.pem"),
        "root",
        "eidolon-owner-trust-readers",
        0o640,
    ),
    "authority-bootstrap.json": (
        Path("/var/lib/eidolon/hub/authority-bootstrap.json"),
        "eidolon",
        "eidolon",
        0o600,
    ),
    "hub-ingress.py": (
        Path("/usr/local/libexec/eidolon-hub-lan-ingress"),
        "root",
        "root",
        0o755,
    ),
    "hub-ingress.service": (
        Path("/etc/systemd/system/eidolon-hub-ingress.service"),
        "root",
        "root",
        0o644,
    ),
    "hub-service-override.conf": (
        Path("/etc/systemd/system/eidolon-hub.service.d/20-eidolon-ops-host.conf"),
        "root",
        "root",
        0o644,
    ),
}

OPTIONAL_HOST_APPLICATION_INPUTS = {
    "commissioning-secrets.json": (
        Path("/etc/eidolon/commissioning-secrets.json"),
        "root",
        "eidolon",
        0o640,
    ),
}

BASE_INSTALL_INPUTS = {**SECRET_INPUTS, **HOST_APPLICATION_INPUTS}
INSTALL_INPUTS = {**BASE_INSTALL_INPUTS, **OPTIONAL_HOST_APPLICATION_INPUTS}

#: The Host layer Ops derives rather than keeps: settings rendered from the
#: Host identity, the ingress program, and the two units that run it. Unlike a
#: credential these are a function of the operator's own source, so a Host that
#: is never allowed to receive a newer one can only be corrected by reinstalling
#: it — which is how a fix for the ingress unit sat undelivered while updates
#: kept succeeding.
#:
#: The Host TLS pair is stable controller-held material, not Owner signing
#: authority. Refreshing it is required to migrate a legacy self-signed Host
#: and to bind a replacement Host identity; byte equality makes ordinary
#: endpoint changes a no-op. Owner signing private keys are never staged.
REFRESHABLE_HOST_APPLICATION_INPUTS = (
    "hub.generated.yaml",
    "hub.crt",
    "hub.key",
    "owner-domain-descriptor.json",
    "owner-domain-root-ca.pem",
    "authority-signing-certificate.pem",
    "authority-bootstrap.json",
    "hub-ingress.py",
    "hub-ingress.service",
    "hub-service-override.conf",
)

#: Install inputs whose non-secret fields are derived from the Host binding.
#: The controller re-renders the whole authoritative environment file so the
#: target never edits or infers credentials while updating those fields.
REFRESHABLE_HOST_BOUND_INPUTS = ("local-api.env", "channel.env")
REFRESHABLE_PRODUCT_SETTINGS = ("agent.yaml", "channel.yaml", "memory.yaml")
REFRESHABLE_HOST_LAYER_INPUTS = (
    *REFRESHABLE_HOST_APPLICATION_INPUTS,
    *REFRESHABLE_HOST_BOUND_INPUTS,
    *REFRESHABLE_PRODUCT_SETTINGS,
)

CORE_COMPONENTS = (
    "eidolon_kernel",
    "eidolon_data",
    "eidolon_hub",
    "eidolon_admin",
)

FIXED_DATA = {
    "system_database": Path("/var/lib/eidolon/eidolon-system.sqlite3"),
    "object_store": Path("/var/lib/eidolon/objects"),
    "bootstrap_database": Path("/var/lib/eidolon-bootstrap/bootstrap.sqlite3"),
    "deployment_evidence": Path("/var/lib/eidolon/deployments"),
}

#: Every authority a backup copies, and the identity that owns it back.
#:
#: A built-in table because no component declares its own operational facts
#: yet; when they do, this is the first thing that should come from them
#: rather than from here. Each of these is SQLite, which can be snapshotted
#: consistently while the service that owns it keeps running.
BACKED_UP_AUTHORITIES = {
    "system": (Path("/var/lib/eidolon/eidolon-system.sqlite3"), "eidolon", "eidolon"),
    "eidolond": (Path("/var/lib/eidolon/eidolond.sqlite3"), "eidolon", "eidolon"),
    "kernel": (Path("/var/lib/eidolon/eidolon-kernel.sqlite3"), "eidolon", "eidolon"),
    "hub": (Path("/var/lib/eidolon/hub/eidolon-hub.sqlite3"), "eidolon", "eidolon"),
    "agent": (Path("/var/lib/eidolon/agent/eidolon-agent.sqlite3"), "eidolon", "eidolon"),
    "channel": (Path("/var/lib/eidolon/channel/provider.sqlite3"), "eidolon", "eidolon"),
    "bootstrap": (
        Path("/var/lib/eidolon-bootstrap/bootstrap.sqlite3"),
        "eidolon-bootstrap",
        "eidolon-bootstrap",
    ),
    "lifecycle": (
        Path("/var/lib/eidolon-lifecycle/lifecycle-workflows.sqlite3"),
        "eidolon-lifecycle",
        "eidolon-lifecycle",
    ),
}

#: Where memory keeps its spaces. Not in ``UNCOVERED_STATE`` any more: memory
#: declares a snapshot of its own now, and the backup asks it for one rather
#: than copying a palace this agent does not understand. The path is still named
#: here because a backup that could not reach the supervisor says which state it
#: went without.
MEMORY_STATE_ROOT = Path("/var/lib/eidolon/memory")

#: State a backup does not carry, named rather than quietly omitted. Each of
#: these needs its owning component to say how it is copied and how that copy
#: is checked; guessing at a JetStream directory or a media store would produce
#: a backup that restores into something subtly wrong, which is worse than one
#: that says what it left out. Memory was the first entry and is now the proof
#: that the fix is a declaration by the owning component, not an exception here.
UNCOVERED_STATE = {
    "nats": (
        Path("/var/lib/eidolon/nats/jetstream"),
        "JetStream stores are not a file copy while the server is running",
    ),
    "objects": (
        Path("/var/lib/eidolon/objects"),
        "normalized media is large and has no declared snapshot",
    ),
    "voiceprints": (
        Path("/var/lib/eidolon/voiceprints"),
        "voiceprint material has no declared snapshot",
    ),
}

#: Paths a release used to install and no longer does. They are named rather
#: than forgotten: a Host installed before the change still carries them, so
#: something has to be responsible for taking them away.
#:
#: /etc/eidolon/hub.yaml was a release copy of Hub's settings that nothing read
#: — a Hub is started with the per-Host rendering at
#: /etc/eidolon/generated/hub.yaml — and it carried the template's placeholder
#: hub_id, so an operator who opened it on a healthy Host read it as proof the
#: Host was never configured.
#:
#: /etc/eidolon/system-services.systemd.example.yaml is the same file as
#: /etc/eidolon/system-services.yaml under the name it had while there was a
#: systemd-only copy of it. Two files claiming to be the service manifest is
#: worse than one: the stale one is the one an operator has no way to rule out.
LEGACY_SYSTEM_ASSETS = (
    Path("/etc/eidolon/hub.yaml"),
    Path("/etc/eidolon/system-services.systemd.example.yaml"),
)

MANAGED_SYSTEM_ASSETS = (
    *(Path("/etc/systemd/system") / unit for unit in PRODUCT_UNITS),
    Path("/etc/eidolon/eidolond.yaml"),
    Path("/etc/eidolon/kernel.yaml"),
    Path("/etc/eidolon/system-services.yaml"),
    Path("/etc/polkit-1/rules.d/60-eidolon-system-manager.rules"),
    Path("/etc/polkit-1/rules.d/60-eidolon-bootstrap-network.rules"),
    Path("/etc/avahi/services/eidolon-local-api.service"),
    Path("/usr/local/libexec/eidolon-livekit-launch"),
    *(destination for destination, _user, _group, _mode in HOST_APPLICATION_INPUTS.values()),
    *LEGACY_SYSTEM_ASSETS,
)

RESET_DEPLOYMENT_ROOTS = (
    Path("/opt/eidolon"),
    Path("/etc/eidolon"),
    Path("/run/eidolon"),
    Path("/run/eidolon-bootstrap"),
    Path("/run/eidolon-lifecycle"),
    Path("/var/log/eidolon"),
)

RESET_AUTHORITY_ROOTS = (
    Path("/var/lib/eidolon"),
    Path("/var/lib/eidolon-bootstrap"),
    Path("/var/lib/eidolon-lifecycle"),
    Path("/var/lib/eidolon-admin"),
)

RELEASE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")

STAGING_NAME = re.compile(
    r"^eidolon-(?:release|artifacts|secrets|backup|authority-(?:backup|restore))-[A-Za-z0-9][A-Za-z0-9._-]{0,63}$"
)

SHA256 = re.compile(r"^[0-9a-f]{64}$")

VAR_TMP = Path("/var/tmp")

RELEASES = Path("/opt/eidolon/releases")

RELEASE_ARTIFACT_STORE_ROOT = Path("/var/cache/eidolon/release-artifacts-v1/sha256")

CURRENT_ROOT = Path("/opt/eidolon/current")

# Shared with the sealed release activator. Reclamation and activation may not
# inspect or mutate the release graph concurrently.
RELEASE_LOCK = Path("/run/lock/eidolon-release.lock")

# Shared with prepare_target.py from the sealed deployment package. A prepared
# tree becomes visible under RELEASES before its environments are finished.
RELEASE_PREPARE_LOCK = Path("/run/lock/eidolon-release-prepare.lock")

# A reboot-safe recovery is unnecessary here: /run disappearing also removes
# every in-flight process. The prepared release remains discoverable on disk.
RECLAMATION_STATE = Path("/run/lock/eidolon-release-candidate.json")

#: Component-neutral operator entries published inside every sealed release.
RELEASE_ACTIVATOR = ".release/bin/eidolon-release"

RELEASE_INTERPRETER = ".release/bin/python"

CURRENT_KERNEL = Path("/opt/eidolon/current/eidolon_kernel")

HOST_ENV_PATH = Path("/etc/eidolon/host.env")

HOST_PORTS_PATH = Path("/etc/eidolon/generated/ports.yaml")

#: Where the Host keeps sentence encoders. Under the state root rather than
#: inside a release: a palace is built with one encoder and cannot be read with
#: another, so the weights have to outlive the release that carried them.
HOST_EMBEDDING_MODEL_ROOT = Path("/var/lib/eidolon/models")

#: What a carried encoder directory calls the record of its own contents.
#:
#: Spelled here as well as on the operator side (`embedding_model.py`) because
#: this package is shipped to the Host alone and may not import upward. A test
#: asserts the two spellings are the same string, which is the only thing that
#: keeps a rename from making the Host quietly refuse every carried model.
EMBEDDING_DIGEST_RECORD = ".files-sha256"

HOST_ENV_VALUE = (
    "EIDOLON_INSTALL_ROOT=/opt/eidolon\n"
    "EIDOLON_WORKSPACE_ROOT=/opt/eidolon/current\n"
    "EIDOLON_ROOT=/opt/eidolon/current\n"
    "EIDOLON_CONFIG_ROOT=/etc/eidolon\n"
    "EIDOLON_STATE_ROOT=/var/lib/eidolon\n"
    "EIDOLON_RUNTIME_ROOT=/run/eidolon\n"
    "EIDOLON_LOG_ROOT=/var/log/eidolon\n"
    "EIDOLON_CACHE_ROOT=/var/cache/eidolon\n"
    "EIDOLON_BOOTSTRAP_STATE_ROOT=/var/lib/eidolon-bootstrap\n"
    "EIDOLON_BOOTSTRAP_RUNTIME_ROOT=/run/eidolon-bootstrap\n"
    "EIDOLON_BOOTSTRAP_STATE_DIR=/var/lib/eidolon-bootstrap\n"
    "EIDOLON_BOOTSTRAP_RUNTIME_DIR=/run/eidolon-bootstrap\n"
    # Host configuration, not a secret. This must equal the 512-dimensional
    # identity declared by Memory's HTTP embedding contract.
    "EIDOLON_MEMORY_EMBEDDING_MODEL=bge-small-zh\n"
    # And where its weights are. Without this the encoder is fetched from the
    # model hub at first use, which a Host may have no route to — and the
    # failure is not an error but a slow, empty answer: seventy seconds of
    # retries, then a search that found nothing because it never ran.
    f"EIDOLON_MEMORY_EMBEDDING_MODEL_DIR={HOST_EMBEDDING_MODEL_ROOT}/bge-small-zh\n"
    # Admin resolves the port registry relative to an operator's checkout when
    # nobody names one, which is a Mac-workstation shape. Name the Host copy.
    f"EIDOLON_PORTS_FILE={HOST_PORTS_PATH}\n"
)

HOST_DIRECTORIES = (
    (Path("/opt/eidolon"), 0o755, "root", "root"),
    (Path("/opt/eidolon/releases"), 0o755, "root", "root"),
    (Path("/opt/eidolon/current"), 0o755, "root", "root"),
    (Path("/var/lib/eidolon"), 0o750, "eidolon", "eidolon"),
    (Path("/var/lib/eidolon/hub"), 0o750, "eidolon", "eidolon"),
    (Path("/var/lib/eidolon/agent"), 0o750, "eidolon", "eidolon"),
    (Path("/var/lib/eidolon/memory"), 0o750, "eidolon", "eidolon"),
    # Encoders are read by services and written only by an install, so unlike
    # the state beside them this is root-owned and world-readable.
    (HOST_EMBEDDING_MODEL_ROOT, 0o755, "root", "root"),
    (Path("/var/lib/eidolon/nats/jetstream"), 0o750, "eidolon", "eidolon"),
    (Path("/var/lib/eidolon/voiceprints"), 0o750, "eidolon", "eidolon"),
    (Path("/var/lib/eidolon/objects"), 0o750, "eidolon", "eidolon"),
    (Path("/var/lib/eidolon/admin"), 0o750, "eidolon", "eidolon"),
    (
        Path("/var/lib/eidolon-lifecycle"),
        0o700,
        "eidolon-lifecycle",
        "eidolon-lifecycle",
    ),
    (
        Path("/var/lib/eidolon-bootstrap"),
        0o710,
        "eidolon-bootstrap",
        "eidolon-bootstrap",
    ),
    (Path("/var/cache/eidolon"), 0o750, "eidolon", "eidolon"),
    (Path("/var/log/eidolon"), 0o750, "eidolon", "eidolon"),
    # Public Owner trust artifacts live below this otherwise non-enumerable
    # configuration root. Execute-only for other services permits traversal of
    # those reviewed paths without granting directory listing or access to any
    # private file; each file's own mode remains the authority.
    (Path("/etc/eidolon"), 0o751, "root", "eidolon"),
    (
        Path("/etc/eidolon/owner-domain"),
        0o750,
        "root",
        "eidolon-owner-trust-readers",
    ),
)

PHASES = (
    "validated",
    "identities",
    "prerequisites",
    "data_baseline",
    "assets",
    "started",
    "completed",
)


def ensure_host_path_contract(
    root: Path,
    chown: Callable[[Path, str, str], None],
    port_registry: str,
) -> None:
    """Materialize the host-profile roots without adopting mutable contents."""

    for value, mode, user, group in HOST_DIRECTORIES:
        path = primitives.host_path(root, value)
        if path.exists() and (path.is_symlink() or not path.is_dir()):
            raise TargetError(f"host path is not a safe directory: {value}")
        path.mkdir(parents=True, exist_ok=True)
        os.chmod(path, mode)
        chown(path, user, group)
    # Sent by the operator rather than restated here: the registry has one
    # author, and a second copy of it inside this file could only ever drift
    # from that one. Derived rather than supplied, so rewriting it is how it
    # stays true; only the credentials are write-once.
    ports = primitives.host_path(root, HOST_PORTS_PATH)
    if ports.is_symlink():
        raise TargetError("existing /etc/eidolon/generated/ports.yaml is not a regular file")
    primitives.atomic_text(ports, port_registry, mode=0o640)
    chown(ports, "root", "eidolon")
    # Rewritten when it differs, for the same reason the port registry above
    # is: this file is derived from the contract in this module, not supplied
    # by an operator and not a credential. Refusing to update it meant a Host
    # installed before a path existed could never learn it — which is how a
    # Host ended up running with an encoder it had been given but could not
    # find, because the line naming its directory was added here and had
    # nowhere to land.
    host_env = primitives.host_path(root, HOST_ENV_PATH)
    if host_env.is_symlink() or (host_env.exists() and not host_env.is_file()):
        raise TargetError("existing /etc/eidolon/host.env is not a regular file")
    primitives.atomic_text(host_env, HOST_ENV_VALUE, mode=0o644)
    chown(host_env, "root", "root")


def fixed_units(payload: Mapping[str, object]) -> tuple[str, ...]:
    value = payload.get("units")
    if value != list(PRODUCT_UNITS):
        raise TargetError("unit set differs from the reviewed product topology")
    return PRODUCT_UNITS


def fixed_data(payload: Mapping[str, object]) -> dict[str, Path]:
    value = payload.get("data")
    if not isinstance(value, dict) or set(value) != set(FIXED_DATA):
        raise TargetError("data path set is invalid")
    result = {name: Path(item) for name, item in value.items() if isinstance(item, str)}
    if result != FIXED_DATA:
        raise TargetError("data paths differ from reviewed system assets")
    return result


def fixed_port_registry(payload: Mapping[str, object]) -> str:
    """The port registry the operator sent, refused rather than invented.

    Which port each component binds is Host topology, and it has one author on
    the operator side. Restating it here would give it a second, and the copy
    a Host wrote itself is precisely the one nobody would think to update.
    """

    value = payload.get("port_registry")
    if not isinstance(value, str) or not value.strip():
        raise TargetError("Host port registry is missing from the operation payload")
    return value


def fixed_memory_admin_url(payload: Mapping[str, object]) -> str:
    """Where memory's supervisor answers, as the operator's registry has it.

    Sent rather than derived here for the same reason the port registry is: the
    assignment has one author on the operator side, and a copy this agent
    computed would be the one nobody thinks to update when a port moves.
    """

    value = payload.get("memory_admin_url")
    if not isinstance(value, str) or not value.strip():
        raise TargetError("memory admin URL is missing from the operation payload")
    parsed = urlparse(value.strip())
    if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost"}:
        raise TargetError(f"memory admin URL must be loopback http: {value}")
    return value.strip().rstrip("/")


#: What a Host records about each source repository a release was built from.
#: Five facts, fixed, because this is the only durable answer to "which commits
#: was that release" — the release directory that carried them is deleted at
#: commit, and the deployment evidence root is explicitly outside reclamation.
SOURCE_PROVENANCE_KEYS = ("revision", "head", "branch", "pinned", "dirty")
_PROVENANCE_COMMIT = re.compile(r"^[0-9a-f]{40}$")


def optional_source_provenance(
    payload: Mapping[str, object],
) -> dict[str, dict[str, object]] | None:
    """Validate the release's source provenance, or accept its absence.

    Optional so a Host stays operable from a workstation that predates this
    record. Validated so what does get stored is worth reading later: evidence
    whose shape nobody can trust is not evidence, and this is the document an
    audit or a rollback decision is made from.
    """

    value = payload.get("sources")
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise TargetError("release source provenance must be a table")
    records: dict[str, dict[str, object]] = {}
    for source_id, record in value.items():
        label = f"release source provenance for {source_id}"
        if not isinstance(source_id, str) or not isinstance(record, Mapping):
            raise TargetError(f"{label} must be an object")
        if set(record) != set(SOURCE_PROVENANCE_KEYS):
            raise TargetError(f"{label} must state exactly {', '.join(SOURCE_PROVENANCE_KEYS)}")
        for name in ("revision", "head"):
            if _PROVENANCE_COMMIT.fullmatch(str(record.get(name))) is None:
                raise TargetError(f"{label} has an invalid {name}")
        branch = record.get("branch")
        if branch is not None and (not isinstance(branch, str) or not branch):
            raise TargetError(f"{label} has an invalid branch")
        for name in ("pinned", "dirty"):
            if not isinstance(record.get(name), bool):
                raise TargetError(f"{label} has a non-boolean {name}")
        records[source_id] = {name: record[name] for name in SOURCE_PROVENANCE_KEYS}
    return records


def fixed_release_id(payload: Mapping[str, object], *, required: bool = True) -> str | None:
    value = payload.get("release_id")
    if value is None and not required:
        return None
    if not isinstance(value, str) or RELEASE_ID.fullmatch(value) is None:
        raise TargetError("release id is invalid")
    return value
