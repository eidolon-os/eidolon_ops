"""Where a Host answers today, ordered by how good the link is.

A Host is named, not addressed: the operator configures the name it publishes
over mDNS, and that name is what the SSH host key is trusted under. The
addresses behind it change with the cable, the access point and the lease, so
they are resolved per run rather than written down.

Between two addresses that both reach the same Host, the wired one is worth a
hundred times the wireless one on a release upload — measured, on this Pi: 120MB
in 2s over USB Ethernet against 205s over Wi-Fi. That is the whole reason this
module exists, so wired candidates come first and the rest follow.

The resolver is not the authority on how many links a Host has. On macOS a
Host that answers on both Wi-Fi and a point-to-point cable resolves to the
Wi-Fi record alone once avahi's unsolicited announcement has aged out of the
cache: the wire is never offered, so it can never be ranked, and a release
bound for the cable is refused for being wireless while the cable is plugged
in and carrying SSH. Both self-assigned link-local addresses being present
does not fix it — that was measured, on this workstation, with 169.254 on both
ends.

So the Host is asked. It already publishes every non-loopback IPv4 it answers
on, because `status` prints them; `rank_addresses` exists to rank that list
with the same rule, and the transport reaches for it when the resolver's best
candidate is not the wire. Which addresses exist is the Host's fact. Which of
them is the good link stays this machine's.

Only IPv4 candidates are offered. The transport also has to move a release
bundle, and the rsync macOS ships (openrsync) reads ``host::path`` as its
daemon syntax — every IPv6 literal collides with it, brackets included. An
endpoint this transport cannot use for all of its work is not a candidate.

One address family needs more than a choice: it needs the choice enforced.
Link-local addresses are configured per interface with the same 169.254.0.0/16
route on every one of them, so the routing table cannot tell which link a
given link-local address is on — it picks whichever route it happens to have
first, and on this workstation that is Wi-Fi:

    169.254   link#14  UCS   en0     <- Wi-Fi
    169.254   link#22  UCSI  en7     <- the wire the Host is actually on

So for a link-local endpoint the interface is not a description, it is an
instruction: it travels with the endpoint and the transport binds to it. For
any routable address it stays absent, because there the kernel's own route
lookup is right and overriding it would only remove a correct answer.
"""

from __future__ import annotations

import ipaddress
import socket
from collections.abc import Callable, Iterable, Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path

from eidolon_ops.process import ProcessRunner, SubprocessRunner

#: Ordering key. "unknown" sits between the two known kinds: an address we
#: cannot attribute to a local interface might be the wire, and demoting it
#: below Wi-Fi would make an unclassifiable platform slower than no
#: classification at all.
LINK_RANK = {"wired": 0, "unknown": 1, "wireless": 2}


@dataclass(frozen=True, slots=True)
class LocalInterface:
    """One of this machine's interfaces: what kind of link, and what it can reach."""

    name: str
    kind: str
    networks: tuple[ipaddress.IPv4Network, ...]

    def reaches(self, address: ipaddress.IPv4Address) -> bool:
        return any(address in network for network in self.networks)


@dataclass(frozen=True, slots=True)
class HostEndpoint:
    """One address the Host answers on, with the local link that reaches it."""

    address: str
    interface: str | None
    link: str

    def describe(self) -> str:
        where = f" via {self.interface}" if self.interface else ""
        return f"{self.address} ({self.link}{where})"

    @property
    def bind_interface(self) -> str | None:
        """The interface the transport must bind to, if leaving it out is a bug.

        Only link-local addresses answer with a name here. Their route is
        ambiguous by construction, so an unbound connection can leave by an
        interface that cannot reach the Host at all — and the failure looks
        like a Host that is down rather than a packet that went the wrong way.
        """

        if self.interface is None:
            return None
        try:
            parsed = ipaddress.IPv4Address(self.address)
        except ValueError:
            return None
        return self.interface if parsed.is_link_local else None


def rank_addresses(
    addresses: Iterable[str],
    *,
    interfaces: Callable[[], tuple[LocalInterface, ...]] | None = None,
) -> tuple[HostEndpoint, ...]:
    """Given addresses, attribute each to a local link and order them best first.

    Ranking is the workstation's job wherever the addresses came from: only
    this machine knows which of its interfaces can reach them and which of
    those is the wire. Duplicates are dropped, because a resolver answering
    twice is not two links.
    """

    local = (interfaces or local_interfaces)()
    endpoints: list[HostEndpoint] = []
    seen: set[str] = set()
    for address in addresses:
        if address in seen:
            continue
        seen.add(address)
        owner = _owning_interface(address, local)
        endpoints.append(
            HostEndpoint(
                address=address,
                interface=owner.name if owner else None,
                link=owner.kind if owner else "unknown",
            )
        )
    return tuple(sorted(endpoints, key=lambda item: LINK_RANK.get(item.link, 1)))


