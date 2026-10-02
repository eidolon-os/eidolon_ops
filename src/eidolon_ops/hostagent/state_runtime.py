"""Execution bindings for shared state operations; no component inventory here."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

from . import contract, lifecycle, primitives, reset


@dataclass(frozen=True)
class StateRuntime:
    staging_root: Path
    host_id: Callable[[], str | None]
    own: Callable[[Path, str, str], None]
    hand_to_operator: Callable[[Path], None]
    stop: Callable[[], object]
    start: Callable[[], Mapping[str, object]]
    prepare: Callable[[], object] | None = None


def system_runtime(payload: Mapping[str, object], host_id: Callable[[], str | None]) -> StateRuntime:
    """The installed-service adapter. Source adapters supply ordinary-user bindings."""
    contract.fixed_units(payload)
    return StateRuntime(
        staging_root=contract.VAR_TMP,
        host_id=host_id,
        own=primitives.chown_path,
        hand_to_operator=primitives.give_to_invoking_operator,
        stop=lambda: reset.command_stop_units(contract.RESET_STOP_UNITS),
        start=lambda: lifecycle.lifecycle("start", payload),
    )
