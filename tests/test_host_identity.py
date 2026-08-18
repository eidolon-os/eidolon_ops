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
