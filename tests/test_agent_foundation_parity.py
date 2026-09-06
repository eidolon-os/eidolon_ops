"""The agent's copy of the foundations, held to Ops's.

The Host agent is injected as one payload and imports nothing from the package
it came from, so it cannot share these tables — and should not, because its job
is to refuse a contract that differs from what it was built against, and
reading the one it was handed would make that circular. The duplication is the
design; drift between the copies is the failure it invites, and this is what
stops it.
"""

from __future__ import annotations

from pathlib import Path

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


@pytest.mark.parametrize("profile_id", sorted(ops.FOUNDATION_PROFILES))
def test_the_doctor_reports_the_board_it_actually_checked(profile_id: str, monkeypatch) -> None:
    """It reported the default's name whatever it had been handed.

    Found by reading rather than by a failure, which is the reason for this
    test: nothing else compares the name in the report to the payload, so an
    RK3588 Host would have called itself a Raspberry Pi and been believed.
    """

    monkeypatch.setattr(agent, "foundation_platform_checks", lambda *_: {"ok": True})
    monkeypatch.setattr(agent, "package_installed", lambda *_: True)
    monkeypatch.setattr(agent, "binary_version", lambda *_: {"healthy": True})
    monkeypatch.setattr(agent, "journal_is_persistent", lambda: True)
    monkeypatch.setattr(agent.primitives, "service_status", lambda *_: {"healthy": True})

    payload = {"foundation": ops.foundation_payload(ops.FOUNDATION_PROFILES[profile_id])}
    assert agent.foundation_doctor(payload)["profile"] == profile_id


def test_provision_sends_the_foundation_this_host_names() -> None:
    """Not whichever the builders default to.

    Taking the default sent a Raspberry Pi's contract to an RK3588 board. The
    agent then did exactly its job — measured that board against the profile it
    was handed and reported, correctly and uselessly, that it is not a
    Raspberry Pi. Found by running a provision plan against the real board,
    which is the only place the two halves meet.
    """

    import inspect

    from eidolon_ops import controller

    source = inspect.getsource(controller.EidolonPiController.provision)
    assert "foundation_payload(profile)" in source
    assert "python_bootstrap_script(profile)" in source
    assert "foundation_profile(self.config.foundation_profile)" in source
    # The defaults still exist for the agent, which has no config to read.
    assert "foundation_payload()" not in source
    assert "python_bootstrap_script()" not in source


@pytest.mark.parametrize("profile_id", sorted(ops.FOUNDATION_PROFILES))
def test_both_sides_fetch_packages_from_the_same_archives(profile_id: str) -> None:
    """Ops writes them into the bootstrap; the agent writes them again to install.

    They were not the same thing before: the agent mapped an OS version to a
    Debian suite and addressed mirrors by Debian's names, so an Ubuntu board
    was refused for wanting a reviewed Debian version. Whatever each side
    writes, it must be the same archives.
    """

    o = ops.FOUNDATION_PROFILES[profile_id]
    a = agent.FOUNDATION_PROFILES[profile_id]
    assert a.apt_suite == o.apt_suite
    assert len(a.apt_sources) == len(o.apt_sources)
    for mine, theirs in zip(a.apt_sources, o.apt_sources, strict=True):
        assert (mine.uris, mine.suites, mine.components, mine.signed_by) == (
            theirs.uris,
            theirs.suites,
            theirs.components,
            theirs.signed_by,
        )


@pytest.mark.parametrize("profile_id", sorted(ops.FOUNDATION_PROFILES))
def test_the_agent_writes_a_sources_list_it_could_fetch_from(profile_id: str) -> None:
    from eidolon_ops.hostagent import foundation_install

    with foundation_install.foundation_apt_options(
        agent.FOUNDATION_PROFILES[profile_id]
    ) as options:
        path = next(
            value.split("=", 1)[1] for value in options if value.startswith("Dir::Etc::sourcelist=")
        )
        text = Path(path).read_text(encoding="utf-8")

    profile = ops.FOUNDATION_PROFILES[profile_id]
    assert "{suite}" not in text, "the suite placeholder must be filled in"
    assert profile.apt_suite in text
    for source in profile.apt_sources:
        assert f"URIs: {source.uris}" in text
        assert f"Signed-By: {source.signed_by}" in text
