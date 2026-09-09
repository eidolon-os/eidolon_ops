"""The pinned non-Eidolon foundations, and whether this Host has one of them.

One entry per board Ops installs onto, held here as the agent's own copy. The
agent is injected as a payload and imports nothing from the package it came
from, so it cannot share these tables with eidolon_ops.foundation — and it
should not: its job is to refuse a contract that differs from what it was
built against, and reading the one it was handed would make that circular.
Tests hold the two copies equal.
"""

from __future__ import annotations

import json
import os
import platform
import re
import shutil
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from . import primitives
from .primitives import TargetError

#: The profile a payload that names none is assumed to want. Every Host built
#: before profiles were plural was this one, and an older workstation must keep
#: working against a newer agent.
DEFAULT_FOUNDATION_PROFILE = "raspberry-pi-os-debian-arm64-v2"

FOUNDATION_PROFILE = DEFAULT_FOUNDATION_PROFILE

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


#: The interpreter this board is given when its OS has none the release can
#: use. The agent's own copy, held equal to Ops's by a test.
CPYTHON_3_13_AARCH64 = {
    "artifact_id": "cpython",
    "version": "3.13.15",
    "url": (
        "https://github.com/astral-sh/python-build-standalone/releases/download/"
        "20260901/cpython-3.13.15%2B20260901-aarch64-unknown-linux-gnu-"
        "install_only_stripped.tar.gz"
    ),
    "sha256": "01ce0ce9189feaead3298abf10d4efe998c55a489b3d5d38ca4f83dda7e7977e",
    "kind": "python-tar",
    "executable": "python3.13",
}


#: Armbian sets Storage=volatile in journald.conf itself; the drop-in that
#: overrides it is written where a drop-in wins. Byte-identical to the copy in
#: eidolon_ops.foundation, held equal by a test.
RK3588_JOURNAL_PERSISTENCE_CONTENT = """\
# Installed by eidolon-ops. Overrides Storage=volatile, which Armbian sets in
# /etc/systemd/journald.conf — under it the journal is held in RAM and lost at
# every boot, leaving a Host unable to account for any failure that happened
# before its last restart.
#
# Bounded on purpose: the default it replaces exists to spare the storage it
# writes to, so this buys back the ability to diagnose rather than an
# unlimited log.
[Journal]
Storage=persistent
SystemMaxUse=512M
SystemMaxFileSize=64M
SystemKeepFree=1G
MaxRetentionSec=30day
"""


@dataclass(frozen=True, slots=True)
class AptSource:
    """One stanza of the sources list the installer writes.

    The agent builds its own rather than reading one out of the payload, for
    the same reason it holds every other table: what it installs from is part
    of what it was reviewed against.
    """

    uris: str
    suites: str
    components: str
    signed_by: str


@dataclass(frozen=True, slots=True)
class AgentFoundationProfile:
    """What the agent needs to know about one board to check a Host is it.

    A subset of what eidolon_ops.foundation holds: the agent never renders a
    bootstrap script, so it carries no apt sources or suite. It carries the
    hardware prefix rather than the shell glob Ops writes into that script,
    which is the same fact in the form each side uses; a test derives one from
    the other so they cannot drift.
    """

    id: str
    architecture: str
    os_ids: tuple[str, ...]
    os_versions: tuple[str, ...]
    hardware_model_prefix: str | None
    minimum_memory_kib: int
    minimum_disk_kib: int
    apt_mirrors: dict[str, str]
    apt_suite: str
    apt_sources: tuple[AptSource, ...]
    apt_packages: tuple[str, ...]
    services: tuple[str, ...]
    journal_persistence: Path
    journal_persistence_content: str
    python_version: str | None
    artifacts: tuple[dict[str, object], ...]


RASPBERRY_PI_OS_TRIXIE = AgentFoundationProfile(
    id=DEFAULT_FOUNDATION_PROFILE,
    architecture="aarch64",
    os_ids=FOUNDATION_OS_IDS,
    os_versions=FOUNDATION_OS_VERSIONS,
    hardware_model_prefix="Raspberry Pi",
    minimum_memory_kib=7 * 1024 * 1024,
    minimum_disk_kib=12 * 1024 * 1024,
    apt_mirrors=FOUNDATION_APT_MIRRORS,
    apt_suite="trixie",
    apt_sources=(
        AptSource(
            uris=FOUNDATION_APT_MIRRORS["debian"],
            suites="{suite} {suite}-updates",
            components="main contrib non-free non-free-firmware",
            signed_by="/usr/share/keyrings/debian-archive-keyring.pgp",
        ),
        AptSource(
            uris=FOUNDATION_APT_MIRRORS["raspberrypi"],
            suites="{suite}",
            components="main",
            signed_by="/usr/share/keyrings/raspberrypi-archive-keyring.pgp",
        ),
        AptSource(
            uris=FOUNDATION_APT_MIRRORS["security"],
            suites="{suite}-security",
            components="main contrib non-free non-free-firmware",
            signed_by="/usr/share/keyrings/debian-archive-keyring.pgp",
        ),
    ),
    apt_packages=FOUNDATION_PACKAGES,
    services=FOUNDATION_SERVICES,
    journal_persistence=JOURNAL_PERSISTENCE,
    journal_persistence_content=JOURNAL_PERSISTENCE_CONTENT,
    python_version=None,
    artifacts=FOUNDATION_ARTIFACTS,
)

