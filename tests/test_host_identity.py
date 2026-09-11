from __future__ import annotations

import pytest

from eidolon_ops.host_identity import (
    HostIdentityError,
    derive_host_lan_identity,
)


def test_host_bound_names_are_stable_and_distinct() -> None:
    first = derive_host_lan_identity(b"a" * 32)
    repeated = derive_host_lan_identity(b"a" * 32)
    second = derive_host_lan_identity(b"b" * 32)

    assert first == repeated
    assert first != second
    assert first.hub_id == first.hub_hostname.removesuffix(".local")
    assert first.host_id.removeprefix("ehost-") in first.hub_id
    assert "eidolon-hub-local" not in {first.hub_id, second.hub_id}

def test_host_identity_rejects_wrong_raw_shape() -> None:
    with pytest.raises(HostIdentityError, match="32 raw"):
        derive_host_lan_identity(b"short")


def test_host_identity_rejects_invalid_ports() -> None:
    identity = derive_host_lan_identity(b"a" * 32)
    with pytest.raises(HostIdentityError, match="port"):
        identity.hub_origin(0)


def test_public_host_id_renders_existing_names_without_private_material():
    from eidolon_ops.host_identity import host_lan_identity_from_id
    original = derive_host_lan_identity(b"a" * 32)
    assert host_lan_identity_from_id(original.host_id) == original


@pytest.mark.parametrize("value", [None, "eidolon-pi5", "ehost-123", "ehost-" + "A" * 20])
def test_public_host_id_rejects_noncanonical_values(value):
    from eidolon_ops.host_identity import host_lan_identity_from_id
    with pytest.raises(HostIdentityError):
        host_lan_identity_from_id(value)
