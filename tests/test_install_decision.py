"""The install decision: one pure function, every situation it can name.

Each refusing situation has one sentence and each proceeding one has one
consequence, and none of them is reached by a cache being stale — the
function takes what the Host reported and what this profile recorded, and
nothing this side remembers about the Host.
"""

from __future__ import annotations

import json

import pytest

from eidolon_ops.hostagent.hardware import binding_for
from eidolon_ops.install_decision import (
    InstallContext,
    InstallDecision,
    decide_install,
    directory_names_host,
    refusal,
)

OWNER = "owner-" + "a" * 20
STRANGER = "owner-" + "b" * 20
HOST_ID = "ehost-" + "c" * 20
OTHER_HOST_ID = "ehost-" + "d" * 20
IDENTITY = "1" * 64
BOARD = {"kind": "device-tree:raspberrypi,5-model-b", "fingerprint": "sha256:" + "a" * 64}
OTHER_BOARD = {"kind": "device-tree:raspberrypi,5-model-b", "fingerprint": "sha256:" + "f" * 64}
LINEAGE = {
    "contract_version": 1,
    "owner_domain_id": OWNER,
    "owner_domain_generation": 8,
    "state_id": "authority-state_board",
}


def _directory(host_id: str = HOST_ID) -> str:
    suffix = host_id.removeprefix("ehost-")
    return json.dumps(
        {
            "owner_domain_id": OWNER,
            "owner_domain_generation": 8,
            "descriptor_uri": f"https://eidolon-hub-{suffix}.local:8443/api/device-onboarding/v1/descriptor",
        }
    )


def _context(
    *,
    lineage=LINEAGE,
    marker=...,
    anchor=...,
    directory=...,
    hardware=BOARD,
    identity=IDENTITY,
) -> InstallContext:
    marker = lineage if marker is ... else marker
    anchor = lineage if anchor is ... else anchor
    return InstallContext(
        marker=marker,
        anchor=anchor,
        established=marker if marker is not None and marker == anchor else None,
        directory=_directory() if directory is ... else directory,
        hardware=hardware,
        identity_sha256=identity,
    )


def _unowned() -> InstallContext:
    return _context(marker=None, anchor=None, directory=None, identity=None)


def _bound(hardware=BOARD) -> bytes:
    return (json.dumps(binding_for(HOST_ID, hardware), sort_keys=True) + "\n").encode()


def _decide(context: InstallContext, *, binding: bytes | None) -> InstallDecision:
    return decide_install(
        context,
        owner_domain_id=OWNER,
        host_id=HOST_ID,
        identity_sha256=IDENTITY,
        binding=binding,
    )


def test_a_board_holding_nothing_that_this_profile_never_delivered_to_is_a_first_install() -> None:
    assert _decide(_unowned(), binding=None) is InstallDecision.FIRST_INSTALL


def test_the_board_this_identity_went_to_holding_its_authority_is_continued() -> None:
    assert _decide(_context(), binding=_bound()) is InstallDecision.CONTINUE


def test_a_host_from_before_bindings_that_proves_it_holds_the_identity_has_its_delivery_adopted() -> None:
    assert _decide(_context(), binding=None) is InstallDecision.ADOPT_DELIVERY


def test_the_second_board_is_refused_at_the_first_gate() -> None:
    """The 2026-09-10 shape: one Owner's material, a second blank board.

    It used to pass the Authority gate (nothing on the board to disagree with)
    and only then meet the binding. Now the binding is the first question with
    an answer, and the board is refused before anything names a generation.
    """

    blank_second_board = _context(
        marker=None, anchor=None, directory=None, identity=None, hardware=OTHER_BOARD
    )
    assert _decide(blank_second_board, binding=_bound()) is InstallDecision.WRONG_BOARD
    # And a second board that has meanwhile stood this Owner's Authority up is
    # still the wrong board: what it holds does not change whose it is.
    assert (
        _decide(_context(hardware=OTHER_BOARD), binding=_bound()) is InstallDecision.WRONG_BOARD
    )


def test_the_delivered_board_holding_nothing_is_the_recovery_case() -> None:
    """Only reachable because the binding is written after the Host proved it
    established an Authority — so a bound board with none really did lose it."""

    context = _context(marker=None, anchor=None, directory=None)
    assert _decide(context, binding=_bound()) is InstallDecision.AUTHORITY_LOST


@pytest.mark.parametrize(
    "marker, anchor",
    [
        ({**LINEAGE, "state_id": "authority-state_other"}, LINEAGE),
        (None, LINEAGE),
        (LINEAGE, None),
    ],
)
def test_a_host_whose_two_copies_disagree_is_an_incident_whatever_this_profile_records(
    marker, anchor
) -> None:
    context = _context(marker=marker, anchor=anchor)
    for binding in (None, _bound(), _bound(OTHER_BOARD)):
        assert _decide(context, binding=binding) is InstallDecision.HOST_INCIDENT


