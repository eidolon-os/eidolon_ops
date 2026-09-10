"""What Ops requires of a board before it can operate it, and how one platform is told.

Two things live here and they are deliberately not mixed. `BringUp` is what Ops
needs to be true of any board, stated in Ops's own terms and derived from the
Host profile — no file name, partition or interface appears in it. A payload
renderer knows one platform's first-boot convention and nothing about why.

That split is the point. Everything between a flashed card and "SSH-ready OS"
used to be typed into an imager's dialog on one laptop, so handing the board to
someone else lost all of it. Making the requirement a reviewed list moves it
into the repository; keeping the mechanism separate means the next OS version
is a new renderer rather than a rewrite — and the mechanism does change, which
is how this was written against `firstrun.sh` before the board itself said
otherwise.

Renderers are keyed by `foundation.profile`, the same id `foundation.py` keys
its packages and pinned artifacts by, and refuse an unknown one the same way.
That id already carries the OS and a revision (`raspberry-pi-os-debian-arm64-v2`),
so an OS that changes its first boot again is a new foundation with a new
renderer while boards still on the old one keep working.

YAML is emitted as text because this package deliberately has no runtime YAML
implementation — see the note beside PyYAML in `pyproject.toml`. The tests
parse what this renders with a real parser.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from eidolon_ops import environment
from eidolon_ops.errors import OperationsError

#: cloud-init's file names on a NoCloud seed. Its choice, not ours.
USER_DATA = "user-data"
META_DATA = "meta-data"
NETWORK_CONFIG = "network-config"

#: Keys the private Wi-Fi input must carry, and only these.
WIFI_SSID = "EIDOLON_WIFI_SSID"
WIFI_PSK = "EIDOLON_WIFI_PSK"
WIFI_COUNTRY = "EIDOLON_WIFI_COUNTRY"


@dataclass(frozen=True, slots=True)
class WifiCredentials:
    """The network a prepared board reaches the internet through.

    Not decoration: `provision` installs Debian packages and pinned artifacts,
    so a board with no route out fails there. It is separate from the cable
    because the two have opposite jobs — this one carries the default route,
    the cable carries releases and must never take it.
    """

    ssid: str
    psk: str
    country: str


@dataclass(frozen=True, slots=True)
class BringUp:
    """What has to be true of a board before Ops can operate it at all.

    Each of these exists because something in Ops requires it, and none of
    them names a platform:

    * ``hostname`` — `endpoints.py` resolves the Host by the name it publishes,
      and `transport.py` trusts its host key under that name via HostKeyAlias.
    * ``user`` — the account the transport connects as.
    * ``authorized_key`` — the transport is ``BatchMode=yes`` with an explicit
      identity, so a key it did not install is a Host it cannot reach.
    * non-interactive sudo for that account — the transport elevates with
      ``sudo --non-interactive``.
    * sshd and mDNS running — the transport at all, and name resolution.
    * a wired link holding IPv4 without waiting for DHCP and never taking the
      default route — the wired-upload gate, and the flapping link that gate
      was diagnosing.
    * ``wifi`` — a route out, for `provision`. Optional: a board on a wired
      DHCP network has one already, and one with neither simply cannot be
      provisioned, which the operation reports rather than discovering later.

    The last four are properties of the rendered payload rather than fields:
    every renderer must produce them, and a renderer that does not is wrong in
    a way its own platform's validator will not catch.
    """

    hostname: str
    user: str
    authorized_key: str
    wifi: WifiCredentials | None = None

    def __post_init__(self) -> None:
        key = self.authorized_key.strip()
        # The one value worth checking: the others come from a profile this
        # package already parsed, while an unusable key is a board that boots
        # perfectly and lets nobody in.
        if not key.startswith(("ssh-", "ecdsa-", "sk-")) or "\n" in key:
            raise OperationsError(
                f"{self.authorized_key[:40]!r} is not one OpenSSH public key line. It is "
                "what the Host will accept, so an unusable value here is a board nothing "
                "can log into."
            )

    @property
    def short_hostname(self) -> str:
        """The name without the suffix Avahi appends for itself.

        The profile addresses the Host as ``<name>.local`` because that is what
        resolves; the Host's own hostname is the label.
        """

        return self.hostname.removesuffix(".local")


def read_wifi_credentials(text: str, *, label: str) -> WifiCredentials:
    """The declared network, from the private ``KEY=value`` input."""

    values = environment.parse(text, label=label)
    missing = sorted({WIFI_SSID, WIFI_PSK, WIFI_COUNTRY} - set(values))
    extra = sorted(set(values) - {WIFI_SSID, WIFI_PSK, WIFI_COUNTRY})
    if missing or extra:
        raise OperationsError(
            f"{label} must carry exactly {WIFI_SSID}, {WIFI_PSK} and {WIFI_COUNTRY}"
            + (f"; missing {', '.join(missing)}" if missing else "")
            + (f"; unexpected {', '.join(extra)}" if extra else "")
        )
    return WifiCredentials(
        ssid=values[WIFI_SSID], psk=values[WIFI_PSK], country=values[WIFI_COUNTRY]
    )


def render(bring_up: BringUp, *, foundation: str) -> dict[str, str]:
    """The first-boot payload for the platform this foundation names."""

    try:
        renderer = PAYLOADS[foundation]
    except KeyError:
        declared = ", ".join(sorted(PAYLOADS))
        raise OperationsError(
            f"no first-boot payload is declared for foundation {foundation!r}. Declared: "
            f"{declared}. The payload is a platform's own convention throughout — where the "
            "pre-boot configuration lives, what reads it, its schema and the wired "
            "interface's name — so a board whose foundation is not among these gets its own "
            "renderer declared beside it rather than another platform's guessed at."
        ) from None
    return renderer(bring_up)


def _raspberry_pi_os_trixie(bring_up: BringUp) -> dict[str, str]:
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

    The wired link goes through `networkmanager.passthrough` with an explicit
    renderer. netplan's own `link-local` key does not round-trip here — setting
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
            f"  - {json.dumps(bring_up.authorized_key.strip())}\n"
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
            # Nothing serves DHCP on a point-to-point cable, so asking for it
            # is 45 seconds of waiting and then a torn-down link, repeatedly —
            # which is what a Host answering in bursts actually is.
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
            f"{_wifi(bring_up.wifi)}"
        ),
    }


def _wifi(wifi: WifiCredentials | None) -> str:
    if wifi is None:
        return ""
    return (
        "  wifis:\n"
        "    wlan0:\n"
        "      renderer: NetworkManager\n"
        "      dhcp4: true\n"
        "      dhcp6: false\n"
        "      optional: true\n"
        f"      regulatory-domain: {json.dumps(wifi.country)}\n"
        "      access-points:\n"
        f"        {json.dumps(wifi.ssid)}:\n"
        f"          password: {json.dumps(wifi.psk)}\n"
    )


#: Which platform's first boot Ops knows how to describe. Keyed by
#: `foundation.profile` so an OS that changes its first boot is a new
#: foundation with a new renderer, not an edit to this one.
PAYLOADS = {"raspberry-pi-os-debian-arm64-v2": _raspberry_pi_os_trixie}
