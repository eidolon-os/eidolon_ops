"""Set aside a Kernel authority this release's Kernel refuses to open.

The Kernel supports no migrations, deliberately: opening a database of unknown
shape as if it were the expected one is worse than refusing. The consequence is
a Host that will not start after a Kernel schema change, and the only way to
start it is to move that file out of the way.

Until now nothing on either Host owned that move. ``reset --wipe-authority-data``
is the only command that produces a startable Host, and it destroys every
authority and advances the Owner Domain generation — several orders too large an
answer to "Kernel is one schema behind". So the move was done by hand, and this
workspace still holds one such file
(``eidolon-kernel.sqlite3.stale-schema-20260825T175934``) with no record anywhere
of what was in it.

Three things make this operation different from that:

* **It only runs when the Kernel says so.** Not when a version number looks
  wrong, not when the unit is observed crash-looping: the installed Kernel is
  asked whether it will open this database, and this refuses unless it says no.
  Without that gate this is a general-purpose button for destroying the Kernel
  authority, and the fact it destroys is one only the Owner can put back.
* **It renames.** The old file stays beside the new one under
  ``.stale-schema-<timestamp>``, the convention the one hand-done instance left
  behind. Nothing can read it back — a backup at a schema this Kernel refuses is
  still a schema this Kernel refuses — but a file that still exists can be
  opened by a person, and a deleted one cannot.
* **It says what it costs, in the Kernel's own words.** The count comes from the
  installed Kernel's ``schema_report``, run through the Kernel's own
  interpreter, rather than from this module restating which table holds an
  Owner's selection. One definition, two readers.

The Mac source run holds the same authority under its own state root and needs
the same three things, so the work is in path-taking helpers and the product
locations are supplied by the two entry points at the bottom.
"""

from __future__ import annotations

import json
import os
import subprocess
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path

from . import contract, primitives
from .primitives import TargetError

KERNEL_DATABASE = Path("/var/lib/eidolon/eidolon-kernel.sqlite3")
KERNEL_UNIT = "eidolon-kernel.service"
#: Stopped with the Kernel, because it is what would start the Kernel again
#: halfway through the rename.
KERNEL_RECONCILER_UNITS = contract.RESET_RECONCILER_UNITS
#: Moved with the database. A ``-wal`` left behind is the previous schema's
#: uncommitted tail, and the new Kernel would find it beside a database it did
#: not write. The ``.lock`` file is deliberately not in this list: it holds no
#: data, and the next Kernel takes the same flock on the same path.
SQLITE_SIDECARS = ("-wal", "-shm", "-journal")
#: The Kernel answers three questions here — will I open this, why not, and what
#: does setting it aside destroy — in one call, so the number an operator read in
#: the refusal and the number they approve here have one source.
CENSUS_SCRIPT = (
    "import json,sys;"
    "from pathlib import Path;"
    "from eidolon_kernel.adapters.persistence.sqlite import schema_report;"
    "print(json.dumps(schema_report(Path(sys.argv[1]))))"
)
#: The suffix the one hand-done instance of this used, kept rather than
#: improved: an operator who finds both files should not have to work out which
#: convention produced which.
SET_ASIDE_PREFIX = ".stale-schema-"


def set_aside_suffix(at: datetime | None = None) -> str:
    moment = at if at is not None else datetime.now(UTC)
    return SET_ASIDE_PREFIX + moment.astimezone(UTC).strftime("%Y%m%dT%H%M%S")


def kernel_evidence(
    *,
    database: Path,
    interpreter: Path,
    command: Callable[..., subprocess.CompletedProcess[str]] = primitives.run,
) -> dict[str, object]:
    """Ask this Host's own Kernel about this Host's own Kernel database."""

    if not interpreter.is_file() or not os.access(interpreter, os.X_OK):
        raise TargetError(
            "the installed Kernel cannot be asked what this database would cost: "
            f"{interpreter} is missing. Refusing rather than setting an authority "
            "aside on a guess"
        )
    result = command((str(interpreter), "-c", CENSUS_SCRIPT, str(database)), timeout=60)
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or "no output"
        raise TargetError(f"Kernel schema inspection failed: {detail}")
    try:
        document = json.loads(result.stdout)
    except ValueError as exc:
        raise TargetError("Kernel schema inspection returned no report") from exc
    if not isinstance(document, dict) or "accepted" not in document:
        raise TargetError("Kernel schema inspection returned an unrecognized report")
    return document


def refuse_unless_warranted(
    report: Mapping[str, object], *, acknowledged: int | None
) -> None:
    """Three ways this must not run, each of them a Host made worse.

    The first is the important one: it runs only when the installed Kernel
    itself says it will not open this file. The second is that same refusal from
    the other side — a Kernel that could not be asked has not said no.

    The third is the confirmation, and it is deliberately proportional. Where
    nothing would be lost, ``--apply`` is enough; most Hosts that fall behind the
    schema are development Hosts holding no Owner selection at all, and a
    ceremony performed every time is a ceremony that stops being read. Where
    selections would be lost, the operator types back how many — which is the one
    number the old ``rm`` never made anybody look at.
    """

    if report.get("accepted") is True:
        raise TargetError(
            "the installed Kernel opens this database; there is nothing to set aside. "
            "This operation exists for a Kernel that refuses its own authority, and it "
            "will not destroy one that works"
        )
    if report.get("accepted") is None:
        raise TargetError(
            "the installed Kernel could not read this database, so it has not said it "
            f"refuses it: {report.get('unreadable') or report.get('refusal') or 'no detail'}"
        )
    selections = report.get("selections")
    if not isinstance(selections, int):
        raise TargetError(
            "this Kernel cannot count the Owner Companion selections in this database, so "
            "how much this would destroy is unknown. Copy the file off this Host before "
            "going further; this operation will not proceed on an unknown loss"
        )
    if selections == 0:
        return
    if acknowledged != selections:
        raise TargetError(
            f"setting this database aside destroys {selections} Owner Companion "
            "selection(s) — which Eidolon answers through each of those devices — and "
            "nothing gives them back. Re-run with --forget-selections "
            f"{selections} to say so deliberately"
        )


