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
    LEGACY_AUTHORITY_STATE,
    HostAuthority,
    OwnerDomainAssetError,
    ensure_owner_domain_assets,
    ensure_owner_material,
    owner_domain_id_of,
    retire_legacy_authority_state,
    verify_served_directory,
)

NOW = datetime(2026, 8, 18, 12, 0, tzinfo=UTC)
#: A Host holding nothing, with the state id fixed so two renders compare equal.
FRESH = HostAuthority(owner_domain_generation=1, state_id="authority-state_fresh", established=False)


def _established(owner_domain_id: str, *, generation: int, state_id: str = "authority-state_board") -> HostAuthority:
    return HostAuthority.established_from(
        {
            "contract_version": 1,
            "owner_domain_id": owner_domain_id,
            "owner_domain_generation": generation,
            "state_id": state_id,
        }
    )


def _snapshot(root: Path) -> dict[str, tuple[bytes, int]]:
    return {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in root.iterdir()}


def test_offline_issuer_keeps_private_signers_off_the_host_bundle(tmp_path: Path) -> None:
    identity = derive_host_lan_identity(b"a" * 32)

    first = ensure_owner_domain_assets(tmp_path / "owner-domain", identity, 8443, FRESH, now=NOW)
    repeated = ensure_owner_domain_assets(tmp_path / "owner-domain", identity, 8443, FRESH, now=NOW)

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
    # No record of the Authority is kept beside the keys: the Host is its ledger.
    assert not (tmp_path / "owner-domain" / LEGACY_AUTHORITY_STATE).exists()


def test_owner_material_is_keys_only_and_names_no_generation(tmp_path: Path) -> None:
    """What `init-inputs` creates: the Owner, with no Host yet to speak for."""

    identity = derive_host_lan_identity(b"a" * 32)
    root = tmp_path / "owner-domain"

    owner_domain_id = ensure_owner_material(root, identity, now=NOW)

    assert {p.name for p in root.iterdir()} == {
        "owner-domain-root.key.pem",
        "owner-domain-root-ca.pem",
        "authority-signing.key.pem",
        "authority-signing-certificate.pem",
        "hub.crt",
        "hub.key",
    }
    assert owner_domain_id_of(root) == owner_domain_id
    # Addressing a Host later reuses every key and only then issues a directory.
    assets = ensure_owner_domain_assets(root, identity, 8443, FRESH, now=NOW)
    assert assets.owner_domain_id == owner_domain_id
    assert json.loads(assets.descriptor)["owner_domain_generation"] == 1


def test_owner_domain_id_needs_a_root(tmp_path: Path) -> None:
    with pytest.raises(OwnerDomainAssetError, match="has no root"):
        owner_domain_id_of(tmp_path / "owner-domain")


def test_the_bundle_names_exactly_what_the_host_established(tmp_path: Path) -> None:
    identity = derive_host_lan_identity(b"a" * 32)
    root = tmp_path / "owner-domain"
    fresh = ensure_owner_domain_assets(root, identity, 8443, FRESH, now=NOW)
    assert fresh.bootstrap_pending is True
    assert json.loads(fresh.authority_bootstrap)["operation"] == "owner-authority.bootstrap"

    board = _established(fresh.owner_domain_id, generation=8)
    held = ensure_owner_domain_assets(root, identity, 8443, board, now=NOW)

    assert held.owner_domain_generation == 8
    assert held.authority_state_id == "authority-state_board"
    assert held.bootstrap_pending is False
    tombstone = json.loads(held.authority_bootstrap)
    assert tombstone == {
        "contract_version": 1,
        "operation": "owner-authority.bootstrap-consumed",
        "owner_domain_id": fresh.owner_domain_id,
        "owner_domain_generation": 8,
        "state_id": "authority-state_board",
    }
    assert json.loads(held.descriptor)["owner_domain_generation"] == 8
    assert held.owner_root_certificate == fresh.owner_root_certificate


def test_an_authority_another_owner_established_cannot_be_rendered_here(tmp_path: Path) -> None:
    identity = derive_host_lan_identity(b"a" * 32)
    root = tmp_path / "owner-domain"
    ensure_owner_material(root, identity, now=NOW)
    before = _snapshot(root)

    with pytest.raises(OwnerDomainAssetError, match="another Owner's Authority"):
        ensure_owner_domain_assets(
            root, identity, 8443, _established("owner-" + "b" * 20, generation=3), now=NOW
        )

    assert _snapshot(root) == before


