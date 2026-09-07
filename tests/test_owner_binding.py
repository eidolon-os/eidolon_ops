"""Withdrawing Bootstrap's claim that Data holds a Workspace for this Host.

The claim and the Workspace live in two stores with two lifecycles, and one of
those lifecycles — a source run's ``reset --wipe-authority-data`` — destroys
the second while keeping the first. What that leaves is a Host every phone is
refused setup on, identically and forever, while every service reads healthy.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
from pathlib import Path

import pytest

from eidolon_ops.hostagent.owner_binding import (
    RELEASE_SCRIPT,
    release_owner_binding,
)
from eidolon_ops.hostagent.primitives import TargetError

pytestmark = pytest.mark.unit


def _interpreter(tmp_path: Path) -> Path:
    path = tmp_path / "python"
    path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return path


def _answer(document: dict[str, object]):
    def command(argv, **_kwargs) -> subprocess.CompletedProcess[str]:
        command.argv = argv  # type: ignore[attr-defined]
        return subprocess.CompletedProcess(argv, 0, json.dumps(document), "")

    return command


def test_a_host_with_no_bootstrap_database_has_no_claim_to_withdraw(
    tmp_path: Path,
) -> None:
    """Absent is not a failure: the reset runs this unconditionally."""

    def refuse(*_a, **_k):
        raise AssertionError("nothing should be asked of a Host with no database")

    result = release_owner_binding(
        database=tmp_path / "missing.sqlite3",
        interpreter=_interpreter(tmp_path),
        command=refuse,
    )

    assert result == {
        "released": False,
        "state": "absent",
        "database": str(tmp_path / "missing.sqlite3"),
    }


def test_the_release_is_asked_of_admins_own_interpreter(tmp_path: Path) -> None:
    """Bootstrap's schema is Bootstrap's, so Bootstrap's code clears the row."""

    database = tmp_path / "bootstrap.sqlite3"
    database.write_bytes(b"")
    interpreter = _interpreter(tmp_path)
    command = _answer(
        {
            "released": True,
            "before": {"workspace_state": "ready"},
            "after": {"workspace_state": "absent"},
        }
    )

    result = release_owner_binding(
        database=database, interpreter=interpreter, command=command
    )

    assert result["released"] is True
    assert result["state"] == "released"
    argv = command.argv  # type: ignore[attr-defined]
    assert argv[0] == str(interpreter)
    assert argv[1] == "-c"
    assert argv[3] == str(database)
    # The script reaches for Bootstrap's own store rather than writing SQL of
    # its own, which is the whole reason it runs under Admin's interpreter.
    assert "SQLiteBootstrapStateStore" in RELEASE_SCRIPT
    assert "release_owner_binding" in RELEASE_SCRIPT


def test_a_missing_admin_interpreter_refuses_rather_than_guesses(
    tmp_path: Path,
) -> None:
    """Refusing is the safe answer, and the message says what it is refusing."""

    database = tmp_path / "bootstrap.sqlite3"
    database.write_bytes(b"")

    with pytest.raises(TargetError, match="no phone can finish setup on"):
        release_owner_binding(
            database=database,
            interpreter=tmp_path / "no-such-python",
            command=_answer({"released": True}),
        )


@pytest.mark.parametrize(
    "result",
    [
        subprocess.CompletedProcess([], 1, "", "ImportError"),
        subprocess.CompletedProcess([], 0, "not json", ""),
        subprocess.CompletedProcess([], 0, json.dumps({"ok": True}), ""),
    ],
)
def test_an_answer_that_is_not_a_release_report_is_not_taken_for_one(
    tmp_path: Path, result: subprocess.CompletedProcess[str]
) -> None:
    database = tmp_path / "bootstrap.sqlite3"
    database.write_bytes(b"")

    with pytest.raises(TargetError):
        release_owner_binding(
            database=database,
            interpreter=_interpreter(tmp_path),
            command=lambda *_a, **_k: result,
        )


@pytest.mark.skipif(
    not (
        Path(__file__).resolve().parents[2]
        / "eidolon_admin/.venv/bin/python"
    ).exists(),
    reason="needs the sibling eidolon_admin worktree and its venv",
)
def test_the_release_script_runs_against_a_real_bootstrap_database(
    tmp_path: Path,
) -> None:
    """The copy of the script held here, run by the code that owns the schema.

    A unit test with a fake subprocess proves the wiring and nothing about the
    script; this is the half that would catch a store method renamed out from
    under it.
    """

    admin = Path(__file__).resolve().parents[2] / "eidolon_admin"
    interpreter = admin / ".venv/bin/python"
    database = tmp_path / "bootstrap.sqlite3"
    seed = subprocess.run(
        [
            str(interpreter),
            "-c",
            "import sys\n"
            "from datetime import UTC, datetime\n"
            "from pathlib import Path\n"
            "from eidolon_admin_server.bootstrap.adapters.persistence.sqlite import (\n"
            "    SQLiteBootstrapStateStore,\n"
            ")\n"
            "store = SQLiteBootstrapStateStore(Path(sys.argv[1]))\n"
            "store.open()\n"
            "now = datetime.now(UTC).isoformat().replace('+00:00', 'Z')\n"
            "store.initialize(now)\n"
            "store.connection.execute(\n"
            "    \"UPDATE bootstrap_state SET workspace_state='ready', \"\n"
            "    \"owner_id='owner_06607258a65055708c91880e8f2fb9a9'\"\n"
            ")\n"
            "store.connection.commit()\n"
            "store.close()\n",
            str(database),
        ],
        capture_output=True,
        text=True,
        cwd=admin / "server",
        env={**os.environ, "PYTHONPATH": str(admin / "server")},
    )
    assert seed.returncode == 0, seed.stderr

    def command(argv, **_kwargs) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            list(argv),
            capture_output=True,
            text=True,
            cwd=admin / "server",
            env={**os.environ, "PYTHONPATH": str(admin / "server")},
        )

    result = release_owner_binding(
        database=database, interpreter=interpreter, command=command
    )

    assert result["released"] is True
    assert result["before"]["workspace_state"] == "ready"
    assert result["after"]["workspace_state"] == "absent"
    # Idempotent, because the reset that runs it does so unconditionally.
    again = release_owner_binding(
        database=database, interpreter=interpreter, command=command
    )
    assert again["released"] is False
