"""One-time state ownership transfer. Call only after stopping the Host.

The SQLite schema is unchanged and belongs to Hub. Ops moves its location,
preserving the original database for rollback; it does not interpret rows.
"""

from __future__ import annotations

import os
import sqlite3
import tempfile
from contextlib import closing
from pathlib import Path

from eidolon_ops.errors import OperationsError


def pending(state_root: Path) -> bool:
    return (state_root / "channel/smarthome.sqlite3").exists() and not (
        state_root / "hub/smarthome.sqlite3"
    ).exists()


def transfer(state_root: Path) -> None:
    """Copy a quiesced authority, including WAL, and publish without overwriting."""
    source = state_root / "channel/smarthome.sqlite3"
    target = state_root / "hub/smarthome.sqlite3"
    if source.is_symlink() or target.is_symlink() or not source.is_file():
        raise OperationsError("smart-home migration requires a regular source database")
    if target.exists():
        raise OperationsError("smart-home destination already exists; refusing to overwrite")
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, name = tempfile.mkstemp(prefix=".smarthome-transfer-", dir=target.parent)
    os.close(fd)
    temporary = Path(name)
    try:
        with (
            closing(sqlite3.connect(f"{source.as_uri()}?mode=ro", uri=True)) as original,
            closing(sqlite3.connect(temporary)) as copied,
        ):
            original.backup(copied)
            if copied.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
                raise OperationsError("smart-home state failed SQLite integrity check")
        with temporary.open("rb") as stream:
            os.fsync(stream.fileno())
        os.link(temporary, target)  # atomic, fails if another destination appeared
        directory = os.open(target.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        temporary.unlink(missing_ok=True)
