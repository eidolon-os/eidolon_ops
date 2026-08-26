from __future__ import annotations

import json
from pathlib import Path

import pytest
from eidolon_sdk.device_foundation.v1.testing import named_device_instance_id

from eidolon_ops import device_baseline
from eidolon_ops.device_baseline import (
    BaselineError,
    Observation,
    Surfaces,
    coverage,
    run,
)
from eidolon_ops.device_management_gate import (
    _CONFIRMATION_STEPS,
    HIL_STEPS,
    GateError,
    verify_hil,
)

pytestmark = pytest.mark.component

_IDENTITY = "a" * 64
# 48 hex characters was never a device instance id; the digest is 64.
_DEVICE = named_device_instance_id("baseline-device")


def _surfaces(tmp_path: Path) -> Surfaces:
    return Surfaces(
        device_serial=tmp_path / "cu.usbmodem",
        android_serial="emulator-5554",
        host_config=tmp_path / "host.toml",
        evidence_root=tmp_path / "evidence",
    )


def _release() -> dict[str, object]:
    return {"release_identity": _IDENTITY}


def _always(passed: bool, *, reason: str = ""):
    def observe(_surfaces: Surfaces) -> Observation:
        return Observation(step_id="x", surface="test", passed=passed, reason=reason)

    return observe


def _all_passing(monkeypatch) -> None:
    monkeypatch.setattr(device_baseline, "OBSERVERS", {step: _always(True) for step in HIL_STEPS})


def test_the_harness_states_its_own_coverage_and_covers_the_whole_sequence() -> None:
    """Coverage is a claim this module makes about itself, so it is asserted.

    Today most of the sequence has no observer. That is a fact to publish, not
    to hide: the whole reason this harness exists is that green reports which
    skipped the interesting part let every cross-boundary defect through.
    """

    reported = coverage()

    assert set(reported["observed"]) | set(reported["unobserved"]) == set(HIL_STEPS)
    assert not set(reported["observed"]) & set(reported["unobserved"])
    # The steps that made this session's chain work are the ones observed.
    assert {"channel_ready_observed", "claim_active_observed", "mount_active_observed"} <= set(
        reported["observed"]
    )


def test_a_step_with_no_observer_fails_rather_than_passing_or_skipping(tmp_path: Path) -> None:
    # A step that still has no observer today. If this one gains one, pick
    # another from coverage()["unobserved"] rather than deleting the test:
    # what it pins is that "nobody taught the harness this yet" fails.
    without = coverage()["unobserved"][0]
    document = run(_surfaces(tmp_path), _release(), _DEVICE, steps=[without])

    step = next(item for item in document["steps"] if item["id"] == without)
    assert step["status"] == "failed"
    evidence = json.loads(Path(step["evidence_path"]).read_text(encoding="utf-8"))
    # And it says which observer is missing, not just that something failed.
    assert "no observer yet" in evidence["reason"]


def test_the_harness_refuses_to_disagree_with_the_gate_about_the_sequence(
    tmp_path: Path, monkeypatch
) -> None:
    """One sequence, one definition. Two copies is the defect this repo keeps
    finding, so the harness refuses to run when its table and the gate's differ."""

    monkeypatch.setattr(
        device_baseline,
        "OBSERVERS",
        {step: _always(True) for step in HIL_STEPS if step != "unmount_observed"},
    )

    with pytest.raises(BaselineError, match="disagree about the sequence"):
        run(_surfaces(tmp_path), _release(), _DEVICE)


def test_a_run_stops_at_the_first_step_that_did_not_happen(tmp_path: Path, monkeypatch) -> None:
    """Later observations must not be attributed to a sequence that never got there."""

    observers = {step: _always(True) for step in HIL_STEPS}
    observers["grant_ack_observed"] = _always(False, reason="no grant was collected")
    monkeypatch.setattr(device_baseline, "OBSERVERS", observers)

    document = run(_surfaces(tmp_path), _release(), _DEVICE)

    statuses = {item["id"]: item["status"] for item in document["steps"]}
    assert statuses["commissioning_started"] == "passed"
    assert statuses["grant_ack_observed"] == "failed"
    # Everything after it is untouched, not passed and not failed.
    assert statuses["claim_active_observed"] == "pending"
    assert statuses["old_generation_rejected"] == "pending"


