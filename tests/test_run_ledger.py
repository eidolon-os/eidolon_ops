"""What stopped a run, counted rather than remembered."""

from __future__ import annotations

import json
import stat
from pathlib import Path

import pytest

from eidolon_ops.progress import Journal, ProgressSink
from eidolon_ops.run_ledger import RunLedger, fingerprint

pytestmark = pytest.mark.unit


def _lines(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_the_same_gate_groups_across_runs_that_name_different_things() -> None:
    """The gates have no identity but their message, so the message becomes one.

    Ranking is the whole point: a gate that fires weekly and a gate that has
    fired once must be distinguishable, and they are not if every run's paths,
    commits and counts make a new key.
    """

    first = fingerprint(
        "a release is sealed from commits, and these worktrees hold changes that "
        "would not be in it: eidolon_hub (1 modified: M hub/composition/app.py)"
    )
    second = fingerprint(
        "a release is sealed from commits, and these worktrees hold changes that "
        "would not be in it: eidolon_sdk (14 modified: M src/other/thing.py, ...)"
    )
    assert first == second

    # A pinned commit and a count are per-run facts, not per-gate ones.
    assert fingerprint("source revision is not the exact commit object: " + "a" * 40) == (
        fingerprint("source revision is not the exact commit object: " + "b" * 40)
    )
    # Two genuinely different gates stay apart.
    assert fingerprint("product settings input drifted from exact Git objects: agent.yaml") != (
        first
    )


def test_a_ledger_is_a_progress_sink_so_phase_timings_need_no_new_seam(tmp_path: Path) -> None:
    ledger = RunLedger(tmp_path / "mac.jsonl")
    assert isinstance(ledger, ProgressSink)

    journal = Journal(ledger)
    journal.begin("bundle")
    journal.append({"phase": "bundle", "result": {}})
    journal.begin("upload_guard")
    journal.append({"phase": "upload_guard", "result": {}})
    ledger.record(operation="deploy", outcome="applied")

    entry = _lines(ledger.path)[0]
    assert [phase["phase"] for phase in entry["phases"]] == ["bundle", "upload_guard"]
    assert all(isinstance(phase["seconds"], float) for phase in entry["phases"])
    assert "stopped_in" not in entry


def test_a_failed_run_records_where_it_stopped_and_which_gate(tmp_path: Path) -> None:
    """The open phase is the answer to "where in the 29 phases did it die".

    ``Journal`` already tracked it, for the console to render one step red.
    Nothing was recording it, so the same question was answered by rereading a
    terminal.
    """

    ledger = RunLedger(tmp_path / "pi5.jsonl")
    journal = Journal(ledger)
    journal.begin("bundle")
    journal.append({"phase": "bundle", "result": {}})
    journal.begin("prepare")  # and then it raised

    ledger.record(
        operation="deploy",
        outcome="failed",
        error="product settings input drifted from exact Git objects: agent.yaml",
    )

    entry = _lines(ledger.path)[0]
    assert entry["stopped_in"] == "prepare"
    assert entry["gate"] == fingerprint(entry["error"])
    assert entry["operation"] == "deploy"
    assert entry["profile"] == "pi5"


def test_the_facts_that_explain_a_run_are_found_where_operations_put_them(
    tmp_path: Path,
) -> None:
    """A deploy puts them in its preflight block, not at the top of the report.

    The first version of this file read only the top level, so every real deploy
    recorded `link=None` and no advance -- and said nothing about it. An absent
    field that exists to explain slow runs is worse than no field: nine deploys
    on the board looked like they had been asked and had nothing to report.
    """

    ledger = RunLedger(tmp_path / "pi5.jsonl")
    ledger.record(
        operation="deploy",
        outcome="applied",
        report={
            "status": "activated",
            "release_id": "20260827-owner-view",
            "local": {
                "link": {"status": "wireless", "endpoint": "192.168.3.40 (wireless via en0)"},
                "source_advance": {"eidolon_hub": 3},
                "release_matrix": {"unrelated": "not carried"},
            },
        },
    )

    entry = _lines(ledger.path)[0]
    assert entry["release_id"] == "20260827-owner-view"
    assert entry["link"]["status"] == "wireless"
    assert entry["source_advance"] == {"eidolon_hub": 3}
    assert "release_matrix" not in entry

    # A flat report still works: the top level is looked at first.
    flat = RunLedger(tmp_path / "mac.jsonl")
    flat.record(operation="status", outcome="observed", report={"link": {"status": "wired"}})
    assert _lines(flat.path)[0]["link"] == {"status": "wired"}


def test_the_ledger_is_private_and_never_fails_the_operation(tmp_path: Path) -> None:
    """A record of runs must not become a reason a run fails."""

    ledger = RunLedger(tmp_path / "nested" / "mac.jsonl")
    ledger.record(operation="status", outcome="observed")

    assert stat.S_IMODE(ledger.path.stat().st_mode) == 0o600
    assert stat.S_IMODE(ledger.path.parent.stat().st_mode) == 0o700

    # Two runs append rather than replace: one line per run is the unit.
    ledger.record(operation="doctor", outcome="observed")
    assert len(_lines(ledger.path)) == 2

    # An unwritable location is reported by nothing and raises nothing.
    blocked = tmp_path / "blocked"
    blocked.mkdir()
    blocked.chmod(0o500)
    try:
        RunLedger(blocked / "deeper" / "mac.jsonl").record(operation="status", outcome="observed")
    finally:
        blocked.chmod(0o700)


def test_the_default_location_is_beside_the_profile_and_overridable(
    tmp_path: Path, monkeypatch
) -> None:
    profile = tmp_path / "config/hosts/pi5.toml"
    profile.parent.mkdir(parents=True)
    profile.write_text("", encoding="utf-8")

    monkeypatch.delenv("EIDOLON_OPS_RUN_LEDGER", raising=False)
    assert RunLedger.for_profile(profile).path == profile.parent / "runs/pi5.jsonl"

    monkeypatch.setenv("EIDOLON_OPS_RUN_LEDGER", str(tmp_path / "elsewhere.jsonl"))
    assert RunLedger.for_profile(profile).path == tmp_path / "elsewhere.jsonl"


def test_a_host_composition_is_not_mistaken_for_a_filename() -> None:
    """`macos-dev/supervisord/none` is an identity worth grouping by.

    The key exists to be read by a person ranking gates, and a substitution that
    swallowed anything containing a slash turned a capability refusal into
    `deploy is not available on this host (macos-dev<path>)` — losing the one
    detail that says which kind of Host refused.
    """

    assert fingerprint("deploy is not available on this Host (macos-dev/supervisord/none)") == (
        "deploy is not available on this host (macos-dev/supervisord/none)"
    )
    # An absolute path is still per-run noise and still goes.
    assert fingerprint("inputs are missing: /Users/someone/eidolon/state/hub/x.json") == (
        "inputs are missing: <path>"
    )
