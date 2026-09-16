from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from cryptography import x509
from cryptography.hazmat.primitives.asymmetric import ec
from eidolon_sdk.device_foundation.v1.directory_tool import issue_descriptor

from eidolon_ops.host_identity import derive_host_lan_identity
from eidolon_ops.owner_domain_assets import (
    OwnerDomainAssetError,
    adopt_host_authority,
    ensure_owner_domain_assets,
    mark_authority_bootstrapped,
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


def test_directory_advertises_the_canonical_admission_authority_route(tmp_path: Path) -> None:
    identity = derive_host_lan_identity(b"a" * 32)
    assets = ensure_owner_domain_assets(tmp_path / "owner-domain", identity, 8443, now=NOW)
    endpoints = {
        endpoint["authority"]: endpoint["uri"]
        for endpoint in json.loads(assets.descriptor)["endpoints"]
    }

    assert endpoints["admission"].endswith("/api/admission/v1")
    assert "/api/device-onboarding/v1" not in endpoints["admission"]


def test_directory_publishes_the_route_it_is_served_from(tmp_path: Path) -> None:
    # This Host is the only party that knows where it serves the document, so it
    # is the only party that may state the route. Consumers used to derive it by
    # appending "/descriptor" to the Admission base — a path no Host answers,
    # which failed commissioning at its final step.
    identity = derive_host_lan_identity(b"a" * 32)
    assets = ensure_owner_domain_assets(tmp_path / "owner-domain", identity, 8443, now=NOW)
    directory = json.loads(assets.descriptor)

    assert directory["descriptor_uri"] == (
        identity.hub_origin(8443) + "/api/device-onboarding/v1/descriptor"
    )
    assert all(
        not endpoint["uri"].endswith("/descriptor")
        for endpoint in directory["endpoints"]
    )


def test_stale_descriptor_route_is_reissued_at_the_next_revision(tmp_path: Path) -> None:
    # The reuse shortcut compares the document it would issue against the one on
    # disk. A field left out of that comparison is a field that never reaches a
    # commissioned device: the stale document keeps being served, correctly
    # signed, forever.
    identity = derive_host_lan_identity(b"a" * 32)
    root = tmp_path / "owner-domain"
    first = ensure_owner_domain_assets(root, identity, 8443, now=NOW)
    current = json.loads(first.descriptor)
    stale = issue_descriptor(
        {
            "owner_domain_id": current["owner_domain_id"],
            "owner_domain_generation": current["owner_domain_generation"],
            "trust_epoch": 1,
            "directory_revision": current["directory_revision"],
            "descriptor_uri": "https://elsewhere.invalid/api/device-onboarding/v1/descriptor",
            "endpoints": current["endpoints"],
            "issued_at": current["issued_at"],
            "expires_at": current["expires_at"],
        },
        owner_root_certificate_pem=(root / "owner-domain-root-ca.pem").read_text(
            encoding="ascii"
        ),
        authority_signing_certificate_pem=(
            root / "authority-signing-certificate.pem"
        ).read_text(encoding="ascii"),
        authority_private_key_pem=(root / "authority-signing.key.pem").read_bytes(),
    )
    (root / "owner-domain-descriptor.json").write_text(
        json.dumps(stale.model_dump(mode="json"), indent=2) + "\n", encoding="utf-8"
    )

    reissued = json.loads(
        ensure_owner_domain_assets(
            root, identity, 8443, now=NOW + timedelta(minutes=1)
        ).descriptor
    )

    assert reissued["descriptor_uri"] == (
        identity.hub_origin(8443) + "/api/device-onboarding/v1/descriptor"
    )
    assert reissued["directory_revision"] == current["directory_revision"] + 1


def test_a_descriptor_older_than_the_contract_is_reissued_not_refused(
    tmp_path: Path,
) -> None:
    """The field this contract grew is a reason to reissue, never to stop.

    Every Host that has ever been provisioned has a descriptor on disk. When the
    signed document gained `descriptor_uri`, that stored copy stopped satisfying
    the model, and this read reported it as invalid — so `deploy` failed on every
    such Host, which is every Host in the field. The revision line is what must
    be continued; which fields the previous document happened to carry is not.
    """

    identity = derive_host_lan_identity(b"a" * 32)
    root = tmp_path / "owner-domain"
    path = root / "owner-domain-descriptor.json"
    ensure_owner_domain_assets(root, identity, 8443, now=NOW)

    stored = json.loads(path.read_text(encoding="utf-8"))
    superseded_revision = stored["directory_revision"]
    del stored["descriptor_uri"]
    path.write_text(json.dumps(stored), encoding="utf-8")

    reissued = json.loads(
        ensure_owner_domain_assets(
            root, identity, 8443, now=NOW + timedelta(minutes=1)
        ).descriptor
    )
    assert reissued["descriptor_uri"] == (
        identity.hub_origin(8443) + "/api/device-onboarding/v1/descriptor"
    )
    assert reissued["directory_revision"] == superseded_revision + 1


def test_existing_descriptor_corruption_fails_closed(tmp_path: Path) -> None:
    identity = derive_host_lan_identity(b"a" * 32)
    root = tmp_path / "owner-domain"
    ensure_owner_domain_assets(root, identity, 8443, now=NOW)
    (root / "owner-domain-descriptor.json").write_text("{}", encoding="utf-8")

    # No Owner Domain named and no revision to continue: reissuing over this
    # would either seize another Owner's directory or restart a revision line
    # devices have already been told about.
    with pytest.raises(OwnerDomainAssetError, match="another Owner Domain"):
        ensure_owner_domain_assets(root, identity, 8443, now=NOW)

    (root / "owner-domain-descriptor.json").write_text("not json", encoding="utf-8")
    with pytest.raises(OwnerDomainAssetError, match="unreadable"):
        ensure_owner_domain_assets(root, identity, 8443, now=NOW)


def test_existing_host_tls_corruption_fails_closed(tmp_path: Path) -> None:
    identity = derive_host_lan_identity(b"a" * 32)
    root = tmp_path / "owner-domain"
    ensure_owner_domain_assets(root, identity, 8443, now=NOW)
    (root / "hub.crt").write_bytes(b"not a certificate")

    with pytest.raises(OwnerDomainAssetError, match="certificate is invalid"):
        ensure_owner_domain_assets(root, identity, 8443, now=NOW)




@pytest.mark.parametrize("missing", [
    ("owner-domain-state.json",),
    ("owner-domain-root.key.pem", "owner-domain-root-ca.pem"),
])
def test_partial_owner_identity_is_not_reissued(tmp_path: Path, missing) -> None:
    material = tmp_path / "owner-domain"
    identity = derive_host_lan_identity(b"a" * 32)
    ensure_owner_domain_assets(material, identity, 8443, now=NOW)
    for name in missing:
        (material / name).unlink()
    before = {p.name: p.read_bytes() for p in material.iterdir()}
    with pytest.raises(OwnerDomainAssetError, match="AUTHORITY_RECOVERY_REQUIRED"):
        ensure_owner_domain_assets(material, identity, 8443, now=NOW)
    assert {p.name: p.read_bytes() for p in material.iterdir()} == before


def _lineage(assets) -> dict[str, object]:
    return {
        "contract_version": 1,
        "owner_domain_id": assets.owner_domain_id,
        "owner_domain_generation": assets.owner_domain_generation,
        "state_id": assets.authority_state_id,
    }


def test_adoption_refuses_a_directory_this_owner_root_did_not_sign(tmp_path: Path) -> None:
    identity = derive_host_lan_identity(b"a" * 32)
    mine = ensure_owner_domain_assets(tmp_path / "mine", identity, 8443, now=NOW)
    stranger = ensure_owner_domain_assets(tmp_path / "stranger", identity, 8443, now=NOW)
    before = {p.name: p.read_bytes() for p in (tmp_path / "mine").iterdir()}

    with pytest.raises(OwnerDomainAssetError, match="another Owner Domain"):
        adopt_host_authority(
            tmp_path / "mine",
            directory=stranger.descriptor,
            lineage=_lineage(stranger),
            now=NOW,
        )

    assert mine.owner_domain_id != stranger.owner_domain_id
    assert {p.name: p.read_bytes() for p in (tmp_path / "mine").iterdir()} == before


def test_adoption_refuses_a_lineage_the_directory_does_not_name(tmp_path: Path) -> None:
    identity = derive_host_lan_identity(b"a" * 32)
    assets = ensure_owner_domain_assets(tmp_path / "owner-domain", identity, 8443, now=NOW)
    claimed = {**_lineage(assets), "owner_domain_generation": assets.owner_domain_generation + 1}

    with pytest.raises(OwnerDomainAssetError, match="does not match the directory"):
        adopt_host_authority(
            tmp_path / "owner-domain",
            directory=assets.descriptor,
            lineage=claimed,
            now=NOW,
        )


def test_adoption_refuses_to_walk_a_live_revision_line_backwards(tmp_path: Path) -> None:
    material = tmp_path / "owner-domain"
    identity = derive_host_lan_identity(b"a" * 32)
    served = ensure_owner_domain_assets(material, identity, 8443, now=NOW)
    # The endpoint moved, so this side issued the next revision of the same
    # lineage and has not delivered it yet. The Host still serves the one above.
    issued = ensure_owner_domain_assets(material, identity, 9443, now=NOW)
    assert issued.owner_domain_generation == served.owner_domain_generation

    with pytest.raises(OwnerDomainAssetError, match="newer directory"):
        adopt_host_authority(
            material, directory=served.descriptor, lineage=_lineage(served), now=NOW
        )

    assert (material / "owner-domain-descriptor.json").read_bytes() == issued.descriptor


def test_adopting_what_this_material_already_holds_writes_nothing(tmp_path: Path) -> None:
    material = tmp_path / "owner-domain"
    identity = derive_host_lan_identity(b"a" * 32)
    assets = ensure_owner_domain_assets(material, identity, 8443, now=NOW)
    mark_authority_bootstrapped(
        material,
        owner_domain_id=assets.owner_domain_id,
        owner_domain_generation=assets.owner_domain_generation,
        authority_state_id=assets.authority_state_id,
    )
    before = {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in material.iterdir()}

    adopted = adopt_host_authority(
        material, directory=assets.descriptor, lineage=_lineage(assets), now=NOW
    )

    assert adopted["owner_domain_generation"] == assets.owner_domain_generation
    assert {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in material.iterdir()} == before


def test_adoption_keeps_every_key_and_only_moves_the_lineage(tmp_path: Path) -> None:
    material = tmp_path / "owner-domain"
    identity = derive_host_lan_identity(b"a" * 32)
    served = ensure_owner_domain_assets(material, identity, 8443, now=NOW)
    kept = _lineage(served)
    # What a second board's install left behind: a generation this Owner did
    # issue, and a directory naming it.
    state = json.loads((material / "owner-domain-state.json").read_text(encoding="utf-8"))
    (material / "owner-domain-state.json").write_text(
        json.dumps(
            {
                **state,
                "owner_domain_generation": state["owner_domain_generation"] + 1,
                "authority_state_id": "authority-state_the-other-board",
                "bootstrap_pending": False,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n",
        encoding="utf-8",
    )
    moved = ensure_owner_domain_assets(material, identity, 8443, now=NOW)
    assert moved.owner_domain_generation == served.owner_domain_generation + 1
    keys = {
        name: (material / name).read_bytes()
        for name in ("owner-domain-root.key.pem", "authority-signing.key.pem", "hub.key", "hub.crt")
    }

    adopted = adopt_host_authority(
        material, directory=served.descriptor, lineage=kept, now=NOW
    )

    assert adopted == {
        "contract_version": 1,
        "owner_domain_id": served.owner_domain_id,
        "owner_domain_generation": served.owner_domain_generation,
        "authority_state_id": served.authority_state_id,
        "bootstrap_pending": False,
    }
    assert (material / "owner-domain-descriptor.json").read_bytes() == served.descriptor
    assert {name: (material / name).read_bytes() for name in keys} == keys
    # And the result is a material the ordinary issuer now leaves alone.
    settled = ensure_owner_domain_assets(material, identity, 8443, now=NOW)
    assert settled.descriptor == served.descriptor
    assert _lineage(settled) == kept


def test_invalid_owner_state_does_not_renew_signers(tmp_path: Path) -> None:
    material = tmp_path / "owner-domain"
    identity = derive_host_lan_identity(b"a" * 32)
    ensure_owner_domain_assets(material, identity, 8443, now=NOW)
    (material / "owner-domain-state.json").write_text("invalid")
    before = {p.name: p.read_bytes() for p in material.iterdir()}
    with pytest.raises(OwnerDomainAssetError, match="state is invalid"):
        ensure_owner_domain_assets(material, identity, 8443, now=NOW + timedelta(days=400))
    assert {p.name: p.read_bytes() for p in material.iterdir()} == before
