"""Where each shipped profile keeps its SSH trust, held to one rule.

The mechanism — a profile-owned known_hosts, a tracked operator set, a tracked
fingerprint — was built for one profile and then only wired into that one. The
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

import subprocess
import tomllib
from pathlib import Path

import pytest

from eidolon_ops.config import ConfigurationError, load_config

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
def test_every_profile_can_state_the_key_it_expects(profile: Path) -> None:
    """The asymmetry this closes: operator keys tracked, host fingerprint not.

    Both are public — a fingerprint is derived from a public key, and the Host
    prints it to anyone who asks — but only one of them travelled. The other
    lived solely in the gitignored profile directory, so a second operator and
    a fresh checkout trusted on first use with nothing to compare against.

    The declaration may be empty. Nothing here can manufacture a value that is
    read off a board, and declaring what a scan returned would pin the key
    rather than confirm it — so an empty one is the honest state, and the field
    still has to parse as one.
    """

    declared = load_config(profile).host.host_fingerprint

    assert declared == "" or declared.startswith("SHA256:")


def test_a_malformed_declaration_is_refused_rather_than_never_matching(tmp_path: Path) -> None:
    """A value that cannot match must not read as "nobody declared one".

    Pasting the whole `ssh-keygen -lf` line rather than its second field is the
    way to get this wrong, and its failure is silent: the comparison would
    match no Host ever and report first use on a Host somebody had declared.
    """

    source = (CONFIG / "eidolon-pi.toml").read_text(encoding="utf-8")
    broken = tmp_path / "eidolon-pi.toml"
    broken.write_text(
        source.replace(
            'host_fingerprint = ""',
            'host_fingerprint = "256 SHA256:rGKurdR2TIc8J2A5A5XXstiwfyIK2SnElnSpUtHn7wo (ED25519)"',
        ),
        encoding="utf-8",
    )

    with pytest.raises(ConfigurationError, match="that field alone"):
        load_config(broken)


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
def test_the_bench_address_is_left_alone(profile: Path) -> None:
    """Where trust is written is not how the board is found.

    The rk3588 profile reaches its board at a literal point-to-point address,
    with its reasons and its exit condition written beside it. Moving its trust
    into the profile's own file changed neither, and this says so — the next
    reader of that unusual `hostname` should find a test that expects it rather
    than guess it survived by accident.
    """

    document = tomllib.loads(profile.read_text(encoding="utf-8"))
    hostname = document["host"]["hostname"]
    alias_source = load_config(profile).host.hostname

    # Whatever the profile names — mDNS name or literal address — is exactly
    # what trust is granted under. That equality is the mechanism.
    assert alias_source == hostname
    if profile.name == "eidolon-rk3588.toml":
        assert hostname == "10.42.0.2"