@pytest.mark.parametrize(
    "lineage",
    [
        {"contract_version": 2, "owner_domain_id": "owner-x", "owner_domain_generation": 1, "state_id": "authority-state_x"},
        {"contract_version": 1, "owner_domain_id": "x", "owner_domain_generation": 1, "state_id": "authority-state_x"},
        {"contract_version": 1, "owner_domain_id": "owner-x", "owner_domain_generation": 0, "state_id": "authority-state_x"},
        {"contract_version": 1, "owner_domain_id": "owner-x", "owner_domain_generation": "1", "state_id": "authority-state_x"},
        {"contract_version": 1, "owner_domain_id": "owner-x", "owner_domain_generation": 1, "state_id": "x"},
        {"contract_version": 1, "owner_domain_id": "owner-x", "owner_domain_generation": 1},
    ],
)
def test_a_lineage_the_host_did_not_shape_is_refused(lineage) -> None:
    with pytest.raises(OwnerDomainAssetError, match="lineage is invalid"):
        HostAuthority.established_from(lineage)


def test_host_tls_leaf_is_owner_root_signed_and_hostname_scoped(tmp_path: Path) -> None:
    identity = derive_host_lan_identity(b"a" * 32)
    assets = ensure_owner_domain_assets(tmp_path / "owner-domain", identity, 8443, FRESH, now=NOW)
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
    first = ensure_owner_domain_assets(tmp_path / "owner-domain", identity, 8443, FRESH, now=NOW)
    second = ensure_owner_domain_assets(
        tmp_path / "owner-domain", identity, 9443, FRESH, now=NOW + timedelta(minutes=1)
    )
    first_directory = json.loads(first.descriptor)
    second_directory = json.loads(second.descriptor)

    assert second.owner_domain_id == first.owner_domain_id
    assert second.owner_root_certificate == first.owner_root_certificate
    assert second.authority_signing_certificate == first.authority_signing_certificate
    assert second_directory["directory_revision"] == first_directory["directory_revision"] + 1
    assert second_directory["owner_domain_generation"] == first_directory["owner_domain_generation"]
    assert all(":9443/" in endpoint["uri"] for endpoint in second_directory["endpoints"])
    assert "host_id" not in second_directory


def test_directory_advertises_the_canonical_admission_authority_route(tmp_path: Path) -> None:
    identity = derive_host_lan_identity(b"a" * 32)
    assets = ensure_owner_domain_assets(tmp_path / "owner-domain", identity, 8443, FRESH, now=NOW)
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
    assets = ensure_owner_domain_assets(tmp_path / "owner-domain", identity, 8443, FRESH, now=NOW)
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
    first = ensure_owner_domain_assets(root, identity, 8443, FRESH, now=NOW)
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
            root, identity, 8443, FRESH, now=NOW + timedelta(minutes=1)
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
    ensure_owner_domain_assets(root, identity, 8443, FRESH, now=NOW)

    stored = json.loads(path.read_text(encoding="utf-8"))
    superseded_revision = stored["directory_revision"]
    del stored["descriptor_uri"]
    path.write_text(json.dumps(stored), encoding="utf-8")

    reissued = json.loads(
        ensure_owner_domain_assets(
            root, identity, 8443, FRESH, now=NOW + timedelta(minutes=1)
        ).descriptor
    )
    assert reissued["descriptor_uri"] == (
        identity.hub_origin(8443) + "/api/device-onboarding/v1/descriptor"
    )
    assert reissued["directory_revision"] == superseded_revision + 1


def test_existing_descriptor_corruption_fails_closed(tmp_path: Path) -> None:
    identity = derive_host_lan_identity(b"a" * 32)
    root = tmp_path / "owner-domain"
    ensure_owner_domain_assets(root, identity, 8443, FRESH, now=NOW)
    (root / "owner-domain-descriptor.json").write_text("{}", encoding="utf-8")

    # No Owner Domain named and no revision to continue: reissuing over this
    # would either seize another Owner's directory or restart a revision line
    # devices have already been told about.
    with pytest.raises(OwnerDomainAssetError, match="another Owner Domain"):
        ensure_owner_domain_assets(root, identity, 8443, FRESH, now=NOW)

    (root / "owner-domain-descriptor.json").write_text("not json", encoding="utf-8")
    with pytest.raises(OwnerDomainAssetError, match="unreadable"):
        ensure_owner_domain_assets(root, identity, 8443, FRESH, now=NOW)


def test_existing_host_tls_corruption_fails_closed(tmp_path: Path) -> None:
    identity = derive_host_lan_identity(b"a" * 32)
    root = tmp_path / "owner-domain"
    ensure_owner_domain_assets(root, identity, 8443, FRESH, now=NOW)
    (root / "hub.crt").write_bytes(b"not a certificate")

    with pytest.raises(OwnerDomainAssetError, match="certificate is invalid"):
        ensure_owner_domain_assets(root, identity, 8443, FRESH, now=NOW)


