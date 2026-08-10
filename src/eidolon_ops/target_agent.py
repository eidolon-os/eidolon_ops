#!/usr/bin/env python3
"""Standalone target-side operations sent over SSH stdin.

Keep this module standard-library-only until an action deliberately imports the
prepared release's ``eidolon_deploy`` package.
"""

from __future__ import annotations

import base64
import fcntl
import grp
import hashlib
import http.client
import json
import os
import platform
import pwd
import re
import shutil
import ssl
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
import uuid
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from ipaddress import IPv4Address, ip_address
from pathlib import Path, PurePosixPath
from urllib.parse import urlparse

PRODUCT_UNITS = (
    "eidolon-bootstrapd.service",
    "eidolond.service",
    "eidolon-data.service",
    "eidolon-data-workspace.service",
    "eidolon-hub.service",
    "eidolon-kernel.service",
    "eidolon-local-api.service",
    "eidolon-admin.service",
    "eidolon-nats.service",
    "eidolon-livekit.service",
    "eidolon-memory-supervisor.service",
    "eidolon-memory-discovery.service",
    "eidolon-agent.service",
    "eidolon-channel-provider.service",
    "eidolon-channel.service",
)
# Stop control/reconciliation entry points before their managed workers.  In
# particular, an active legacy eidolond can race a later Kernel stop with a
# start transaction and make systemd cancel the reset job.
RESET_STOP_UNITS = (
    "eidolon-admin.service",
    "eidolon-local-api.service",
    "eidolond.service",
    "eidolon-bootstrapd.service",
    "eidolon-channel-provider.service",
    "eidolon-channel.service",
    "eidolon-agent.service",
    "eidolon-memory-discovery.service",
    "eidolon-memory-supervisor.service",
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
    "eidolon-admin.service",
)
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
INSTALL_INPUTS = {**SECRET_INPUTS, **HOST_APPLICATION_INPUTS}
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
MANAGED_SYSTEM_ASSETS = (
    *(Path("/etc/systemd/system") / unit for unit in PRODUCT_UNITS),
    Path("/etc/eidolon/eidolond.yaml"),
    Path("/etc/eidolon/kernel.yaml"),
    Path("/etc/eidolon/hub.yaml"),
    Path("/etc/eidolon/system-services.systemd.example.yaml"),
    Path("/etc/polkit-1/rules.d/60-eidolon-system-manager.rules"),
    Path("/etc/polkit-1/rules.d/60-eidolon-bootstrap-network.rules"),
    Path("/etc/avahi/services/eidolon-local-api.service"),
    Path("/usr/local/libexec/eidolon-livekit-launch"),
    *(destination for destination, _user, _group, _mode in HOST_APPLICATION_INPUTS.values()),
)
RESET_DEPLOYMENT_ROOTS = (
    Path("/opt/eidolon"),
    Path("/etc/eidolon"),
    Path("/run/eidolon"),
    Path("/run/eidolon-bootstrap"),
    Path("/var/log/eidolon"),
)
RESET_AUTHORITY_ROOTS = (
    Path("/var/lib/eidolon"),
    Path("/var/lib/eidolon-bootstrap"),
    Path("/var/lib/eidolon-admin"),
)
_RELEASE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_STAGING_NAME = re.compile(r"^eidolon-(?:release|secrets)-[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_VAR_TMP = Path("/var/tmp")
_RELEASES = Path("/opt/eidolon/releases")
#: Component-neutral operator entries published inside every sealed release.
RELEASE_ACTIVATOR = ".release/bin/eidolon-release"
RELEASE_INTERPRETER = ".release/bin/python"
_CURRENT_KERNEL = Path("/opt/eidolon/current/eidolon_kernel")
HOST_ENV_PATH = Path("/etc/eidolon/host.env")
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
)
HOST_DIRECTORIES = (
    (Path("/opt/eidolon"), 0o755, "root", "root"),
    (Path("/opt/eidolon/releases"), 0o755, "root", "root"),
    (Path("/opt/eidolon/current"), 0o755, "root", "root"),
    (Path("/var/lib/eidolon"), 0o750, "eidolon", "eidolon"),
    (Path("/var/lib/eidolon/agent"), 0o750, "eidolon", "eidolon"),
    (Path("/var/lib/eidolon/memory"), 0o750, "eidolon", "eidolon"),
    (Path("/var/lib/eidolon/nats/jetstream"), 0o750, "eidolon", "eidolon"),
    (Path("/var/lib/eidolon/voiceprints"), 0o750, "eidolon", "eidolon"),
    (Path("/var/lib/eidolon/objects"), 0o750, "eidolon", "eidolon"),
    (Path("/var/lib/eidolon/admin"), 0o750, "eidolon", "eidolon"),
    (
        Path("/var/lib/eidolon-bootstrap"),
        0o710,
        "eidolon-bootstrap",
        "eidolon-bootstrap",
    ),
    (Path("/var/cache/eidolon"), 0o750, "eidolon", "eidolon"),
    (Path("/var/log/eidolon"), 0o750, "eidolon", "eidolon"),
    (Path("/etc/eidolon"), 0o750, "root", "eidolon"),
)
_PHASES = (
    "validated",
    "identities",
    "prerequisites",
    "data_baseline",
    "assets",
    "started",
    "completed",
)
_FOUNDATION_PROFILE = "raspberry-pi-os-debian-arm64-v2"
_FOUNDATION_OS_IDS = ("debian", "raspbian")
_FOUNDATION_OS_VERSIONS = ("13",)
_FOUNDATION_APT_MIRRORS = {
    "debian": "https://mirror.nju.edu.cn/debian/",
    "raspberrypi": "https://archive.raspberrypi.com/debian/",
    "security": "https://mirror.nju.edu.cn/debian-security/",
}
_APT_COMMAND_OPTIONS = (
    "-o",
    "Acquire::ForceIPv4=true",
    "-o",
    "Acquire::Retries=3",
)
_FOUNDATION_PACKAGES = (
    "alsa-utils",
    "avahi-daemon",
    "avahi-utils",
    "bluez",
    "build-essential",
    "ca-certificates",
    "cmake",
    "curl",
    "dbus",
    "ffmpeg",
    "git",
    "git-lfs",
    "iproute2",
    "libatomic1",
    "libffi-dev",
    "libgl1",
    "libglib2.0-0t64",
    "libgomp1",
    "libnss-mdns",
    "libsndfile1",
    "libsqlite3-dev",
    "libssl-dev",
    "lsof",
    "network-manager",
    "ninja-build",
    "pkg-config",
    "pkexec",
    "polkitd",
    "python3",
    "python3-pip",
    "python3-venv",
    "rsync",
    "sqlite3",
    "tar",
    "xz-utils",
)
_FOUNDATION_SERVICES = (
    "bluetooth.service",
    "NetworkManager.service",
    "avahi-daemon.service",
)
_FOUNDATION_ARTIFACTS = (
    {
        "artifact_id": "nats-server",
        "version": "2.14.0",
        "url": "https://api.github.com/repos/nats-io/nats-server/releases/assets/409045586",
        "sha256": "ce7dc5f7d97b70dabc38b13157fed28d7d06227860676143c15c62c5c297996c",
        "kind": "tar-binary",
        "executable": "nats-server",
    },
    {
        "artifact_id": "livekit-server",
        "version": "1.11.0",
        "url": "https://api.github.com/repos/livekit/livekit/releases/assets/398737306",
        "sha256": "6741466bc12e75544338292ab2c1c02c02f3c626568230b5548fffc53e5a87ff",
        "kind": "tar-binary",
        "executable": "livekit-server",
    },
    {
        "artifact_id": "uv",
        "version": "0.11.15",
        "url": (
            "https://files.pythonhosted.org/packages/af/50/"
            "4bc8a148274feabee2d9c9f1fa15009e10c0228dfe57981ee3ea2ef1d481/"
            "uv-0.11.15-py3-none-manylinux_2_17_aarch64."
            "manylinux2014_aarch64.musllinux_1_1_aarch64.whl"
        ),
        "sha256": "c0cf52cd6d50bb9e05e2d968f45f80761107e4cbc8d4a26d9758f9d8274aaec1",
        "kind": "pip-wheel",
        "executable": "uv",
    },
    {
        "artifact_id": "node",
        "version": "22.23.2",
        "url": "https://nodejs.org/dist/v22.23.2/node-v22.23.2-linux-arm64.tar.xz",
        "sha256": "fff4078c5def658577f92c88db7db3bc0072924bfb93fe52c1e744a54e94abb8",
        "kind": "node-tar",
        "executable": "node",
    },
)
_FOUNDATION_VERSION_PREFIXES = {
    "nats-server": ("nats-server: v2.14.0", "v2.14.0"),
    "livekit-server": ("livekit-server version 1.11.0", "1.11.0"),
    "uv": ("uv 0.11.15",),
    "node": ("v22.23.2",),
}
_FOUNDATION_EVIDENCE = Path("/var/lib/eidolon-ops/foundation-v2.json")
_FOUNDATION_CACHE = Path("/var/cache/eidolon/ops/artifacts")
_FOUNDATION_LIBRARY = Path("/usr/local/lib/eidolon-foundation")
_FOUNDATION_LOCK = Path("/run/lock/eidolon-foundation.lock")
_LOCAL_BIN = Path("/usr/local/bin")
_LOCAL_LIB = Path("/usr/local/lib")
_APP_PREFLIGHT = Path("/opt/eidolon/current/eidolon_admin/.venv/bin/eidolon-bootstrap-preflight")
_HOST_IDENTITY = Path("/var/lib/eidolon-bootstrap/host_identity.ed25519")
_COMMISSIONING_TLS = Path("/var/lib/eidolon-bootstrap/commissioning_tls.pem")
_BOOTSTRAP_SOCKET = Path("/run/eidolon-bootstrap/control.sock")
_MDNS_DEFINITION = Path("/etc/avahi/services/eidolon-local-api.service")


