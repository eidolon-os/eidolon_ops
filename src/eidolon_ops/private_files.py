"""Write a file nobody else may read, or do not write it at all.

Three copies of this existed, one of which created the file world-readable for
the instant between ``open`` and ``chmod``. Material is written through one
implementation so that window cannot reappear in only some of them.
"""

from __future__ import annotations

import os
from pathlib import Path


def atomic_private_file(path: Path, content: bytes, *, mode: int = 0o600) -> None:
    """Replace ``path`` with ``content``, never observable at a wider mode."""

    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        os.chmod(path, mode)
    finally:
        temporary.unlink(missing_ok=True)
