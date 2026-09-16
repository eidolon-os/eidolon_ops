"""Which release each live process is actually executing from.

A ``current`` symlink is a statement about the next start, not about the
processes running now.  A unit that re-executed in the window between its stop
and the symlink flip loaded the previous release, and afterwards every signal
this product publishes reads green: ``systemctl is-active`` says active,
``/health`` answers ok, the ``current`` links point at the new release, and the
source comparison behind ``pending`` — which is derived from those links —
reports a Host that matches every checkout.  The process went on serving the
code it loaded.  On 2026-09-16 that meant ``GET /v1/session-traces`` answered
404 on a Host whose deployed source contained the route, until somebody
restarted the unit by hand.

The only durable evidence of that state is what each process was executed
from, so that is what this module reads, per process, out of ``/proc``:

``exe``
    the path of the executed file, as the kernel resolved it.  This names the
    release for a component that runs a real binary, because the ExecStart
    goes through ``/opt/eidolon/current/<component>`` and the kernel records
    what that symlink resolved to at exec time.

``cmdline``
    every argument, argv[0] included.  This is what names the release for the
    Python components, and it is the reason ``exe`` alone is not enough: each
    component runs a console script from its own per-release virtualenv, whose
    shebang holds the absolute interpreter path baked in when that virtualenv
    was built.  The kernel executes
    ``/opt/eidolon/releases/<id>/<component>/.venv/bin/python`` and puts it in
    argv[0] — while ``exe`` follows that interpreter's own symlink and
    resolves to ``/usr/bin/python3.13``, which names no release at all.

``cgroup``
    the systemd unit the process belongs to, so a report can name something an
    operator can restart rather than a pid that will not exist tomorrow.

Every process is read, rather than each unit's ``MainPID``, for the same
reason the collector beside this one derives its active graph from the links
rather than from a component table: the process that outlives a release is
precisely the one nobody thought to enumerate — a worker a unit forked, or a
unit a table forgot.
"""

from __future__ import annotations

import os
import re
from collections.abc import Iterator, Mapping
from pathlib import Path

from . import contract, primitives
from .primitives import TargetError

#: A process directory under ``/proc``.  Everything else there is a kernel
#: surface, and ``self`` is this reader looking at itself.
_PID = re.compile(r"^[1-9][0-9]*$")

#: What the kernel appends to a ``/proc`` link whose file has been unlinked —
#: the state a swept release leaves behind, and the one this exists to prevent.
_DELETED = " (deleted)"

#: A systemd unit as it appears in a cgroup path.  Only the two kinds the
#: product topology has: a ``.slice`` or ``.scope`` names a grouping, not
#: something an operator restarts.
_UNIT = re.compile(r"[A-Za-z0-9@:_.-]+\.(?:service|socket)")


def observe(*, root: Path = Path("/")) -> dict[str, object]:
    """Every live process running out of a release, grouped by release.

    Fails closed.  A ``/proc`` this cannot read is reported as an error rather
    than as an absence of processes: the whole point of this reading is to
    stand between a sweep and a live release, and a check that answers "none"
    when it cannot see is not a check.
    """

    releases = _release_roots(root)
    found: dict[str, list[dict[str, object]]] = {}
    for entry in _processes(primitives.host_path(root, Path("/proc"))):
        for release_id in sorted(_references(entry, releases)):
            found.setdefault(release_id, []).append(
                {"pid": int(entry.name), "unit": _unit(entry)}
            )
    for holders in found.values():
        holders.sort(key=lambda item: (str(item["unit"] or ""), int(str(item["pid"]))))
    return {"status": "observed", "releases": found}


def report(*, root: Path = Path("/")) -> dict[str, object]:
    """The same reading, as a field on a report that is not allowed to fail.

    ``status`` is asked constantly, and it is what someone runs when they
    already suspect trouble; it must not become unreachable because one of its
    readings did.  But an unreadable answer is published as unreadable and
    never as an empty one — "nothing runs an old release" and "I could not
    look" are the two answers this entire module exists to keep apart.
    """

    try:
        return observe(root=root)
    except TargetError as exc:
        return {"status": "unreadable", "releases": {}, "error": str(exc)}


def holders(observation: Mapping[str, object]) -> dict[str, list[dict[str, object]]]:
    """The release-to-process map out of an observation, checked not trusted."""

    releases = observation.get("releases")
    if not isinstance(releases, dict):
        raise TargetError("running release observation is invalid")
    return releases


def units_running(observation: Mapping[str, object], release_ids: set[str]) -> list[str]:
    """The units, named once each and in topology order, holding those releases.

    A pid is not an answer to "what do I restart", and the same unit holding
    two releases is one thing to restart, not two.
    """

    named: set[str] = set()
    for release_id, processes in holders(observation).items():
        if release_id not in release_ids:
            continue
        for process in processes:
            unit = process.get("unit")
            if isinstance(unit, str) and unit:
                named.add(unit)
    ordered = [unit for unit in contract.PRODUCT_UNITS if unit in named]
    return ordered + sorted(named.difference(ordered))


def active_release_ids(*, root: Path = Path("/")) -> set[str]:
    """Which release the component links name, read the same way twice over.

    Returns a set rather than one id on purpose.  A Host mid-cutover has links
    pointing at two releases, and collapsing that to "the" active release would
    invent an answer to a question the Host is in the middle of answering.
    """

    releases = _release_roots(root)
    named: set[str] = set()
    for component, absolute in contract.CURRENT_LINKS.items():
        link = primitives.host_path(root, absolute)
        if not link.is_symlink():
            continue
        release_id = _release_id(str(link.resolve(strict=False)), releases)
        if release_id is None:
            raise TargetError(f"current link points outside the release root: {component}")
        named.add(release_id)
    return named


