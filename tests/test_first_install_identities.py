"""What a Host being installed for the first time has to be given.

Every check here failed to matter on a Host that had been installed before,
because the identities it needs were left behind by the previous run. The
first real install onto a fresh board is what found them.
"""

from __future__ import annotations

import inspect

import pytest

from eidolon_ops.hostagent import identities, install

pytestmark = pytest.mark.unit


def test_install_creates_the_group_the_path_contract_chowns_to() -> None:
    """install chowns to the owner-trust group in the statement after this.

    The service-identity cutover creates it too, and runs later. On a Host with
    a previous install it was already there; on a fresh one the install failed
    naming a group and saying nothing about which step should have made it.
    """

    source = inspect.getsource(install.TargetInstaller._ensure_identities_and_directories)
    created = "_ensure_service_group(identities.OWNER_TRUST_GROUP)"
    assert created in source, "install must create the group, not merely name it"
    assert source.index(created) < source.index("ensure_host_path_contract"), (
        "the group has to exist before anything is chowned to it"
    )


def test_the_readers_install_creates_are_put_in_that_group() -> None:
    """Otherwise the files are readable by a group with no members in it."""

    source = inspect.getsource(install.TargetInstaller._ensure_identities_and_directories)
    for name in identities.OWNER_TRUST_READERS:
        assert f'_ensure_service_identity("{name}")' in source, (
            f"{name} reads owner trust and install does not create it"
        )
    assert "identities.OWNER_TRUST_READERS" in source


def test_the_contract_really_does_chown_to_that_group() -> None:
    """If it stopped, the two checks above would be guarding nothing."""

    from eidolon_ops.hostagent import contract

    source = inspect.getsource(contract)
    assert identities.OWNER_TRUST_GROUP in source
