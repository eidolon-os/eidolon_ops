from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from eidolon_ops.host_identity import (
    HostIdentityError,
    derive_host_lan_identity,
    generate_hub_tls_identity,
    validate_hub_tls_identity,
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


def test_hub_tls_is_p256_host_bound_and_time_bounded() -> None:
    identity = derive_host_lan_identity(b"a" * 32)
    instant = datetime(2026, 8, 10, tzinfo=UTC)
    certificate, private_key = generate_hub_tls_identity(identity, now=instant)

    validate_hub_tls_identity(certificate, private_key, identity, now=instant)
    with pytest.raises(HostIdentityError, match="SAN"):
        validate_hub_tls_identity(
            certificate,
            private_key,
            derive_host_lan_identity(b"b" * 32),
            now=instant,
        )
    with pytest.raises(HostIdentityError, match="validity"):
        validate_hub_tls_identity(
            certificate,
            private_key,
            identity,
            now=instant + timedelta(days=3651),
        )


def test_host_identity_rejects_wrong_raw_shape() -> None:
    with pytest.raises(HostIdentityError, match="32 raw"):
        derive_host_lan_identity(b"short")


def test_host_identity_rejects_invalid_ports_validity_and_tls_material() -> None:
    identity = derive_host_lan_identity(b"a" * 32)
    with pytest.raises(HostIdentityError, match="port"):
        identity.hub_origin(0)
    with pytest.raises(HostIdentityError, match="validity"):
        generate_hub_tls_identity(identity, validity_days=0)
    with pytest.raises(HostIdentityError, match="unreadable"):
        validate_hub_tls_identity(b"invalid", b"invalid", identity)

    certificate, _private_key = generate_hub_tls_identity(identity)
    _other_certificate, other_key = generate_hub_tls_identity(identity)
    with pytest.raises(HostIdentityError, match="do not match"):
        validate_hub_tls_identity(certificate, other_key, identity)