UBUNTU_2604_RK3588 = AgentFoundationProfile(
    id="ubuntu-2604-rk3588-arm64-v1",
    architecture="aarch64",
    os_ids=("ubuntu",),
    os_versions=("26",),
    hardware_model_prefix="RK3588",
    minimum_memory_kib=15 * 1024 * 1024,
    minimum_disk_kib=12 * 1024 * 1024,
    apt_mirrors={"ubuntu": "https://mirror.nju.edu.cn/ubuntu-ports/"},
    apt_suite="resolute",
    apt_sources=(
        AptSource(
            uris="https://mirror.nju.edu.cn/ubuntu-ports/",
            suites="{suite} {suite}-updates {suite}-security",
            components="main restricted universe multiverse",
            signed_by="/usr/share/keyrings/ubuntu-archive-keyring.gpg",
        ),
    ),
    apt_packages=FOUNDATION_PACKAGES,
    services=FOUNDATION_SERVICES,
    journal_persistence=Path("/etc/systemd/journald.conf.d/99-eidolon-persistent.conf"),
    journal_persistence_content=RK3588_JOURNAL_PERSISTENCE_CONTENT,
    python_version="3.13.15",
    artifacts=(*FOUNDATION_ARTIFACTS, CPYTHON_3_13_AARCH64),
)

FOUNDATION_PROFILES: dict[str, AgentFoundationProfile] = {
    profile.id: profile for profile in (RASPBERRY_PI_OS_TRIXIE, UBUNTU_2604_RK3588)
}


FOUNDATION_VERSION_PREFIXES = {
    "nats-server": ("nats-server: v2.14.0", "v2.14.0"),
    "livekit-server": ("livekit-server version 1.11.0", "1.11.0"),
    "uv": ("uv 0.11.15",),
    "node": ("v22.23.2",),
    # `python3.13 --version` says "Python 3.13.15"; the bare number is what a
    # different build of the same version would also print, so both are here
    # for the same reason the others are.
    "python3.13": ("Python 3.13.15", "3.13.15"),
}

FOUNDATION_EVIDENCE = Path("/var/lib/eidolon-ops/foundation-v2.json")

FOUNDATION_CACHE = Path("/var/cache/eidolon/ops/artifacts")

FOUNDATION_LIBRARY = Path("/usr/local/lib/eidolon-foundation")

FOUNDATION_LOCK = Path("/run/lock/eidolon-foundation.lock")

LOCAL_BIN = Path("/usr/local/bin")

LOCAL_LIB = Path("/usr/local/lib")


#: What a capability adds to the artifacts above. The agent's own copy, for the
#: reason every other copy in this module exists: it runs on a Host with no
#: access to the operator's package, and a contract it cannot independently
#: derive is a contract it cannot refuse. A drift test in eidolon_ops keeps the
#: two equal.
CAPABILITY_FOUNDATION_ARTIFACTS: dict[str, tuple[dict[str, str], ...]] = {
    "local_llm": (
        {
            "artifact_id": "llama-server",
            "version": "b10865",
            "url": (
                "https://api.github.com/repos/ggml-org/llama.cpp/"
                "releases/assets/550738865"
            ),
            "sha256": "1e5f497d80aedba65481457a8fc86c5c0fc659d30bf696623e3ccd526cb4edf8",
            "kind": "tar-tree",
            "executable": "llama-server",
            "top_level": "llama-b10865",
        },
    ),
}


def declared_foundation_capabilities(payload: Mapping[str, object]) -> frozenset[str]:
    """What the operator says this Host can do, as the foundation contract has it.

    A payload naming none is one from before Hosts differed, and gets the
    baseline — so an older workstation keeps working against a newer agent.
    """

    value = payload.get("foundation")
    declared = value.get("capabilities", []) if isinstance(value, Mapping) else []
    if not isinstance(declared, list) or not all(isinstance(item, str) for item in declared):
        raise TargetError("foundation capabilities must be an array of strings")
    # The closed set is not restated here. A name this table has no artifacts
    # for adds none — and if the operator sent artifacts anyway, the contract
    # comparison below refuses the whole thing. Where a misspelt capability
    # actually matters is the install payload, and `contract.declared_
    # capabilities` checks it against the closed set there.
    return frozenset(declared)


