"""Read a component's existing provider credentials, and write them back safely.

Only the fixed external keys are imported, and only when the value looks like a
real secret rather than the placeholder a template ships with.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from pathlib import Path

from eidolon_ops.errors import InstallInputError

ENV_KEY = re.compile(r"^[A-Z][A-Z0-9_]*$")
#: Which external provider key each component already owns. Nothing else is
#: read out of a component's own environment file.
EXTERNAL_KEYS = {
    "eidolon_agent": ("EIDOLON_AGENT_LLM_API_KEY",),
    "eidolon_channel": (
        "OPENAI_LLM_API_KEY",
        "BAILIAN_STT_API_KEY",
        "BAILIAN_TTS_API_KEY",
    ),
    "eidolon_memory": ("EIDOLON_MEMORY_LLM_API_KEY",),
}
OPTIONAL_CHANNEL_KEYS = ("SENSETIME_STT_API_KEY", "SENSETIME_TTS_API_KEY")


def parse_provider_env(path: Path) -> dict[str, str]:
    if not path.is_file() or path.is_symlink():
        raise InstallInputError(f"provider env source is missing or unsafe: {path}")
    values: dict[str, str] = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError) as exc:
        raise InstallInputError(f"provider env source is unreadable: {path}") from exc
    for line_number, raw in enumerate(lines, start=1):
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        key, separator, value = stripped.partition("=")
        if not separator or ENV_KEY.fullmatch(key) is None or key in values:
            raise InstallInputError(f"provider env syntax is invalid: {path}:{line_number}")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        if any(character.isspace() or ord(character) < 32 for character in value):
            raise InstallInputError(f"provider env value has unsupported whitespace: {path}:{key}")
        values[key] = value
    return values


def usable_secret(value: str | None, *, key: str) -> bool:
    if value is None or len(value) < 8:
        return False
    lowered = value.lower()
    return value != key and not lowered.startswith("your-") and "placeholder" not in lowered


def serialize_env(values: Mapping[str, str]) -> bytes:
    for key, value in values.items():
        if (
            ENV_KEY.fullmatch(key) is None
            or not value
            or any(character.isspace() or ord(character) < 32 for character in value)
        ):
            raise InstallInputError(f"generated env entry is unsafe: {key}")
    text = "".join(f"{key}={values[key]}\n" for key in sorted(values))
    return text.encode("utf-8")
