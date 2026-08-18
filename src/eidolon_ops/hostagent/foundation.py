"""The pinned non-Eidolon foundation, and whether this Host has it."""

from __future__ import annotations

import json
import os
import platform
import re
import shutil
from collections.abc import Mapping
from pathlib import Path

from . import primitives
from .primitives import TargetError

FOUNDATION_PROFILE = "raspberry-pi-os-debian-arm64-v2"

FOUNDATION_OS_IDS = ("debian", "raspbian")

FOUNDATION_OS_VERSIONS = ("13",)

FOUNDATION_APT_MIRRORS = {
    "debian": "https://mirror.nju.edu.cn/debian/",
    "raspberrypi": "https://archive.raspberrypi.com/debian/",
    "security": "https://mirror.nju.edu.cn/debian-security/",
}

APT_COMMAND_OPTIONS = (
    "-o",
    "Acquire::ForceIPv4=true",
    "-o",
    "Acquire::Retries=3",
)

FOUNDATION_PACKAGES = (
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

#: The Pi OS default keeps the journal in RAM, so a Host forgets why anything
#: went wrong the moment it restarts. Overridden here, bounded, because a
#: product that cannot account for its own failures cannot be supported. The
#: content is sent with the contract rather than restated on the Host.
#: Where journald keeps a journal that survives a reboot. One place says
#: it, so the check and the installer cannot come to disagree.
JOURNAL_DIRECTORY = Path("/var/log/journal")
JOURNAL_PERSISTENCE = Path("/etc/systemd/journald.conf.d/50-eidolon-persistent.conf")
JOURNAL_PERSISTENCE_CONTENT = """\
# Installed by eidolon-ops. Overrides the Raspberry Pi OS default of
# Storage=volatile, under which the journal is held in RAM and lost at every
# boot — leaving a Host unable to account for any failure that happened before
# its last restart.
#
# Bounded on purpose: the default it replaces exists to spare the SD card, so
# this buys back the ability to diagnose rather than an unlimited log.
[Journal]
Storage=persistent
SystemMaxUse=512M
SystemMaxFileSize=64M
SystemKeepFree=1G
MaxRetentionSec=30day
"""

FOUNDATION_SERVICES = (
    "bluetooth.service",
    "NetworkManager.service",
    "avahi-daemon.service",
)

FOUNDATION_ARTIFACTS = (
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

FOUNDATION_VERSION_PREFIXES = {
    "nats-server": ("nats-server: v2.14.0", "v2.14.0"),
    "livekit-server": ("livekit-server version 1.11.0", "1.11.0"),
    "uv": ("uv 0.11.15",),
    "node": ("v22.23.2",),
}

FOUNDATION_EVIDENCE = Path("/var/lib/eidolon-ops/foundation-v2.json")

FOUNDATION_CACHE = Path("/var/cache/eidolon/ops/artifacts")

FOUNDATION_LIBRARY = Path("/usr/local/lib/eidolon-foundation")

FOUNDATION_LOCK = Path("/run/lock/eidolon-foundation.lock")

LOCAL_BIN = Path("/usr/local/bin")

LOCAL_LIB = Path("/usr/local/lib")

def expected_foundation() -> dict[str, object]:
    return {
        "profile": FOUNDATION_PROFILE,
        "architecture": "aarch64",
        "os_ids": list(FOUNDATION_OS_IDS),
        "os_versions": list(FOUNDATION_OS_VERSIONS),
        "apt_mirrors": dict(FOUNDATION_APT_MIRRORS),
        "apt_packages": list(FOUNDATION_PACKAGES),
        "services": list(FOUNDATION_SERVICES),
        "artifacts": [dict(artifact) for artifact in FOUNDATION_ARTIFACTS],
        "journal_persistence": JOURNAL_PERSISTENCE_CONTENT,
    }

def foundation_contract(payload: Mapping[str, object]) -> dict[str, object]:
    value = payload.get("foundation")
    expected = expected_foundation()
    if value != expected:
        raise TargetError("foundation contract differs from the reviewed pinned profile")
    return expected

def os_release(root: Path = Path("/")) -> dict[str, str]:
    path = primitives.host_path(root, Path("/etc/os-release"))
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

def foundation_platform_checks() -> dict[str, bool]:
    release = os_release()
    machine = platform.machine().lower()
    os_id = release.get("ID", "")
    version = release.get("VERSION_ID", "").split(".", maxsplit=1)[0]
    init = primitives.read_text(Path("/proc/1/comm"))
    memory_kib = 0
    memory = primitives.read_text(Path("/proc/meminfo")) or ""
    model = (primitives.read_text(Path("/proc/device-tree/model")) or "").rstrip("\x00")
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
        "supported_os": os_id in FOUNDATION_OS_IDS,
        "supported_os_version": version in FOUNDATION_OS_VERSIONS,
        "systemd_pid1": init == "systemd",
        "memory_at_least_8_gib": memory_kib >= 7 * 1024 * 1024,
        "disk_free_at_least_12_gib": free_bytes >= 12 * 1024**3,
    }


def journal_is_persistent() -> bool:
    """Whether this Host will still know why something failed after a reboot.

    Asked of the journal on disk rather than of the drop-in this installs. A
    drop-in that is present but outranked, or one journald has not reloaded,
    leaves the evidence in RAM exactly as if it had never been written — and
    what an operator needs to know is where the logs are, not what a file
    says they should be.
    """

    return any(JOURNAL_DIRECTORY.glob("*/system.journal"))

def package_installed(package: str) -> bool:
    result = primitives.run(("/usr/bin/dpkg-query", "-W", "-f=${Status}", package), timeout=20)
    return result.returncode == 0 and result.stdout.strip() == "install ok installed"

def binary_version(executable: str) -> dict[str, object]:
    path = LOCAL_BIN / executable
    if not path.is_file() or not os.access(path, os.X_OK):
        return {"healthy": False, "path": str(path), "error": "missing"}
    result = primitives.run((str(path), "--version"), timeout=20)
    output = (result.stdout.strip() or result.stderr.strip()).splitlines()
    version = output[0] if output else ""
    prefixes = FOUNDATION_VERSION_PREFIXES[executable]
    return {
        "healthy": result.returncode == 0 and any(prefix in version for prefix in prefixes),
        "path": str(path),
        "version": version,
    }

def foundation_doctor(payload: Mapping[str, object]) -> dict[str, object]:
    contract = foundation_contract(payload)
    platform_checks = foundation_platform_checks()
    packages = {package: package_installed(package) for package in contract["apt_packages"]}
    artifacts = {
        artifact["artifact_id"]: binary_version(str(artifact["executable"]))
        for artifact in contract["artifacts"]
    }
    services = {unit: primitives.service_status(unit) for unit in contract["services"]}
    journal_persistent = journal_is_persistent()
    evidence: object = None
    if FOUNDATION_EVIDENCE.is_file():
        try:
            evidence = json.loads(FOUNDATION_EVIDENCE.read_text(encoding="utf-8"))
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
        and evidence.get("profile") == FOUNDATION_PROFILE
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
        # A Host that cannot say why it failed last time is degraded, not
        # merely inconvenient: it is one reboot away from being undiagnosable,
        # and that is a property of the machine, not of the incident.
        and journal_persistent
        and evidence_healthy
    )
    return {
        "status": "healthy" if healthy else "degraded",
        "journal_persistent": journal_persistent,
        "profile": FOUNDATION_PROFILE,
        "platform": platform_checks,
        "packages": packages,
        "artifacts": artifacts,
        "services": services,
        "evidence": evidence,
        "evidence_healthy": evidence_healthy,
    }