def expected_foundation(
    profile: AgentFoundationProfile | None = None,
    capabilities: frozenset[str] = frozenset(),
) -> dict[str, object]:
    profile = profile or FOUNDATION_PROFILES[DEFAULT_FOUNDATION_PROFILE]
    return {
        "profile": profile.id,
        "architecture": profile.architecture,
        "os_ids": list(profile.os_ids),
        "os_versions": list(profile.os_versions),
        "apt_mirrors": dict(profile.apt_mirrors),
        "apt_packages": list(profile.apt_packages),
        "services": list(profile.services),
        "capabilities": sorted(capabilities),
        "artifacts": [
            dict(artifact)
            for artifact in (
                *profile.artifacts,
                *(
                    artifact
                    for capability in sorted(capabilities)
                    for artifact in CAPABILITY_FOUNDATION_ARTIFACTS.get(capability, ())
                ),
            )
        ],
        "journal_persistence": profile.journal_persistence_content,
    }


def requested_profile(payload: Mapping[str, object]) -> AgentFoundationProfile:
    """Which reviewed profile this payload claims to be, or refuse.

    Naming one the agent was not built for is refused before it is compared:
    the alternative is a mismatch reported as "differs from the reviewed
    profile", which reads as drift in a profile both sides know rather than as
    a Host asking for one this agent has never seen.

    A payload naming none is the Raspberry Pi, because every Host built before
    profiles were plural was that one and an older workstation has to keep
    working against a newer agent.
    """

    value = payload.get("foundation")
    named = value.get("profile") if isinstance(value, Mapping) else None
    if named is None:
        return FOUNDATION_PROFILES[DEFAULT_FOUNDATION_PROFILE]
    if not isinstance(named, str) or named not in FOUNDATION_PROFILES:
        known = ", ".join(sorted(FOUNDATION_PROFILES))
        raise TargetError(
            f"foundation profile is not one this agent was built for: {named!r}. It knows: {known}"
        )
    return FOUNDATION_PROFILES[named]


def foundation_contract(payload: Mapping[str, object]) -> dict[str, object]:
    value = payload.get("foundation")
    expected = expected_foundation(
        requested_profile(payload), declared_foundation_capabilities(payload)
    )
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


def foundation_platform_checks(
    profile: AgentFoundationProfile | None = None,
) -> dict[str, bool]:
    """Whether this machine is the board the profile describes.

    Three keys used to answer for the Raspberry Pi in their own names —
    ``raspberry_pi_hardware``, ``memory_at_least_8_gib``,
    ``disk_free_at_least_12_gib``. On a second board those are reports whose
    wording is wrong while their value is right, which is worse than either.
    They now name what is being checked and leave what it is checked against
    to the profile.
    """

    profile = profile or FOUNDATION_PROFILES[DEFAULT_FOUNDATION_PROFILE]
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
        "supported_architecture": machine in {profile.architecture, "arm64"},
        "supported_hardware": profile.hardware_model_prefix is None
        or model.startswith(profile.hardware_model_prefix),
        "supported_os": os_id in profile.os_ids,
        "supported_os_version": version in profile.os_versions,
        "systemd_pid1": init == "systemd",
        "sufficient_memory": memory_kib >= profile.minimum_memory_kib,
        "sufficient_disk": free_bytes // 1024 >= profile.minimum_disk_kib,
    }


def journal_is_persistent() -> bool:
    """Whether this Host will still know why something failed after a reboot.

    Asked of systemd's own merged configuration, by systemd's own precedence
    rule: every drop-in is concatenated in order and the last Storage= wins.
    That is the property a reboot preserves.

    Not asked of the journal on disk, which was the first attempt and was
    wrong. Reverting to volatile storage leaves the previously written files
    exactly where they were, so a Host that had stopped persisting still had a
    /var/log/journal/<machine-id>/system.journal to find — and since this
    check gates the foundation installer, a Host that lost its drop-in would
    have been declared healthy and never given it back. Verified on the real
    Pi by deleting the drop-in: effective Storage=volatile, stale files
    present, old check True.
    """

    result = primitives.run(
        ("/usr/bin/systemd-analyze", "cat-config", "systemd/journald.conf"),
        timeout=30,
    )
    if result.returncode != 0:
        return False
    storage = None
    for line in result.stdout.splitlines():
        stripped = line.strip()
        if stripped.startswith("Storage="):
            storage = stripped.split("=", 1)[1].strip()
    return storage == "persistent"


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
    profile = requested_profile(payload)
    contract = foundation_contract(payload)
    platform_checks = foundation_platform_checks(profile)
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
        and evidence.get("profile") == profile.id
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
        "profile": profile.id,
        "platform": platform_checks,
        "packages": packages,
        "artifacts": artifacts,
        "services": services,
        "evidence": evidence,
        "evidence_healthy": evidence_healthy,
    }
