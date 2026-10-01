"""What this workstation's network currently looks like, asked rather than declared.

An address a Host once had tells an operator nothing about whether a device can
reach it now, so every value here is read from the machine at the moment it is
needed — including the name lookup, which is a real query rather than a search
for a line in a log.
"""

from __future__ import annotations

import ipaddress
import json
import platform
import re

from eidolon_ops.process import ProcessRunner

_INET = re.compile(r"\binet\s+(\d+\.\d+\.\d+\.\d+)\b")
_DSCACHE_ADDRESS = re.compile(r"\bip_address:\s*(\d+\.\d+\.\d+\.\d+)\b")


def interface_addresses(runner: ProcessRunner) -> set[str]:
    if platform.system() == "Linux":
        entries = json.loads(runner.run(("ip", "-j", "-4", "address", "show"), timeout=10).stdout)
        return {
            address["local"] for entry in entries for address in entry.get("addr_info", ())
            if address.get("family") == "inet"
        }
    return set(_INET.findall(runner.run(("ifconfig",), timeout=10).stdout))


def observed_lan_address(runner: ProcessRunner, addresses: set[str] | None = None) -> str:
    """The address this Host currently answers on, not one it once had."""

    if addresses is None:
        addresses = interface_addresses(runner)
    addresses = {value for value in addresses if not (
        ipaddress.ip_address(value).is_loopback or ipaddress.ip_address(value).is_link_local
        or ipaddress.ip_address(value).is_unspecified or ipaddress.ip_address(value).is_multicast
    )}
    if platform.system() == "Linux":
        routes = json.loads(runner.run(("ip", "-j", "-4", "route", "show", "default"), timeout=10).stdout)
        preferred = {
            entry["prefsrc"] for entry in routes if entry.get("prefsrc") in addresses
        }
        if len(preferred) == 1:
            return preferred.pop()
        devices = {entry["dev"] for entry in routes if entry.get("dev")}
        if len(devices) == 1:
            detail = json.loads(runner.run(("ip", "-j", "-4", "address", "show", "dev", devices.pop()), timeout=10).stdout)
            candidates = {
                entry["local"] for interface in detail for entry in interface.get("addr_info", ())
                if entry.get("family") == "inet" and entry.get("local") in addresses
            }
            if len(candidates) == 1:
                return candidates.pop()
        return next(iter(addresses)) if len(addresses) == 1 else ""
    route = runner.run(("/sbin/route", "-n", "get", "default"), timeout=10)
    interface = ""
    for line in route.stdout.splitlines():
        name, separator, value = line.partition(":")
        if separator and name.strip() == "interface":
            interface = value.strip()
            break
    if interface:
        detail = runner.run(("ifconfig", interface), timeout=10)
        found = _INET.search(detail.stdout)
        if found is not None and found.group(1) in addresses:
            return found.group(1)
    routable = sorted(addresses, key=lambda value: int(ipaddress.ip_address(value)))
    # A stable order is not evidence that phones can reach the first address.
    # The profile's existing explicit LAN address resolves multi-interface ambiguity.
    return routable[0] if len(routable) == 1 else ""


def name_resolves_to(runner: ProcessRunner, hostname: str, address: str) -> bool:
    """A device finds this Host by name; the name has to reach the address.

    This asks the resolver. The check it replaced read an mDNS registration
    line out of a log file, which stayed true long after the registration
    itself had stopped being.
    """

    if not address:
        return False
    if platform.system() == "Linux":
        result = runner.run(("getent", "ahostsv4", hostname), timeout=10)
        return address in {line.split()[0] for line in result.stdout.splitlines() if line.strip()}
    result = runner.run(("dscacheutil", "-q", "host", "-a", "name", hostname), timeout=10)
    return address in _DSCACHE_ADDRESS.findall(result.stdout)
