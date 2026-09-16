"""One-time delivery bookkeeping; never consulted by ordinary runtime or updates."""
from __future__ import annotations

import json
import stat
from pathlib import Path

from eidolon_ops.errors import OperationsError
from eidolon_ops.hostagent.hardware import BINDING_FILE, binding_for, verify_binding
from eidolon_ops.hostagent.primitives import exclusive
from eidolon_ops.private_inputs import ensure_private_parent, write_private_file


def recorded_delivery(root: Path) -> bytes | None:
    """The binding this profile already holds, or None. Reads, never writes.

    Separate from :func:`bind_delivery` so an operation can report what is
    recorded without deciding anything: the write path refuses an absent
    binding it is not allowed to create, and a plan needs to describe that
    state rather than raise it.
    """

    path = root / BINDING_FILE
    if not path.exists() and not path.is_symlink():
        return None
    if path.is_symlink() or not path.is_file() or stat.S_IMODE(path.stat().st_mode) != 0o600:
        raise OperationsError("Host delivery binding is unsafe")
    return path.read_bytes()


def bind_delivery(root: Path, host_id: str, hardware: dict[str, str],
                  *, allow_create: bool = True) -> bytes:
    ensure_private_parent(root)
    with exclusive(root / ".host-delivery.lock"):
        return _bind_delivery(root, host_id, hardware, allow_create=allow_create)


def _bind_delivery(root: Path, host_id: str, hardware: dict[str, str],
                   *, allow_create: bool) -> bytes:
    path = root / BINDING_FILE
    if path.exists() or path.is_symlink():
        if path.is_symlink() or not path.is_file() or stat.S_IMODE(path.stat().st_mode) != 0o600:
            raise OperationsError("Host delivery binding is unsafe")
        raw = path.read_bytes()
        verify_binding(raw, host_id, hardware)
        return raw
    if not allow_create:
        raise OperationsError(
            "used legacy identity has no hardware delivery evidence; use ordinary deploy "
            "to preserve the installed Host, or initialize independent inputs for a new board"
        )
    raw = (json.dumps(binding_for(host_id, hardware), sort_keys=True) + "\n").encode()
    write_private_file(path, raw)
    return raw