def test_an_observer_that_raises_becomes_a_failed_step_with_its_reason(
    tmp_path: Path, monkeypatch
) -> None:
    def explode(_surfaces: Surfaces) -> Observation:
        raise BaselineError("device serial port is absent: /dev/nope")

    observers = {step: _always(True) for step in HIL_STEPS}
    observers["release_identity_proven"] = explode
    monkeypatch.setattr(device_baseline, "OBSERVERS", observers)

    document = run(_surfaces(tmp_path), _release(), _DEVICE)

    first = document["steps"][0]
    assert first["status"] == "failed"
    evidence = json.loads(Path(first["evidence_path"]).read_text(encoding="utf-8"))
    assert "/dev/nope" in evidence["reason"]


def test_a_completed_run_is_evidence_the_gate_accepts(tmp_path: Path, monkeypatch) -> None:
    """The point of the harness: what it writes is what the gate verifies.

    Producing evidence in a shape the gate rejects would leave the sequence
    exactly as unrepeatable as doing it by hand.
    """

    _all_passing(monkeypatch)

    document = run(_surfaces(tmp_path), _release(), _DEVICE)

    assert verify_hil(document, _release()) == {
        "status": "device_management_hil_passed",
        "release_identity": _IDENTITY,
        "device_id": _DEVICE,
        "steps": len(HIL_STEPS),
    }


def test_destructive_steps_carry_the_exact_confirmation_the_gate_demands(
    tmp_path: Path, monkeypatch
) -> None:
    _all_passing(monkeypatch)

    document = run(_surfaces(tmp_path), _release(), _DEVICE)

    for step in document["steps"]:
        if step["id"] in _CONFIRMATION_STEPS:
            assert step["confirmation"] == f"ERASE {_DEVICE} FOR {_IDENTITY}"
        else:
            assert "confirmation" not in step


def test_evidence_that_changed_after_the_run_is_rejected(tmp_path: Path, monkeypatch) -> None:
    """The digest is load-bearing, so a rewritten evidence file must not verify."""

    _all_passing(monkeypatch)
    document = run(_surfaces(tmp_path), _release(), _DEVICE)
    tampered = Path(document["steps"][0]["evidence_path"])
    tampered.write_text(
        '{"step_id": "release_identity_proven", "passed": true}\n', encoding="utf-8"
    )

    with pytest.raises(GateError, match="drifted"):
        verify_hil(document, _release())


def test_a_run_without_a_release_identity_is_refused(tmp_path: Path) -> None:
    with pytest.raises(BaselineError, match="release_identity"):
        run(_surfaces(tmp_path), {}, _DEVICE)


def test_asking_for_a_step_the_sequence_does_not_have_is_refused(tmp_path: Path) -> None:
    with pytest.raises(BaselineError, match="no such steps"):
        run(_surfaces(tmp_path), _release(), _DEVICE, steps=["make_it_work"])


def test_the_phone_is_read_through_content_desc_not_text(monkeypatch, tmp_path: Path) -> None:
    """Flutter draws into one view, so reading ``text`` returns an empty screen.

    That looked exactly like a blank app and cost real time this session, so the
    reader is pinned to the attribute that actually carries the labels.
    """

    dump = (
        '<hierarchy><node text="" content-desc="已接入" />'
        '<node text="" content-desc="" />'
        '<node text="ignored" content-desc="挂载 revision / 2" /></hierarchy>'
    )

    class _Result:
        returncode = 0
        stdout = dump
        stderr = ""

    monkeypatch.setattr(device_baseline, "_run", lambda *_a, **_k: _Result())

    screen = device_baseline.phone_screen(_surfaces(tmp_path))

    assert screen.splitlines() == ["已接入", "挂载 revision / 2"]


