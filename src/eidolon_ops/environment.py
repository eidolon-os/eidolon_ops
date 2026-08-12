"""One reading of an environment file, and one way to change values in it.

The Mac and Pi paths each had their own parser and their own merge, with
different opinions about what an invalid line is. They render the same files
for the same components, so a difference between them could only ever be a bug
in one of them.
"""

from __future__ import annotations

import re
from collections.abc import Mapping

#: A component's own variable: whoever owns the component names it.
SERVICE_KEY = re.compile(r"[A-Z][A-Z0-9_]*")
#: A variable Ops itself generates. It stays inside one namespace so a
#: generated file can never quietly redefine something it does not own.
OPS_KEY = re.compile(r"EIDOLON_[A-Z0-9_]*")


class EnvironmentFileError(ValueError):
    """An environment file, or a value written into one, is invalid."""


def parse(
    value: str,
    *,
    label: str = "environment",
    key: re.Pattern[str] | None = None,
) -> dict[str, str]:
    """Read ``KEY=value`` lines, refusing anything that is not exactly that."""

    values: dict[str, str] = {}
    for raw in value.splitlines():
        if not raw:
            continue
        name, separator, content = raw.partition("=")
        if (
            not separator
            or not name
            or not content
            or name in values
            or (key is not None and key.fullmatch(name) is None)
        ):
            raise EnvironmentFileError(f"{label} is invalid")
        values[name] = content
    return values


def serialize(values: Mapping[str, str]) -> str:
    return "".join(f"{name}={values[name]}\n" for name in sorted(values))


def merge(value: str, replacements: Mapping[str, str], *, label: str = "environment") -> str:
    """Set values, adding names the seed does not have."""

    parsed = parse(value, label=label)
    for name, replacement in replacements.items():
        _require_safe(name, replacement, label=label)
        parsed[name] = replacement
    return serialize(parsed)


def replace(value: str, replacements: Mapping[str, str], *, label: str = "environment") -> str:
    """Set values that must already be declared, refusing to invent a name."""

    parsed = parse(value, label=label)
    for name, replacement in replacements.items():
        if name not in parsed:
            raise EnvironmentFileError(f"{label} lacks {name}")
        _require_safe(name, replacement, label=label)
        parsed[name] = replacement
    return serialize(parsed)


def serialize_ops(values: Mapping[str, str]) -> str:
    """Serialize an Ops-generated file, refusing an unsafe name or value."""

    for name, value in values.items():
        if OPS_KEY.fullmatch(name) is None:
            raise EnvironmentFileError(f"unsafe generated profile key: {name}")
        _require_safe(name, value, label="generated profile")
    return serialize(values)


def _require_safe(name: str, value: str, *, label: str) -> None:
    if not value or any(character in value for character in "\n\r\0"):
        raise EnvironmentFileError(f"{label} value is unsafe: {name}")