def test_a_host_established_by_another_owner_is_foreign_whichever_board_it_is() -> None:
    stranger = {**LINEAGE, "owner_domain_id": STRANGER}
    context = _context(lineage=stranger)
    for binding in (None, _bound(), _bound(OTHER_BOARD)):
        assert _decide(context, binding=binding) is InstallDecision.FOREIGN_AUTHORITY


@pytest.mark.parametrize(
    "context",
    [
        _context(identity="2" * 64),
        _context(identity=None),
        _context(directory=_directory(OTHER_HOST_ID)),
        _context(directory=None),
        _context(directory="not json"),
    ],
)
def test_an_unbound_host_that_cannot_prove_it_holds_the_identity_is_unproven(context) -> None:
    assert _decide(context, binding=None) is InstallDecision.IDENTITY_UNPROVEN


def test_the_directory_must_publish_itself_under_this_host_id() -> None:
    assert directory_names_host(_directory(), HOST_ID)
    assert not directory_names_host(_directory(OTHER_HOST_ID), HOST_ID)
    assert not directory_names_host(None, HOST_ID)
    assert not directory_names_host(json.dumps({"descriptor_uri": "https://x.invalid/"}), HOST_ID)
    assert not directory_names_host(json.dumps([]), HOST_ID)


def test_each_situation_says_what_it_does() -> None:
    proceeding = {
        InstallDecision.FIRST_INSTALL,
        InstallDecision.CONTINUE,
        InstallDecision.ADOPT_DELIVERY,
    }
    for decision in InstallDecision:
        assert decision.proceeds is (decision in proceeding)
    assert {decision for decision in InstallDecision if decision.records_delivery} == {
        InstallDecision.FIRST_INSTALL,
        InstallDecision.ADOPT_DELIVERY,
    }
    assert {decision for decision in InstallDecision if decision.host_established} == {
        InstallDecision.CONTINUE,
        InstallDecision.ADOPT_DELIVERY,
    }


@pytest.mark.parametrize(
    "decision, context, fragment",
    [
        (
            InstallDecision.HOST_INCIDENT,
            _context(anchor=None),
            "AUTHORITY_RECOVERY_REQUIRED",
        ),
        (
            InstallDecision.FOREIGN_AUTHORITY,
            _context(lineage={**LINEAGE, "owner_domain_id": STRANGER}),
            STRANGER,
        ),
        (InstallDecision.WRONG_BOARD, _context(hardware=OTHER_BOARD), "sha256:" + "f" * 64),
        (InstallDecision.AUTHORITY_LOST, _context(marker=None, anchor=None), "AUTHORITY_LOST"),
        (InstallDecision.IDENTITY_UNPROVEN, _context(identity=None), "IDENTITY_UNPROVEN"),
    ],
)
def test_every_refusal_is_one_sentence_that_names_the_situation(decision, context, fragment) -> None:
    sentence = refusal(decision, context, owner_domain_id=OWNER)
    assert fragment in sentence
    # No refusal offers the verb that no longer exists, and none of them
    # invents a generation to move to.
    assert "trust-host-authority" not in sentence
    assert "advance" not in sentence


@pytest.mark.parametrize(
    "decision",
    [InstallDecision.FIRST_INSTALL, InstallDecision.CONTINUE, InstallDecision.ADOPT_DELIVERY],
)
def test_a_proceeding_situation_has_no_refusal(decision) -> None:
    with pytest.raises(ValueError):
        refusal(decision, _context(), owner_domain_id=OWNER)


def test_an_observation_is_taken_only_in_the_shape_the_agent_sends() -> None:
    observed = {
        "status": "observed",
        "marker": LINEAGE,
        "anchor": LINEAGE,
        "established": LINEAGE,
        "directory": _directory(),
        "hardware": BOARD,
        "identity_sha256": IDENTITY,
    }
    context = InstallContext.from_observation(observed)
    assert context.established == LINEAGE
    assert context.report()["serves_directory"] is True
    assert context.report()["holds_identity"] is True
    assert "directory" not in context.report()

    for broken in (
        {**observed, "status": "failed"},
        {key: value for key, value in observed.items() if key != "hardware"},
        {**observed, "hardware": {"kind": "x"}},
        {**observed, "identity_sha256": "short"},
        {**observed, "directory": 7},
        {**observed, "marker": "not a mapping"},
        {**observed, "unexpected": True},
    ):
        with pytest.raises(ValueError):
            InstallContext.from_observation(broken)