@pytest.mark.parametrize("missing", [
    ("owner-domain-root.key.pem",),
    ("owner-domain-root.key.pem", "owner-domain-root-ca.pem"),
])
def test_partial_owner_identity_is_not_reissued(tmp_path: Path, missing) -> None:
    material = tmp_path / "owner-domain"
    identity = derive_host_lan_identity(b"a" * 32)
    ensure_owner_domain_assets(material, identity, 8443, FRESH, now=NOW)
    for name in missing:
        (material / name).unlink()
    before = {p.name: p.read_bytes() for p in material.iterdir()}
    with pytest.raises(OwnerDomainAssetError, match=r"AUTHORITY_RECOVERY_REQUIRED|incomplete"):
        ensure_owner_domain_assets(material, identity, 8443, FRESH, now=NOW)
    assert {p.name: p.read_bytes() for p in material.iterdir()} == before


# -- the directory a Host serves is the baseline of this material's revision line --


def _board_serving(tmp_path: Path, *, generation: int = 8):
    """Material that has stood a Host up at ``generation``, and the directory it serves."""

    identity = derive_host_lan_identity(b"a" * 32)
    material = tmp_path / "owner-domain"
    fresh = ensure_owner_domain_assets(material, identity, 8443, FRESH, now=NOW)
    board = _established(fresh.owner_domain_id, generation=generation)
    served = ensure_owner_domain_assets(material, identity, 8443, board, now=NOW)
    return identity, material, board, served.descriptor


def test_adopting_what_this_material_already_holds_writes_nothing(tmp_path: Path) -> None:
    identity, material, board, served = _board_serving(tmp_path)
    before = _snapshot(material)

    again = ensure_owner_domain_assets(
        material, identity, 8443, board, served_directory=served, now=NOW
    )

    assert again.descriptor == served
    assert _snapshot(material) == before


def test_a_hosts_directory_replaces_a_stale_one_and_moves_no_key(tmp_path: Path) -> None:
    """What a second board's install left behind, taken back from the Host.

    This material was moved to a generation the board on the bench never had,
    and holds a directory at it. The board serves the directory devices hold,
    signed by this Owner root. It is adopted byte for byte; every key stays.
    """

    identity, material, board, served = _board_serving(tmp_path, generation=8)
    keys = {
        name: (material / name).read_bytes()
        for name in ("owner-domain-root.key.pem", "authority-signing.key.pem", "hub.key", "hub.crt")
    }
    other_board = _established(board.owner_domain_id or "", generation=9, state_id="authority-state_the-other-board")
    moved = ensure_owner_domain_assets(material, identity, 8443, other_board, now=NOW)
    assert json.loads(moved.descriptor)["owner_domain_generation"] == 9

    settled = ensure_owner_domain_assets(
        material, identity, 8443, board, served_directory=served, now=NOW
    )

    assert settled.descriptor == served
    assert settled.owner_domain_generation == 8
    assert (material / "owner-domain-descriptor.json").read_bytes() == served
    assert {name: (material / name).read_bytes() for name in keys} == keys
    # And the ordinary issuer now leaves it alone.
    assert ensure_owner_domain_assets(material, identity, 8443, board, now=NOW).descriptor == served


def test_an_undelivered_newer_revision_is_not_walked_back(tmp_path: Path) -> None:
    identity, material, board, served = _board_serving(tmp_path)
    # The endpoint moved, so this side issued the next revision of the same
    # lineage and has not delivered it yet. The Host still serves the one above.
    issued = ensure_owner_domain_assets(material, identity, 9443, board, now=NOW)
    assert json.loads(issued.descriptor)["directory_revision"] == json.loads(served)["directory_revision"] + 1

    kept = ensure_owner_domain_assets(
        material, identity, 9443, board, served_directory=served, now=NOW
    )

    assert kept.descriptor == issued.descriptor
    assert (material / "owner-domain-descriptor.json").read_bytes() == issued.descriptor


def test_a_directory_another_owner_signed_is_refused(tmp_path: Path) -> None:
    identity = derive_host_lan_identity(b"a" * 32)
    mine = ensure_owner_domain_assets(tmp_path / "mine", identity, 8443, FRESH, now=NOW)
    stranger = ensure_owner_domain_assets(tmp_path / "stranger", identity, 8443, FRESH, now=NOW)
    board = _established(mine.owner_domain_id, generation=1)
    before = _snapshot(tmp_path / "mine")

    with pytest.raises(OwnerDomainAssetError, match="another Owner Domain"):
        ensure_owner_domain_assets(
            tmp_path / "mine", identity, 8443, board, served_directory=stranger.descriptor, now=NOW
        )

    assert mine.owner_domain_id != stranger.owner_domain_id
    assert _snapshot(tmp_path / "mine") == before


