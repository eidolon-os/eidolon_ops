"""The pinned host foundations, one per kind of board Ops installs onto.

Code-owned, deliberately. Changing a package, a download URL or a digest is a
supply-chain decision and belongs in a reviewed release change, not in a file
an operator edits — which is why this is a table in Python and not a TOML the
host config points at. What the operator chooses is *which* reviewed profile
their Host is; what that profile contains is not theirs to alter.

A second board is a second entry here. Everything that differs between boards
— which OS the bootstrap will accept, what hardware it insists on, the apt
sources it fetches from, the packages, the prebuilt binaries — is a field on
the profile rather than a module constant, so adding one is filling in a row
rather than finding every place the first board's answer was assumed.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path

FOUNDATION_ARCHITECTURE = "aarch64"
FOUNDATION_OS_IDS = ("debian", "raspbian")
FOUNDATION_OS_VERSIONS = ("13",)
APT_MIRRORS = {
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

FOUNDATION_SERVICES = (
    "bluetooth.service",
    "NetworkManager.service",
    "avahi-daemon.service",
)

#: Where the Host's own account of itself is kept, and how much of it.
#:
#: Raspberry Pi OS ships /usr/lib/systemd/journald.conf.d/40-rpi-volatile-storage.conf,
#: which sets Storage=volatile so an SD card is not worn out by logging. The
#: cost is that the journal lives in RAM and is gone at the next boot — so a
#: Host cannot say anything about a failure that preceded a restart, and the
#: usual first move in diagnosing one ("what did it say before it went?")
#: returns nothing. That is how the Channel worker's stall stayed
#: uninvestigated: by the time anyone looked, the evidence had been rebooted
#: away.
#:
#: Persistence belongs to the foundation rather than to a release: it has to
#: hold before anything is installed, and it must not be undone by rolling
#: back to an older release. The bounds are the other half of the decision —
#: the Pi OS default exists for a real reason, and an unbounded journal on a
#: finite card would be a different bug — so this trades a fixed, small slice
#: of the card for the ability to explain a failure after the fact.
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
        url="https://api.github.com/repos/nats-io/nats-server/releases/assets/409045586",
        sha256="ce7dc5f7d97b70dabc38b13157fed28d7d06227860676143c15c62c5c297996c",
        kind="tar-binary",
        executable="nats-server",
    ),
    FoundationArtifact(
        artifact_id="livekit-server",
        version="1.11.0",
        url="https://api.github.com/repos/livekit/livekit/releases/assets/398737306",
        sha256="6741466bc12e75544338292ab2c1c02c02f3c626568230b5548fffc53e5a87ff",
        kind="tar-binary",
        executable="livekit-server",
    ),
    FoundationArtifact(
        artifact_id="uv",
        version="0.11.15",
        url=(
            "https://files.pythonhosted.org/packages/af/50/"
            "4bc8a148274feabee2d9c9f1fa15009e10c0228dfe57981ee3ea2ef1d481/"
            "uv-0.11.15-py3-none-manylinux_2_17_aarch64."
            "manylinux2014_aarch64.musllinux_1_1_aarch64.whl"
        ),
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


@dataclass(frozen=True, slots=True)
class AptSource:
    """One stanza of the deb822 sources list the bootstrap writes."""

    uris: str
    #: May contain ``{suite}``, which the profile's suite fills in.
    suites: str
    components: str
    signed_by: str


@dataclass(frozen=True, slots=True)
class FoundationProfile:
    """One reviewed board: what it is, and what a Host of it must have.

    Every field is something the first board answered implicitly. A second
    board answers them differently — a different OS gate, no Raspberry Pi in
    /proc/device-tree/model, Ubuntu's archives instead of Debian's — and the
    point of naming them is that it then answers them all, rather than
    inheriting whichever ones nobody noticed were assumptions.
    """

    id: str
    #: What the bootstrap's refusals call this board, in an operator's words.
    display_name: str
    architecture: str
    os_ids: tuple[str, ...]
    os_versions: tuple[str, ...]
    #: Shell glob matched against /proc/device-tree/model. ``None`` where the
    #: OS gate is the whole check and the board is not identifiable that way.
    hardware_model_match: str | None
    #: What that glob means, for the refusal message.
    hardware_display_name: str | None
    #: What the bootstrap refuses to run on. Below these a Host would install
    #: and then fail later, further from the cause.
    minimum_memory_kib: int
    minimum_disk_kib: int
    #: How those two thresholds are said out loud when one is not met.
    minimum_memory_label: str
    minimum_disk_label: str
    apt_suite: str
    apt_mirrors: dict[str, str]
    apt_sources: tuple[AptSource, ...]
    apt_packages: tuple[str, ...]
    bootstrap_packages: tuple[str, ...]
    services: tuple[str, ...]
    journal_persistence: Path
    journal_persistence_content: str
    artifacts: tuple[FoundationArtifact, ...]


RASPBERRY_PI_OS_TRIXIE = FoundationProfile(
    id="raspberry-pi-os-debian-arm64-v2",
    display_name="Debian/Raspberry Pi OS",
    architecture="aarch64",
    os_ids=("debian", "raspbian"),
    os_versions=("13",),
    hardware_model_match="Raspberry\\ Pi*",
    hardware_display_name="Raspberry Pi",
    minimum_memory_kib=7340032,
    minimum_disk_kib=12582912,
    minimum_memory_label="8 GiB-class RAM",
    minimum_disk_label="12 GiB free disk",
    apt_suite="trixie",
    apt_mirrors=APT_MIRRORS,
    apt_sources=(
        AptSource(
            uris=APT_MIRRORS["debian"],
            suites="{suite} {suite}-updates",
            components="main contrib non-free non-free-firmware",
            signed_by="/usr/share/keyrings/debian-archive-keyring.pgp",
        ),
        AptSource(
            uris=APT_MIRRORS["raspberrypi"],
            suites="{suite}",
            components="main",
            signed_by="/usr/share/keyrings/raspberrypi-archive-keyring.pgp",
        ),
        AptSource(
            uris=APT_MIRRORS["security"],
            suites="{suite}-security",
            components="main contrib non-free non-free-firmware",
            signed_by="/usr/share/keyrings/debian-archive-keyring.pgp",
        ),
    ),
    apt_packages=APT_PACKAGES,
    bootstrap_packages=BOOTSTRAP_PACKAGES,
    services=FOUNDATION_SERVICES,
    journal_persistence=JOURNAL_PERSISTENCE,
    journal_persistence_content=JOURNAL_PERSISTENCE_CONTENT,
    artifacts=FOUNDATION_ARTIFACTS,
)


#: Armbian sets Storage=volatile in /etc/systemd/journald.conf itself rather
#: than in a vendor drop-in, and this board proves what that costs: after a
#: reboot `journalctl --list-boots` lists only the current one. Verified on the
#: board that a drop-in in /etc/systemd/journald.conf.d/ is parsed after the
#: main file and wins. The 99- prefix rather than the Pi's 50- is also from
#: that check: a vendor drop-in named syslog.conf sorts after 50- and would
#: take precedence if it ever set Storage.
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


#: Orange Pi 5 Max and its kin. Every value below was read off the board or
#: fetched and checked, not carried across from the Raspberry Pi by assumption:
#: `ID=ubuntu VERSION_ID=26.04` and `RK3588 OPi 5 Max` come from the board,
#: all 35 apt packages were confirmed available under this suite by name, the
#: three foundation services are present and enabled, and the mirror was
#: fetched to confirm it carries resolute, resolute-updates and
#: resolute-security for arm64.
UBUNTU_2604_RK3588 = FoundationProfile(
    id="ubuntu-2604-rk3588-arm64-v1",
    display_name="Ubuntu 26.04 on RK3588",
    architecture="aarch64",
    os_ids=("ubuntu",),
    #: The bootstrap compares the major version, so 26.04 is "26".
    os_versions=("26",),
    hardware_model_match="RK3588*",
    hardware_display_name="RK3588",
    #: The 16 GB variant is the one measured; an 8 GB RK3588 exists and has
    #: not been tried with the local models, so the floor admits what was
    #: verified and refuses what was not. The board reports 16342360 kB.
    minimum_memory_kib=15728640,
    #: The Pi's figure, carried deliberately: the NPU model artifacts are not
    #: declared yet, and this has to rise when they are.
    minimum_disk_kib=12582912,
    minimum_memory_label="16 GiB-class RAM",
    minimum_disk_label="12 GiB free disk",
    apt_suite="resolute",
    #: Same mirror family as the reviewed Raspberry Pi profile, and confirmed
    #: to carry this suite: Ubuntu's arm64 archive is ubuntu-ports, which
    #: ports.ubuntu.com also serves.
    apt_mirrors={"ubuntu": "https://mirror.nju.edu.cn/ubuntu-ports/"},
    apt_sources=(
        AptSource(
            uris="https://mirror.nju.edu.cn/ubuntu-ports/",
            #: One stanza, unlike Debian's three: Ubuntu serves updates and
            #: security out of the same archive.
            suites="{suite} {suite}-updates {suite}-security",
            components="main restricted universe multiverse",
            signed_by="/usr/share/keyrings/ubuntu-archive-keyring.gpg",
        ),
    ),
    apt_packages=APT_PACKAGES,
    bootstrap_packages=BOOTSTRAP_PACKAGES,
    services=FOUNDATION_SERVICES,
    journal_persistence=Path("/etc/systemd/journald.conf.d/99-eidolon-persistent.conf"),
    journal_persistence_content=RK3588_JOURNAL_PERSISTENCE_CONTENT,
    #: The same four prebuilt binaries. All are aarch64 and none is
    #: Debian-specific; the NPU runtime is deliberately not here — it belongs
    #: to the component that loads it, gated on the rknpu2 capability, so a
    #: board that runs no local models does not carry it.
    artifacts=FOUNDATION_ARTIFACTS,
)


#: Every board Ops will install onto, by the id a host config names.
FOUNDATION_PROFILES: dict[str, FoundationProfile] = {
    profile.id: profile for profile in (RASPBERRY_PI_OS_TRIXIE, UBUNTU_2604_RK3588)
}


def foundation_profile(profile_id: str) -> FoundationProfile:
    """The reviewed profile a host config named, or refuse with the choices."""

    try:
        return FOUNDATION_PROFILES[profile_id]
    except KeyError:
        known = ", ".join(sorted(FOUNDATION_PROFILES))
        raise KeyError(f"foundation.profile must be a reviewed profile: {known}") from None


def foundation_payload(
    profile: FoundationProfile = RASPBERRY_PI_OS_TRIXIE,
) -> dict[str, object]:
    """Return the exact target-side contract, with no operator-controlled URL.

    The shape is the Host agent's wire contract and does not vary by profile;
    only the values in it do.
    """

    return {
        "profile": profile.id,
        "architecture": profile.architecture,
        "os_ids": list(profile.os_ids),
        "os_versions": list(profile.os_versions),
        "apt_mirrors": dict(profile.apt_mirrors),
        "apt_packages": list(profile.apt_packages),
        "services": list(profile.services),
        # Carried like every other reviewed foundation fact, and held on the
        # Host as well so it can refuse a payload that differs from the profile
        # it was built against. test_foundation.py keeps the two equal.
        "journal_persistence": profile.journal_persistence_content,
        "artifacts": [asdict(artifact) for artifact in profile.artifacts],
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


#: Kept out of the bootstrap template so the profile that has no hardware to
#: check simply contributes nothing, rather than the template growing a branch.
_HARDWARE_GATE = """model=$(tr -d '\000' </proc/device-tree/model 2>/dev/null || true)
case \"$model\" in
  {match}) ;;
  *) echo 'foundation bootstrap requires {name} hardware' >&2; exit 1 ;;
