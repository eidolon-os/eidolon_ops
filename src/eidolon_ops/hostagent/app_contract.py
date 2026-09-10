"""The Host-bound application contract, and the address it is bound to.

A declared address is honoured for a static setup; otherwise the Host reports
the one it currently answers on, because an address it once had says nothing
about whether a device can reach it now.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from ipaddress import IPv4Address, ip_address
from urllib.parse import urlparse

from . import primitives
from .primitives import TargetError


def observed_lan_address() -> IPv4Address:
    """A current local IPv4 candidate; default egress is only a preference."""

    route = primitives.run(("/usr/sbin/ip", "-4", "route", "get", "1.1.1.1"), timeout=15)
    if route.returncode != 0:
        route = primitives.run(("/sbin/ip", "-4", "route", "get", "1.1.1.1"), timeout=15)
    found = re.search(r"\bsrc\s+(\d+\.\d+\.\d+\.\d+)\b", route.stdout)
    addresses = host_addresses()
    usable = []
    for value in addresses:
        address = ip_address(value)
        if (isinstance(address, IPv4Address) and address.is_private
                and not address.is_loopback and not address.is_link_local
                and not address.is_unspecified and not address.is_multicast):
            usable.append(address)
    usable.sort(key=int)
    preferred = found.group(1) if found else None
    for address in usable:
        if str(address) == preferred:
            return address
    if usable:
        return usable[0]
    raise TargetError("Host has no usable private IPv4 interface address")


def host_addresses() -> set[str]:
    """IPv4 addresses on interfaces the Host currently has up."""

    result = primitives.run(("/usr/sbin/ip", "-4", "-o", "addr", "show", "up"), timeout=15)
    if result.returncode != 0:
        result = primitives.run(("/sbin/ip", "-4", "-o", "addr", "show", "up"), timeout=15)
    return set(re.findall(r"\binet\s+(\d+\.\d+\.\d+\.\d+)", result.stdout))

def fixed_app(payload: Mapping[str, object]) -> dict[str, object]:
    value = payload.get("app")
    expected = {
        "host_id",
        "owner_domain_id",
        "hub_hostname",
        "hub_https_port",
        "hub_origin",
        "livekit_client_url",
        "allow_insecure_livekit",
    }
    if not isinstance(value, dict) or not expected <= set(value):
        raise TargetError("Host application contract is missing or malformed")
    if not set(value) <= (expected | {"lan_ipv4"}):
        raise TargetError("Host application contract is missing or malformed")
    host_id = value.get("host_id")
    if not isinstance(host_id, str) or re.fullmatch(r"ehost-[0-9a-f]{20}", host_id) is None:
        raise TargetError("Host application Host ID is invalid")
    suffix = host_id.removeprefix("ehost-")
    hub_id = f"eidolon-hub-{suffix}"
    hub_hostname = f"{hub_id}.local"
    port = value.get("hub_https_port")
    if (
        value.get("hub_hostname") != hub_hostname
        or type(port) is not int
        or not 1 <= port <= 65535
        or value.get("hub_origin") != f"https://{hub_hostname}:{port}"
    ):
        raise TargetError("Host application Hub identity is not Host-bound")
    owner_domain_id = value.get("owner_domain_id")
    if (
        not isinstance(owner_domain_id, str)
        or re.fullmatch(r"owner-[0-9a-f]{20}", owner_domain_id) is None
    ):
        raise TargetError("Host application Owner Domain ID is invalid")
    # A declared address is honoured for static setups; otherwise the Host
    # reports the one it currently answers on, because an address it once had
    # tells an operator nothing about whether devices can reach it now.
    declared = value.get("lan_ipv4")
    if declared is None:
        address = observed_lan_address()
    else:
        try:
            address = ip_address(str(declared))
        except ValueError as exc:
            raise TargetError("Host application LAN address is invalid") from exc
        if not isinstance(address, IPv4Address) or not address.is_private or address.is_loopback:
            raise TargetError("Host application LAN address must be private IPv4")
    livekit = value.get("livekit_client_url")
    try:
        parsed = urlparse(livekit) if isinstance(livekit, str) else None
        if parsed is not None:
            _parsed_port = parsed.port
    except ValueError as exc:
        raise TargetError("Host application LiveKit origin is invalid") from exc
    allow_insecure = value.get("allow_insecure_livekit")
    # Scheme and port with no host — `ws://:7880` — is the third legal form and
    # the one Ops writes when nothing was declared. It says the host is decided
    # when a binding is minted, which is the truth: neither Ops nor this Host
    # knows which of its addresses a device will reach, and the two values that
    # used to be written here were both wrong for that reason — a deploy-time
    # address that went stale, and a `.local` name that Android's getaddrinfo
    # cannot resolve at all.
    host_deferred = parsed is not None and parsed.hostname is None and parsed.port is not None
    if (
        parsed is None
        or parsed.scheme not in {"ws", "wss"}
        or not (parsed.hostname or host_deferred)
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
        or not isinstance(allow_insecure, bool)
        or (parsed.scheme == "ws" and not allow_insecure)
        # A plaintext origin that names a host has to name this Host and no
        # other. Both its current address and its Host-bound name do that.
        # Naming nothing is not a weaker claim than naming this Host — it is no
        # claim, resolved later on this Host, which is the only place the answer
        # exists.
        or (
            parsed.scheme == "ws"
            and not host_deferred
            and parsed.hostname not in {str(address), hub_hostname}
        )
    ):
        raise TargetError("Host application LiveKit origin is invalid")
    # The resolved address travels with the contract. Discovery ran here, so a
    # consumer that repeated it could disagree with what was just validated —
    # and one that read the declared value found nothing when none was declared.
    return {**value, "lan_ipv4": str(address)}

def optional_app(payload: Mapping[str, object]) -> dict[str, object] | None:
    if "app" not in payload:
        return None
    return fixed_app(payload)
