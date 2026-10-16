"""Recover an abandoned Unix supervisor endpoint without trusting a PID file."""
from __future__ import annotations

import errno
import os
from pathlib import Path
import socket
import stat
import sys


def reclaim_abandoned_socket(path: Path) -> bool:
    """Unlink only an operator-owned socket whose connect returns ECONNREFUSED.

    A pathname is not a reusable connection: a restarted supervisor must bind
    its own listening socket. Live, inaccessible and replaced endpoints are
    deliberately left intact. Normal supervisor reuse stays with XML-RPC.
    """
    try:
        before = path.lstat()
    except FileNotFoundError:
        return False
    if not stat.S_ISSOCK(before.st_mode) or before.st_uid != os.getuid():
        raise RuntimeError(f"refusing to remove non-socket or foreign endpoint: {path}")
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as probe:
        probe.settimeout(1.0)
        try:
            probe.connect(str(path))
        except OSError as exc:
            if exc.errno != errno.ECONNREFUSED:
                raise RuntimeError(f"cannot establish abandoned endpoint: {path}: {exc}") from exc
        else:
            raise RuntimeError(f"endpoint has a listener but supervisor control is unavailable: {path}")
    try:
        after = path.lstat()
    except FileNotFoundError:
        return False
    if (before.st_dev, before.st_ino, before.st_ctime_ns) != (
        after.st_dev, after.st_ino, after.st_ctime_ns
    ):
        raise RuntimeError(f"endpoint changed during recovery: {path}")
    path.unlink()
    return True


if __name__ == "__main__":
    try:
        if reclaim_abandoned_socket(Path(sys.argv[1])):
            print("Recovered abandoned supervisor socket")
    except (OSError, RuntimeError) as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(1)
