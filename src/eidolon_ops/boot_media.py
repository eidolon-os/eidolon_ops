"""What a freshly flashed card is told, so a board comes up SSH-ready anywhere.

Everything between a flashed card and "SSH-ready OS" — the Host's name, its
account, non-interactive sudo, the deploy key, and a wired link that is not
waiting for a DHCP server that cannot exist — used to be typed into an imager's
dialog on one particular laptop. Hand the board to someone else and none of it
survives. All five are already in the Host profile, so they are rendered from
it.

Two facts about the mechanism were established by asking this OS rather than
remembering an older one, and both would otherwise fail silently:

Raspberry Pi OS 13 seeds cloud-init from the boot partition (`ds=nocloud` in
`cmdline.txt`); there is no `firstrun.sh` any more. The payload is three files,
and they are the same three an imager writes — so writing them replaces the
imager's, which is the whole intent and not a conflict.

`instance-id` is cloud-init's, and `meta-data` carries it because a NoCloud
seed needs the file. It is derived from the Host's name and therefore stable:
on a freshly flashed card the value cannot matter — `/var/lib/cloud` is empty
and every module runs regardless — so a fresh id per rendering would buy
nothing for the only case this serves, while costing reproducibility and
making "silently reconfigure a card that already booted" the default. Stable
means rendering twice gives the same bytes, and re-seeding a booted card is a
no-op rather than a surprise.

The wired link goes through `networkmanager.passthrough` with an explicit
`renderer`. netplan's own `link-local` key does not round-trip here (setting
`ipv4.method=link-local` on a running Host leaves it absent from `netplan get`
while NetworkManager holds it), and netplan refuses a device carrying
`networkmanager` settings without a renderer — "networkmanager backend settings
found but renderer is not NetworkManager". The image's global renderer file is
not in scope when the seed is parsed alone, so inheriting it would produce a
card netplan will not read: a board with no wired link and nothing saying why.

YAML is emitted as text because this package deliberately has no runtime YAML
implementation — see the note beside PyYAML in `pyproject.toml`. The tests
parse what this renders with a real parser.
"""

from __future__ import annotations

import json

from eidolon_ops.errors import OperationsError

#: cloud-init's file names on a NoCloud seed. Its choice, not ours.
USER_DATA = "user-data"
META_DATA = "meta-data"
NETWORK_CONFIG = "network-config"

#: Raspberry Pi OS's name for the Pi's built-in Ethernet.
WIRED_INTERFACE = "eth0"

#: The one foundation this payload is written for, and the reason there is a
#: check rather than a default.
#:
#: Everything here is that platform's convention, not a general one: cloud-init
#: seeded from a FAT32 boot partition, these three file names, netplan with the
#: NetworkManager renderer, and `eth0`. The capability is declared on the
#: SSH/systemd adapter, which more than one board uses — the rk3588 profile
#: reached this and would have been handed `eth0` for an interface called
#: `enP3p49s0`. That card boots, comes up with no wired link, and says nothing
#: about why. A second platform gets its own payload declared beside its own
#: foundation; until one exists, guessing on its behalf is worse than refusing.
FOUNDATION = "raspberry-pi-os-debian-arm64-v2"


def require_supported_foundation(foundation: str) -> None:
    """Refuse a Host whose first boot this payload does not describe."""

    if foundation != FOUNDATION:
        raise OperationsError(
            f"boot-media renders the {FOUNDATION!r} first boot, and this Host declares "
            f"{foundation!r}. The payload is that platform's convention throughout — its "
            f"boot partition layout, its first-boot mechanism and its interface name "
            f"({WIRED_INTERFACE}) — so writing it for another board produces a card that "
            "boots with no wired link and nothing saying why."
        )


def render(*, hostname: str, user: str, authorized_key: str) -> dict[str, str]:
    """The three files, keyed by the name cloud-init expects them under."""

    key = authorized_key.strip()
    # The one value worth checking here: the others come from a reviewed
    # profile this package already parsed, while an unusable key is a board
    # that boots perfectly and lets nobody in.
    if not key.startswith(("ssh-", "ecdsa-", "sk-")) or "\n" in key:
        raise OperationsError(
            f"{authorized_key[:40]!r} is not one OpenSSH public key line. It is what the "
            "Host will accept, so an unusable value here is a board nothing can log into."
        )
    # The profile addresses the Host as `<name>.local` because that is what
    # resolves; the Host's own hostname is the label, and Avahi appends the rest.
    name = hostname.removesuffix(".local")
    return {
        USER_DATA: (
            "#cloud-config\n"
            "# Rendered by eidolon-ops from the Host profile.\n"
            f"hostname: {name}\n"
            "manage_etc_hosts: true\n"
            "preserve_hostname: false\n"
            # Declared rather than assumed of the image: Ops finds the Host by
            # the name it publishes, so no Avahi is no Host.
            "packages:\n- avahi-daemon\n"
            "user:\n"
            f"  name: {user}\n"
            "  shell: /bin/bash\n"
            "  groups: [sudo]\n"
            # The transport elevates with `sudo --non-interactive`; without
            # this every install step stops at the first elevation.
            '  sudo: ["ALL=(ALL) NOPASSWD:ALL"]\n'
            "  ssh_authorized_keys:\n"
            f"  - {json.dumps(key)}\n"
            # No console password is rendered, so the account has none rather
            # than a guessable one. The plan says what that costs.
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
            f"    {WIRED_INTERFACE}:\n"
            "      renderer: NetworkManager\n"
            # Nothing serves DHCP on a point-to-point cable, so asking for it
            # is 45 seconds of waiting and then a torn-down link, repeatedly —
            # which is what a Host answering in bursts actually is. Link-local
            # means both ends self-assign and stay up; never-default keeps the
            # route out on whatever carries it.
            "      dhcp4: false\n"
            "      dhcp6: false\n"
            "      optional: true\n"
            "      networkmanager:\n"
            "        passthrough:\n"
            '          ipv4.method: "link-local"\n'
            '          ipv6.method: "link-local"\n'
            '          ipv4.never-default: "true"\n'
            '          ipv6.never-default: "true"\n'
            '          connection.autoconnect: "true"\n'
        ),
    }
