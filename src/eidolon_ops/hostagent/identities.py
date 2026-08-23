"""Idempotent service-principal cutover for first install and upgrades."""

from __future__ import annotations

import os
from collections.abc import Mapping

from . import contract, primitives
from .primitives import TargetError

SERVICE_USERS = (
    "eidolon",
    "eidolon-bootstrap",
    "eidolon-local-api",
    "eidolon-lifecycle",
)
SOCKET_GROUP = "eidolon-lifecycle-client"


def ensure_service_identities(payload: Mapping[str, object]) -> dict[str, object]:
    contract.fixed_units(payload)
    if os.geteuid() != 0:
        raise TargetError("service identity cutover requires root")
    _ensure_group(SOCKET_GROUP)
    for name in SERVICE_USERS:
        _ensure_user(name)
    observed: dict[str, int] = {}
    for name in SERVICE_USERS:
        uid = _id_value(("/usr/bin/id", "-u", name), "service uid inspection")
        if uid == 0:
            raise TargetError(f"service identity must not be root: {name}")
        primary = primitives.checked(
            "service primary group inspection", ("/usr/bin/id", "-gn", name)
        ).stdout.strip()
        if primary != name:
            raise TargetError(f"service identity has unexpected primary group: {name}")
        observed[name] = uid
    if len(set(observed.values())) != len(observed):
        raise TargetError("service identities must have distinct UIDs")
    group = primitives.checked(
        "socket group inspection", ("/usr/bin/getent", "group", SOCKET_GROUP)
    ).stdout.strip()
    fields = group.split(":")
    if len(fields) != 4 or fields[3].strip():
        raise TargetError("socket group has persistent members")
    return {
        "status": "service_identities_ready",
        "uids": observed,
        "socket_group": SOCKET_GROUP,
        "persistent_socket_group_members": [],
    }


def _ensure_group(name: str) -> None:
    result = primitives.run(("/usr/bin/getent", "group", name), timeout=30)
    if result.returncode != 0:
        primitives.checked(
            "service group creation", ("/usr/sbin/groupadd", "--system", name)
        )


def _ensure_user(name: str) -> None:
    _ensure_group(name)
    result = primitives.run(("/usr/bin/id", "-u", name), timeout=30)
    if result.returncode != 0:
        primitives.checked(
            "service user creation",
            (
                "/usr/sbin/useradd",
                "--system",
                "--gid",
                name,
                "--home-dir",
                "/nonexistent",
                "--shell",
                "/usr/sbin/nologin",
                name,
            ),
        )


def _id_value(command: tuple[str, ...], operation: str) -> int:
    value = primitives.checked(operation, command).stdout.strip()
    try:
        return int(value)
    except ValueError as exc:
        raise TargetError(f"{operation} returned an invalid value") from exc
