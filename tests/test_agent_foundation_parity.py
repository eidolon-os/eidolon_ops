"""The agent's copy of the foundations, held to Ops's.

The Host agent is injected as one payload and imports nothing from the package
it came from, so it cannot share these tables — and should not, because its job
is to refuse a contract that differs from what it was built against, and
reading the one it was handed would make that circular. The duplication is the
design; drift between the copies is the failure it invites, and this is what
stops it.
"""

from __future__ import annotations

import pytest

from eidolon_ops import foundation as ops
from eidolon_ops.hostagent import foundation as agent

pytestmark = pytest.mark.unit


def test_both_sides_know_the_same_boards() -> None:
    assert set(agent.FOUNDATION_PROFILES) == set(ops.FOUNDATION_PROFILES)


@pytest.mark.parametrize("profile_id", sorted(ops.FOUNDATION_PROFILES))
def test_the_agent_expects_exactly_what_ops_sends(profile_id: str) -> None:
    """Equality, because that is how the agent checks it on the Host.

    Anything unequal here is a Host that refuses the payload built for it, and
    the message it gives says the contract drifted rather than that two tables
    disagree.
    """

    assert agent.expected_foundation(agent.FOUNDATION_PROFILES[profile_id]) == (
        ops.foundation_payload(ops.FOUNDATION_PROFILES[profile_id])
    )


@pytest.mark.parametrize("profile_id", sorted(ops.FOUNDATION_PROFILES))
def test_the_two_hardware_gates_are_the_same_fact(profile_id: str) -> None:
    """Ops writes a shell glob; the agent uses startswith. One fact, two forms.

    Ops's glob is escaped for a shell `case`, so "Raspberry\\ Pi*" and the
    prefix "Raspberry Pi" have to be compared after unescaping.
    """

    o = ops.FOUNDATION_PROFILES[profile_id]
    a = agent.FOUNDATION_PROFILES[profile_id]
    if o.hardware_model_match is None:
        assert a.hardware_model_prefix is None
        return
    assert o.hardware_model_match.replace("\\", "") == a.hardware_model_prefix + "*"


@pytest.mark.parametrize("profile_id", sorted(ops.FOUNDATION_PROFILES))
def test_the_two_floors_are_the_same_numbers(profile_id: str) -> None:
    """A Host would install and then fail later if these disagreed."""

    o = ops.FOUNDATION_PROFILES[profile_id]
    a = agent.FOUNDATION_PROFILES[profile_id]
    assert a.minimum_memory_kib == o.minimum_memory_kib
    assert a.minimum_disk_kib == o.minimum_disk_kib
    assert a.architecture == o.architecture
    assert a.os_ids == o.os_ids
    assert a.os_versions == o.os_versions


@pytest.mark.parametrize("profile_id", sorted(ops.FOUNDATION_PROFILES))
def test_the_journal_drop_in_goes_to_the_same_place(profile_id: str) -> None:
    o = ops.FOUNDATION_PROFILES[profile_id]
    a = agent.FOUNDATION_PROFILES[profile_id]
    assert a.journal_persistence == o.journal_persistence
    assert a.journal_persistence_content == o.journal_persistence_content


def test_a_payload_naming_no_profile_is_the_board_that_predates_them() -> None:
    """An older workstation must keep working against a newer agent."""

    assert (
        agent.requested_profile({}) is agent.FOUNDATION_PROFILES[agent.DEFAULT_FOUNDATION_PROFILE]
    )
    assert agent.DEFAULT_FOUNDATION_PROFILE == "raspberry-pi-os-debian-arm64-v2"


def test_a_payload_naming_an_unknown_profile_is_refused_by_name() -> None:
    from eidolon_ops.hostagent.primitives import TargetError

    with pytest.raises(TargetError, match="not one this agent was built for"):
        agent.requested_profile({"foundation": {"profile": "ubuntu-2804-rk3688"}})
