from __future__ import annotations

import ipaddress
import socket
from types import SimpleNamespace

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

    def _probe(endpoint: HostEndpoint, _port: int, _timeout: float) -> bool:
        tried.append(endpoint.address)
        return endpoint.address == "192.168.1.26"

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


def test_a_link_local_endpoint_carries_the_interface_as_an_instruction() -> None:
    """The routing table cannot answer this one, so the endpoint has to.

    Every self-assigned interface carries the same 169.254.0.0/16, so a
    link-local address matches more than one route and the kernel takes
    whichever it holds first. On the workstation this was found on, that is
    Wi-Fi, while the Host is on the wire.
    """

    endpoints = resolve_endpoints(
        "eidolon-pi5.local",
        22,
        resolver=lambda *_args, **_kwargs: _answers("169.254.19.7"),
        interfaces=_interfaces,
    )

    assert endpoints[0].bind_interface == "en7"


def test_a_routable_endpoint_asks_for_no_binding() -> None:
    endpoints = resolve_endpoints(
        "eidolon-pi5.local",
        22,
        resolver=lambda *_args, **_kwargs: _answers("192.168.1.10"),
        interfaces=_interfaces,
    )

    # Its route is unambiguous, and overriding a correct route can only take
    # away a working path — for instance when the Host is reachable two ways.
    assert endpoints[0].interface == "en0"
    assert endpoints[0].bind_interface is None


def test_the_wire_wins_when_both_links_claim_a_link_local_address() -> None:
    both = (
        LocalInterface("en0", "wireless", (ipaddress.IPv4Network("169.254.0.0/16"),)),
        LocalInterface("en7", "wired", (ipaddress.IPv4Network("169.254.0.0/16"),)),
    )

    endpoints = resolve_endpoints(
        "eidolon-pi5.local",
        22,
        resolver=lambda *_args, **_kwargs: _answers("169.254.19.7"),
        interfaces=lambda: both,
    )

    # This is the real shape of a workstation with a USB Ethernet adapter: two
    # interfaces, one address range, and nothing in ifconfig's ordering that
    # means anything. Taking the first match would pick by luck.
    assert endpoints[0].bind_interface == "en7"
    assert endpoints[0].link == "wired"


def test_the_upload_says_which_link_it_is_about_to_take() -> None:
    """Choosing the wire is automatic; saying so was not.

    The Host is configured by name, its addresses are resolved per run, and
    wired candidates rank first — so whoever deploys already gets the fast path
    without knowing it exists. The gap was that only ``status`` reported the
    choice. The operation where the difference is felt moves about a gigabyte,
    and a cable that is not in looked exactly like a slow afternoon.
    """

    from eidolon_ops.release_transaction import ReleaseTransaction

    class FakeTransport:
        def __init__(self, endpoint: HostEndpoint | None) -> None:
            self._endpoint = endpoint
            self.described = 0

        @property
        def endpoint(self) -> HostEndpoint | None:
            return self._endpoint

        def describe(self) -> str:
            self.described += 1
            return "resolved"

    def report(endpoint: HostEndpoint | None) -> dict:
        transaction = object.__new__(ReleaseTransaction)
        transaction.transport = FakeTransport(endpoint)
        transaction.preflight = SimpleNamespace(
            config=SimpleNamespace(host=SimpleNamespace(hostname="eidolon-pi5.local"))
        )
        return ReleaseTransaction._link_report(transaction)

    wired = report(HostEndpoint(address="169.254.181.137", interface="en7", link="wired"))
    assert wired["status"] == "wired"
    assert wired["endpoint"] == "169.254.181.137 (wired via en7)"
    # Nothing to warn about: this is the link the ranking exists to pick.
    assert "note" not in wired

    wireless = report(HostEndpoint(address="192.168.3.40", interface="en0", link="wireless"))
    assert wireless["status"] == "wireless"
    assert "cable" in wireless["note"]

    # A Host that answers on neither is a different failure, and the upload
    # itself will report it. This says what it knows and does not invent a link.
    assert report(None)["status"] == "unresolved"
