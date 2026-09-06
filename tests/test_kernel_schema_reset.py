"""The remediation for a Kernel that will not open its own authority.

The refusal itself is correct and stays: a Kernel that opened a database of
unknown shape would be worse than one that will not start. What had no owner was
what comes next. A Host in that state can only be started by moving the file
aside, and the only command that produced a startable Host was
``reset --wipe-authority-data`` — which destroys every authority on the machine
and advances the Owner Domain generation, voiding every Claim, to move one file.

So it was done by hand. ``.eidolon/wiped-20260825T222356/state/`` in this
workspace still holds ``eidolon-kernel.sqlite3.stale-schema-20260825T175934``:
somebody's rename, with nothing anywhere saying what was in it. It was schema v3
and it held one device mount with a Companion attached.

These tests are about the three properties that make this operation different
from that rename: it runs only when the Kernel says the file is refused, it
renames rather than deletes, and it will not destroy an Owner's Companion
selection without the operator having read how many.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from eidolon_ops.hostagent import contract
from eidolon_ops.hostagent import kernel_schema as target
from eidolon_ops.hostagent.primitives import TargetError

pytestmark = pytest.mark.component

_INTERPRETER = contract.CURRENT_KERNEL / ".venv/bin/python"


def _payload(**extra: object) -> dict[str, object]:
    return {"units": list(contract.PRODUCT_UNITS), **extra}


class FakeKernel:
    """Stands in for the installed Kernel's ``schema_report``.

    A fake rather than a real Kernel because what is under test here is the ops
    side of the seam: which answers this operation acts on, and which it refuses
    to act on. The Kernel's own half — that the report is true — is tested in
    ``eidolon_kernel``, against real databases.
    """

    def __init__(self, report: dict[str, object] | None, *, returncode: int = 0) -> None:
        self.report = report
        self.returncode = returncode
        self.commands: list[tuple[str, ...]] = []

    def __call__(self, command, *, timeout: float = 0) -> subprocess.CompletedProcess[str]:
        value = tuple(command)
        self.commands.append(value)
        if value[0] == "/usr/bin/systemctl":
            return subprocess.CompletedProcess(value, 0, "", "")
        return subprocess.CompletedProcess(
            value,
            self.returncode,
            "" if self.report is None else json.dumps(self.report),
            "" if self.returncode == 0 else "boom",
        )

    @property
    def systemctl(self) -> list[tuple[str, ...]]:
        return [item for item in self.commands if item[0] == "/usr/bin/systemctl"]


def _report(**overrides: object) -> dict[str, object]:
    document: dict[str, object] = {
        "path": str(target.KERNEL_DATABASE),
        "code_schema_version": 8,
        "accepted": False,
        "refusal": "kernel SQLite table kernel_requests does not match schema v8",
        "notice": "Setting it aside destroys 2 of 3 Body assignments.",
        "schema_version": 8,
        "mounts": 3,
        "assignments": 3,
        "selections": 2,
        "countable": True,
        "unreadable": None,
    }
    document.update(overrides)
    return document


def _host(root: Path, *, sidecars: bool = True) -> Path:
    database = root / target.KERNEL_DATABASE.relative_to("/")
    database.parent.mkdir(parents=True, exist_ok=True)
    database.write_bytes(b"SQLite format 3\0")
    interpreter = root / _INTERPRETER.relative_to("/")
    interpreter.parent.mkdir(parents=True, exist_ok=True)
    interpreter.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    interpreter.chmod(0o755)
    if sidecars:
        Path(str(database) + "-wal").write_bytes(b"tail")
        Path(str(database) + "-shm").write_bytes(b"shared")
    Path(str(database) + ".lock").write_bytes(b"")
    return database


def test_the_plan_reports_the_kernels_own_count_and_changes_nothing(tmp_path: Path) -> None:
    database = _host(tmp_path)
    kernel = FakeKernel(_report())

    plan = target.kernel_schema_plan(_payload(), root=tmp_path, command=kernel)

    assert plan["status"] == "planned"
    assert plan["kernel"]["selections"] == 2
    assert plan["destroys"] == "Setting it aside destroys 2 of 3 Body assignments."
    assert plan["renames"] == [
        str(target.KERNEL_DATABASE),
        str(target.KERNEL_DATABASE) + "-wal",
        str(target.KERNEL_DATABASE) + "-shm",
    ]
    assert database.is_file()
    assert kernel.systemctl == []


def test_a_kernel_that_opens_its_authority_gets_no_remediation(tmp_path: Path) -> None:
    """Without this gate the operation is a button that destroys a working Host.

    Nothing else here is load-bearing if this is not: the fact it moves out of
    reach can only be put back by the Owner, one Body at a time, and the two
    Hosts that hold it are the ones people debug on.
    """

    _host(tmp_path)
    kernel = FakeKernel(_report(accepted=True, refusal=None))

    with pytest.raises(TargetError, match="there is nothing to set aside"):
        target.kernel_schema_reset(
            _payload(), root=tmp_path, command=kernel, manage_services=False
        )

    assert (tmp_path / target.KERNEL_DATABASE.relative_to("/")).is_file()


def test_a_kernel_that_could_not_be_asked_is_not_a_kernel_that_said_no(
    tmp_path: Path,
) -> None:
    _host(tmp_path)
    kernel = FakeKernel(
        _report(accepted=None, unreadable="file is not a database", selections=None)
    )

    with pytest.raises(TargetError, match="has not said it refuses it"):
        target.kernel_schema_reset(
            _payload(), root=tmp_path, command=kernel, manage_services=False
        )


def test_a_loss_this_kernel_cannot_count_is_refused_rather_than_guessed(
    tmp_path: Path,
) -> None:
    """The shape the real incident file has: a Kernel that cannot count it.

    Proceeding here would destroy an unknown number of Owner selections while
    reporting nothing, which is exactly the silence being replaced.
    """

    _host(tmp_path)
    kernel = FakeKernel(_report(selections=None, assignments=None, countable=False))

    with pytest.raises(TargetError, match="will not proceed on an unknown loss"):
        target.kernel_schema_reset(
            _payload(), root=tmp_path, command=kernel, manage_services=False
        )


def test_destroying_owner_selections_needs_the_number_typed_back(tmp_path: Path) -> None:
    """A boolean acknowledgement is one nobody has to read the count to give."""

    _host(tmp_path)
    kernel = FakeKernel(_report())

    with pytest.raises(TargetError, match="--forget-selections 2"):
        target.kernel_schema_reset(
            _payload(), root=tmp_path, command=kernel, manage_services=False
        )
    with pytest.raises(TargetError, match="--forget-selections 2"):
        target.kernel_schema_reset(
            _payload(forget_selections=1),
            root=tmp_path,
            command=kernel,
            manage_services=False,
        )
    assert (tmp_path / target.KERNEL_DATABASE.relative_to("/")).is_file()


def test_a_host_with_nothing_to_lose_needs_no_ceremony(tmp_path: Path) -> None:
    """Most Hosts behind the schema are development Hosts holding no selection.

    A confirmation demanded there too is one the operator learns to type without
    reading, on the way to the Host where it is true.
    """

    _host(tmp_path, sidecars=False)
    kernel = FakeKernel(_report(selections=0, assignments=0, mounts=0))

    result = target.kernel_schema_reset(
        _payload(), root=tmp_path, command=kernel, manage_services=False
    )

    assert result["status"] == "kernel_schema_reset"
    assert result["selections_destroyed"] == 0


def test_the_authority_is_renamed_beside_itself_and_never_deleted(tmp_path: Path) -> None:
    """A file that still exists can be opened by a person; a deleted one cannot.

    Nothing here can read it back into a running Kernel — a backup at a refused
    schema is still a refused schema — and the suffix is the one the single
    hand-done instance already used, so an operator meeting both files does not
    have to work out which convention produced which.
    """

    database = _host(tmp_path)
    kernel = FakeKernel(_report())

    result = target.kernel_schema_reset(
        _payload(forget_selections=2),
        root=tmp_path,
        command=kernel,
        manage_services=False,
    )

    suffix = result["set_aside_suffix"]
    assert suffix.startswith(".stale-schema-")
    assert not database.exists()
    assert Path(str(database) + suffix).read_bytes() == b"SQLite format 3\0"
    assert Path(str(database) + "-wal" + suffix).is_file()
    assert Path(str(database) + "-shm" + suffix).is_file()
    assert [item["from"] for item in result["renamed"]] == [
        "eidolon-kernel.sqlite3",
        "eidolon-kernel.sqlite3-wal",
        "eidolon-kernel.sqlite3-shm",
    ]
    # The lock file holds no data and the next Kernel takes the same flock on
    # the same path, so it stays where it is rather than accumulating a dated
    # copy per repair.
    assert Path(str(database) + ".lock").is_file()


def test_the_reconciler_is_stopped_before_the_rename_and_started_after(
    tmp_path: Path,
) -> None:
    """Stopping the Kernel alone would leave what restarts it running.

    ``eidolond`` owns the Kernel's desired state, so a Kernel stopped on its own
    is a Kernel about to be started again — possibly between the database being
    renamed and the sidecars following it.
    """

    _host(tmp_path)
    kernel = FakeKernel(_report())

    target.kernel_schema_reset(
        _payload(forget_selections=2), root=tmp_path, command=kernel
    )

    stop, start = (item for item in kernel.systemctl if item[1] in {"stop", "start"})
    assert stop == ("/usr/bin/systemctl", "stop", "eidolond.service", "eidolon-kernel.service")
    assert start == ("/usr/bin/systemctl", "start", "eidolond.service")


def test_a_host_with_no_kernel_authority_is_told_so_rather_than_repaired(
    tmp_path: Path,
) -> None:
    interpreter = tmp_path / _INTERPRETER.relative_to("/")
    interpreter.parent.mkdir(parents=True, exist_ok=True)
    interpreter.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    interpreter.chmod(0o755)

    result = target.kernel_schema_reset(
        _payload(), root=tmp_path, command=FakeKernel(None), manage_services=False
    )

    assert result["status"] == "absent"


def test_a_host_whose_kernel_cannot_be_asked_at_all_refuses(tmp_path: Path) -> None:
    """Fail closed. An unanswerable question is not a licence to guess."""

    database = tmp_path / target.KERNEL_DATABASE.relative_to("/")
    database.parent.mkdir(parents=True, exist_ok=True)
    database.write_bytes(b"SQLite format 3\0")

    with pytest.raises(TargetError, match="Refusing rather than setting an authority aside"):
        target.kernel_schema_plan(_payload(), root=tmp_path, command=FakeKernel(_report()))