def resolve_endpoints(
    hostname: str,
    port: int,
    *,
    resolver: Callable[..., list] | None = None,
    interfaces: Callable[[], tuple[LocalInterface, ...]] | None = None,
) -> tuple[HostEndpoint, ...]:
    """Every IPv4 address ``hostname`` resolves to, best link first.

    This is what the workstation's own resolver knows, and it is not always
    every link the Host has — see the note on asking the Host in the module
    docstring.
    """

    resolve = resolver or socket.getaddrinfo
    try:
        answers = resolve(hostname, port, socket.AF_INET, socket.SOCK_STREAM)
    except OSError:
        return ()
    return rank_addresses(
        (str(sockaddr[0]) for *_unused, sockaddr in answers), interfaces=interfaces
    )


def _owning_interface(address: str, interfaces: Sequence[LocalInterface]) -> LocalInterface | None:
    """The best local interface that reaches ``address``.

    More than one can claim it, and for link-local addresses more than one
    usually does — every self-assigned interface carries the same /16. Ranking
    rather than taking the first makes the answer the wire instead of whichever
    interface ``ifconfig`` happened to print first.
    """

    try:
        parsed = ipaddress.IPv4Address(address)
    except ValueError:
        return None
    owners = [interface for interface in interfaces if interface.reaches(parsed)]
    if not owners:
        return None
    return min(owners, key=lambda item: LINK_RANK.get(item.kind, 1))


def local_interfaces(runner: ProcessRunner | None = None) -> tuple[LocalInterface, ...]:
    """This machine's interfaces, each with its link kind and IPv4 networks.

    Two platforms, two facts of life: macOS names hardware ports, Linux marks a
    wireless device with a ``wireless`` directory. An interface we cannot
    classify is reported as "unknown", which only costs ordering.
    """

    runner = runner or SubprocessRunner()
    networks = _ipv4_networks(runner)
    kinds = _link_kinds(runner)
    return tuple(
        LocalInterface(name=name, kind=kinds.get(name, "unknown"), networks=tuple(values))
        for name, values in networks.items()
    )


def _link_kinds(runner: ProcessRunner) -> dict[str, str]:
    sysfs = Path("/sys/class/net")
    if sysfs.is_dir():
        return {
            device.name: "wireless" if (device / "wireless").exists() else "wired"
            for device in sysfs.iterdir()
            if device.name != "lo"
        }
    result = runner.run(("networksetup", "-listallhardwareports"), timeout=15)
    if result.returncode != 0:
        return {}
    return _parse_hardware_ports(result.stdout.splitlines())


def _parse_hardware_ports(lines: Iterable[str]) -> dict[str, str]:
    kinds: dict[str, str] = {}
    port = ""
    for raw in lines:
        line = raw.strip()
        if line.startswith("Hardware Port:"):
            port = line.removeprefix("Hardware Port:").strip().casefold()
        elif line.startswith("Device:") and port:
            device = line.removeprefix("Device:").strip()
            if device:
                kinds[device] = "wireless" if ("wi-fi" in port or "airport" in port) else "wired"
            port = ""
    return kinds


def _ipv4_networks(runner: ProcessRunner) -> dict[str, list[ipaddress.IPv4Network]]:
    result = runner.run(("ifconfig", "-a"), timeout=15)
    if result.returncode != 0:
        return {}
    networks: dict[str, list[ipaddress.IPv4Network]] = {}
    for name, address, netmask in _parse_ifconfig(result.stdout.splitlines()):
        try:
            interface = ipaddress.IPv4Interface(f"{address}/{netmask}")
        except ValueError:
            continue
        networks.setdefault(name, []).append(interface.network)
    return networks


def _parse_ifconfig(lines: Iterable[str]) -> Iterator[tuple[str, str, str]]:
    """Yield ``(interface, address, netmask)`` from ifconfig output.

    macOS prints the mask as ``0xffff0000`` and Linux as dotted quad; both are
    accepted so the same reading works on either operator machine.
    """

    name = ""
    for raw in lines:
        if raw and not raw[0].isspace():
            name = raw.split(":", 1)[0].strip()
            continue
        fields = raw.split()
        if not name or not fields or fields[0] != "inet":
            continue
        address = fields[1]
        if "netmask" not in fields:
            continue
        mask = fields[fields.index("netmask") + 1]
        if mask.startswith("0x"):
            try:
                mask = str(ipaddress.IPv4Address(int(mask, 16)))
            except ValueError:
                continue
        yield name, address, mask


def first_reachable(
    endpoints: Sequence[HostEndpoint],
    port: int,
    *,
    timeout: float,
    probe: Callable[[HostEndpoint, int, float], bool] | None = None,
) -> HostEndpoint | None:
    """The best-ranked endpoint that accepts a connection, or nothing.

    The probe is given the whole candidate rather than its address, because
    reaching a link-local one is a question about an interface as much as about
    an address, and only the candidate knows which interface that is.
    """

    attempt = probe or _accepts_connection
    for endpoint in endpoints:
        if attempt(endpoint, port, timeout):
            return endpoint
    return None


def _accepts_connection(endpoint: HostEndpoint, port: int, timeout: float) -> bool:
    """Whether this interpreter can open a socket to the candidate.

    A last resort, and a weaker question than it looks: on macOS the answer
    also depends on whether this interpreter holds Local Network permission,
    so a Host sitting on a cable can be reported unreachable while `ssh` to
    the same address connects. Callers that own a real transport should probe
    with it instead.
    """

    try:
        with socket.create_connection((endpoint.address, port), timeout=timeout):
            return True
    except OSError:
        return False
