from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from cryptography import x509
from cryptography.hazmat.primitives.asymmetric import ec

from eidolon_ops.host_identity import derive_host_lan_identity
from eidolon_ops.owner_domain_assets import (
    OwnerDomainAssetError,
    ensure_owner_domain_assets,
    mark_authority_bootstrapped,
    reset_owner_authority,
)

NOW = datetime(2026, 8, 18, 12, 0, tzinfo=UTC)


def test_offline_issuer_keeps_private_signers_off_the_host_bundle(tmp_path: Path) -> None:
    identity = derive_host_lan_identity(b"a" * 32)

    first = ensure_owner_domain_assets(tmp_path / "owner-domain", identity, 8443, now=NOW)
    repeated = ensure_owner_domain_assets(tmp_path / "owner-domain", identity, 8443, now=NOW)

    assert repeated == first
    assert first.owner_domain_id.startswith("owner-")
    assert set(first.__dataclass_fields__) == {
        "owner_domain_id",
        "owner_domain_generation",
        "authority_state_id",
        "bootstrap_pending",
        "authority_bootstrap",
        "descriptor",
        "owner_root_certificate",
        "authority_signing_certificate",
        "tls_certificate",
        "tls_private_key",
    }
    private_names = {path.name for path in (tmp_path / "owner-domain").iterdir() if ".key." in path.name}
    assert private_names == {
        "owner-domain-root.key.pem",
        "authority-signing.key.pem",
    }


def test_host_tls_leaf_is_owner_root_signed_and_hostname_scoped(tmp_path: Path) -> None:
    identity = derive_host_lan_identity(b"a" * 32)
    assets = ensure_owner_domain_assets(tmp_path / "owner-domain", identity, 8443, now=NOW)
    root = x509.load_pem_x509_certificate(assets.owner_root_certificate)
    leaf = x509.load_pem_x509_certificate(assets.tls_certificate)

    assert leaf.issuer == root.subject
    assert leaf.subject != leaf.issuer
    assert leaf.extensions.get_extension_for_class(
        x509.SubjectAlternativeName
    ).value.get_values_for_type(x509.DNSName) == [identity.hub_hostname]
    public = root.public_key()
    assert isinstance(public, ec.EllipticCurvePublicKey)
    public.verify(
        leaf.signature,
        leaf.tbs_certificate_bytes,
        ec.ECDSA(leaf.signature_hash_algorithm),
    )


def test_host_endpoint_change_advances_only_directory_revision(tmp_path: Path) -> None:
    identity = derive_host_lan_identity(b"a" * 32)
    first = ensure_owner_domain_assets(tmp_path / "owner-domain", identity, 8443, now=NOW)
    second = ensure_owner_domain_assets(
        tmp_path / "owner-domain", identity, 9443, now=NOW + timedelta(minutes=1)
    )
    first_directory = json.loads(first.descriptor)
    second_directory = json.loads(second.descriptor)

    assert second.owner_domain_id == first.owner_domain_id
    assert second.owner_root_certificate == first.owner_root_certificate
    assert second.authority_signing_certificate == first.authority_signing_certificate
    assert second_directory["directory_revision"] == first_directory["directory_revision"] + 1
    assert all(":9443/" in endpoint["uri"] for endpoint in second_directory["endpoints"])
    assert "host_id" not in second_directory


def test_existing_descriptor_corruption_fails_closed(tmp_path: Path) -> None:
    identity = derive_host_lan_identity(b"a" * 32)
    root = tmp_path / "owner-domain"
    ensure_owner_domain_assets(root, identity, 8443, now=NOW)
    (root / "owner-domain-descriptor.json").write_text("{}", encoding="utf-8")

    with pytest.raises(OwnerDomainAssetError, match="descriptor is invalid"):
        ensure_owner_domain_assets(root, identity, 8443, now=NOW)


def test_existing_host_tls_corruption_fails_closed(tmp_path: Path) -> None:
    identity = derive_host_lan_identity(b"a" * 32)
    root = tmp_path / "owner-domain"
    ensure_owner_domain_assets(root, identity, 8443, now=NOW)
    (root / "hub.crt").write_bytes(b"not a certificate")

    with pytest.raises(OwnerDomainAssetError, match="certificate is invalid"):
        ensure_owner_domain_assets(root, identity, 8443, now=NOW)


def test_authority_reset_is_explicit_monotonic_and_issues_one_lineage(tmp_path: Path) -> None:
    identity = derive_host_lan_identity(b"a" * 32)
    root = tmp_path / "owner-domain"
    first = ensure_owner_domain_assets(root, identity, 8443, now=NOW)

    next_generation, next_state_id = reset_owner_authority(
        root,
        expected_owner_domain_id=first.owner_domain_id,
        expected_generation=first.owner_domain_generation,
    )
    reset = ensure_owner_domain_assets(root, identity, 8443, now=NOW + timedelta(minutes=1))

    assert next_generation == reset.owner_domain_generation == 2
    assert next_state_id == reset.authority_state_id
    assert reset.owner_domain_id == first.owner_domain_id
    assert reset.owner_root_certificate == first.owner_root_certificate
    assert json.loads(reset.descriptor)["directory_revision"] == 1
    assert json.loads(reset.authority_bootstrap)["operation"] == (
        "owner-authority.bootstrap"
    )
    assert reset.bootstrap_pending is True
    with pytest.raises(OwnerDomainAssetError, match="reset target changed"):
        reset_owner_authority(
            root,
            expected_owner_domain_id=first.owner_domain_id,
            expected_generation=1,
        )

    mark_authority_bootstrapped(
        root,
        owner_domain_id=reset.owner_domain_id,
        owner_domain_generation=reset.owner_domain_generation,
        authority_state_id=reset.authority_state_id,
    )
    consumed = ensure_owner_domain_assets(root, identity, 8443, now=NOW + timedelta(minutes=1))
    assert json.loads(consumed.authority_bootstrap)["operation"] == (
        "owner-authority.bootstrap-consumed"
    )
    assert consumed.bootstrap_pending is False
