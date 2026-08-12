from __future__ import annotations

import ipaddress
import socket

import pytest

from eidolon_ops.endpoints import (
    HostEndpoint,
    LocalInterface,
    first_reachable,
    local_interfaces,
    resolve_endpoints,
)
from eidolon_ops.process import ProcessResult

pytestmark = pytest.mark.unit

MACOS_PORTS = """Hardware Ports:

Hardware Port: USB 10/100/1000 LAN
Device: en7
Ethernet Address: 00:e1:8c:68:2e:a4

Hardware Port: Wi-Fi
Device: en0
Ethernet Address: d0:c0:50:d9:8b:0d
"""

MACOS_IFCONFIG = """lo0: flags=8049<UP,LOOPBACK> mtu 16384
\tinet 127.0.0.1 netmask 0xff000000
en0: flags=8863<UP,BROADCAST> mtu 1500
\tinet 192.168.1.10 netmask 0xfffffc00 broadcast 192.168.3.255
en7: flags=8863<UP,BROADCAST> mtu 1500
\tinet 169.254.19.7 netmask 0xffff0000 broadcast 169.254.255.255
"""


class _Runner:
    def __init__(self, outputs: dict[str, str]) -> None:
        self.outputs = outputs

    def run(self, command, **_kwargs):
        return ProcessResult(0, self.outputs.get(command[0], ""), "")


def _answers(*addresses: str):
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, 22)) for address in addresses]


def _interfaces():
    return (
        LocalInterface("en0", "wireless", (ipaddress.IPv4Network("192.168.0.0/22"),)),
        LocalInterface("en7", "wired", (ipaddress.IPv4Network("169.254.0.0/16"),)),
    )


def test_the_wired_address_comes_first_whatever_the_resolver_says() -> None:
    endpoints = resolve_endpoints(
        "eidolon-pi5.local",
        22,
        resolver=lambda *_a, **_k: _answers("192.168.1.26", "169.254.55.2"),
        interfaces=_interfaces,
    )

    assert [endpoint.address for endpoint in endpoints] == [
        "169.254.55.2",
        "192.168.1.26",
    ]
    assert endpoints[0].interface == "en7"
    assert endpoints[1].link == "wireless"


def test_an_address_no_local_interface_reaches_is_still_offered() -> None:
    # A routed address is not on any of our subnets, and refusing to try it
    # would strand a Host that is reachable through a gateway.
    endpoints = resolve_endpoints(
        "eidolon-pi5.local",
        22,
        resolver=lambda *_a, **_k: _answers("10.20.30.40"),
        interfaces=_interfaces,
    )

    assert endpoints == (HostEndpoint(address="10.20.30.40", interface=None, link="unknown"),)


def test_an_unresolvable_name_yields_no_candidates() -> None:
    def _explode(*_args, **_kwargs):
        raise socket.gaierror("nodename nor servname provided")

    assert resolve_endpoints("nowhere.local", 22, resolver=_explode) == ()


def test_the_first_endpoint_that_answers_wins() -> None:
    candidates = (
        HostEndpoint(address="169.254.55.2", interface="en7", link="wired"),
        HostEndpoint(address="192.168.1.26", interface="en0", link="wireless"),
    )
    tried: list[str] = []

    def _probe(address: str, _port: int, _timeout: float) -> bool:
        tried.append(address)
        return address == "192.168.1.26"

    chosen = first_reachable(candidates, 22, timeout=1, probe=_probe)

    assert chosen == candidates[1]
    assert tried == ["169.254.55.2", "192.168.1.26"]
    assert first_reachable((), 22, timeout=1, probe=_probe) is None


def test_macos_interfaces_are_read_as_kinds_and_networks() -> None:
    runner = _Runner({"networksetup": MACOS_PORTS, "ifconfig": MACOS_IFCONFIG})

    interfaces = {item.name: item for item in local_interfaces(runner)}

    assert interfaces["en7"].kind == "wired"
    assert interfaces["en0"].kind == "wireless"
    assert interfaces["en7"].reaches(ipaddress.IPv4Address("169.254.55.2"))
    assert not interfaces["en0"].reaches(ipaddress.IPv4Address("169.254.55.2"))
    assert interfaces["en0"].reaches(ipaddress.IPv4Address("192.168.1.26"))
