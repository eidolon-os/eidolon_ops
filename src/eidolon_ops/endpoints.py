"""Where a Host answers today, ordered by how good the link is.

A Host is named, not addressed: the operator configures the name it publishes
over mDNS, and that name is what the SSH host key is trusted under. The
addresses behind it change with the cable, the access point and the lease, so
they are resolved per run rather than written down.

Between two addresses that both reach the same Host, the wired one is worth a
hundred times the wireless one on a release upload — measured, on this Pi: 120MB
in 2s over USB Ethernet against 205s over Wi-Fi. That is the whole reason this
module exists, so wired candidates come first and the rest follow.

Only IPv4 candidates are offered. The transport also has to move a release
bundle, and the rsync macOS ships (openrsync) reads ``host::path`` as its
daemon syntax — every IPv6 literal collides with it, brackets included. An
endpoint this transport cannot use for all of its work is not a candidate.
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


def resolve_endpoints(
    hostname: str,
    port: int,
    *,
    resolver: Callable[..., list] | None = None,
    interfaces: Callable[[], tuple[LocalInterface, ...]] | None = None,
) -> tuple[HostEndpoint, ...]:
    """Every IPv4 address ``hostname`` resolves to, best link first."""

    resolve = resolver or socket.getaddrinfo
    try:
        answers = resolve(hostname, port, socket.AF_INET, socket.SOCK_STREAM)
    except OSError:
        return ()
    local = (interfaces or local_interfaces)()
    endpoints: list[HostEndpoint] = []
    seen: set[str] = set()
    for _family, _type, _proto, _canonname, sockaddr in answers:
        address = str(sockaddr[0])
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


def _owning_interface(address: str, interfaces: Sequence[LocalInterface]) -> LocalInterface | None:
    try:
        parsed = ipaddress.IPv4Address(address)
    except ValueError:
        return None
    for interface in interfaces:
        if interface.reaches(parsed):
            return interface
    return None


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
    probe: Callable[[str, int, float], bool] | None = None,
) -> HostEndpoint | None:
    """The best-ranked endpoint that accepts a connection, or nothing."""

    attempt = probe or _accepts_connection
    for endpoint in endpoints:
        if attempt(endpoint.address, port, timeout):
            return endpoint
    return None


def _accepts_connection(address: str, port: int, timeout: float) -> bool:
    try:
        with socket.create_connection((address, port), timeout=timeout):
            return True
    except OSError:
        return False
