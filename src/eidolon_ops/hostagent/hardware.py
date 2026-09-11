"""Local hardware evidence for one-time Host provisioning.

This is not a network identifier or an authentication credential. Never derive
keys from it. Permanent platform identifiers are deliberately independent of
NetworkManager, interface selection, IP addresses and installed OS machine-id.
"""
from __future__ import annotations

import hashlib
import json
import platform
import plistlib
import re
import subprocess
from collections.abc import Callable
from pathlib import Path

BINDING_FILE = "host_delivery.json"


class HostHardwareError(ValueError):
    """Hardware cannot be identified or provisioned state belongs to another board."""


def observe_hardware(*, root: Path = Path("/"), system: str | None = None,
                     run: Callable = subprocess.run) -> dict[str, str]:
    system = platform.system() if system is None else system
    if system == "Linux":
        serial = root / "proc/device-tree/serial-number"
        if serial.is_file():
            value = serial.read_bytes().rstrip(b"\x00\n").decode("ascii").strip().lower()
            compatible = root / "proc/device-tree/compatible"
            family = compatible.read_bytes().split(b"\x00")[0].decode("ascii") if compatible.is_file() else ""
            kind = "device-tree:" + family
        else:
            path = root / "sys/class/dmi/id/product_uuid"
            if not path.is_file():
                raise HostHardwareError("platform has no supported permanent hardware identifier")
            value, kind = path.read_text().strip().lower(), "dmi-product-uuid"
    elif system == "Darwin":
        result = run(("/usr/sbin/ioreg", "-rd1", "-c", "IOPlatformExpertDevice", "-a"),
                     capture_output=True, check=True, timeout=10)
        entries = plistlib.loads(result.stdout)
        value = str(entries[0].get("IOPlatformUUID", "")).strip().lower()
        kind = "apple-platform-uuid"
    else:
        raise HostHardwareError("platform has no supported permanent hardware identifier")
    normalized = value.replace("-", "")
    if not normalized or set(normalized) <= {"0"} or set(normalized) <= {"f"}:
        raise HostHardwareError("permanent hardware identifier is empty or a placeholder")
    if re.fullmatch(r"[0-9a-f]{8,64}", normalized) is None:
        raise HostHardwareError("permanent hardware identifier is invalid")
    digest = hashlib.sha256((kind + "\0" + value).encode()).hexdigest()
    return {"kind": kind, "fingerprint": "sha256:" + digest}


def binding_for(host_id: str, hardware: dict[str, str]) -> dict[str, object]:
    if re.fullmatch(r"ehost-[0-9a-f]{20}", host_id) is None:
        raise HostHardwareError("invalid public Host identity")
    if set(hardware) != {"kind", "fingerprint"} or not hardware["kind"] or re.fullmatch(
        r"sha256:[0-9a-f]{64}", hardware["fingerprint"]
    ) is None:
        raise HostHardwareError("invalid hardware observation")
    return {"contract_version": 1, "host_id": host_id, "hardware": hardware}


def verify_binding(raw: bytes, host_id: str, hardware: dict[str, str]) -> None:
    try:
        actual = json.loads(raw)
    except (ValueError, UnicodeDecodeError) as exc:
        raise HostHardwareError("Host hardware binding is unreadable") from exc
    if not isinstance(actual, dict) or type(actual.get("contract_version")) is not int or actual != binding_for(host_id, hardware):
        raise HostHardwareError(
            "Host identity was provisioned for another board; initialize an independent Host "
            "or use an explicit complete restore, never reuse its credentials implicitly"
        )
