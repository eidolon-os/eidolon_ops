"""What Ops requires of a board, and the two ways that requirement reaches one.

`BringUp` is what has to be true before Ops can operate a board at all, stated
in Ops's own terms and derived from the Host profile. No file name, partition,
interface or command appears in it. That is the whole point: the requirement is
a reviewed product decision, and it used to live only in whoever last typed it
into an imager's dialog.

Only one thing varies, and it is not the requirement — it is the channel the
requirement travels down, which the board's own state decides for you:

    a board that has never booted   ── its boot medium, written from elsewhere
    a board that is already running ── a shell on it, however you got one

Those are the only two, because they are the only two surfaces a board has
before Ops has an account on it. Pre-boot means a medium; post-boot means a
shell. Which medium (card, SSD, eMMC) and which shell (SSH, HDMI, serial) do
not reach this far — that is the operator's business, and pinning the design to
one of them was what made this inflexible in the first place.

Both forms are a platform's own convention: the same OS decides its first-boot
hook *and* what commands configure a running system. So a platform declares
both, together, keyed by `foundation.profile` — the same id `foundation.py`
keys its packages and pinned artifacts by, refused the same way when unknown.
That id carries the OS and a revision, so an OS that changes either convention
is a new foundation with a new declaration while boards on the old one keep
working.

YAML and shell are emitted as text. This package deliberately has no runtime
YAML implementation — see the note beside PyYAML in `pyproject.toml` — and the
tests check what is rendered with a real parser and a real shell.
"""

from __future__ import annotations

import json
import shlex
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

from eidolon_ops.errors import OperationsError

#: The two channels a board has before Ops has an account on it. Its own state
#: chooses: a board that has never booted has only its medium, one that is
#: running has only a shell.
Via = Literal["boot-medium", "shell"]
BOOT_MEDIUM: Via = "boot-medium"
SHELL: Via = "shell"

#: What the rendered shell delivery is called when written out.
SCRIPT = "eidolon-bring-up.sh"

#: cloud-init's file names on a NoCloud seed. Its choice, not ours.
USER_DATA = "user-data"
META_DATA = "meta-data"
NETWORK_CONFIG = "network-config"


@dataclass(frozen=True, slots=True)
class BringUp:
    """What has to be true of a board before Ops can operate it at all.

    Each of these exists because something in Ops requires it, and none of
    them names a platform:

    * ``hostname`` — `endpoints.py` resolves the Host by the name it publishes,
      and `transport.py` trusts its host key under that name via HostKeyAlias.
    * ``user`` — the account the transport connects as.
    * ``authorized_keys`` — every operator public key this Host accepts. The
      transport is ``BatchMode=yes`` with an explicit identity, so a key the
      board was not given is a board that operator cannot reach.
    * non-interactive sudo for that account — the transport elevates with
      ``sudo --non-interactive``.
    * sshd and mDNS running — the transport at all, and name resolution.
    * a wired link that holds an address on a point-to-point cable and takes
      a lease on a real network — the wired-upload gate, the flapping link
      that gate was diagnosing, and the route out `provision` needs.

    The last three are properties of the rendered payload rather than fields:
    every renderer must produce them, and a renderer that does not is wrong in
    a way its own platform's validator will not catch.

    The Host's own Wi-Fi is deliberately absent. The product configures it
    over BLE from the phone, so rendering it here would be a second path to
    one fact — and would make Ops hold a credential for something it does not
    own.
    """

    hostname: str
    user: str
    authorized_keys: tuple[str, ...]

    def __post_init__(self) -> None:
        # The one value worth checking here: the rest comes from a profile this
        # package already parsed, while an unusable key is a board that boots
        # perfectly and lets nobody in.
        if not self.authorized_keys:
            raise OperationsError(
                "this Host accepts no operator keys, which is a board nobody can log into"
            )
        for key in self.authorized_keys:
            if not key.startswith(("ssh-", "ecdsa-", "sk-")) or "\n" in key:
                raise OperationsError(
                    f"{key[:40]!r} is not an OpenSSH public key line. These are what the "
                    "Host will accept, so an unusable value here is a board an operator "
                    "cannot log into."
                )

    @property
    def short_hostname(self) -> str:
        """The name without the suffix Avahi appends for itself.

        The profile addresses the Host as ``<name>.local`` because that is what
        resolves; the Host's own hostname is the label.
        """

        return self.hostname.removesuffix(".local")