def test_an_unreadable_phone_is_an_error_not_an_empty_screen(monkeypatch, tmp_path: Path) -> None:
    class _Failed:
        returncode = 0
        stdout = ""
        stderr = ""

    monkeypatch.setattr(device_baseline, "_run", lambda *_a, **_k: _Failed())

    with pytest.raises(BaselineError, match="could not be read back"):
        device_baseline.phone_screen(_surfaces(tmp_path))


def test_a_missing_device_port_is_named(tmp_path: Path) -> None:
    with pytest.raises(BaselineError, match="serial port is absent"):
        device_baseline.device_log(_surfaces(tmp_path), seconds=0.01)


def test_the_host_surface_reports_which_readiness_facts_are_unmet(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(
        device_baseline,
        "host_readiness",
        lambda _surfaces: {
            "status": "degraded",
            "checks": {"hub_lan_reachable": False, "hub_name_published_by_host": False, "x": True},
        },
    )
    monkeypatch.setattr(device_baseline, "_OBSERVE_TIMEOUT_SECONDS", 0.0)

    observed = device_baseline.OBSERVERS["release_identity_proven"](_surfaces(tmp_path))

    assert observed.passed is False
    assert observed.detail["failing"] == ["hub_lan_reachable", "hub_name_published_by_host"]


def test_an_idle_device_says_so_instead_of_only_saying_no_match(
    monkeypatch, tmp_path: Path
) -> None:
    """Two different answers about a device, and the second one is actionable.

    "the log never matched" and "the device is idle and this step is transient"
    send an operator to different places. Several steps in this sequence can
    only be seen while it runs — the firmware cannot be asked what binding it
    holds — so an idle window has to say that, not just report a miss.
    """

    monkeypatch.setattr(
        device_baseline,
        "device_log",
        lambda _surfaces, seconds: (
            "I (6960227) SystemInfo: free sram: 45999 minimal sram: 33675\n"
            "I (6970227) SystemInfo: free sram: 45999 minimal sram: 33675\n"
        ),
    )
    monkeypatch.setattr(device_baseline, "_OBSERVE_TIMEOUT_SECONDS", 0.0)

    observed = device_baseline.OBSERVERS["channel_ready_observed"](_surfaces(tmp_path))

    assert observed.passed is False
    assert observed.detail["idle"] is True
    assert "idle" in observed.reason and "while the sequence is running" in observed.reason


def test_a_device_that_holds_a_room_is_observed_even_between_sessions(
    monkeypatch, tmp_path: Path
) -> None:
    """The durable fact, not only the one that exists during a call.

    The device writes its Hub config down with the room it was given. While it
    had no binding that field was empty — ``status=waiting-binding room=`` —
    which is exactly the shape the two-hour channel loss took.
    """

    monkeypatch.setattr(
        device_baseline,
        "device_log",
        lambda _surfaces, seconds: (
            "I (1) HubConfigStore: Saved Hub config identity=d status=bound "
            "room=eidolon-device-36300611ceb2de39d5ef10be server=wss://h:7880\n"
        ),
    )

    observed = device_baseline.OBSERVERS["channel_ready_observed"](_surfaces(tmp_path))

    assert observed.passed is True
    assert observed.detail["idle"] is False


def test_a_device_still_waiting_for_a_binding_does_not_pass(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(
        device_baseline,
        "device_log",
        lambda _surfaces, seconds: (
            "I (1) HubConfigStore: Saved Hub config identity= status=waiting-binding "
            "room= server=\n"
        ),
    )
    monkeypatch.setattr(device_baseline, "_OBSERVE_TIMEOUT_SECONDS", 0.0)

    observed = device_baseline.OBSERVERS["channel_ready_observed"](_surfaces(tmp_path))

    assert observed.passed is False
    assert observed.detail["idle"] is False