esac
"""


def _apt_sources(profile: FoundationProfile) -> str:
    """The deb822 stanzas, as the printf argument list the script uses."""

    lines: list[str] = []
    for position, source in enumerate(profile.apt_sources):
        if position:
            lines.append("''")
        # Suites keeps the shell's own $suite rather than the resolved value:
        # the script sets it once above, and reading it here is what shows a
        # reviewer that the stanzas and the suite cannot drift apart.
        suites = source.suites.format(suite="$suite")
        lines.append("'Types: deb'")
        lines.append(f"'URIs: {source.uris}'")
        lines.append(f'"Suites: {suites}"')
        lines.append(f"'Components: {source.components}'")
        lines.append(f"'Signed-By: {source.signed_by}'")
    # The format string carries a real newline, and each argument three
    # spaces, because that is exactly what the hand-written template produced
    # once Python collapsed its line continuations. Byte-identical output is
    # what says this refactor changed no board's bootstrap.
    return "printf '%s\n'" + "".join(f"   {line}" for line in lines)


def python_bootstrap_script(
    profile: FoundationProfile = RASPBERRY_PI_OS_TRIXIE,
) -> bytes:
    """The script that makes a bare board able to be provisioned.

    Every gate below is the profile's, not this board's: the OS it accepts,
    the hardware it insists on, the archives it fetches from and the space it
    needs. A board that answers any of them differently gets a profile, not a
    branch in here.
    """

    packages = " ".join(profile.bootstrap_packages)
    apt_options = " ".join(APT_COMMAND_OPTIONS)
    os_gate = "|".join(
        f"{os_id}:{version}" for os_id in profile.os_ids for version in profile.os_versions
    )
    hardware_gate = (
        _HARDWARE_GATE.format(
            match=profile.hardware_model_match, name=profile.hardware_display_name
        )
        if profile.hardware_model_match
        else ""
    )
    sources = _apt_sources(profile)
    return f"""#!/bin/sh
