"""Collect and put back the memory spaces, by asking the component that owns them.

Every other authority in a backup is a SQLite file this agent copies itself. A
memory space is not: it is a palace directory whose layout belongs to MemPalace,
a sibling ledgers directory that is ours, and an embedder identity without which
a copy cannot be restored under the right encoder. Guessing at any of that is
how a backup restores into something that opens and then answers wrongly, which
is why memory sat on ``UNCOVERED_STATE`` until the component declared a snapshot
of its own.

So this asks. Memory's supervisor owns realm lifecycle and knows where palaces
live; it produces a self-describing directory with a manifest, and this agent's
job is the operator half: say where, collect what came back, and check that the
manifest describes what is actually on disk. A manifest nobody checks is a
sentence a backup tells itself.

Two operational facts shape the code:

- **The supervisor writes as its own account.** Root creates the directory and
  hands it to that account first; otherwise the snapshot fails on a permission
  the operator would then have to diagnose from inside another process's log.
- **A restore reads it the same way**, so an uploaded backup is handed over
  before it is asked for, and taken back afterwards. The operator keeps the
  copy; the service only ever borrows it.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from collections.abc import Mapping
from pathlib import Path
from urllib.parse import urlsplit

from . import contract, primitives
from .primitives import TargetError

#: Where the spaces sit inside a backup directory, beside the authority copies.
SPACES_DIRECTORY = "memory"

#: The account memory's supervisor runs as, and therefore the one that has to be
#: able to write a snapshot. Same identity the palaces themselves are owned by.
SERVICE_ACCOUNT = ("eidolon", "eidolon")

#: The routes this asks for, in the form memory declares them in its ops
#: contract. Named rather than inlined so ``tests/test_component_contract_drift``
#: can hold the two side by side: a component that renames its snapshot action
#: should break a test here, not a backup on a board.
LIST_ACTION = "GET /api/admin/realms"
SNAPSHOT_ACTION = "POST /api/admin/realms/{memory_realm_id}/snapshot"
RESTORE_ACTION = "POST /api/admin/realms/{memory_realm_id}/restore"

_TIMEOUT_LIST_SECONDS = 30.0
#: A snapshot is a ``VACUUM INTO`` per file over a palace that may hold years of
#: someone's memory. Long enough to finish on a board, short enough that a wedged
#: supervisor does not hold a backup open forever.
_TIMEOUT_SNAPSHOT_SECONDS = 600.0
_TIMEOUT_RESTORE_SECONDS = 900.0


def _request(base_url: str, path: str, *, method: str, body: Mapping[str, object] | None,
             timeout: float) -> object:
    if not path.startswith("/api/admin/"):
        raise TargetError(f"refusing to call memory outside its admin API: {path}")
    request = urllib.request.Request(
        f"{base_url}{path}",
        method=method,
        data=None if body is None else json.dumps(body).encode("utf-8"),
        headers={"content-type": "application/json"} if body is not None else {},
    )
    if urlsplit(request.full_url).hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise TargetError(f"memory admin API must be on this Host: {base_url}")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace").strip()
        raise TargetError(f"memory refused {method} {path}: {exc.code} {detail}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise TargetError(f"memory is not answering on {base_url}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise TargetError(f"memory answered {path} with something that is not JSON") from exc


def realm_ids(base_url: str) -> list[str]:
    """Which spaces this Host holds, according to the component that holds them."""

    method, path = LIST_ACTION.split(" ", 1)
    listed = _request(base_url, path, method=method, body=None,
                      timeout=_TIMEOUT_LIST_SECONDS)
    if not isinstance(listed, list):
        raise TargetError("memory did not answer with a list of realms")
    identifiers: list[str] = []
    for entry in listed:
        spec = entry.get("spec") if isinstance(entry, dict) else None
        realm = (spec or {}).get("memory_realm_id") if isinstance(spec, dict) else None
        if not isinstance(realm, str) or not realm:
            raise TargetError("memory listed a realm without an id")
        identifiers.append(realm)
    return identifiers


def capture(base_url: str, destination: Path) -> list[dict[str, object]]:
    """Snapshot every space into ``destination``, and check what came back.

    Raises rather than returning a partial set: a backup that carries three of
    four spaces is one an operator would restore believing it whole.
    """

    root = Path(destination)
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    # The supervisor writes these, not this agent.
    primitives.chown_path(root, *SERVICE_ACCOUNT)
    captured: list[dict[str, object]] = []
    for realm in realm_ids(base_url):
        target = root / _directory_name(realm)
        method, path = SNAPSHOT_ACTION.split(" ", 1)
        answer = _request(
            base_url,
            path.format(memory_realm_id=realm),
            method=method,
            body={"destination": str(target)},
            timeout=_TIMEOUT_SNAPSHOT_SECONDS,
        )
        if not isinstance(answer, dict):
            raise TargetError(f"memory answered a snapshot of {realm} with a non-object")
        manifest = answer.get("manifest")
        if not isinstance(manifest, dict):
            raise TargetError(f"snapshot of {realm} came back without a manifest")
        _verify(target, manifest, realm=realm)
        captured.append(
            {
                "memory_space_id": realm,
                "directory": f"{SPACES_DIRECTORY}/{_directory_name(realm)}",
                "taken_at": manifest.get("taken_at"),
                "embedder_identity": manifest.get("embedder_identity"),
                "file_count": len(manifest.get("entries") or []),
                "bytes": sum(int(entry.get("bytes", 0)) for entry in manifest["entries"]),
            }
        )
    _hand_to_operator(root)
    return captured


def put_back(base_url: str, source: Path, spaces: list[Mapping[str, object]]) -> list[dict]:
    """Ask memory to become each copy again, one space at a time.

    Ordered after the product is running because only a live supervisor can take
    a realm's runner off its palace — one process holds a palace. The refusals
    that matter (a different realm, a different embedder, a file that does not
    match its digest) are memory's to make, and it makes them before it writes.
    """

    root = Path(source)
    restored: list[dict] = []
    for space in spaces:
        realm = space.get("memory_space_id")
        directory = space.get("directory")
        if not isinstance(realm, str) or not isinstance(directory, str):
            raise TargetError("backup names a memory space without an id or directory")
        copy = root / Path(directory).name
        if copy.parent != root or not copy.is_dir():
            raise TargetError(f"backup is missing the copy of {realm}")
        _hand_to_service(copy)
        try:
            method, path = RESTORE_ACTION.split(" ", 1)
            answer = _request(
                base_url,
                path.format(memory_realm_id=realm),
                method=method,
                body={"source": str(copy)},
                timeout=_TIMEOUT_RESTORE_SECONDS,
            )
        finally:
            # The operator keeps their backup; the service only borrowed it.
            _hand_to_operator(copy)
        if not isinstance(answer, dict):
            raise TargetError(f"memory answered the restore of {realm} with a non-object")
        restored.append(
            {
                "memory_space_id": realm,
                "worker_running": answer.get("worker_running"),
                "taken_at": (answer.get("restored") or {}).get("taken_at"),
            }
        )
    return restored


def _verify(directory: Path, manifest: Mapping[str, object], *, realm: str) -> None:
    """Check the manifest against the files, which is the operator's half.

    Memory decided what a complete space is; this decides whether what landed on
    disk is that. Both halves are needed: a manifest that lists a file nobody
    wrote describes a backup, not a copy.
    """

    entries = manifest.get("entries")
    if not isinstance(entries, list) or not entries:
        raise TargetError(f"snapshot of {realm} lists no files")
    for entry in entries:
        if not isinstance(entry, dict):
            raise TargetError(f"snapshot of {realm} has a malformed entry")
        relative = entry.get("path")
        if not isinstance(relative, str) or relative.startswith("/") or ".." in relative:
            raise TargetError(f"snapshot of {realm} names a file outside itself: {relative}")
        path = directory / relative
        if not path.is_file():
            raise TargetError(f"snapshot of {realm} is missing {relative}")
        if primitives.file_sha256(path) != entry.get("sha256"):
            raise TargetError(f"snapshot of {realm} does not match its digest: {relative}")


def _directory_name(realm: str) -> str:
    """A realm id as a directory name, refused rather than sanitised.

    Sanitising would let two realms land in one directory, and a backup that
    silently holds one space twice is worse than one that refuses to be taken.
    """

    if not realm or "/" in realm or realm.startswith(".") or realm in {".", ".."}:
        raise TargetError(f"memory realm id is not usable as a directory: {realm!r}")
    return realm


def _hand_to_service(path: Path) -> None:
    for child in _tree(path):
        primitives.chown_path(child, *SERVICE_ACCOUNT)


def _hand_to_operator(path: Path) -> None:
    for child in _tree(path):
        primitives.give_to_invoking_operator(child)


def _tree(path: Path) -> list[Path]:
    return [path, *(child for child in Path(path).rglob("*"))]


def unavailable(reason: str) -> dict[str, object]:
    """The entry a backup adds to ``not_covered`` when memory could not answer.

    A Host whose memory service is down still has authorities worth copying, and
    a backup that refused to exist would leave the operator with nothing at all.
    What it must not do is stay silent: the same table that named memory as
    uncovered while it had no declared snapshot now names it as uncovered *this
    time*, with the reason.
    """

    return {
        "state": "memory",
        "path": str(contract.MEMORY_STATE_ROOT),
        "reason": reason,
    }