def read_operator_keys(text: str, *, label: str) -> tuple[str, ...]:
    """The operator public keys this Host accepts, from an authorized_keys file.

    A list rather than one key, and a declaration rather than a machine-local
    file, because two facts were living in `host.identity_file`: which private
    key *this* machine connects with, and which key the *board* accepts. While
    they were one field the board could only ever trust one key, so a second
    operator had to be handed somebody's private key — the one thing least
    worth copying, and it takes the audit trail with it.

    Split, each operator keeps their own private key and the Host's accepted
    set is a reviewed product decision: adding a person is a line, removing
    one is a line, and the board's own `authorized_keys` shows how many people
    can reach it. Public keys are public, so this file belongs in the
    repository — that is the point, not an oversight.

    Read in authorized_keys format because that is the format the board wants;
    there is no translation to get wrong, and a review reads as one line per
    person.
    """

    keys: list[str] = []
    for number, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if not line.startswith(("ssh-", "ecdsa-", "sk-")):
            raise OperationsError(
                f"{label}:{number} is not an OpenSSH public key line. This file is the set "
                "of operators who can reach this Host; a line that is not a key is either "
                "a mistake or an operator who silently will not be able to."
            )
        keys.append(line)
    if not keys:
        raise OperationsError(
            f"{label} declares no operator keys. Every key that can reach this Host is "
            "named here, so an empty set is a board nobody can log into."
        )
    return tuple(keys)


@dataclass(frozen=True, slots=True)
class Platform:
    """How one platform's board is told what Ops requires of it, both ways.

    Declared together rather than in two registries, because they are two
    expressions of one thing and a platform that has only one of them is a
    platform Ops can reach in only one situation. Keeping them side by side is
    what makes that visible instead of discovered.
    """

    #: Files for a boot medium, keyed by the name each must be written under.
    boot_medium: Callable[[BringUp], dict[str, str]]
    #: One script to run as root on the running board.
    shell: Callable[[BringUp], str]


def render(bring_up: BringUp, *, foundation: str, via: Via) -> dict[str, str]:
    """The requirement, expressed for one channel, as files to write out.

    The shell delivery is one file too. A caller that has an output directory
    and a set of names to write in it does not need to know which channel it
    asked for, and neither does the plan.
    """

    try:
        platform = PLATFORMS[foundation]
    except KeyError:
        declared = ", ".join(sorted(PLATFORMS))
        raise OperationsError(
            f"no bring-up is declared for foundation {foundation!r}. Declared: {declared}. "
            "Both forms are a platform's own convention — its first-boot hook and its "
            "schema, and the commands that configure a running system — so a board whose "
            "foundation is not among these gets its own declaration rather than another "
            "platform's guessed at."
        ) from None
    if via == BOOT_MEDIUM:
        return platform.boot_medium(bring_up)
    return {SCRIPT: platform.shell(bring_up)}