set -eu
if [ \"$(uname -s)\" != Linux ]; then
  echo 'foundation bootstrap requires Linux' >&2
  exit 1
fi
case \"$(uname -m)\" in
  {profile.architecture}|arm64) ;;
  *) echo 'foundation bootstrap requires {profile.architecture}' >&2; exit 1 ;;
esac
if [ ! -r /etc/os-release ]; then
  echo 'foundation bootstrap requires /etc/os-release' >&2
  exit 1
fi
. /etc/os-release
version_major=${{VERSION_ID%%.*}}
case \"${{ID:-}}:${{version_major:-}}\" in
  {os_gate}) ;;
  *) echo 'foundation bootstrap requires reviewed {profile.display_name}' >&2; exit 1 ;;
esac
if [ \"$(cat /proc/1/comm 2>/dev/null || true)\" != systemd ]; then
  echo 'foundation bootstrap requires systemd as PID 1' >&2
  exit 1
fi
{hardware_gate}memory_kib=$(awk '/^MemTotal:/ {{print $2}}' /proc/meminfo)
if [ \"${{memory_kib:-0}}\" -lt {profile.minimum_memory_kib} ]; then
  echo 'foundation bootstrap requires at least {profile.minimum_memory_label}' >&2
  exit 1
fi
disk_kib=$(df -Pk / | awk 'NR == 2 {{print $4}}')
if [ \"${{disk_kib:-0}}\" -lt {profile.minimum_disk_kib} ]; then
  echo 'foundation bootstrap requires at least {profile.minimum_disk_label}' >&2
  exit 1
fi
export DEBIAN_FRONTEND=noninteractive
suite={profile.apt_suite}
apt_root=$(mktemp -d)
trap 'rm -rf "$apt_root"' EXIT
mkdir -p "$apt_root/parts" "$apt_root/lists/partial"
{sources}   >"$apt_root/eidolon.sources"
apt_source_options="-o Dir::Etc::sourcelist=$apt_root/eidolon.sources -o Dir::Etc::sourceparts=$apt_root/parts -o Dir::State::lists=$apt_root/lists"
apt-get {apt_options} $apt_source_options update
apt-get {apt_options} $apt_source_options install -y --no-install-recommends {packages}
test -x /usr/bin/python3
printf '{{"status":"bootstrapped","python3":true}}\\n'
""".encode()