class TargetError(RuntimeError):
    """A target invariant or fixed operation failed closed."""


def _run(
    command: Sequence[str],
    *,
    timeout: float = 120,
    cwd: Path | None = None,
    env: Mapping[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            tuple(command),
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=cwd,
            env=env,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise TargetError(f"command could not run: {command[0]}: {exc}") from exc


def _checked(
    operation: str,
    command: Sequence[str],
    *,
    timeout: float = 120,
    cwd: Path | None = None,
    env: Mapping[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    result = _run(command, timeout=timeout, cwd=cwd, env=env)
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or "no diagnostic output"
        raise TargetError(f"{operation} failed: {detail}")
    return result


def _host_path(root: Path, path: Path) -> Path:
    if not path.is_absolute():
        raise TargetError(f"target path must be absolute: {path}")
    return path if root == Path("/") else root / path.relative_to("/")


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _atomic_json(path: Path, document: object, *, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    payload = (json.dumps(document, indent=2, sort_keys=True) + "\n").encode("utf-8")
    try:
        with temporary.open("xb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _atomic_text(path: Path, value: str, *, mode: int = 0o644) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    payload = value.encode("utf-8")
    try:
        with temporary.open("xb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _atomic_symlink(target: Path, link: Path) -> None:
    link.parent.mkdir(parents=True, exist_ok=True)
    temporary = link.with_name(f".{link.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.symlink_to(target)
        os.replace(temporary, link)
    finally:
        temporary.unlink(missing_ok=True)


def _ensure_host_path_contract(
    root: Path,
    chown: Callable[[Path, str, str], None],
) -> None:
    """Materialize the host-profile roots without adopting mutable contents."""

    for value, mode, user, group in HOST_DIRECTORIES:
        path = _host_path(root, value)
        if path.exists() and (path.is_symlink() or not path.is_dir()):
            raise TargetError(f"host path is not a safe directory: {value}")
        path.mkdir(parents=True, exist_ok=True)
        os.chmod(path, mode)
        chown(path, user, group)
    host_env = _host_path(root, HOST_ENV_PATH)
    if host_env.exists() or host_env.is_symlink():
        if (
            host_env.is_symlink()
            or not host_env.is_file()
            or host_env.read_text(encoding="utf-8") != HOST_ENV_VALUE
        ):
            raise TargetError("existing /etc/eidolon/host.env violates the path contract")
        return
    _atomic_text(host_env, HOST_ENV_VALUE, mode=0o644)
    chown(host_env, "root", "root")


@contextmanager
def _exclusive(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, 0o600)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise TargetError("another first-install transaction is in progress") from exc
        yield
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def _payload(value: str) -> dict[str, object]:
    try:
        document = json.loads(base64.urlsafe_b64decode(value.encode("ascii")))
    except (ValueError, UnicodeError, json.JSONDecodeError) as exc:
        raise TargetError("operator payload is invalid") from exc
    if not isinstance(document, dict):
        raise TargetError("operator payload must be an object")
    return document


def _fixed_units(payload: Mapping[str, object]) -> tuple[str, ...]:
    value = payload.get("units")
    if value != list(PRODUCT_UNITS):
        raise TargetError("unit set differs from the reviewed product topology")
    return PRODUCT_UNITS


def _fixed_data(payload: Mapping[str, object]) -> dict[str, Path]:
    value = payload.get("data")
    if not isinstance(value, dict) or set(value) != set(FIXED_DATA):
        raise TargetError("data path set is invalid")
    result = {name: Path(item) for name, item in value.items() if isinstance(item, str)}
    if result != FIXED_DATA:
        raise TargetError("data paths differ from reviewed system assets")
    return result


def _fixed_app(payload: Mapping[str, object]) -> dict[str, object]:
    value = payload.get("app")
    expected = {
        "host_id",
        "hub_id",
        "hub_hostname",
        "hub_https_port",
        "hub_origin",
        "lan_ipv4",
        "livekit_client_url",
        "allow_insecure_livekit",
    }
    if not isinstance(value, dict) or set(value) != expected:
        raise TargetError("Host application contract is missing or malformed")
    host_id = value.get("host_id")
    if not isinstance(host_id, str) or re.fullmatch(r"ehost-[0-9a-f]{20}", host_id) is None:
        raise TargetError("Host application Host ID is invalid")
    suffix = host_id.removeprefix("ehost-")
    hub_id = f"eidolon-hub-{suffix}"
    hub_hostname = f"{hub_id}.local"
    port = value.get("hub_https_port")
    if (
        value.get("hub_id") != hub_id
        or value.get("hub_hostname") != hub_hostname
        or type(port) is not int
        or not 1 <= port <= 65535
        or value.get("hub_origin") != f"https://{hub_hostname}:{port}"
    ):
        raise TargetError("Host application Hub identity is not Host-bound")
    try:
        address = ip_address(str(value.get("lan_ipv4")))
    except ValueError as exc:
        raise TargetError("Host application LAN address is invalid") from exc
    if not isinstance(address, IPv4Address) or not address.is_private or address.is_loopback:
        raise TargetError("Host application LAN address must be private IPv4")
    livekit = value.get("livekit_client_url")
    try:
        parsed = urlparse(livekit) if isinstance(livekit, str) else None
        if parsed is not None:
            _parsed_port = parsed.port
    except ValueError as exc:
        raise TargetError("Host application LiveKit origin is invalid") from exc
    allow_insecure = value.get("allow_insecure_livekit")
    if (
        parsed is None
        or parsed.scheme not in {"ws", "wss"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
        or not isinstance(allow_insecure, bool)
        or (parsed.scheme == "ws" and not allow_insecure)
        or (parsed.scheme == "ws" and parsed.hostname != str(address))
    ):
        raise TargetError("Host application LiveKit origin is invalid")
    return dict(value)


def _optional_app(payload: Mapping[str, object]) -> dict[str, object] | None:
    if "app" not in payload:
        return None
    return _fixed_app(payload)


def _release_id(payload: Mapping[str, object], *, required: bool = True) -> str | None:
    value = payload.get("release_id")
    if value is None and not required:
        return None
    if not isinstance(value, str) or _RELEASE_ID.fullmatch(value) is None:
        raise TargetError("release id is invalid")
    return value


def _unit_status(unit: str) -> dict[str, object]:
    result = _run(
        (
            "/usr/bin/systemctl",
            "show",
            unit,
            "--property=LoadState,ActiveState,SubState,NRestarts",
        )
    )
    if result.returncode != 0:
        return {"error": result.stderr.strip() or result.stdout.strip() or "systemctl failed"}
    values: dict[str, object] = {}
    for line in result.stdout.splitlines():
        key, separator, value = line.partition("=")
        if separator and key in {"LoadState", "ActiveState", "SubState", "NRestarts"}:
            values[key] = int(value) if key == "NRestarts" and value.isdecimal() else value
    return values


def status(payload: Mapping[str, object]) -> dict[str, object]:
    units = _fixed_units(payload)
    links: dict[str, str | None] = {}
    for component_id, path in CURRENT_LINKS.items():
        links[component_id] = os.readlink(path) if path.is_symlink() else None
    evidence = FIXED_DATA["deployment_evidence"]
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
        "units": {
            **{unit: _unit_status(unit) for unit in units},
            **(
                {"eidolon-hub-ingress.service": _unit_status("eidolon-hub-ingress.service")}
                if _optional_app(payload) is not None
                else {}
            ),
        },
        "current_links": links,
        "recent_receipts": receipts,
        "installations": installations,
    }


def doctor_host(payload: Mapping[str, object]) -> dict[str, object]:
    _fixed_units(payload)
    _fixed_data(payload)
    remote_uv = payload.get("remote_uv")
    if not isinstance(remote_uv, str) or not Path(remote_uv).is_absolute():
        raise TargetError("remote uv path is invalid")
    host_env = HOST_ENV_PATH
    checks = {
        "system": platform.system().lower() == "linux",
        "machine": platform.machine().lower() == "aarch64",
        "root": os.geteuid() == 0,
        "systemctl": Path("/usr/bin/systemctl").is_file(),
        "systemd_analyze": Path("/usr/bin/systemd-analyze").is_file(),
        "python3": Path("/usr/bin/python3").is_file(),
        "uv": Path(remote_uv).is_file() and os.access(remote_uv, os.X_OK),
        "host_path_contract": host_env.is_file()
        and host_env.read_text(encoding="utf-8") == HOST_ENV_VALUE,
    }
    release_id = _release_id(payload, required=False)
    release_doctor: object = None
    if release_id is not None:
        descriptor = _RELEASES / release_id / "release.json"
        cli = _RELEASES / release_id / RELEASE_ACTIVATOR
        result = _run((str(cli), "doctor", str(descriptor)), timeout=180)
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


def _expected_foundation() -> dict[str, object]:
    return {
        "profile": _FOUNDATION_PROFILE,
        "architecture": "aarch64",
        "os_ids": list(_FOUNDATION_OS_IDS),
        "os_versions": list(_FOUNDATION_OS_VERSIONS),
        "apt_mirrors": dict(_FOUNDATION_APT_MIRRORS),
        "apt_packages": list(_FOUNDATION_PACKAGES),
        "services": list(_FOUNDATION_SERVICES),
        "artifacts": [dict(artifact) for artifact in _FOUNDATION_ARTIFACTS],
    }


def _foundation_contract(payload: Mapping[str, object]) -> dict[str, object]:
    value = payload.get("foundation")
    expected = _expected_foundation()
    if value != expected:
        raise TargetError("foundation contract differs from the reviewed pinned profile")
    return expected


def _os_release(root: Path = Path("/")) -> dict[str, str]:
    path = _host_path(root, Path("/etc/os-release"))
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return {}
    result: dict[str, str] = {}
    for line in lines:
        key, separator, value = line.partition("=")
        if separator and re.fullmatch(r"[A-Z_]+", key):
            result[key] = value.strip().strip("\"'")
    return result


def _foundation_platform_checks() -> dict[str, bool]:
    os_release = _os_release()
    machine = platform.machine().lower()
    os_id = os_release.get("ID", "")
    version = os_release.get("VERSION_ID", "").split(".", maxsplit=1)[0]
    init = _read_text(Path("/proc/1/comm"))
    memory_kib = 0
    memory = _read_text(Path("/proc/meminfo")) or ""
    model = (_read_text(Path("/proc/device-tree/model")) or "").rstrip("\x00")
    match = re.search(r"^MemTotal:\s+(\d+)\s+kB$", memory, re.MULTILINE)
    if match is not None:
        memory_kib = int(match.group(1))
    try:
        free_bytes = shutil.disk_usage("/").free
    except OSError:
        free_bytes = 0
    return {
        "linux": platform.system().lower() == "linux",
        "aarch64": machine in {"aarch64", "arm64"},
        "raspberry_pi_hardware": model.startswith("Raspberry Pi"),
        "supported_os": os_id in _FOUNDATION_OS_IDS,
        "supported_os_version": version in _FOUNDATION_OS_VERSIONS,
        "systemd_pid1": init == "systemd",
        "memory_at_least_8_gib": memory_kib >= 7 * 1024 * 1024,
        "disk_free_at_least_12_gib": free_bytes >= 12 * 1024**3,
    }


def _package_installed(package: str) -> bool:
    result = _run(("/usr/bin/dpkg-query", "-W", "-f=${Status}", package), timeout=20)
    return result.returncode == 0 and result.stdout.strip() == "install ok installed"


def _binary_version(executable: str) -> dict[str, object]:
    path = _LOCAL_BIN / executable
    if not path.is_file() or not os.access(path, os.X_OK):
        return {"healthy": False, "path": str(path), "error": "missing"}
    result = _run((str(path), "--version"), timeout=20)
    output = (result.stdout.strip() or result.stderr.strip()).splitlines()
    version = output[0] if output else ""
    prefixes = _FOUNDATION_VERSION_PREFIXES[executable]
    return {
        "healthy": result.returncode == 0 and any(prefix in version for prefix in prefixes),
        "path": str(path),
        "version": version,
    }


def _service_status(unit: str) -> dict[str, object]:
    enabled = _run(("/usr/bin/systemctl", "is-enabled", unit), timeout=20)
    active = _run(("/usr/bin/systemctl", "is-active", unit), timeout=20)
    return {
        "healthy": enabled.returncode == 0 and active.returncode == 0,
        "enabled": enabled.stdout.strip(),
        "active": active.stdout.strip(),
    }


def foundation_doctor(payload: Mapping[str, object]) -> dict[str, object]:
    contract = _foundation_contract(payload)
    platform_checks = _foundation_platform_checks()
    packages = {package: _package_installed(package) for package in contract["apt_packages"]}
    artifacts = {
        artifact["artifact_id"]: _binary_version(str(artifact["executable"]))
        for artifact in contract["artifacts"]
    }
    services = {unit: _service_status(unit) for unit in contract["services"]}
    evidence: object = None
    if _FOUNDATION_EVIDENCE.is_file():
        try:
            evidence = json.loads(_FOUNDATION_EVIDENCE.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            evidence = {"status": "unreadable"}
    expected_artifacts = {
        artifact["artifact_id"]: {
            "version": artifact["version"],
            "sha256": artifact["sha256"],
        }
        for artifact in contract["artifacts"]
    }
    evidence_healthy = (
        isinstance(evidence, dict)
        and evidence.get("schema_version") == 1
        and evidence.get("profile") == _FOUNDATION_PROFILE
        and evidence.get("status") == "installed"
        and evidence.get("phase") == "completed"
        and evidence.get("error") is None
        and evidence.get("artifacts") == expected_artifacts
    )
    healthy = (
        all(platform_checks.values())
        and all(packages.values())
        and all(bool(item["healthy"]) for item in artifacts.values())
        and all(bool(item["healthy"]) for item in services.values())
        and evidence_healthy
    )
    return {
        "status": "healthy" if healthy else "degraded",
        "profile": _FOUNDATION_PROFILE,
        "platform": platform_checks,
        "packages": packages,
        "artifacts": artifacts,
        "services": services,
        "evidence": evidence,
        "evidence_healthy": evidence_healthy,
    }


def _download_verified(artifact: Mapping[str, str]) -> Path:
    _FOUNDATION_CACHE.mkdir(parents=True, exist_ok=True, mode=0o755)
    suffixes = {
        "node-tar": ".tar.xz",
        "pip-wheel": ".whl",
        "tar-binary": ".tar.gz",
    }
    try:
        suffix = suffixes[artifact["kind"]]
    except KeyError as exc:
        raise TargetError(f"unsupported foundation artifact kind: {artifact['kind']}") from exc
    filename = f"{artifact['artifact_id']}-{artifact['version']}{suffix}"
    if artifact["kind"] == "pip-wheel":
        filename = PurePosixPath(artifact["url"].split("?", 1)[0]).name
        if not filename.endswith(".whl") or "/" in filename or filename in {"", ".", ".."}:
            raise TargetError("pip wheel URL does not contain a valid filename")
    destination = _FOUNDATION_CACHE / filename
    if destination.is_file() and _file_sha256(destination) == artifact["sha256"]:
        return destination
    temporary = destination.with_name(f".{destination.name}.partial")
    headers = ()
    if artifact["url"].startswith("https://api.github.com/"):
        headers = (
            "--header",
            "Accept: application/octet-stream",
            "--header",
            "X-GitHub-Api-Version: 2022-11-28",
        )
    _checked(
        f"download {artifact['artifact_id']}",
        (
            "/usr/bin/curl",
            "--fail",
            "--location",
            "--retry",
            "3",
            "--retry-all-errors",
            "--connect-timeout",
            "20",
            "--speed-limit",
            "1024",
            "--speed-time",
            "60",
            "--continue-at",
            "-",
            *headers,
            "--output",
            str(temporary),
            artifact["url"],
        ),
        timeout=900,
    )
    if _file_sha256(temporary) != artifact["sha256"]:
        temporary.unlink(missing_ok=True)
        raise TargetError(f"downloaded artifact hash mismatch: {artifact['artifact_id']}")
    os.chmod(temporary, 0o644)
    os.replace(temporary, destination)
    return destination


def _install_managed_link(link: Path, target: Path) -> None:
    if link.is_symlink() and link.resolve() == target.resolve():
        return
    if link.exists() or link.is_symlink():
        raise TargetError(f"refusing to replace unmanaged executable: {link}")
    temporary = link.with_name(f".{link.name}.{uuid.uuid4().hex}.tmp")
    temporary.symlink_to(target)
    os.replace(temporary, link)


def _install_tar_binary(artifact: Mapping[str, str]) -> None:
    archive_path = _download_verified(artifact)
    destination = (
        _FOUNDATION_LIBRARY / artifact["artifact_id"] / artifact["version"] / artifact["executable"]
    )
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
    if not destination.is_file():
        with tarfile.open(archive_path, "r:*") as archive:
            members = [
                member
                for member in archive.getmembers()
                if member.isfile()
                and PurePosixPath(member.name).name == artifact["executable"]
                and member.size <= 256 * 1024**2
            ]
            if len(members) != 1:
                raise TargetError(
                    f"artifact has an invalid executable set: {artifact['artifact_id']}"
                )
            stream = archive.extractfile(members[0])
            if stream is None:
                raise TargetError(f"artifact executable cannot be read: {artifact['artifact_id']}")
            temporary = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.tmp")
            try:
                with temporary.open("xb") as output:
                    shutil.copyfileobj(stream, output)
                    output.flush()
                    os.fsync(output.fileno())
                os.chmod(temporary, 0o755)
                os.replace(temporary, destination)
            finally:
                temporary.unlink(missing_ok=True)
    _install_managed_link(_LOCAL_BIN / artifact["executable"], destination)


def _safe_node_member(member: tarfile.TarInfo, top: str) -> bool:
    name = PurePosixPath(member.name)
    if name.is_absolute() or ".." in name.parts or not name.parts or name.parts[0] != top:
        return False
    if member.issym() or member.islnk():
        target = PurePosixPath(member.linkname)
        if target.is_absolute():
            return False
        combined = name.parent.joinpath(target)
        depth = 0
        for part in combined.parts:
            depth += -1 if part == ".." else (0 if part in {"", "."} else 1)
            if depth < 1:
                return False
    return True


def _install_node(artifact: Mapping[str, str]) -> None:
    archive_path = _download_verified(artifact)
    top = f"node-v{artifact['version']}-linux-arm64"
    destination = _LOCAL_LIB / top
    if not destination.is_dir():
        with tempfile.TemporaryDirectory(prefix="eidolon-node-", dir=_LOCAL_LIB) as raw:
            stage = Path(raw)
            with tarfile.open(archive_path, "r:xz") as archive:
                members = archive.getmembers()
                if not members or not all(_safe_node_member(member, top) for member in members):
                    raise TargetError("Node archive contains an unsafe member")
                archive.extractall(stage, filter="data")
            extracted = stage / top
            if not (extracted / "bin/node").is_file():
                raise TargetError("Node archive is missing bin/node")
            os.replace(extracted, destination)
    for executable in ("node", "npm", "npx", "corepack"):
        _install_managed_link(_LOCAL_BIN / executable, destination / "bin" / executable)


def _install_uv(artifact: Mapping[str, str]) -> None:
    current = _binary_version("uv")
    if current["healthy"]:
        return
    path = _LOCAL_BIN / "uv"
    if path.exists() or path.is_symlink():
        raise TargetError(f"refusing to replace unmanaged executable: {path}")
    wheel = _download_verified(artifact)
    requirement = _VAR_TMP / f"eidolon-uv-{uuid.uuid4().hex}.txt"
    try:
        requirement.write_text(
            f"uv @ {wheel.as_uri()} --hash=sha256:{artifact['sha256']}\n",
            encoding="utf-8",
        )
        os.chmod(requirement, 0o600)
        _checked(
            "install pinned uv wheel",
            (
                "/usr/bin/python3",
                "-m",
                "pip",
                "install",
                "--break-system-packages",
                "--disable-pip-version-check",
                "--no-deps",
                "--no-index",
                "--only-binary=:all:",
                "--require-hashes",
                "--requirement",
                str(requirement),
            ),
            timeout=900,
        )
    finally:
        requirement.unlink(missing_ok=True)


@contextmanager
def _foundation_apt_options(contract: Mapping[str, object]) -> Iterator[tuple[str, ...]]:
    version = _os_release().get("VERSION_ID", "").split(".", 1)[0]
    suites = {"13": "trixie"}
    if version not in suites:
        raise TargetError("foundation apt mirror requires reviewed Debian version")
    mirrors = contract["apt_mirrors"]
    if not isinstance(mirrors, dict):
        raise TargetError("foundation apt mirror contract is invalid")
    suite = suites[version]
    with tempfile.TemporaryDirectory(prefix="eidolon-apt-") as temporary:
        root = Path(temporary)
        source_parts = root / "parts"
        lists = root / "lists"
        source_parts.mkdir()
        (lists / "partial").mkdir(parents=True)
        sources = root / "eidolon.sources"
        sources.write_text(
            "\n".join(
                (
                    "Types: deb",
                    f"URIs: {mirrors['debian']}",
                    f"Suites: {suite} {suite}-updates",
                    "Components: main contrib non-free non-free-firmware",
                    "Signed-By: /usr/share/keyrings/debian-archive-keyring.pgp",
                    "",
                    "Types: deb",
                    f"URIs: {mirrors['raspberrypi']}",
                    f"Suites: {suite}",
                    "Components: main",
                    "Signed-By: /usr/share/keyrings/raspberrypi-archive-keyring.pgp",
                    "",
                    "Types: deb",
                    f"URIs: {mirrors['security']}",
                    f"Suites: {suite}-security",
                    "Components: main contrib non-free non-free-firmware",
                    "Signed-By: /usr/share/keyrings/debian-archive-keyring.pgp",
                    "",
                )
            ),
            encoding="utf-8",
        )
        yield (
            *_APT_COMMAND_OPTIONS,
            "-o",
            f"Dir::Etc::sourcelist={sources}",
            "-o",
            f"Dir::Etc::sourceparts={source_parts}",
            "-o",
            f"Dir::State::lists={lists}",
        )


def foundation_install(payload: Mapping[str, object]) -> dict[str, object]:
    contract = _foundation_contract(payload)
    if os.geteuid() != 0:
        raise TargetError("foundation installation requires root")
    platform_checks = _foundation_platform_checks()
    required_platform = dict(platform_checks)
    if not all(required_platform.values()):
        raise TargetError(f"unsupported Raspberry Pi host platform: {required_platform}")
    with _exclusive(_FOUNDATION_LOCK):
        evidence: dict[str, object] = {
            "schema_version": 1,
            "profile": _FOUNDATION_PROFILE,
            "status": "installing",
            "phase": "validated",
            "artifacts": {
                artifact["artifact_id"]: {
                    "version": artifact["version"],
                    "sha256": artifact["sha256"],
                }
                for artifact in contract["artifacts"]
            },
            "error": None,
            "updated_at": int(time.time()),
        }

        def record(status: str, phase: str, error: str | None = None) -> None:
            evidence.update(
                {
                    "status": status,
                    "phase": phase,
                    "error": error,
                    "updated_at": int(time.time()),
                }
            )
            _atomic_json(_FOUNDATION_EVIDENCE, evidence)

        phase = "validated"
        record("installing", phase)
        try:
            environment = dict(os.environ)
            environment["DEBIAN_FRONTEND"] = "noninteractive"
            with _foundation_apt_options(contract) as apt_options:
                _checked(
                    "refresh apt metadata",
                    ("/usr/bin/apt-get", *apt_options, "update"),
                    timeout=900,
                    env=environment,
                )
                _checked(
                    "install foundation packages",
                    (
                        "/usr/bin/apt-get",
                        *apt_options,
                        "install",
                        "-y",
                        "--no-install-recommends",
                        *contract["apt_packages"],
                    ),
                    timeout=1800,
                    env=environment,
                )
            phase = "packages"
            record("installing", phase)
            for artifact in contract["artifacts"]:
                kind = artifact["kind"]
                if kind == "tar-binary":
                    _install_tar_binary(artifact)
                elif kind == "pip-wheel":
                    _install_uv(artifact)
                elif kind == "node-tar":
                    _install_node(artifact)
                else:
                    raise TargetError(f"unsupported foundation artifact kind: {kind}")
            phase = "artifacts"
            record("installing", phase)
            for unit in contract["services"]:
                _checked(
                    f"enable foundation service {unit}",
                    ("/usr/bin/systemctl", "enable", "--now", unit),
                    timeout=120,
                )
            phase = "completed"
            record("installed", phase)
            result = foundation_doctor(payload)
            if result["status"] != "healthy":
                raise TargetError(
                    "foundation installation completed but the health gate is degraded"
                )
            result["evidence"] = dict(evidence)
            return {
                "status": "installed",
                "profile": _FOUNDATION_PROFILE,
                "doctor": result,
            }
        except Exception as exc:
            record("failed", phase, str(exc))
            raise


def _https_json(path: str) -> dict[str, object]:
    return _https_json_endpoint("127.0.0.1", 9002, path, label="Local API")


def _https_json_endpoint(host: str, port: int, path: str, *, label: str) -> dict[str, object]:
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    connection = http.client.HTTPSConnection(host, port, timeout=5, context=context)
    try:
        connection.request("GET", path)
        response = connection.getresponse()
        payload = response.read(1024 * 1024)
    except (OSError, http.client.HTTPException) as exc:
        raise TargetError(f"{label} self-check failed: {exc}") from exc
    finally:
        connection.close()
    if response.status != 200:
        raise TargetError(f"{label} self-check returned HTTP {response.status}")
    try:
        document = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise TargetError(f"{label} self-check returned invalid JSON") from exc
    if not isinstance(document, dict):
        raise TargetError(f"{label} self-check returned a non-object")
    return document


def _environment_values(path: Path) -> dict[str, str]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError) as exc:
        raise TargetError(f"Host application environment is unreadable: {path}") from exc
    values: dict[str, str] = {}
    for raw in lines:
        if not raw:
            continue
        key, separator, value = raw.partition("=")
        if not separator or not key or not value or key in values:
            raise TargetError(f"Host application environment is invalid: {path}")
        values[key] = value
    return values


def _host_application_ready(
    app: Mapping[str, object], *, root: Path = Path("/")
) -> dict[str, object]:
    hostname = str(app["hub_hostname"])
    hub_id = str(app["hub_id"])
    address = str(app["lan_ipv4"])
    port = int(app["hub_https_port"])
    origin = str(app["hub_origin"])
    settings_value = Path("/etc/eidolon/generated/hub.yaml")
    certificate_value = Path("/etc/eidolon/tls/hub.crt")
    private_key_value = Path("/etc/eidolon/tls/hub.key")
    settings_path = _host_path(root, settings_value)
    certificate_path = _host_path(root, certificate_value)
    private_key_path = _host_path(root, private_key_value)
    files = {
        "hub_settings": _private_file_check(settings_path, 0o640, "root", "eidolon"),
        "hub_certificate": _private_file_check(certificate_path, 0o640, "root", "eidolon"),
        "hub_private_key": _private_file_check(private_key_path, 0o640, "root", "eidolon"),
    }
    settings = ""
    local_api_values: dict[str, str] = {}
    channel_values: dict[str, str] = {}
    try:
        settings = settings_path.read_text(encoding="utf-8")
        local_api_values = _environment_values(_host_path(root, Path("/etc/eidolon/local-api.env")))
        channel_values = _environment_values(_host_path(root, Path("/etc/eidolon/channel.env")))
    except (OSError, UnicodeDecodeError, TargetError):
        pass
    certificate_ok = False
    try:
        decoded = ssl._ssl._test_decode_cert(str(certificate_path))
        sans = decoded.get("subjectAltName", ())
        starts = ssl.cert_time_to_seconds(decoded["notBefore"])
        expires = ssl.cert_time_to_seconds(decoded["notAfter"])
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(certificate_path, private_key_path)
        instant = time.time()
        certificate_ok = sans == (("DNS", hostname),) and starts <= instant and expires > instant
    except (KeyError, OSError, ssl.SSLError, ValueError):
        certificate_ok = False
    try:
        health = _https_json_endpoint(address, port, "/health", label="Hub LAN ingress")
        descriptor = _https_json_endpoint(
            address,
            port,
            "/api/device-onboarding/v1/descriptor",
            label="Hub LAN descriptor",
        )
    except TargetError as exc:
        health = {"error": str(exc)}
        descriptor = {}
    resolution = _run(("/usr/bin/avahi-resolve-host-name", "-4", hostname), timeout=10)
    resolved_addresses = {
        line.split("\t", 1)[1].strip() for line in resolution.stdout.splitlines() if "\t" in line
    }
    browse = _run(
        ("/usr/bin/avahi-browse", "-rtp", "_eidolon-hub._tcp"),
        timeout=15,
    )
    mdns_records = []
    for line in browse.stdout.splitlines():
        fields = line.split(";")
        if (
            len(fields) >= 10
            and fields[0] == "="
            and fields[3] == hub_id
            and fields[4] == "_eidolon-hub._tcp"
        ):
            mdns_records.append(fields)
    expected_descriptor = origin + "/api/device-onboarding/v1/descriptor"
    expected_mdns = bool(mdns_records) and all(
        fields[6] == hostname
        and fields[7] == address
        and fields[8] == str(port)
        and expected_descriptor in ";".join(fields[9:])
        for fields in mdns_records
    )
    checks = {
        "files": all(bool(value["healthy"]) for value in files.values()),
        "certificate": certificate_ok,
        "hub_settings": (
            f"hub_id: {hub_id}" in settings
            and f"public_base_url: {origin}" in settings
            and "hub_id: eidolon-hub-local" not in settings
        ),
        "local_api_target": (
            local_api_values.get("EIDOLON_LOCAL_API_HUB_ID") == hub_id
            and local_api_values.get("EIDOLON_LOCAL_API_HUB_DESCRIPTOR_URI")
            == origin + "/api/device-onboarding/v1/descriptor"
            and local_api_values.get("EIDOLON_LOCAL_API_HUB_TLS_CERTIFICATE")
            == str(certificate_value)
        ),
        "livekit_origin": (
            channel_values.get("EIDOLON_LIVEKIT_CLIENT_URL") == app["livekit_client_url"]
            and channel_values.get("EIDOLON_CHANNEL_PROVIDER_ALLOW_INSECURE_LAN_CLIENT_URL")
            == ("1" if app["allow_insecure_livekit"] else "0")
        ),
        "hub_lan_health": health.get("status") == "ok",
        "hub_descriptor": (
            descriptor.get("hub_id") == hub_id
            and descriptor.get("descriptor_uri") == origin + "/api/device-onboarding/v1/descriptor"
        ),
        "mdns_resolves_to_host": resolution.returncode == 0 and resolved_addresses == {address},
        "mdns_contract": browse.returncode == 0 and expected_mdns,
    }
    return {
        "healthy": all(checks.values()),
        "identity": {
            "host_id": app["host_id"],
            "hub_id": hub_id,
            "hub_hostname": hostname,
        },
        "checks": checks,
        "files": files,
        "resolution": sorted(resolved_addresses),
        "hub_health": health,
    }


def _private_file_check(path: Path, mode: int, user: str, group: str) -> dict[str, object]:
    try:
        metadata = path.stat(follow_symlinks=False)
        expected_uid = pwd.getpwnam(user).pw_uid
        expected_gid = grp.getgrnam(group).gr_gid
    except (OSError, KeyError):
        return {"healthy": False, "path": str(path), "error": "missing identity or file"}
    healthy = (
        stat.S_ISREG(metadata.st_mode)
        and not path.is_symlink()
        and stat.S_IMODE(metadata.st_mode) == mode
        and metadata.st_uid == expected_uid
        and metadata.st_gid == expected_gid
    )
    return {
        "healthy": healthy,
        "path": str(path),
        "mode": f"{stat.S_IMODE(metadata.st_mode):04o}",
        "owner": f"{metadata.st_uid}:{metadata.st_gid}",
    }


def app_ready(payload: Mapping[str, object]) -> dict[str, object]:
    """Prove the host-side prerequisites for mobile App commissioning."""

    _fixed_units(payload)
    app = _optional_app(payload)
    try:
        preflight_result = _run((str(_APP_PREFLIGHT),), timeout=60)
    except TargetError as exc:
        preflight: object = {"ok": False, "error": str(exc)}
    else:
        if preflight_result.returncode not in {0, 1}:
            preflight = {
                "ok": False,
                "error": preflight_result.stderr.strip() or "Bootstrap preflight could not run",
            }
        else:
            try:
                preflight = json.loads(preflight_result.stdout)
            except json.JSONDecodeError:
                preflight = {"ok": False, "error": "Bootstrap preflight output is invalid"}
    services = {
        unit: _unit_status(unit)
        for unit in (
            "eidolon-bootstrapd.service",
            "eidolon-local-api.service",
            *(("eidolon-hub-ingress.service",) if app is not None else ()),
            "bluetooth.service",
            "NetworkManager.service",
            "avahi-daemon.service",
        )
    }
    service_health = all(
        value.get("ActiveState") == "active" and value.get("SubState") == "running"
        for value in services.values()
    )
    files = {
        "host_identity": _private_file_check(
            _HOST_IDENTITY,
            0o600,
            "eidolon-bootstrap",
            "eidolon-bootstrap",
        ),
        "commissioning_tls": _private_file_check(
            _COMMISSIONING_TLS,
            0o640,
            "eidolon-bootstrap",
            "eidolon-bootstrap",
        ),
    }
    sockets = {
        "bootstrap_control": _BOOTSTRAP_SOCKET.is_socket(),
    }
    local_api: dict[str, object]
    try:
        health = _https_json("/healthz")
        descriptor = _https_json("/api/local/v1/descriptor")
        descriptor_valid = (
            health.get("status") == "ok"
            and descriptor.get("contract_version") == "1"
            and isinstance(descriptor.get("host_id"), str)
            and re.fullmatch(r"ehost-[0-9a-f]{20}", str(descriptor["host_id"])) is not None
            and isinstance(descriptor.get("host_public_key_fingerprint"), str)
            and str(descriptor["host_public_key_fingerprint"]).startswith("sha256:")
            and isinstance(descriptor.get("ble_service_uuid"), str)
        )
        local_api = {
            "healthy": descriptor_valid,
            "health_status": health.get("status"),
            "bootstrap_status": health.get("bootstrap"),
            "host_id": descriptor.get("host_id"),
            "ble_service_uuid": descriptor.get("ble_service_uuid"),
            "tls": "loopback self-check only; App pins the BLE-advertised TLS SPKI",
        }
    except TargetError as exc:
        local_api = {"healthy": False, "error": str(exc)}
    mdns = {
        "healthy": _MDNS_DEFINITION.is_file() and not _MDNS_DEFINITION.is_symlink(),
        "service_type": "_eidolon-local-api._tcp",
        "port": 9002,
    }
    host_application = _host_application_ready(app) if app is not None else None
    preflight_ok = isinstance(preflight, dict) and preflight.get("ok") is True
    healthy = (
        preflight_ok
        and service_health
        and all(bool(value["healthy"]) for value in files.values())
        and all(sockets.values())
        and bool(local_api["healthy"])
        and bool(mdns["healthy"])
        and (host_application is None or bool(host_application["healthy"]))
    )
    return {
        "status": "app_ready" if healthy else "degraded",
        "preflight": preflight,
        "services": services,
        "files": files,
        "sockets": sockets,
        "local_api": local_api,
        "mdns": mdns,
        "host_application": host_application,
        "scope": (
            "Host-side commissioning readiness only. A real phone must still verify BLE, "
            "Host proof, TLS SPKI pinning, Controller claim, Wi-Fi checkpoint and Workspace setup."
        ),
    }


def guard_upload(payload: Mapping[str, object]) -> dict[str, object]:
    release_id = _release_id(payload)
    transfer_id = payload.get("transfer_id")
    if not isinstance(transfer_id, str) or _SHA256.fullmatch(transfer_id) is None:
        raise TargetError("upload transfer identity is invalid")
    prepared = _RELEASES / release_id
    if (prepared / "release.json").is_file() and (prepared / "release.json.sha256").is_file():
        return {
            "status": "already_prepared",
            "path": str(prepared),
            "transfer_id": transfer_id,
        }
    path = _VAR_TMP / f"eidolon-release-{release_id}"
    marker = path / ".eidolon-upload.json"
    expected = {
        "schema_version": 1,
        "release_id": release_id,
        "transfer_id": transfer_id,
    }
    if not path.exists() and not path.is_symlink():
        path.mkdir(mode=0o700)
        _atomic_json(marker, expected)
        return {
            "status": "ready_for_upload",
            "path": str(path),
            "transfer_id": transfer_id,
        }
    try:
        metadata = path.stat(follow_symlinks=False)
    except OSError as exc:
        raise TargetError(f"remote bundle path is not resumable: {path}") from exc
    owned_directory = (
        not path.is_symlink()
        and stat.S_ISDIR(metadata.st_mode)
        and stat.S_IMODE(metadata.st_mode) == 0o700
        and metadata.st_uid == os.geteuid()
    )
    if not marker.exists():
        closed_shape = {
            "bundle.json",
            "prepare_target.py",
            "python-dependencies.tar.gz",
            "sources",
        }
        manifest = path / "bundle.json"
        if (
            not owned_directory
            or {item.name for item in path.iterdir()} != closed_shape
            or not manifest.is_file()
            or manifest.is_symlink()
            or _file_sha256(manifest) != transfer_id
        ):
            raise TargetError(f"remote bundle path is not resumable: {path}")
        return {
            "status": "ready_for_prepare",
            "path": str(path),
            "transfer_id": transfer_id,
        }
    try:
        document = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise TargetError(f"remote bundle path is not resumable: {path}") from exc
    if not owned_directory or document != expected:
        raise TargetError(f"remote bundle path identity or ownership drifted: {path}")
    return {
        "status": "resume_upload",
        "path": str(path),
        "transfer_id": transfer_id,
    }


def finalize_upload(payload: Mapping[str, object]) -> dict[str, object]:
    release_id = _release_id(payload)
    transfer_id = payload.get("transfer_id")
    if not isinstance(transfer_id, str) or _SHA256.fullmatch(transfer_id) is None:
        raise TargetError("upload transfer identity is invalid")
    path = _VAR_TMP / f"eidolon-release-{release_id}"
    marker = path / ".eidolon-upload.json"
    try:
        metadata = path.stat(follow_symlinks=False)
    except OSError as exc:
        raise TargetError("uploaded bundle directory is missing") from exc
    if (
        path.is_symlink()
        or not stat.S_ISDIR(metadata.st_mode)
        or stat.S_IMODE(metadata.st_mode) != 0o700
        or metadata.st_uid != os.geteuid()
    ):
        raise TargetError("uploaded bundle directory ownership drifted")
    closed_shape = {
        "bundle.json",
        "prepare_target.py",
        "python-dependencies.tar.gz",
        "sources",
    }
    actual = {item.name for item in path.iterdir()}
    if marker.exists():
        try:
            document = json.loads(marker.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise TargetError("upload marker is unreadable") from exc
        expected = {
            "schema_version": 1,
            "release_id": release_id,
            "transfer_id": transfer_id,
        }
        if document != expected or actual != closed_shape | {marker.name}:
            raise TargetError("uploaded bundle shape or identity drifted")
    elif actual != closed_shape:
        raise TargetError("uploaded bundle shape or identity drifted")
    manifest = path / "bundle.json"
    if manifest.is_symlink() or not manifest.is_file() or _file_sha256(manifest) != transfer_id:
        raise TargetError("uploaded bundle manifest digest drifted")
    if marker.exists():
        marker.unlink()
        status = "finalized"
    else:
        status = "already_finalized"
    return {"status": status, "path": str(path), "transfer_id": transfer_id}


def cleanup_stage(payload: Mapping[str, object]) -> dict[str, object]:
    release_id = _release_id(payload)
    path = _VAR_TMP / f"eidolon-secrets-{release_id}"
    if path.parent != _VAR_TMP or _STAGING_NAME.fullmatch(path.name) is None:
        raise TargetError("secret staging path is unsafe")
    if path.exists():
        if path.is_symlink() or not path.is_dir():
            raise TargetError("secret staging path is not a directory")
        shutil.rmtree(path)
    return {"status": "cleaned", "path": str(path)}


class _DeploymentRunner:
    def __init__(self, command_result_type: type) -> None:
        self._command_result_type = command_result_type

    def run(self, *command: str):
        result = _run(command)
        return self._command_result_type(result.returncode, result.stdout, result.stderr)


class TargetInstaller:
    """Resumable first-install state machine for an otherwise unowned namespace."""

    def __init__(
        self,
        *,
        release: object,
        secret_stage: Path,
        data: Mapping[str, Path],
        host: object,
        root: Path = Path("/"),
        command: Callable[..., subprocess.CompletedProcess[str]] = _run,
        manage_ownership: bool = True,
        app_check: Callable[[], dict[str, object]] | None = None,
    ) -> None:
        self.release = release
        self.secret_stage = secret_stage
        self.data = data
        self.host = host
        self.root = root.resolve()
        self.command = command
        self.manage_ownership = manage_ownership
        self.app_check = app_check
        self.release_id = str(release.release_id)
        self.evidence_dir = _host_path(
            self.root,
            data["deployment_evidence"] / f"install-{self.release_id}",
        )
        self.journal_path = self.evidence_dir / "install.json"
        self.lock_path = _host_path(self.root, Path("/run/lock/eidolon-install.lock"))

    def install(self) -> dict[str, object]:
        if os.geteuid() != 0 and self.root == Path("/"):
            raise TargetError("first install requires root")
        inputs = self._input_digests()
        with _exclusive(self.lock_path):
            journal = self._load_or_begin(inputs)
            if journal.get("status") == "completed":
                result = self.host.doctor(self.release)
                return {
                    "status": "already_installed",
                    "release_id": self.release_id,
                    "app": self._require_app_ready(),
                    **result,
                }
            phase = str(journal["phase"])
            try:
                if self._before(phase, "identities"):
                    self._ensure_identities_and_directories()
                    phase = self._record(journal, "identities")
                if self._before(phase, "prerequisites"):
                    self._install_prerequisites(inputs)
                    phase = self._record(journal, "prerequisites")
                else:
                    # A resumed transaction must re-prove the private inputs it
                    # installed before it is allowed to start or switch again.
                    self._install_prerequisites(inputs)
                if self._before(phase, "data_baseline"):
                    self._create_data_v2_baseline()
                    phase = self._record(journal, "data_baseline")
                if self._before(phase, "assets"):
                    self.host.install_assets(self.release)
                    self.host.switch_components(self.release)
                    self.host.reload_systemd()
                    self._enable_product_units()
                    phase = self._record(journal, "assets")
                if self._before(phase, "started"):
                    self.host.start_release(self.release)
                    self.host.wait_ready(self.release)
                    self._start_host_application()
                    result = self.host.doctor(self.release)
                    app_result = self._require_app_ready()
                    phase = self._record(journal, "started")
                else:
                    self._start_host_application()
                    result = self.host.doctor(self.release)
                    app_result = self._require_app_ready()
                phase = self._record(journal, "completed", status="completed")
                return {
                    "status": "installed",
                    "release_id": self.release_id,
                    "phase": phase,
                    "app": app_result,
                    **result,
                }
            except Exception as exc:
                stop_error: str | None = None
                if not self._before(phase, "assets"):
                    try:
                        self.host.quiesce(self.release)
                    except Exception as stop_exc:
                        stop_error = str(stop_exc)
                journal.update(
                    {
                        "status": "failed",
                        "phase": phase,
                        "error": str(exc),
                        "stop_error": stop_error,
                        "updated_at": int(time.time()),
                    }
                )
                _atomic_json(self.journal_path, journal)
                raise

    def _require_app_ready(self) -> dict[str, object] | None:
        if self.app_check is None:
            return None
        result = self.app_check()
        if result.get("status") != "app_ready":
            raise TargetError("mobile App commissioning gate is degraded")
        return result

    def _input_digests(self) -> dict[str, str]:
        if not self.secret_stage.is_dir() or self.secret_stage.is_symlink():
            raise TargetError("secret staging directory is missing or unsafe")
        actual = {path.name for path in self.secret_stage.iterdir()}
        if frozenset(actual) not in {frozenset(SECRET_INPUTS), frozenset(INSTALL_INPUTS)}:
            raise TargetError("secret staging file set is invalid")
        values: dict[str, str] = {}
        for name in sorted(actual):
            path = self.secret_stage / name
            if not path.is_file() or path.is_symlink():
                raise TargetError(f"secret staging input is unsafe: {name}")
            values[name] = _file_sha256(path)
        return values

    def _load_or_begin(self, inputs: Mapping[str, str]) -> dict[str, object]:
        if self.journal_path.exists():
            try:
                document = json.loads(self.journal_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise TargetError("install journal is unreadable") from exc
            if (
                not isinstance(document, dict)
                or document.get("schema_version") != 1
                or document.get("release_id") != self.release_id
                or document.get("input_sha256") != dict(inputs)
                or document.get("phase") not in _PHASES
            ):
                raise TargetError("install journal identity or inputs do not match")
            return document
        self._assert_clean_namespace()
        self.evidence_dir.mkdir(parents=True, mode=0o700)
        os.chmod(self.evidence_dir, 0o700)
        document: dict[str, object] = {
            "schema_version": 1,
            "release_id": self.release_id,
            "status": "running",
            "phase": "validated",
            "input_sha256": dict(inputs),
            "updated_at": int(time.time()),
        }
        _atomic_json(self.journal_path, document)
        return document

    def _assert_clean_namespace(self) -> None:
        conflicts: list[str] = []
        for component in self.release.components:
            link = _host_path(self.root, component.current_link)
            if link.exists() or link.is_symlink():
                conflicts.append(str(component.current_link))
        for path in (self.data["system_database"], self.data["bootstrap_database"]):
            if _host_path(self.root, path).exists():
                conflicts.append(str(path))
        for destination, _user, _group, _mode in INSTALL_INPUTS.values():
            if _host_path(self.root, destination).exists():
                conflicts.append(str(destination))
        for asset in self.release.system_assets:
            if _host_path(self.root, asset.destination).exists():
                conflicts.append(str(asset.destination))
        for namespace in (
            Path("/var/lib/eidolon"),
            Path("/var/lib/eidolon-bootstrap"),
            Path("/var/lib/eidolon/admin"),
            Path("/etc/eidolon"),
        ):
            path = _host_path(self.root, namespace)
            if path.is_dir() and any(path.iterdir()):
                conflicts.append(f"{namespace}/*")
        if conflicts:
            raise TargetError(
                "first install refuses an existing unowned namespace: "
                + ", ".join(sorted(conflicts))
            )

    @staticmethod
    def _before(current: str, target: str) -> bool:
        return _PHASES.index(current) < _PHASES.index(target)

    def _record(
        self,
        journal: dict[str, object],
        phase: str,
        *,
        status: str = "running",
    ) -> str:
        journal.update(
            {
                "phase": phase,
                "status": status,
                "error": None,
                "stop_error": None,
                "updated_at": int(time.time()),
            }
        )
        _atomic_json(self.journal_path, journal)
        return phase

    def _ensure_identities_and_directories(self) -> None:
        if self.root == Path("/"):
            self._ensure_service_identity("eidolon")
            self._ensure_service_identity("eidolon-bootstrap")
        _ensure_host_path_contract(self.root, self._chown)

    def _ensure_service_identity(self, name: str) -> None:
        group = self.command(("/usr/bin/getent", "group", name), timeout=30)
        if group.returncode != 0:
            self._command_checked(
                "service group creation", ("/usr/sbin/groupadd", "--system", name)
            )
        user = self.command(("/usr/bin/id", "-u", name), timeout=30)
        if user.returncode != 0:
            self._command_checked(
                "service user creation",
                (
                    "/usr/sbin/useradd",
                    "--system",
                    "--gid",
                    name,
                    "--home-dir",
                    "/nonexistent",
                    "--shell",
                    "/usr/sbin/nologin",
                    name,
                ),
            )
        primary = self._command_checked("service identity inspection", ("/usr/bin/id", "-gn", name))
        if primary.stdout.strip() != name:
            raise TargetError(f"service identity has unexpected primary group: {name}")

    def _install_prerequisites(self, inputs: Mapping[str, str]) -> None:
        selected_inputs = {name: INSTALL_INPUTS[name] for name in inputs}
        for name, (destination_value, user, group, mode) in selected_inputs.items():
            source = self.secret_stage / name
            destination = _host_path(self.root, destination_value)
            if destination.exists():
                if (
                    destination.is_symlink()
                    or not destination.is_file()
                    or _file_sha256(destination) != inputs[name]
                    or stat.S_IMODE(destination.stat().st_mode) != mode
                ):
                    raise TargetError(
                        f"existing prerequisite differs during resume: {destination_value}"
                    )
                continue
            destination.parent.mkdir(parents=True, exist_ok=True)
            temporary = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.tmp")
            try:
                shutil.copyfile(source, temporary)
                os.chmod(temporary, mode)
                self._chown(temporary, user, group)
                os.replace(temporary, destination)
            finally:
                temporary.unlink(missing_ok=True)

    def _create_data_v2_baseline(self) -> None:
        data_component = self.release.components_by_id["eidolon_data"]
        data_root = _host_path(self.root, data_component.release_path)
        database = _host_path(self.root, self.data["system_database"])
        if database.exists() and (database.is_symlink() or not database.is_file()):
            raise TargetError("Data authority path is unsafe")
        command = (
            str(data_root / ".venv/bin/alembic"),
            "-c",
            str(data_root / "alembic.ini"),
            "upgrade",
            "head",
        )
        environment = dict(os.environ)
        environment["EIDOLON_DATA_SQLITE_PATH"] = str(database)
        environment["EIDOLON_DATA_OBJECT_STORE_PATH"] = str(
            _host_path(self.root, self.data["object_store"])
        )
        result = self.command(command, timeout=300, cwd=data_root, env=environment)
        if result.returncode != 0:
            detail = result.stderr.strip() or result.stdout.strip() or "no diagnostic output"
            raise TargetError(f"Data V2 baseline failed: {detail}")
        if not database.is_file() or database.is_symlink():
            raise TargetError("Data V2 baseline did not create its authority database")
        os.chmod(database, 0o640)
        self._chown(database, "eidolon", "eidolon")

    def _enable_product_units(self) -> None:
        self._command_checked(
            "product unit enablement",
            ("/usr/bin/systemctl", "enable", *DIRECT_ENABLE_UNITS),
        )
        ingress = _host_path(self.root, HOST_APPLICATION_INPUTS["hub-ingress.service"][0])
        if ingress.is_file():
            self._command_checked(
                "Host application unit enablement",
                ("/usr/bin/systemctl", "enable", "eidolon-hub-ingress.service"),
            )

    def _start_host_application(self) -> None:
        ingress = _host_path(self.root, HOST_APPLICATION_INPUTS["hub-ingress.service"][0])
        if ingress.is_file():
            self._command_checked(
                "Host application ingress start",
                ("/usr/bin/systemctl", "start", "eidolon-hub-ingress.service"),
            )

    def _command_checked(self, operation: str, command: Sequence[str]):
        result = self.command(command, timeout=120)
        if result.returncode != 0:
            detail = result.stderr.strip() or result.stdout.strip() or "no diagnostic output"
            raise TargetError(f"{operation} failed: {detail}")
        return result

    def _chown(self, path: Path, user: str, group: str) -> None:
        if not self.manage_ownership or self.root != Path("/"):
            return
        try:
            uid = pwd.getpwnam(user).pw_uid
            gid = grp.getgrnam(group).gr_gid
        except KeyError as exc:
            raise TargetError(f"required service identity is missing: {user}:{group}") from exc
        os.chown(path, uid, gid)


def install(payload: Mapping[str, object]) -> dict[str, object]:
    release_id = _release_id(payload)
    data = _fixed_data(payload)
    descriptor = _RELEASES / release_id / "release.json"
    secret_stage = _VAR_TMP / f"eidolon-secrets-{release_id}"
    if secret_stage.parent != _VAR_TMP or _STAGING_NAME.fullmatch(secret_stage.name) is None:
        raise TargetError("secret staging path is unsafe")
    try:
        from eidolon_deploy.linux import CommandResult, LinuxDeploymentHost
        from eidolon_deploy.manifest import load_release_descriptor
    except ImportError as exc:
        raise TargetError("prepared release deployment package is unavailable") from exc
    release = load_release_descriptor(descriptor)
    if release.release_id != release_id:
        raise TargetError("prepared release identity mismatch")
    host = LinuxDeploymentHost(
        runner=_DeploymentRunner(CommandResult),
        readiness_timeout_seconds=90,
    )
    return TargetInstaller(
        release=release,
        secret_stage=secret_stage,
        data=data,
        host=host,
        app_check=lambda: app_ready(payload),
    ).install()


def _refuse_nested_mounts(path: Path) -> None:
    """Never let recursive cleanup cross a mounted filesystem boundary."""

    for directory, names, _files in os.walk(path, followlinks=False):
        parent = Path(directory)
        for name in names:
            candidate = parent / name
            if not candidate.is_symlink() and os.path.ismount(candidate):
                raise TargetError(f"removal root contains a mount: {candidate}")


def _reset_wipes_authority_data(payload: Mapping[str, object]) -> bool:
    value = payload.get("wipe_authority_data", False)
    if type(value) is not bool:
        raise TargetError("wipe_authority_data must be a boolean")
    return value


def _reset_paths(*, wipe_authority_data: bool) -> tuple[Path, ...]:
    paths = set(MANAGED_SYSTEM_ASSETS) | set(RESET_DEPLOYMENT_ROOTS)
    if wipe_authority_data:
        paths.update(RESET_AUTHORITY_ROOTS)
    return tuple(sorted(paths, key=str))


def reset_plan(
    payload: Mapping[str, object],
    *,
    root: Path = Path("/"),
) -> dict[str, object]:
    """Describe a clean reinstall boundary without changing the Host."""

    _fixed_units(payload)
    _fixed_data(payload)
    wipe_authority_data = _reset_wipes_authority_data(payload)
    root = root.resolve()
    detected = [
        str(path)
        for path in _reset_paths(wipe_authority_data=wipe_authority_data)
        if (_host_path(root, path).exists() or _host_path(root, path).is_symlink())
    ]
    staging = _host_path(root, _VAR_TMP)
    staged = []
    if staging.is_dir() and not staging.is_symlink():
        staged = sorted(
            str(_VAR_TMP / path.name)
            for path in staging.iterdir()
            if _STAGING_NAME.fullmatch(path.name) is not None
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


def _remove_reset_path(path: Path, *, display: Path) -> bool:
    if not (path.exists() or path.is_symlink()):
        return False
    if path.is_symlink() or path.is_file():
        path.unlink()
        return True
    if not path.is_dir() or os.path.ismount(path):
        raise TargetError(f"reset target is not a removable owned path: {display}")
    _refuse_nested_mounts(path)
    shutil.rmtree(path)
    return True


def reset_host(
    payload: Mapping[str, object],
    *,
    root: Path = Path("/"),
    command: Callable[..., subprocess.CompletedProcess[str]] = _run,
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
    lock_path = _host_path(root, Path("/run/lock/eidolon-install.lock"))
    with _exclusive(lock_path):
        if manage_services:
            for unit in RESET_STOP_UNITS:
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
                stopped = command(("/usr/bin/systemctl", "stop", unit), timeout=120)
                if stopped.returncode != 0:
                    state = command(("/usr/bin/systemctl", "is-active", unit), timeout=20)
                    if state.stdout.strip() not in {"inactive", "failed", "unknown"}:
                        detail = stopped.stderr.strip() or stopped.stdout.strip() or unit
                        raise TargetError(f"reset could not stop product unit: {detail}")
                disabled = command(("/usr/bin/systemctl", "disable", unit), timeout=120)
                service_results.append(
                    {
                        "unit": unit,
                        "stop_returncode": stopped.returncode,
                        "disable_returncode": disabled.returncode,
                    }
                )
        for value in _reset_paths(wipe_authority_data=wipe_authority_data):
            if _remove_reset_path(_host_path(root, value), display=value):
                removed.append(str(value))
        staging = _host_path(root, _VAR_TMP)
        if staging.is_dir() and not staging.is_symlink():
            for path in sorted(staging.iterdir()):
                if _STAGING_NAME.fullmatch(path.name) is None:
                    continue
                display = _VAR_TMP / path.name
                if _remove_reset_path(path, display=display):
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


def active_release(payload: Mapping[str, object]) -> dict[str, object]:
    """Resolve the active release's operator entries on the target itself.

    The deployer must not derive these from a component directory name; the
    target owns its own layout and reports the published, component-neutral
    entries here.
    """

    _fixed_units(payload)
    if not _CURRENT_KERNEL.is_symlink():
        raise TargetError("no Eidolon release is currently active")
    release_root = _CURRENT_KERNEL.resolve().parent
    if release_root.parent != _RELEASES:
        raise TargetError("the active release link points outside the release root")
    entries = {
        "activator": release_root / RELEASE_ACTIVATOR,
        "interpreter": release_root / RELEASE_INTERPRETER,
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
    _fixed_units(payload)
    app = _optional_app(payload)
    try:
        from eidolon_deploy.linux import LinuxDeploymentHost
        from eidolon_deploy.manifest import load_release_descriptor
    except ImportError as exc:
        raise TargetError("active release deployment package is unavailable") from exc
    active_kernel = _CURRENT_KERNEL.resolve()
    descriptor = active_kernel.parent / "release.json"
    release = load_release_descriptor(descriptor)
    host = LinuxDeploymentHost(readiness_timeout_seconds=90)
    with host.exclusive_activation():
        host.preflight(release)
        if action in {"stop", "restart"}:
            host.quiesce(release)
        if action in {"start", "restart"}:
            host.start_release(release)
            host.wait_ready(release)
            if app is not None:
                ingress = _run(
                    ("/usr/bin/systemctl", "start", "eidolon-hub-ingress.service"),
                    timeout=120,
                )
                if ingress.returncode != 0:
                    raise TargetError(
                        "Host application ingress failed to start: "
                        + (ingress.stderr.strip() or ingress.stdout.strip() or "no output")
                    )
    return {
        "status": action + "ed" if action != "stop" else "stopped",
        "release_id": release.release_id,
    }


def rollback_plan(payload: Mapping[str, object]) -> dict[str, object]:
    release_id = _release_id(payload)
    snapshot_value = payload.get("snapshot")
    if not isinstance(snapshot_value, str):
        raise TargetError("snapshot path is invalid")
    snapshot = Path(snapshot_value)
    expected_parent = FIXED_DATA["deployment_evidence"]
    if not snapshot.is_absolute() or snapshot.parent != expected_parent or not snapshot.is_dir():
        raise TargetError("snapshot is outside the fixed deployment evidence root or missing")
    descriptor = _RELEASES / release_id / "release.json"
    if not descriptor.is_file():
        raise TargetError("release descriptor is missing")
    return {
        "status": "rollback_planned",
        "release_id": release_id,
        "snapshot": str(snapshot),
        "descriptor": str(descriptor),
    }


def logs(payload: Mapping[str, object]) -> dict[str, object]:
    units = _fixed_units(payload)
    requested = payload.get("unit")
    selectable_units = (
        (*units, "eidolon-hub-ingress.service") if _optional_app(payload) is not None else units
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
        result = _run(command, timeout=60)
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
        "boot_id": _read_text(Path("/proc/sys/kernel/random/boot_id")),
        "uptime_seconds": _read_uptime(),
        "disk": {
            "total_bytes": root_usage.total,
            "used_bytes": root_usage.used,
            "free_bytes": root_usage.free,
        },
        "journal": journal["entries"],
        "redaction": "No env content, private key content, database content, or process environment collected.",
    }


def _read_text(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return None


def _read_uptime() -> float | None:
    value = _read_text(Path("/proc/uptime"))
    if value is None:
        return None
    try:
        return float(value.split()[0])
    except (ValueError, IndexError):
        return None


def main(argv: Sequence[str] | None = None) -> int:
    arguments = tuple(sys.argv[1:] if argv is None else argv)
    if len(arguments) != 2:
        print(
            json.dumps({"status": "failed", "error": "expected action and payload"}),
            file=sys.stderr,
        )
        return 2
    action, encoded = arguments
    try:
        payload = _payload(encoded)
        if action == "status":
            result = status(payload)
        elif action == "foundation-doctor":
            result = foundation_doctor(payload)
        elif action == "foundation-install":
            result = foundation_install(payload)
        elif action == "app-ready":
            result = app_ready(payload)
        elif action == "doctor-host":
            result = doctor_host(payload)
        elif action == "guard-upload":
            result = guard_upload(payload)
        elif action == "finalize-upload":
            result = finalize_upload(payload)
        elif action == "cleanup-stage":
            result = cleanup_stage(payload)
        elif action == "install":
            result = install(payload)
        elif action == "active-release":
            result = active_release(payload)
        elif action == "reset-plan":
            result = reset_plan(payload)
        elif action == "reset-host":
            result = reset_host(payload)
        elif action in {"start", "stop", "restart"}:
            result = lifecycle(action, payload)
        elif action == "rollback-plan":
            result = rollback_plan(payload)
        elif action == "logs":
            result = logs(payload)
        elif action == "diagnose":
            result = diagnose(payload)
        else:
            raise TargetError("unknown target action")
    except Exception as exc:
        print(json.dumps({"status": "failed", "error": str(exc)}, sort_keys=True), file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
