"""Where each shipped profile keeps its SSH trust, held to one rule.

The mechanism — a profile-owned known_hosts and a tracked operator set — was
built for one profile and then only wired into that one. The
second board kept pointing at the operator's `~/.ssh/known_hosts` for a year,
which is where the design says trust must not live, and nothing failed: every
operation worked, because the mechanism this bypasses is the one that only
speaks up when a board is swapped.

So the rule is checked against every profile in the repository rather than
against the one it was written for. A third board is added by writing its
profile, and the thing most likely to be forgotten is the thing nothing else
would report.
"""

from __future__ import annotations

import ipaddress
import subprocess
import tomllib
from pathlib import Path

import pytest

from eidolon_ops.config import load_config

pytestmark = pytest.mark.unit

REPOSITORY = Path(__file__).resolve().parents[1]
CONFIG = REPOSITORY / "config"
#: Every real operations profile — a board this repository actually deploys to.
#: The examples are documentation and carry placeholder paths on purpose.
PROFILES = sorted(
    path
    for path in CONFIG.glob("eidolon-*.toml")
    if not path.name.endswith(".example.toml")
)


def _identifier(profile: Path) -> str:
    return profile.name


def _tracked(path: Path) -> bool:
    """Whether git would carry this file — tracked, or added but not committed.

    `--cached --others --exclude-standard` rather than `--error-unmatch`, the
    same reading `test_repository_path_contract.py` uses: a profile and the
    fingerprint file it names arrive in one change, and holding the second to a
    rule the first is not yet held to would fail for a reason nobody means. It
    still excludes everything gitignored, which is the distinction that matters
    here — `.eidolon-ops/` is never shippable.
    """

    result = subprocess.run(
        (
            "git",
            "-C",
            str(REPOSITORY),
            "ls-files",
            "-z",
            "--cached",
            "--others",
            "--exclude-standard",
            "--",
            str(path),
        ),
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        # Outside the repository, which for the paths asked about here means a
        # file in the operator's own home. Not shippable, which is the answer.
        return False
    return bool(result.stdout.strip(b"\0"))


def test_there_are_profiles_to_check() -> None:
    """Without this, every parametrized test below passes by finding nothing."""

    assert PROFILES, f"no operations profiles found under {CONFIG}"


@pytest.mark.parametrize("profile", PROFILES, ids=_identifier)
def test_no_profile_keeps_its_trust_in_the_operators_own_known_hosts(profile: Path) -> None:
    """The failure this exists for, stated as the thing that must not be true.

    `~/.ssh/known_hosts` is the one location the design rules out: the edit is
    untracked, it sits among a hundred unrelated hosts, and it is easy to get
    wrong in the direction that matters — deleting the wrong line fails loudly,
    pasting an unverified key fails silently and forever. A board swap there is
    a hand edit, and the workaround an operator reaches for when a hand edit is
    hard is `-o StrictHostKeyChecking=no`.
    """

    known_hosts = load_config(profile).host.known_hosts_file

    assert known_hosts.parent != Path.home() / ".ssh", (
        f"{profile.name} records its host key trust in the operator's own "
        f"~/.ssh/known_hosts ({known_hosts}). It belongs in this profile's own "
        "directory beside its other private inputs."
    )
    # Positively: the profile's own directory, which is where the private
    # inputs already are and which is gitignored — so the location is reviewed
    # and the key never is.
    assert ".eidolon-ops" in known_hosts.parts, (
        f"{profile.name} keeps its known_hosts outside the profile's own "
        f"directory: {known_hosts}"
    )


@pytest.mark.parametrize("profile", PROFILES, ids=_identifier)
def test_every_profile_states_its_operators_rather_than_implying_them(profile: Path) -> None:
    """Otherwise the board trusts whoever holds `identity_file`, and only them."""

    operators = load_config(profile).host.operator_keys_file

    assert operators is not None, (
        f"{profile.name} declares no host.operator_keys_file, so the only thing saying "
        "who may reach this board is the private key on this one workstation — and a "
        "second operator can only be added by handing them a copy of it."
    )
    assert operators.is_file(), f"{profile.name} names {operators}, which does not exist"
    assert _tracked(operators), (
        f"{operators.name} is not tracked. A public key is public, and who may operate a "
        "board is a reviewed decision rather than whatever sits in one laptop's ~/.ssh."
    )


@pytest.mark.parametrize("profile", PROFILES, ids=_identifier)
def test_a_profile_keeps_its_private_material_out_of_the_repository(profile: Path) -> None:
    """The other half of the same rule, so "tracked" never generalizes.

    Fingerprints and operator public keys are tracked because they are public.
    The private key this workstation connects with is not, and neither is the
    known_hosts the profile writes — both stay outside the repository, under
    the operator's home or the gitignored profile directory.
    """

    host = load_config(profile).host
    for label, path in (
        ("host.identity_file", host.identity_file),
        ("host.known_hosts_file", host.known_hosts_file),
    ):
        assert not _tracked(path), f"{profile.name}: {label} points at a tracked file ({path})"


@pytest.mark.parametrize("profile", PROFILES, ids=_identifier)
def test_a_profile_names_its_host_rather_than_addressing_one(profile: Path) -> None:
    """The first domino, and the reason a bench needed aligning at all.

    A Host with a name is found by it: the board self-assigns a link-local
    address, publishes the name over mDNS, and Ops resolves it per run. A Host
    with an address instead needs that address to be fixed, which means the
    board is configured to hold it and the workstation is configured onto its
    subnet — and then swapping to a board that does it the other way means
    reconfiguring both ends by hand.

    That is not hypothetical: this profile carried `10.42.0.2` because
    `bring-up` had no declaration for its platform, so its link was configured
    by hand, and by hand meant a literal. Two boards then could not share one
    cable without someone editing the workstation in between.
    """

    document = tomllib.loads(profile.read_text(encoding="utf-8"))
    hostname = document["host"]["hostname"]

    with pytest.raises(ValueError):
        ipaddress.ip_address(hostname.removesuffix(".local"))

    # And whatever the profile names is exactly what trust is granted under.
    # That equality is the mechanism: the name, not the link, is the identity.
    assert load_config(profile).host.hostname == hostname


@pytest.mark.parametrize("profile", PROFILES, ids=_identifier)
def test_no_profile_declares_a_management_network_it_no_longer_needs(
    profile: Path,
) -> None:
    """A link nothing has to be told about is one nothing can get wrong.

    `management_networks` exists for a bench link the Host cannot tell apart
    from the product LAN — a routable subnet only this workstation is on. A
    self-assigned 169.254/16 is not that: every service that publishes an
    address to a device already filters link-local, without being told. The
    declaration was the cost of the literal, and it is not free — until it was
    added, a device took the workstation's address and could not route to the
    Host at all (2026-09-15, BOX-3).

    Not forbidden, because a future Host may genuinely have such a link. But a
    profile that names its Host has no reason to carry one, and a profile that
    grows one back is worth a second look.
    """

    declared = load_config(profile).host.management_networks

    assert declared == (), (
        f"{profile.name} declares {declared}, which a link-local bench cable does not "
        "need — check whether this Host is being addressed rather than named again."
    )