def _raspberry_pi_os_trixie_medium(bring_up: BringUp) -> dict[str, str]:
    """Raspberry Pi OS 13's first boot: cloud-init, seeded from the boot partition.

    Established by asking this OS rather than remembering an older one, and
    both of these fail silently otherwise:

    The image seeds cloud-init from the boot partition (`ds=nocloud` in
    `cmdline.txt`); there is no `firstrun.sh` any more. The payload is three
    files, and they are the same three an imager writes, so writing them
    replaces the imager's — the intent, not a conflict.

    `instance-id` is cloud-init's and `meta-data` carries it because a NoCloud
    seed needs the file. It is derived from the Host's name and therefore
    stable: on a freshly flashed card the value cannot matter, since
    `/var/lib/cloud` is empty and every module runs regardless. A fresh id per
    rendering would buy nothing there while costing reproducibility and making
    "silently reconfigure a card that already booted" the default.

    Two properties change what the image would have done with the network,
    and both were measured on a board rather than reasoned about. Everything
    else is the image's own default, restated so the deviation is visible.

    `ipv4.link-local=4` is NetworkManager's "fallback". On a point-to-point
    cable nothing answers DHCP, and by default NetworkManager fails the
    connection and retries — a teardown loop that is the Host answering in
    bursts and dropping every session. With fallback it assigns a link-local
    address and holds it: 169.254/16, then 30 consecutive probes over 90
    seconds with none lost.

    `ipv6.method=link-local` is what lets the connection finish activating.
    Changing that one property and nothing else moved the device from
    "connecting (getting IP configuration)", where it stayed indefinitely, to
    "connected" — a cable carries no router advertisement and no DHCPv6 for
    `auto` to wait for. IPv4 worked either way, which is exactly why this is
    the kind of thing only a board can tell you.

    It is not free to leave out, and it is not needed at runtime either. This
    workstation reaches a wired endpoint only over that cable — on a shared
    LAN the board's address belongs to Wi-Fi as far as `endpoints.py` is
    concerned — and `require_wired_release_upload` makes a wired endpoint
    mandatory for a release. A shipped Host has no cable, no carrier, and
    therefore no activated connection: `optional: true` and an absent link
    cost it nothing.

    `never-default` is deliberately not set: a link-local address has no
    gateway and cannot take a default route, while setting it would stop the
    cable being the route out on a real network.

    The passthrough needs an explicit renderer. netplan's own `link-local` key does not round-trip here — setting
    `ipv4.method=link-local` on a running Host leaves it absent from `netplan
    get` while NetworkManager holds it — and netplan refuses a device carrying
    `networkmanager` settings without a renderer: "networkmanager backend
    settings found but renderer is not NetworkManager". The image's global
    renderer file is not in scope when the seed is parsed alone, so inheriting
    it would produce a card netplan will not read.
    """

    #: This platform's name for the board's built-in Ethernet.
    interface = "eth0"
    name = bring_up.short_hostname
    return {
        USER_DATA: (
            "#cloud-config\n"
            "# Rendered by eidolon-ops from the Host profile.\n"
            f"hostname: {name}\n"
            "manage_etc_hosts: true\n"
            "preserve_hostname: false\n"
            # Declared rather than assumed of the image: Ops reaches the Host
            # by the name it publishes, so no Avahi is no Host.
            "packages:\n- avahi-daemon\n"
            "user:\n"
            f"  name: {bring_up.user}\n"
            "  shell: /bin/bash\n"
            "  groups: [sudo]\n"
            '  sudo: ["ALL=(ALL) NOPASSWD:ALL"]\n'
            "  ssh_authorized_keys:\n"
            + "".join(f"  - {json.dumps(key)}\n" for key in bring_up.authorized_keys)
            +
            # No console password is rendered, so the account has none rather
            # than a guessable one. The operation's report says what that costs.
            "  lock_passwd: true\n"
            # The deploy key is installed in the same breath, so password
            # authentication would only be a second, weaker way in.
            "ssh_pwauth: false\n"
            "runcmd:\n"
            "- [systemctl, enable, --now, ssh]\n"
            "- [systemctl, enable, --now, avahi-daemon]\n"
        ),
        META_DATA: f"instance-id: eidolon-ops-{name}\n",
        NETWORK_CONFIG: (
            "# Rendered by eidolon-ops from the Host profile.\n"
            "network:\n"
            "  version: 2\n"
            "  ethernets:\n"
            f"    {interface}:\n"
            "      renderer: NetworkManager\n"
            # The image's own defaults, restated so the two properties Ops
            # does change are visible rather than buried in a rewrite.
            "      dhcp4: true\n"
            "      optional: true\n"
            "      networkmanager:\n"
            "        passthrough:\n"
            # 4 is NetworkManager's "fallback": try DHCP, and assign a
            # link-local address if nothing answers.
            '          ipv4.link-local: "4"\n'
            # Not carried over from an earlier design — measured. With
            # `ipv6.method=auto` the device sits in "connecting (getting IP
            # configuration)" indefinitely, because a point-to-point cable
            # carries no router advertisement and no DHCPv6 to wait for.
            '          ipv6.method: "link-local"\n'
        ),
    }


