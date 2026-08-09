"""Pinned Raspberry Pi host foundation contract.

The profile is deliberately code-owned: changing a package, download URL, or
digest is a reviewed release change, not an operator-side configuration tweak.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass

FOUNDATION_PROFILE = "raspberry-pi-os-debian-arm64-v1"
FOUNDATION_ARCHITECTURE = "aarch64"
FOUNDATION_OS_IDS = ("debian", "raspbian")
FOUNDATION_OS_VERSIONS = ("12", "13")

BOOTSTRAP_PACKAGES = (
    "ca-certificates",
    "curl",
    "python3",
    "python3-pip",
)

APT_PACKAGES = (
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

FOUNDATION_SERVICES = (
    "bluetooth.service",
    "NetworkManager.service",
    "avahi-daemon.service",
)


@dataclass(frozen=True, slots=True)
class FoundationArtifact:
    artifact_id: str
    version: str
    url: str
    sha256: str
    kind: str
    executable: str


FOUNDATION_ARTIFACTS = (
    FoundationArtifact(
        artifact_id="nats-server",
        version="2.14.0",
        url=(
            "https://github.com/nats-io/nats-server/releases/download/"
            "v2.14.0/nats-server-v2.14.0-linux-arm64.tar.gz"
        ),
        sha256="ce7dc5f7d97b70dabc38b13157fed28d7d06227860676143c15c62c5c297996c",
        kind="tar-binary",
        executable="nats-server",
    ),
    FoundationArtifact(
        artifact_id="livekit-server",
        version="1.11.0",
        url=(
            "https://github.com/livekit/livekit/releases/download/"
            "v1.11.0/livekit_1.11.0_linux_arm64.tar.gz"
        ),
        sha256="6741466bc12e75544338292ab2c1c02c02f3c626568230b5548fffc53e5a87ff",
        kind="tar-binary",
        executable="livekit-server",
    ),
    FoundationArtifact(
        artifact_id="uv",
        version="0.11.15",
        url="https://pypi.org/project/uv/0.11.15/",
        sha256="c0cf52cd6d50bb9e05e2d968f45f80761107e4cbc8d4a26d9758f9d8274aaec1",
        kind="pip-wheel",
        executable="uv",
    ),
    FoundationArtifact(
        artifact_id="node",
        version="22.23.2",
        url="https://nodejs.org/dist/v22.23.2/node-v22.23.2-linux-arm64.tar.xz",
        sha256="fff4078c5def658577f92c88db7db3bc0072924bfb93fe52c1e744a54e94abb8",
        kind="node-tar",
        executable="node",
    ),
)


def foundation_payload() -> dict[str, object]:
    """Return the exact target-side contract, with no operator-controlled URL."""

    return {
        "profile": FOUNDATION_PROFILE,
        "architecture": FOUNDATION_ARCHITECTURE,
        "os_ids": list(FOUNDATION_OS_IDS),
        "os_versions": list(FOUNDATION_OS_VERSIONS),
        "apt_packages": list(APT_PACKAGES),
        "services": list(FOUNDATION_SERVICES),
        "artifacts": [asdict(artifact) for artifact in FOUNDATION_ARTIFACTS],
    }


def python_probe_script() -> bytes:
    return b"""#!/bin/sh
set -eu
if [ -x /usr/bin/python3 ]; then
  printf '{"python3":true}\n'
else
  printf '{"python3":false}\n'
fi
"""


def python_bootstrap_script() -> bytes:
    packages = " ".join(BOOTSTRAP_PACKAGES)
    return f"""#!/bin/sh
set -eu
if [ \"$(uname -s)\" != Linux ]; then
  echo 'foundation bootstrap requires Linux' >&2
  exit 1
fi
case \"$(uname -m)\" in
  aarch64|arm64) ;;
  *) echo 'foundation bootstrap requires aarch64' >&2; exit 1 ;;
esac
if [ ! -r /etc/os-release ]; then
  echo 'foundation bootstrap requires /etc/os-release' >&2
  exit 1
fi
. /etc/os-release
version_major=${{VERSION_ID%%.*}}
case \"${{ID:-}}:${{version_major:-}}\" in
  debian:12|debian:13|raspbian:12|raspbian:13) ;;
  *) echo 'foundation bootstrap requires reviewed Debian/Raspberry Pi OS' >&2; exit 1 ;;
esac
if [ \"$(cat /proc/1/comm 2>/dev/null || true)\" != systemd ]; then
  echo 'foundation bootstrap requires systemd as PID 1' >&2
  exit 1
fi
model=$(tr -d '\000' </proc/device-tree/model 2>/dev/null || true)
case \"$model\" in
  Raspberry\\ Pi*) ;;
  *) echo 'foundation bootstrap requires Raspberry Pi hardware' >&2; exit 1 ;;
esac
memory_kib=$(awk '/^MemTotal:/ {{print $2}}' /proc/meminfo)
if [ \"${{memory_kib:-0}}\" -lt 7340032 ]; then
  echo 'foundation bootstrap requires at least 8 GiB-class RAM' >&2
  exit 1
fi
disk_kib=$(df -Pk / | awk 'NR == 2 {{print $4}}')
if [ \"${{disk_kib:-0}}\" -lt 12582912 ]; then
  echo 'foundation bootstrap requires at least 12 GiB free disk' >&2
  exit 1
fi
export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y --no-install-recommends {packages}
test -x /usr/bin/python3
printf '{{"status":"bootstrapped","python3":true}}\\n'
""".encode()
