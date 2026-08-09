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
from pathlib import Path, PurePosixPath

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
    "eidolon-channel.service",
)
DIRECT_ENABLE_UNITS = (
    "eidolon-bootstrapd.service",
    "eidolond.service",
    "eidolon-local-api.service",
    "eidolon-admin.service",
)
CURRENT_LINKS = {
    "eidolon_kernel": Path("/srv/eidolon/current/eidolon_kernel"),
    "eidolon_data": Path("/srv/eidolon/current/eidolon_data"),
    "eidolon_hub": Path("/srv/eidolon/current/eidolon_hub"),
    "eidolon_admin": Path("/srv/eidolon/current/eidolon_admin"),
    "eidolon_agent": Path("/srv/eidolon/current/eidolon_agent"),
    "eidolon_channel": Path("/srv/eidolon/current/eidolon_channel"),
    "eidolon_memory": Path("/srv/eidolon/current/eidolon_memory"),
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
FIXED_DATA = {
    "system_database": Path("/var/lib/eidolon/eidolon-system.sqlite3"),
    "object_store": Path("/var/lib/eidolon/objects"),
    "bootstrap_database": Path("/var/lib/eidolon-bootstrap/bootstrap.sqlite3"),
    "deployment_evidence": Path("/var/lib/eidolon/deployments"),
}
_RELEASE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_STAGING_NAME = re.compile(r"^eidolon-(?:release|secrets)-[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_VAR_TMP = Path("/var/tmp")
_RELEASES = Path("/srv/eidolon/releases")
_CURRENT_KERNEL = Path("/srv/eidolon/current/eidolon_kernel")
_PHASES = (
    "validated",
    "identities",
    "prerequisites",
    "data_baseline",
    "assets",
    "started",
    "completed",
)
_FOUNDATION_PROFILE = "raspberry-pi-os-debian-arm64-v1"
_FOUNDATION_OS_IDS = ("debian", "raspbian")
_FOUNDATION_OS_VERSIONS = ("12", "13")
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
    "libglib2.0-0",
    "libgomp1",
    "libnss-mdns",
    "libsndfile1",
    "libsqlite3-dev",
    "libssl-dev",
    "lsof",
    "network-manager",
    "ninja-build",
    "pkg-config",
    "policykit-1",
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
        "url": (
            "https://github.com/nats-io/nats-server/releases/download/"
            "v2.14.0/nats-server-v2.14.0-linux-arm64.tar.gz"
        ),
        "sha256": "ce7dc5f7d97b70dabc38b13157fed28d7d06227860676143c15c62c5c297996c",
        "kind": "tar-binary",
        "executable": "nats-server",
    },
    {
        "artifact_id": "livekit-server",
        "version": "1.11.0",
        "url": (
            "https://github.com/livekit/livekit/releases/download/"
            "v1.11.0/livekit_1.11.0_linux_arm64.tar.gz"
        ),
        "sha256": "6741466bc12e75544338292ab2c1c02c02f3c626568230b5548fffc53e5a87ff",
        "kind": "tar-binary",
        "executable": "livekit-server",
    },
    {
        "artifact_id": "uv",
        "version": "0.11.15",
        "url": "https://pypi.org/project/uv/0.11.15/",
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
_FOUNDATION_EVIDENCE = Path("/var/lib/eidolon-ops/foundation-v1.json")
_FOUNDATION_CACHE = Path("/var/cache/eidolon-ops/artifacts")
_FOUNDATION_LIBRARY = Path("/usr/local/lib/eidolon-foundation")
_FOUNDATION_LOCK = Path("/run/lock/eidolon-foundation.lock")
_LOCAL_BIN = Path("/usr/local/bin")
_LOCAL_LIB = Path("/usr/local/lib")
_APP_PREFLIGHT = Path("/srv/eidolon/current/eidolon_admin/.venv/bin/eidolon-bootstrap-preflight")
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
        "units": {unit: _unit_status(unit) for unit in units},
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
    checks = {
        "system": platform.system().lower() == "linux",
        "machine": platform.machine().lower() == "aarch64",
        "root": os.geteuid() == 0,
        "systemctl": Path("/usr/bin/systemctl").is_file(),
        "systemd_analyze": Path("/usr/bin/systemd-analyze").is_file(),
        "python3": Path("/usr/bin/python3").is_file(),
        "uv": Path(remote_uv).is_file() and os.access(remote_uv, os.X_OK),
    }
    release_id = _release_id(payload, required=False)
    release_doctor: object = None
    if release_id is not None:
        descriptor = _RELEASES / release_id / "release.json"
        cli = _RELEASES / release_id / "eidolon_kernel/.venv/bin/eidolon-release"
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
    healthy = (
        all(platform_checks.values())
        and all(packages.values())
        and all(bool(item["healthy"]) for item in artifacts.values())
        and all(bool(item["healthy"]) for item in services.values())
    )
    evidence: object = None
    if _FOUNDATION_EVIDENCE.is_file():
        try:
            evidence = json.loads(_FOUNDATION_EVIDENCE.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            evidence = {"status": "unreadable"}
    return {
        "status": "healthy" if healthy else "degraded",
        "profile": _FOUNDATION_PROFILE,
        "platform": platform_checks,
        "packages": packages,
        "artifacts": artifacts,
        "services": services,
        "evidence": evidence,
    }


def _download_verified(artifact: Mapping[str, str]) -> Path:
    _FOUNDATION_CACHE.mkdir(parents=True, exist_ok=True, mode=0o755)
    suffix = ".tar.xz" if artifact["kind"] == "node-tar" else ".tar.gz"
    destination = _FOUNDATION_CACHE / f"{artifact['artifact_id']}-{artifact['version']}{suffix}"
    if destination.is_file() and _file_sha256(destination) == artifact["sha256"]:
        return destination
    temporary = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.tmp")
    try:
        _checked(
            f"download {artifact['artifact_id']}",
            (
                "/usr/bin/curl",
                "--fail",
                "--location",
                "--retry",
                "3",
                "--retry-all-errors",
                "--output",
                str(temporary),
                artifact["url"],
            ),
            timeout=900,
        )
        if _file_sha256(temporary) != artifact["sha256"]:
            raise TargetError(f"downloaded artifact hash mismatch: {artifact['artifact_id']}")
        os.chmod(temporary, 0o644)
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)
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
    requirement = Path("/var/tmp") / f"eidolon-uv-{uuid.uuid4().hex}.txt"
    try:
        requirement.write_text(
            f"uv=={artifact['version']} --hash=sha256:{artifact['sha256']}\n",
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
                "--only-binary=:all:",
                "--require-hashes",
                "--requirement",
                str(requirement),
            ),
            timeout=900,
        )
    finally:
        requirement.unlink(missing_ok=True)


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
            _checked(
                "refresh apt metadata",
                ("/usr/bin/apt-get", "update"),
                timeout=900,
                env=environment,
            )
            _checked(
                "install foundation packages",
                (
                    "/usr/bin/apt-get",
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
            phase = "services"
            record("verifying", phase)
            result = foundation_doctor(payload)
            if result["status"] != "healthy":
                raise TargetError(
                    "foundation installation completed but the health gate is degraded"
                )
            phase = "completed"
            record("installed", phase)
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
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    connection = http.client.HTTPSConnection("127.0.0.1", 9002, timeout=5, context=context)
    try:
        connection.request("GET", path)
        response = connection.getresponse()
        payload = response.read(1024 * 1024)
    except (OSError, http.client.HTTPException) as exc:
        raise TargetError(f"Local API self-check failed: {exc}") from exc
    finally:
        connection.close()
    if response.status != 200:
        raise TargetError(f"Local API self-check returned HTTP {response.status}")
    try:
        document = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise TargetError("Local API self-check returned invalid JSON") from exc
    if not isinstance(document, dict):
        raise TargetError("Local API self-check returned a non-object")
    return document


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
    preflight_result = _run((str(_APP_PREFLIGHT),), timeout=60)
    if preflight_result.returncode not in {0, 1}:
        preflight: object = {
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
    preflight_ok = isinstance(preflight, dict) and preflight.get("ok") is True
    healthy = (
        preflight_ok
        and service_health
        and all(bool(value["healthy"]) for value in files.values())
        and all(sockets.values())
        and bool(local_api["healthy"])
        and bool(mdns["healthy"])
    )
    return {
        "status": "app_ready" if healthy else "degraded",
        "preflight": preflight,
        "services": services,
        "files": files,
        "sockets": sockets,
        "local_api": local_api,
        "mdns": mdns,
        "scope": (
            "Host-side commissioning readiness only. A real phone must still verify BLE, "
            "Host proof, TLS SPKI pinning, Controller claim, Wi-Fi checkpoint and Workspace setup."
        ),
    }


def guard_upload(payload: Mapping[str, object]) -> dict[str, object]:
    release_id = _release_id(payload)
    path = _VAR_TMP / f"eidolon-release-{release_id}"
    if path.exists() or path.is_symlink():
        raise TargetError(f"remote bundle path already exists: {path}")
    return {"status": "ready_for_upload", "path": str(path)}


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
                    result = self.host.doctor(self.release)
                    app_result = self._require_app_ready()
                    phase = self._record(journal, "started")
                else:
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
        if actual != set(SECRET_INPUTS):
            raise TargetError("secret staging file set is invalid")
        values: dict[str, str] = {}
        for name in SECRET_INPUTS:
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
        for destination, _user, _group, _mode in SECRET_INPUTS.values():
            if _host_path(self.root, destination).exists():
                conflicts.append(str(destination))
        for asset in self.release.system_assets:
            if _host_path(self.root, asset.destination).exists():
                conflicts.append(str(asset.destination))
        for namespace in (
            Path("/var/lib/eidolon"),
            Path("/var/lib/eidolon-bootstrap"),
            Path("/var/lib/eidolon-admin"),
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
        directories = (
            (Path("/srv/eidolon/current"), 0o755, "root", "root"),
            (Path("/var/lib/eidolon"), 0o750, "eidolon", "eidolon"),
            (self.data["object_store"], 0o750, "eidolon", "eidolon"),
            (Path("/var/lib/eidolon-admin"), 0o750, "eidolon", "eidolon"),
            (Path("/var/lib/eidolon-bootstrap"), 0o710, "eidolon-bootstrap", "eidolon-bootstrap"),
            (Path("/etc/eidolon"), 0o750, "root", "eidolon"),
        )
        for value, mode, user, group in directories:
            path = _host_path(self.root, value)
            path.mkdir(parents=True, exist_ok=True)
            os.chmod(path, mode)
            self._chown(path, user, group)

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
        for name, (destination_value, user, group, mode) in SECRET_INPUTS.items():
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


def lifecycle(action: str, payload: Mapping[str, object]) -> dict[str, object]:
    _fixed_units(payload)
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
    if requested is not None and requested not in units:
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
    selected = (requested,) if isinstance(requested, str) else units
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
    arguments = tuple(argv or sys.argv[1:])
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
        elif action == "cleanup-stage":
            result = cleanup_stage(payload)
        elif action == "install":
            result = install(payload)
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
