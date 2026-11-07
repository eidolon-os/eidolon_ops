"""Workstation bundle ownership, bounded retention and transaction exclusion.

The lock lives outside bundle directories and is never unlinked: removing a
locked inode would let another process acquire a different lock at the same path.
"""
from __future__ import annotations

import fcntl
import json
import math
import os
import time
from contextlib import contextmanager, suppress
from functools import wraps
from pathlib import Path

from eidolon_ops.config import validate_release_id
from eidolon_ops.errors import OperationsError

RETENTION_SECONDS = 48 * 60 * 60
MAX_BUNDLES = 8


class BundleLifecycle:
    def __init__(self, root: Path):
        self.root = root
        self.path = root / ".ops-bundles.json"

    @contextmanager
    def lock(self):
        self.root.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.root / ".ops-bundles.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise OperationsError("another release is using this bundle root; retry later") from exc
            yield
        finally:
            os.close(fd)

    def read(self):
        try:
            if self.path.is_symlink():
                raise ValueError("symlinked registry")
            if not self.path.exists():
                return {}
            records = json.loads(self.path.read_text())
            if not isinstance(records, dict):
                raise ValueError("invalid registry")
            for key, item in records.items():
                validate_release_id(key)
                if item["state"] not in {"building", "failed", "prepared", "committed"}:
                    raise ValueError("invalid state")
                if (not isinstance(item["updated_at"], (int, float))
                        or not math.isfinite(item["updated_at"])):
                    raise ValueError("invalid timestamp")
            return records
        except (OSError, ValueError, TypeError, KeyError) as exc:
            raise OperationsError(f"cannot safely read bundle registry: {self.path}") from exc

    def write(self, records):
        temporary = self.path.with_suffix(".tmp")
        fd = os.open(temporary, os.O_CREAT | os.O_TRUNC | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "w") as stream:
            json.dump(records, stream, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, self.path)

    def mark(self, release_id, state, *, create=False):
        records = self.read()
        if create or release_id in records:
            records[release_id] = {"state": state, "updated_at": time.time()}
            self.write(records)

    def sweep(self, remove, *, exclude, now=None, retention_seconds=RETENTION_SECONDS):
        records = self.read()
        cutoff = (time.time() if now is None else now) - retention_seconds
        expired, failures = [], []
        for release_id, item in list(records.items()):
            if release_id == exclude:
                continue
            if not (self.root / release_id).exists():
                del records[release_id]
                continue
            # Prepared packages await an explicit activate/abandon decision.
            if item["state"] == "prepared":
                continue
            if item["state"] != "committed" and item["updated_at"] > cutoff:
                continue
            result = remove(release_id)
            if result["status"] in {"removed", "absent"}:
                expired.append(result)
                del records[release_id]
            else:
                failures.append(result)
        self.write(records)
        return {"expired": expired, "failures": failures}


def bundle_transaction(method):
    """Hold exclusion through preflight, upload, activation and final cleanup."""
    @wraps(method)
    def wrapped(self, *args, **kwargs):
        if method.__name__ == "install" and not kwargs.get("apply", False):
            return method(self, *args, **kwargs)
        release_id = validate_release_id(kwargs["release_id"])
        lifecycle = BundleLifecycle(self.config.workspace.bundle_root)
        with lifecycle.lock():
            cleanup = lifecycle.sweep(self.bundles._remove_local_bundle, exclude=release_id)
            try:
                result = method(self, *args, **kwargs)
            except BaseException:
                # Preserve the original failure even if bookkeeping cannot be written.
                with suppress(OSError, OperationsError):
                    if lifecycle.read().get(release_id, {}).get("state") != "prepared":
                        lifecycle.mark(release_id, "failed")
                raise
            try:
                lifecycle.mark(release_id, "prepared" if result.get("status") == "dry_run" else "committed")
                records = lifecycle.read()
                if not (lifecycle.root / release_id).exists():
                    records.pop(release_id, None)
                    lifecycle.write(records)
            except (OSError, OperationsError) as exc:
                cleanup["failures"].append({"error": str(exc)})
            result["local_bundle_reclamation"] = cleanup
            return result
    return wrapped
