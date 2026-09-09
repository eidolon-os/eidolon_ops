"""The agent's copy of the foundations, held to Ops's.

The Host agent is injected as one payload and imports nothing from the package
it came from, so it cannot share these tables — and should not, because its job
is to refuse a contract that differs from what it was built against, and
reading the one it was handed would make that circular. The duplication is the
design; drift between the copies is the failure it invites, and this is what
stops it.
"""

from __future__ import annotations

import dataclasses
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
    assert "foundation_payload(profile" in source
    # And the capabilities the same way: defaulting them would send a contract
    # with no capability artifacts to a Host that declared some, and the agent
    # would refuse the payload built for it.
    assert "foundation_payload(profile, self.config.capabilities)" in source
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


@pytest.mark.parametrize("profile_id", sorted(ops.FOUNDATION_PROFILES))
def test_both_sides_agree_on_the_interpreter(profile_id: str) -> None:
    o = ops.FOUNDATION_PROFILES[profile_id]
    a = agent.FOUNDATION_PROFILES[profile_id]
    assert a.python_version == o.python_version
    carried = [x.artifact_id for x in o.artifacts if x.artifact_id == "cpython"]
    assert bool(carried) == (o.python_version is not None), (
        "a profile that names a Python must carry one, and one that names none must not"
    )


def test_the_interpreter_a_profile_carries_satisfies_every_repository() -> None:
    """The version the foundation provides against the version releases need.

    These are two statements of one fact in different repositories, and nothing
    compared them. The Host discovered the disagreement instead — from uv, in
    the middle of a remote build, phrased as a complaint about download policy
    rather than as a foundation that does not fit the release.
    """

    import re
    import tomllib
    from pathlib import Path

    repositories = Path(__file__).resolve().parents[2]
    pins: dict[str, str] = {}
    for pyproject in sorted(repositories.glob("eidolon_*/pyproject.toml")):
        with pyproject.open("rb") as handle:
            requires = tomllib.load(handle).get("project", {}).get("requires-python")
        if requires:
            pins[pyproject.parent.name] = requires
    if not pins:
        pytest.skip("needs the sibling repositories to read their pins")

    # Every repository is expected to state the same pin; a split is its own bug.
    assert len(set(pins.values())) == 1, f"repositories disagree on Python: {pins}"
    lower, upper = re.fullmatch(r">=(\d+\.\d+),<(\d+\.\d+)", next(iter(pins.values()))).groups()

    for profile in ops.FOUNDATION_PROFILES.values():
        if profile.python_version is None:
            # The OS provides it; what it provides cannot be read from here.
            continue
        major, minor, *_ = profile.python_version.split(".")
        series = f"{major}.{minor}"
        assert series == lower, (
            f"{profile.id} carries Python {profile.python_version}, "
            f"and the repositories pin {lower} <= python < {upper}"
        )


def test_both_sides_know_the_same_capability_artifacts() -> None:
    """Ops holds dataclasses, the agent holds plain dicts — one fact, two forms.

    A capability whose artifacts only Ops knows would be carried and never
    installed; one only the agent knows would make the agent expect a file the
    release never brought.
    """

    assert set(agent.CAPABILITY_FOUNDATION_ARTIFACTS) == set(
        ops.CAPABILITY_FOUNDATION_ARTIFACTS
    )
    for capability, artifacts in ops.CAPABILITY_FOUNDATION_ARTIFACTS.items():
        mirrored = agent.CAPABILITY_FOUNDATION_ARTIFACTS[capability]
        assert len(mirrored) == len(artifacts)
        for source, copy in zip(artifacts, mirrored):
            assert copy == {
                key: value
                for key, value in dataclasses.asdict(source).items()
                if value is not None
            }


@pytest.mark.parametrize("capability", sorted(ops.CAPABILITY_FOUNDATION_ARTIFACTS))
@pytest.mark.parametrize("profile_id", sorted(ops.FOUNDATION_PROFILES))
def test_a_capability_does_not_change_what_the_two_sides_agree_on(
    profile_id: str, capability: str
) -> None:
    """The same equality as the plain case, once the Host has asked for more.

    Every capability a Host can declare has to survive this on its own, because
    that is the payload the agent will be handed.
    """

    declared = frozenset({capability})
    assert agent.expected_foundation(
        agent.FOUNDATION_PROFILES[profile_id], declared
    ) == ops.foundation_payload(ops.FOUNDATION_PROFILES[profile_id], declared)


def test_capability_artifacts_are_only_the_ones_a_host_can_declare() -> None:
    """These names are the closed capability set, checked where it lives.

    A misspelt key here would be silently inert — the Host would declare the
    real name, find no artifacts under it, and install nothing.
    """

    from eidolon_ops.config import HOST_CAPABILITIES

    assert set(ops.CAPABILITY_FOUNDATION_ARTIFACTS) <= HOST_CAPABILITIES


def test_every_artifact_any_host_can_install_has_version_evidence() -> None:
    """Missing here, a doctor raises KeyError the moment the file exists.

    The artifact was added and this was not, so the dry run passed — the
    binary was absent and the check returns early — and the apply installed it
    correctly and then failed comparing it against nothing. Found on a board,
    which is the wrong place: every artifact any Host can be asked to install
    is enumerable here.
    """

    executables = {
        artifact.executable
        for profile in ops.FOUNDATION_PROFILES.values()
        for artifact in profile.artifacts
    }
    executables |= {
        artifact.executable
        for artifacts in ops.CAPABILITY_FOUNDATION_ARTIFACTS.values()
        for artifact in artifacts
    }

    assert executables <= set(agent.FOUNDATION_VERSION_PREFIXES), sorted(
        executables - set(agent.FOUNDATION_VERSION_PREFIXES)
    )
    # And nothing left behind by an artifact that is gone.
    assert set(agent.FOUNDATION_VERSION_PREFIXES) <= executables, sorted(
        set(agent.FOUNDATION_VERSION_PREFIXES) - executables
    )