def test_a_forged_directory_is_refused(tmp_path: Path) -> None:
    identity, material, board, served = _board_serving(tmp_path)
    forged = json.loads(served)
    forged["issued_at"] = "2020-01-01T00:00:00Z"
    before = _snapshot(material)

    with pytest.raises(OwnerDomainAssetError, match="not signed by this Owner root"):
        ensure_owner_domain_assets(
            material, identity, 8443, board, served_directory=json.dumps(forged).encode(), now=NOW
        )
    with pytest.raises(OwnerDomainAssetError, match="not a readable document"):
        ensure_owner_domain_assets(
            material, identity, 8443, board, served_directory=b"not json", now=NOW
        )

    assert _snapshot(material) == before


def test_a_directory_at_a_generation_the_host_did_not_establish_is_its_own_incident(
    tmp_path: Path,
) -> None:
    identity, material, board, served = _board_serving(tmp_path, generation=8)
    elsewhere = _established(board.owner_domain_id or "", generation=9)

    with pytest.raises(OwnerDomainAssetError, match="its own copies disagree"):
        ensure_owner_domain_assets(
            material, identity, 8443, elsewhere, served_directory=served, now=NOW
        )


def test_an_expired_directory_a_host_still_serves_is_adopted_and_reissued(tmp_path: Path) -> None:
    identity, material, board, served = _board_serving(tmp_path)
    later = NOW + timedelta(days=400)

    renewed = ensure_owner_domain_assets(
        material, identity, 8443, board, served_directory=served, now=later
    )

    document = json.loads(renewed.descriptor)
    assert document["owner_domain_generation"] == 8
    assert document["directory_revision"] == json.loads(served)["directory_revision"] + 1
    # Adoption checked the signature, not the window: the Host's expired
    # document was still this Owner's, and expired means reissue.
    assert verify_served_directory(material, served).owner_domain_generation == 8


def test_verifying_a_served_directory_reads_the_material_and_writes_nothing(tmp_path: Path) -> None:
    identity, material, board, served = _board_serving(tmp_path)
    stranger = ensure_owner_domain_assets(tmp_path / "stranger", identity, 8443, FRESH, now=NOW)
    before = _snapshot(material)

    assert verify_served_directory(material, served).owner_domain_id == board.owner_domain_id
    with pytest.raises(OwnerDomainAssetError, match="another Owner Domain"):
        verify_served_directory(material, stranger.descriptor)
    with pytest.raises(OwnerDomainAssetError, match="not signed by this Owner root"):
        verify_served_directory(material, served.replace(b'"directory_revision": 1', b'"directory_revision": 2'))

    assert _snapshot(material) == before


# -- the record this side used to keep ------------------------------------------


def test_the_legacy_authority_record_is_tolerated_and_retired(tmp_path: Path) -> None:
    identity = derive_host_lan_identity(b"a" * 32)
    material = tmp_path / "owner-domain"
    first = ensure_owner_domain_assets(material, identity, 8443, FRESH, now=NOW)
    legacy = material / LEGACY_AUTHORITY_STATE
    legacy.write_text('{"owner_domain_generation": 9, "bootstrap_pending": false}\n', encoding="utf-8")
    legacy.chmod(0o600)

    # Not an extra file, and not read: the bundle is what the Host says it is.
    assert ensure_owner_domain_assets(material, identity, 8443, FRESH, now=NOW) == first
    assert owner_domain_id_of(material) == first.owner_domain_id

    assert retire_legacy_authority_state(material) is True
    assert not legacy.exists()
    assert retire_legacy_authority_state(material) is False
    assert ensure_owner_domain_assets(material, identity, 8443, FRESH, now=NOW) == first


def test_a_material_directory_with_an_unknown_file_is_refused(tmp_path: Path) -> None:
    identity = derive_host_lan_identity(b"a" * 32)
    material = tmp_path / "owner-domain"
    ensure_owner_domain_assets(material, identity, 8443, FRESH, now=NOW)
    (material / "notes.txt").write_text("x", encoding="utf-8")

    with pytest.raises(OwnerDomainAssetError, match="extra files"):
        ensure_owner_domain_assets(material, identity, 8443, FRESH, now=NOW)
