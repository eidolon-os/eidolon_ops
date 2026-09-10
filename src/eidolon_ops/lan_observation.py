"""What this workstation's network currently looks like, asked rather than declared.

An address a Host once had tells an operator nothing about whether a device can
reach it now, so every value here is read from the machine at the moment it is
needed — including the name lookup, which is a real query rather than a search
for a line in a log.
"""

from __future__ import annotations

import re

from eidolon_ops.process import ProcessRunner

_INET = re.compile(r"\binet\s+(\d+\.\d+\.\d+\.\d+)\b")
_DSCACHE_ADDRESS = re.compile(r"\bip_address:\s*(\d+\.\d+\.\d+\.\d+)\b")


def interface_addresses(runner: ProcessRunner) -> set[str]:
    return set(_INET.findall(runner.run(("ifconfig",), timeout=10).stdout))


def observed_lan_address(runner: ProcessRunner, addresses: set[str] | None = None) -> str:
    """The address this Host currently answers on, not one it once had."""

    if addresses is None:
        addresses = interface_addresses(runner)
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
        if found is not None:
            return found.group(1)
    routable = sorted(value for value in addresses if not value.startswith("127."))
    return routable[0] if routable else ""


def name_resolves_to(runner: ProcessRunner, hostname: str, address: str) -> bool:
    """A device finds this Host by name; the name has to reach the address.

    This asks the resolver. The check it replaced read an mDNS registration
    line out of a log file, which stayed true long after the registration
    itself had stopped being.
    """

    if not address:
        return False
    result = runner.run(("dscacheutil", "-q", "host", "-a", "name", hostname), timeout=10)
    return address in _DSCACHE_ADDRESS.findall(result.stdout)
