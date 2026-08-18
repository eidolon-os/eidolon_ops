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
    """The address this Host currently answers on, read from its default route."""

    route = primitives.run(("/usr/sbin/ip", "-4", "route", "get", "1.1.1.1"), timeout=15)
    if route.returncode != 0:
        route = primitives.run(("/sbin/ip", "-4", "route", "get", "1.1.1.1"), timeout=15)
    found = re.search(r"\bsrc\s+(\d+\.\d+\.\d+\.\d+)\b", route.stdout)
    if found is None:
        raise TargetError("Host has no routable IPv4 address")
    address = ip_address(found.group(1))
    if not isinstance(address, IPv4Address) or not address.is_private or address.is_loopback:
        raise TargetError("Host default route address must be private IPv4")
    return address

def host_addresses() -> set[str]:
    """Every IPv4 address this Host currently holds."""

    result = primitives.run(("/usr/sbin/ip", "-4", "-o", "addr", "show"), timeout=15)
    if result.returncode != 0:
        result = primitives.run(("/sbin/ip", "-4", "-o", "addr", "show"), timeout=15)
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
    if (
        parsed is None
        or parsed.scheme not in {"ws", "wss"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
        or not isinstance(allow_insecure, bool)
        or (parsed.scheme == "ws" and not allow_insecure)
        # A plaintext origin has to name this Host and no other. Both its
        # current address and its Host-bound name do that; the name is the
        # better answer because it does not go stale when the address moves,
        # which is why the operator side asks for it. Accepting only the
        # literal left no value that satisfied both ends, and the placeholder
        # that shipped to devices was the residue of that.
        or (parsed.scheme == "ws" and parsed.hostname not in {str(address), hub_hostname})
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