def _raspberry_pi_os_trixie_shell(bring_up: BringUp) -> str:
    """The same requirement, applied to a board that is already running.

    This is the channel for every board the boot medium cannot reach: one that
    boots from an SSD the workstation cannot mount, one somebody else flashed
    with something else, one that is not on this desk. It asks for a shell and
    nothing more, so how the operator got that shell — SSH, HDMI, a serial
    cable — never reaches this code.

    Ops does not run it. The transport is ``BatchMode=yes`` with an explicit
    identity, so it cannot log into a board that does not yet have that key —
    which is the situation. What would close that gap is Ops holding a
    password, and it should not: the credential that opens a board for the
    first time is the operator's, used by the operator.

    Written to be run twice. Every step is a state, not an edit: an account
    that exists is left alone, and `authorized_keys` is reconciled to exactly
    the declared operator set.

    Reconciled, not appended. Append-only was safe while there was one key,
    but it makes removing an operator impossible — a line deleted from the
    declaration would never reach the board. So this file is Ops's: it equals
    the declared set, and the script prints any line it dropped. A personal
    key someone left in the Ops account's `authorized_keys` was never
    sanctioned by the declaration, and losing it is the point of having one.

    The network comes last on purpose. `nmcli con up` on the interface a
    session is running over drops that session, and by then everything else
    has already been applied.
    """

    user = shlex.quote(bring_up.user)
    name = shlex.quote(bring_up.short_hostname)
    keys = "\n".join(bring_up.authorized_keys)
    return f"""#!/bin/sh
# Rendered by eidolon-ops from the Host profile. Run as root on the board:
#
#     sudo sh {SCRIPT}
#
# Safe to run again. Reports at the end what Ops will find.
set -eu

USER={user}
HOSTNAME={name}
KEYS=$(cat <<'EIDOLON_OPERATOR_KEYS'
{keys}
EIDOLON_OPERATOR_KEYS
)

[ "$(id -u)" = 0 ] || {{ echo "run this as root" >&2; exit 1; }}

# The name Ops resolves the Host by, and trusts its host key under.
hostnamectl set-hostname "$HOSTNAME"
grep -q "[[:space:]]$HOSTNAME\\$" /etc/hosts || printf '127.0.1.1\t%s\n' "$HOSTNAME" >> /etc/hosts

# The account the transport connects as. Left alone if it is already there.
id -u "$USER" >/dev/null 2>&1 || useradd --create-home --shell /bin/bash "$USER"
usermod --append --groups sudo "$USER"
HOME_DIR=$(getent passwd "$USER" | cut -d: -f6)

# The declared operator set. This file is Ops's, so it is made equal to the
# declaration rather than added to — otherwise a line removed from the
# declaration would never reach the board, and nobody could be revoked.
install -d -m 700 -o "$USER" -g "$USER" "$HOME_DIR/.ssh"
AUTHORIZED="$HOME_DIR/.ssh/authorized_keys"
touch "$AUTHORIZED"
printf '%s\\n' "$KEYS" | while IFS= read -r line; do
  [ -n "$line" ] || continue
  grep -qxF "$line" "$AUTHORIZED" || echo "adding operator key: $(echo "$line" | cut -c1-40)..."
done
while IFS= read -r line; do
  [ -n "$line" ] || continue
  printf '%s\\n' "$KEYS" | grep -qxF "$line" || echo "removing key not in the declaration: $(echo "$line" | cut -c1-40)..."
done < "$AUTHORIZED"
printf '%s\\n' "$KEYS" > "$AUTHORIZED"
chown "$USER:$USER" "$AUTHORIZED"
chmod 600 "$AUTHORIZED"

# Non-interactive sudo. The transport elevates with `sudo --non-interactive`
# and stops at the first elevation without it.
printf '%s ALL=(ALL) NOPASSWD:ALL\n' "$USER" > /etc/sudoers.d/"$USER"
chmod 440 /etc/sudoers.d/"$USER"
visudo -c -q -f /etc/sudoers.d/"$USER"

# sshd, and the mDNS that lets Ops find the Host by name at all.
command -v avahi-daemon >/dev/null 2>&1 || {{ apt-get update && apt-get install -y avahi-daemon; }}
systemctl enable --now ssh avahi-daemon

# The wired link, last: this drops a session running over it. `link-local
# fallback` keeps the connection up on a point-to-point cable where nothing
# answers DHCP, and `ipv6.method link-local` is what lets it finish activating
# there — both measured on a board rather than reasoned about.
DEVICE=$(nmcli -t -f DEVICE,TYPE dev status | awk -F: '$2=="ethernet"{{print $1; exit}}')
[ -n "$DEVICE" ] || {{ echo "no ethernet device found" >&2; exit 1; }}
CONNECTION=$(nmcli -t -f NAME,DEVICE con show | awk -F: -v d="$DEVICE" '$2==d{{print $1; exit}}')
[ -n "$CONNECTION" ] || {{
  nmcli con add type ethernet ifname "$DEVICE" con-name eidolon-wired
  CONNECTION=eidolon-wired
}}
nmcli con modify "$CONNECTION" ipv4.method auto ipv4.link-local fallback ipv6.method link-local
if ! nmcli con up "$CONNECTION"; then
  echo "configuration written; wired activation failed or disconnected; reconnect and rerun to verify" >&2
  exit 75
fi

echo "===== what Ops will find ====="
hostname
id -un "$USER" >/dev/null 2>&1 && echo "account: $USER"
su -s /bin/sh "$USER" -c 'sudo -n -u root true' || {{ echo "sudo: target account cannot elevate" >&2; exit 1; }}
echo "sudo: non-interactive root elevation verified"
echo "operator keys: $(grep -c . "$AUTHORIZED") accepted"
for SERVICE in ssh avahi-daemon; do
  systemctl is-active --quiet "$SERVICE" || {{ echo "$SERVICE is not active" >&2; exit 1; }}
done
ip -4 -o addr show dev "$DEVICE" | grep -q ' inet ' || {{ echo "wired interface has no IPv4 address" >&2; exit 1; }}
ip -4 -br addr show "$DEVICE"
"""


#: Which platforms Ops knows how to bring up, and in which forms. Keyed by
#: `foundation.profile` so an OS that changes either convention is a new
#: foundation with a new declaration, not an edit to this one.
PLATFORMS = {
    "raspberry-pi-os-debian-arm64-v2": Platform(
        boot_medium=_raspberry_pi_os_trixie_medium,
        shell=_raspberry_pi_os_trixie_shell,
    )
}