def set_aside(database: Path, *, suffix: str) -> list[dict[str, str]]:
    """Rename the database and its sidecars out of the way. Never delete."""

    renamed: list[dict[str, str]] = []
    for source in (database, *(Path(str(database) + tail) for tail in SQLITE_SIDECARS)):
        if source.is_symlink() or not source.is_file():
            continue
        destination = Path(str(source) + suffix)
        if destination.exists():
            raise TargetError(f"a set-aside Kernel authority is already here: {destination}")
        source.rename(destination)
        renamed.append({"from": source.name, "to": destination.name})
    return renamed


def plan_document(*, database: Path, display: Path, report: Mapping[str, object]) -> dict[str, object]:
    """What the operator is being asked to approve, in one shape for both Hosts."""

    return {
        "status": "planned",
        "database": str(display),
        "kernel": dict(report),
        "renames": [
            str(display),
            *(
                str(display) + suffix
                for suffix in SQLITE_SIDECARS
                if Path(str(database) + suffix).is_file()
            ),
        ],
        "preserves": [
            "every other authority on this Host, and the Owner Domain generation",
            "the set-aside file itself, renamed rather than deleted",
        ],
        "rebuilds": "device mounts, from the Hub Claim stream the Kernel replays on start",
        "destroys": report.get("notice"),
    }


def absent_document(display: Path) -> dict[str, object]:
    return {
        "status": "absent",
        "database": str(display),
        "detail": "this Host has no Kernel authority to set aside",
    }


def require_regular_file(database: Path, *, display: Path) -> None:
    if database.is_symlink() or not database.is_file():
        raise TargetError(f"Kernel authority is not a safe regular file: {display}")


def acknowledged_selections(payload: Mapping[str, object]) -> int | None:
    value = payload.get("forget_selections")
    if value is None:
        return None
    if type(value) is not int or value < 0:
        raise TargetError("forget_selections must be a whole number of selections")
    return value


# -- the product Host -----------------------------------------------------------


def _product_interpreter(root: Path) -> Path:
    return primitives.host_path(root, contract.CURRENT_KERNEL / ".venv/bin/python")


def kernel_schema_plan(
    payload: Mapping[str, object],
    *,
    root: Path = Path("/"),
    command: Callable[..., subprocess.CompletedProcess[str]] = primitives.run,
) -> dict[str, object]:
    """What setting this Host's Kernel authority aside would take with it."""

    contract.fixed_units(payload)
    root = root.resolve()
    database = primitives.host_path(root, KERNEL_DATABASE)
    if not database.exists():
        return absent_document(KERNEL_DATABASE)
    require_regular_file(database, display=KERNEL_DATABASE)
    report = kernel_evidence(
        database=database, interpreter=_product_interpreter(root), command=command
    )
    return plan_document(database=database, display=KERNEL_DATABASE, report=report)


def kernel_schema_reset(
    payload: Mapping[str, object],
    *,
    root: Path = Path("/"),
    command: Callable[..., subprocess.CompletedProcess[str]] = primitives.run,
    manage_services: bool = True,
    at: datetime | None = None,
) -> dict[str, object]:
    """Rename the refused Kernel authority aside and let the Kernel build a new one."""

    if os.geteuid() != 0 and root == Path("/"):
        raise TargetError("setting the Kernel authority aside requires root")
    root = root.resolve()
    plan = kernel_schema_plan(payload, root=root, command=command)
    if plan["status"] == "absent":
        return plan
    acknowledged = acknowledged_selections(payload)
    report = plan["kernel"]
    assert isinstance(report, dict)
    refuse_unless_warranted(report, acknowledged=acknowledged)

    database = primitives.host_path(root, KERNEL_DATABASE)
    suffix = set_aside_suffix(at)
    lock_path = primitives.host_path(root, Path("/run/lock/eidolon-install.lock"))
    with primitives.exclusive(lock_path):
        # Asked again under the lock. Nothing is moved on the strength of a
        # report taken before a concurrent install or reset could have replaced
        # the very file this is about to rename.
        again = kernel_evidence(
            database=database, interpreter=_product_interpreter(root), command=command
        )
        refuse_unless_warranted(again, acknowledged=acknowledged)
        if manage_services:
            _checked(
                command,
                ("/usr/bin/systemctl", "stop", *KERNEL_RECONCILER_UNITS, KERNEL_UNIT),
                operation="Kernel quiesce for setting its authority aside",
                timeout=180,
            )
        renamed = set_aside(database, suffix=suffix)
        if manage_services:
            # The reconciler, not the Kernel: it owns the Kernel's desired
            # state, so starting the Kernel directly here would be a second
            # opinion about whether it should be running at all.
            _checked(
                command,
                ("/usr/bin/systemctl", "start", *KERNEL_RECONCILER_UNITS),
                operation="Kernel start after setting its authority aside",
                timeout=180,
            )
    return {
        "status": "kernel_schema_reset",
        "database": str(KERNEL_DATABASE),
        "set_aside_suffix": suffix,
        "renamed": renamed,
        "selections_destroyed": again.get("selections"),
        "kernel": again,
    }


def _checked(
    command: Callable[..., subprocess.CompletedProcess[str]],
    value: Sequence[str],
    *,
    operation: str,
    timeout: int,
) -> None:
    result = command(tuple(value), timeout=timeout)
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or "no output"
        raise TargetError(f"{operation} failed: {detail}")