def _processes(procfs: Path) -> Iterator[Path]:
    try:
        entries = sorted(procfs.iterdir(), key=lambda item: item.name)
    except OSError as exc:
        raise TargetError(
            "running processes cannot be read, so no release can be proven unused"
        ) from exc
    for entry in entries:
        if _PID.fullmatch(entry.name) is not None:
            yield entry


def _references(entry: Path, releases: tuple[str, ...]) -> set[str]:
    found: set[str] = set()
    for value in _executed_paths(entry):
        release_id = _release_id(value, releases)
        if release_id is not None:
            found.add(release_id)
    return found


def _executed_paths(entry: Path) -> Iterator[str]:
    """Every path this process was executed through, from both readings.

    A process that exits between the listing and the read is simply gone, and
    a kernel thread has neither reading; both arrive here as nothing, which is
    the truth about a process that holds no release open.
    """

    executable = _readlink(entry / "exe")
    if executable is not None:
        yield executable
    yield from _cmdline(entry / "cmdline")


def _readlink(path: Path) -> str | None:
    try:
        value = os.readlink(path)
    except OSError:
        return None
    return value.removesuffix(_DELETED)


def _cmdline(path: Path) -> tuple[str, ...]:
    try:
        raw = path.read_bytes()
    except OSError:
        return ()
    return tuple(
        part.decode("utf-8", "replace").removesuffix(_DELETED)
        for part in raw.split(b"\0")
        if part
    )


def _release_roots(root: Path) -> tuple[str, ...]:
    """The release root spelled both ways a /proc reading can come back in.

    argv holds the literal string a shebang or an ExecStart was written with,
    while ``exe`` and a link target come back resolved.  The two differ the
    moment any parent of the release root is itself a symlink, and matching
    only one of them would make a live release look unused — which is the one
    mistake this reading exists to prevent.
    """

    releases = primitives.host_path(root, contract.RELEASES)
    spellings = {str(releases), str(releases.resolve(strict=False))}
    return tuple(sorted(spellings))


def _release_id(value: str, roots: tuple[str, ...]) -> str | None:
    for releases in roots:
        prefix = f"{releases}/"
        if not value.startswith(prefix):
            continue
        name = value[len(prefix) :].split("/", 1)[0]
        if contract.RELEASE_ID.fullmatch(name) is not None:
            return name
    return None


def _unit(entry: Path) -> str | None:
    """The systemd unit owning this process, from its cgroup path.

    Read from the cgroup rather than matched against a unit table, so a worker
    a unit forked is reported under the unit an operator can act on.  The
    deepest unit segment wins: a service with sub-cgroups puts ``init.scope``
    below ``eidolon-channel.service``, and the service is the restartable one.
    """

    try:
        raw = (entry / "cgroup").read_text(encoding="utf-8")
    except OSError:
        return None
    for line in raw.splitlines():
        # cgroup v2: "0::/system.slice/eidolon-channel-provider.service"
        # cgroup v1: "1:name=systemd:/system.slice/eidolon-channel-provider.service"
        for segment in reversed(line.rpartition(":")[2].split("/")):
            if _UNIT.fullmatch(segment) is not None:
                return segment
    return None


def converge(
    payload: Mapping[str, object], *, root: Path = Path("/")
) -> dict[str, object]:
    """Restart every unit still executing a release the links no longer name.

    Runs immediately after an activation and before its health gate, which is
    the only ordering that makes the gate mean anything: a gate that passes
    against a process left over from the previous release has measured the
    previous release.  It is also the ordering that keeps a failure here
    recoverable — the transaction has not yet crossed into commit, so a unit
    that will not come off the old release rolls the deploy back instead of
    ending it half done.

    Restarts are targeted rather than a full release restart: the remedy that
    was proven by hand on 2026-09-16 is one ``systemctl restart`` of the unit
    that disagreed, and a Host that has just passed activation has no reason
    to take its database down again.  They are issued in product startup
    order, and only for units this Host's reviewed topology contains.

    Then it reads again.  A restart that reports success and leaves the
    process where it was is the same class of failure as the one this whole
    module exists for, so the second reading — not the exit status of
    ``systemctl`` — is what decides.
    """

    topology = set(contract.fixed_units(payload))
    seconds = contract.release_readiness_seconds(payload)
    active = active_release_ids(root=root)
    if len(active) != 1:
        raise TargetError(
            "release convergence requires every current link to name one release; "
            f"the Host publishes {sorted(active) or 'none'}"
        )
    before = observe(root=root)
    stale = set(holders(before)).difference(active)
    if not stale:
        return {
            "status": "converged",
            "release_id": next(iter(active)),
            "restarted": [],
            "running_releases": holders(before),
        }
    restarted = [unit for unit in units_running(before, stale) if unit in topology]
    for unit in restarted:
        primitives.checked(
            f"stale unit restart: {unit}",
            ("/usr/bin/systemctl", "restart", unit),
            timeout=seconds,
        )
    after = observe(root=root)
    remaining = set(holders(after)).difference(active)
    if remaining:
        raise TargetError(
            "these processes still run a release this Host no longer publishes: "
            + _describe(after, remaining)
            + f"; the Host's current links name {next(iter(active))}"
        )
    return {
        "status": "reconverged",
        "release_id": next(iter(active)),
        "restarted": restarted,
        "stale_release_ids": sorted(stale),
        "running_releases": holders(after),
    }


def _describe(observation: Mapping[str, object], release_ids: set[str]) -> str:
    """Name what is holding a release, so the reader has something to act on."""

    lines: list[str] = []
    for release_id in sorted(release_ids):
        for process in holders(observation)[release_id]:
            unit = process.get("unit") or "no systemd unit"
            lines.append(f"{unit} (pid {process['pid']}) on {release_id}")
    return ", ".join(lines)
