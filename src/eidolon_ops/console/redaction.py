"""A second pair of hands over what leaves this process.

Every report Ops produces is already written not to carry credential values —
each one says so, and the input set's own contract is that values never enter a
receipt or a diagnostic. This is not a substitute for that. It is the check that
costs nothing and would have caught it if some component's report one day names
a key the operator should never have been shown in a browser tab.

An operation may declare that one key is exactly what it exists to reveal —
``commissioning-code`` mints a Setup code for a human to read — and that key is
let through, marked, and never written anywhere but the response.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence

__all__ = ["REDACTED", "redacted"]

REDACTED = "[redacted by console]"

#: Whole key names that never carry anything an operator needs to read.
_NAMES = frozenset(
    {
        "api_key",
        "credential",
        "credentials",
        "passphrase",
        "password",
        "private_key",
        "secret",
        "secrets",
        "token",
    }
)
#: Suffixes, so ``service_token`` and ``hub_private_key`` are covered without
#: taking ``secret_staging`` — a path — with them.
_SUFFIXES = (
    "_api_key",
    "_credential",
    "_passphrase",
    "_password",
    "_private_key",
    "_secret",
    "_token",
)


def redacted(value: object, *, allow: Iterable[str] = ()) -> object:
    """Walk a report and mask the values of keys that should never be read."""

    permitted = frozenset(allow)
    return _walk(value, permitted)


def _walk(value: object, permitted: frozenset[str]) -> object:
    if isinstance(value, Mapping):
        return {
            str(key): (
                REDACTED
                if _is_sensitive(str(key), permitted)
                else _walk(item, permitted)
            )
            for key, item in value.items()
        }
    if isinstance(value, Sequence) and not isinstance(value, str | bytes):
        return [_walk(item, permitted) for item in value]
    return value


def _is_sensitive(key: str, permitted: frozenset[str]) -> bool:
    if key in permitted:
        return False
    lowered = key.lower()
    return lowered in _NAMES or lowered.endswith(_SUFFIXES)
