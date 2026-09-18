"""Offline Owner Domain issuer and public Host delivery bundle.

The Owner root and delegated directory signer live only in the controller-side
private material directory.  A Host receives the signed directory, public
certificates, and its own TLS leaf/key.  A complete Host restore preserves this
material.  An explicit new Host receives a new Owner root; ordinary endpoint
changes only revise its signed directory.

What this side does *not* keep is which Authority a Host has established.
That fact is born on the Host — Hub writes its marker when it consumes a
bootstrap capability — and the Host is its only ledger.  Every operation that
needs the generation or the state id observes the Host first and renders from
what it finds (:class:`HostAuthority`).  This module used to hold a second
copy in ``owner-domain-state.json``; the copy could disagree with the Host,
did on 2026-09-10 when one Owner's material commissioned a second board, and
then had no honest way back.  Nothing here can produce a generation the Host
did not establish: a Host holding nothing is rendered at 1, and a Host holding
something is rendered at exactly that.
"""

from __future__ import annotations

import json
import secrets
import stat
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from cryptography import x509
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID
from eidolon_sdk.device_foundation.v1 import (
    AuthorityLocator,
    AuthorityLocatorError,
    OwnerDomainDescriptor,
    OwnerDomainTrustAnchor,
    descriptor_key_id,
    verify_descriptor,
)
from eidolon_sdk.device_foundation.v1.directory_tool import issue_descriptor

from eidolon_ops.host_identity import HostLanIdentity
from eidolon_ops.private_inputs import ensure_private_parent, write_private_file


class OwnerDomainAssetError(ValueError):
    """The offline trust material or issued public bundle is unsafe."""


_ROOT_KEY = "owner-domain-root.key.pem"
_ROOT_CERTIFICATE = "owner-domain-root-ca.pem"
_SIGNER_KEY = "authority-signing.key.pem"
_SIGNER_CERTIFICATE = "authority-signing-certificate.pem"
_DESCRIPTOR = "owner-domain-descriptor.json"
_TLS_CERTIFICATE = "hub.crt"
_TLS_KEY = "hub.key"
_MATERIAL_NAMES = {
    _ROOT_KEY,
    _ROOT_CERTIFICATE,
    _SIGNER_KEY,
    _SIGNER_CERTIFICATE,
    _DESCRIPTOR,
    _TLS_CERTIFICATE,
    _TLS_KEY,
}
OWNER_DOMAIN_MATERIAL_NAMES = frozenset(_MATERIAL_NAMES)
#: The controller-side Authority record this module used to keep. Inert now:
#: it named a generation and a state id the Host is the only ledger for. It is
#: tolerated in a material directory so an older backup still restores, and
#: :func:`retire_legacy_authority_state` removes it, because a file that looks
#: authoritative and is not is where the next confident wrong answer comes from.
LEGACY_AUTHORITY_STATE = "owner-domain-state.json"


@dataclass(frozen=True, slots=True)
class HostAuthority:
    """What one Host has established, or that it has established nothing yet.

    Built from the Host's own report — its database marker and lineage anchor,
    agreeing — or minted fresh for a Host that holds nothing.  Fresh means
    generation 1 and a new state id: the only generation this side will ever
    put a name to, because advancing one was removed with the epochs it served,
    and every higher value in the field is history a Host already holds.
    """

    owner_domain_generation: int
    state_id: str
    established: bool
    owner_domain_id: str | None = None

    @classmethod
    def fresh(cls) -> HostAuthority:
        return cls(
            owner_domain_generation=1,
            state_id="authority-state_" + secrets.token_urlsafe(24),
            established=False,
        )

    @classmethod
    def established_from(cls, lineage: Mapping[str, object]) -> HostAuthority:
        if (
            not isinstance(lineage, Mapping)
            or set(lineage) != {"contract_version", "owner_domain_id", "owner_domain_generation", "state_id"}
            or lineage.get("contract_version") != 1
            or not isinstance(lineage.get("owner_domain_id"), str)
            or not str(lineage["owner_domain_id"]).startswith("owner-")
            or type(lineage.get("owner_domain_generation")) is not int
            or int(lineage["owner_domain_generation"]) < 1
            or not isinstance(lineage.get("state_id"), str)
            or not str(lineage["state_id"]).startswith("authority-state_")
        ):
            raise OwnerDomainAssetError("Host Authority lineage is invalid")
        return cls(
            owner_domain_generation=int(lineage["owner_domain_generation"]),
            state_id=str(lineage["state_id"]),
            established=True,
            owner_domain_id=str(lineage["owner_domain_id"]),
        )

    def lineage(self, owner_domain_id: str) -> dict[str, object]:
        """The marker a Hub writes, or has written, for this Authority."""

        if self.established and self.owner_domain_id != owner_domain_id:
            raise OwnerDomainAssetError(
                "this Host has established another Owner's Authority; "
                "this material cannot speak for it"
            )
        return {
            "contract_version": 1,
            "owner_domain_id": owner_domain_id,
            "owner_domain_generation": self.owner_domain_generation,
            "state_id": self.state_id,
        }


@dataclass(frozen=True, slots=True)
class OwnerDomainAssets:
    owner_domain_id: str
    owner_domain_generation: int
    authority_state_id: str
    bootstrap_pending: bool
    authority_bootstrap: bytes
    descriptor: bytes
    owner_root_certificate: bytes
    authority_signing_certificate: bytes
    tls_certificate: bytes
    tls_private_key: bytes


def authority_lineage(assets: OwnerDomainAssets) -> dict[str, object]:
    """The Authority marker a Hub writes when it accepts this capability."""

    return {
        "contract_version": 1,
        "owner_domain_id": assets.owner_domain_id,
        "owner_domain_generation": assets.owner_domain_generation,
        "state_id": assets.authority_state_id,
    }


def ensure_owner_material(
    material_root: Path, identity: HostLanIdentity, *, now: datetime | None = None
) -> str:
    """Create the Owner root, directory signer and Host TLS leaf if absent.

    The keys only.  No directory is issued and no capability rendered, because
    both name a generation, and a generation is something a Host establishes —
    there is none to name until a Host is being addressed.  Returns the Owner
    Domain id, which is a function of the root and nothing else.
    """

    instant = (now or datetime.now(UTC)).astimezone(UTC)
    _require_private_material_root(material_root)
    _require_complete_or_empty(material_root)
    root_key, root_certificate = _owner_root(material_root, instant)
    _directory_signer(material_root, root_key, root_certificate, instant)
    _host_tls(material_root, identity, root_key, root_certificate, instant)
    return _owner_domain_id(root_certificate)


def owner_domain_id_of(material_root: Path) -> str:
    """The Owner Domain this material speaks for, read from its root and nothing else."""

    _require_private_material_root(material_root)
    path = material_root / _ROOT_CERTIFICATE
    if not path.is_file():
        raise OwnerDomainAssetError("Owner Domain material has no root; initialize inputs first")
    return _owner_domain_id(_certificate(path))


def ensure_owner_domain_assets(
    material_root: Path,
    identity: HostLanIdentity,
    port: int,
    host_authority: HostAuthority,
    *,
    served_directory: bytes | None = None,
    now: datetime | None = None,
) -> OwnerDomainAssets:
    """Issue or reuse one Owner Domain bundle for the Authority a Host holds.

    ``host_authority`` is what the Host reported, or a fresh one for a Host that
    holds nothing.  ``served_directory`` is the signed directory the Host
    serves, when it serves one: it is adopted byte for byte as the baseline of
    this material's revision line, so every device already holding it goes on
    holding exactly it, and only a real endpoint change issues a revision after
    it.  Signing keys never leave ``material_root``.
    """

    if not 1 <= port <= 65535:
        raise OwnerDomainAssetError("Owner Domain endpoint port is invalid")
    instant = (now or datetime.now(UTC)).astimezone(UTC)
    _require_private_material_root(material_root)
    _require_complete_or_empty(material_root)
    root_key, root_certificate = _owner_root(material_root, instant)
    owner_domain_id = _owner_domain_id(root_certificate)
    lineage = host_authority.lineage(owner_domain_id)
    signer_key, signer_certificate = _directory_signer(
        material_root, root_key, root_certificate, instant
    )
    tls_certificate, tls_private_key = _host_tls(
        material_root, identity, root_key, root_certificate, instant
    )
    descriptor = _directory(
        material_root,
        identity,
        port,
        owner_domain_id,
        host_authority.owner_domain_generation,
        root_certificate,
        signer_certificate,
        signer_key,
        instant,
        served=served_directory,
    )
    return OwnerDomainAssets(
        owner_domain_id=owner_domain_id,
        owner_domain_generation=host_authority.owner_domain_generation,
        authority_state_id=host_authority.state_id,
        bootstrap_pending=not host_authority.established,
        authority_bootstrap=_bootstrap_document(lineage, established=host_authority.established),
        descriptor=descriptor,
        owner_root_certificate=root_certificate.public_bytes(serialization.Encoding.PEM),
        authority_signing_certificate=signer_certificate.public_bytes(
            serialization.Encoding.PEM
        ),
        tls_certificate=tls_certificate,
        tls_private_key=tls_private_key,
    )


def verify_served_directory(material_root: Path, directory: bytes) -> OwnerDomainDescriptor:
    """The directory a Host serves, accepted only if this material's Owner root signed it.

    Signature and delegation only — not the validity window, because an expired
    directory a Host still serves is still this Owner's, and the answer to
    expired is a reissue, not a refusal.  Reads the material; writes nothing.
    """

    _require_private_material_root(material_root)
    root_certificate = _certificate(material_root / _ROOT_CERTIFICATE)
    signer_certificate = _certificate(material_root / _SIGNER_CERTIFICATE)
    descriptor = _parse_directory(directory, label="the directory this Host serves")
    if descriptor.owner_domain_id != _owner_domain_id(root_certificate):
        raise OwnerDomainAssetError(
            "the directory this Host serves names another Owner Domain"
        )
    _require_signed_by_this_owner(descriptor, root_certificate, signer_certificate)
    return descriptor


def retire_legacy_authority_state(material_root: Path) -> bool:
    """Remove the controller-side Authority record, if this material still has one.

    Nothing reads it any more.  Returns whether there was one to remove, so the
    operation that did it can say so.
    """

    path = material_root / LEGACY_AUTHORITY_STATE
    if path.is_symlink() or not path.is_file():
        return False
    path.unlink()
    return True


def _require_private_material_root(root: Path) -> None:
    if root.exists():
        if root.is_symlink() or not root.is_dir() or stat.S_IMODE(root.stat().st_mode) != 0o700:
            raise OwnerDomainAssetError("Owner Domain material directory is unsafe")
        extra = {path.name for path in root.iterdir()} - _MATERIAL_NAMES - {LEGACY_AUTHORITY_STATE}
        if extra:
            raise OwnerDomainAssetError("Owner Domain material directory has extra files")
        for path in root.iterdir():
            if path.is_symlink() or not path.is_file() or stat.S_IMODE(path.stat().st_mode) != 0o600:
                raise OwnerDomainAssetError(f"Owner Domain material is unsafe: {path.name}")
        return
    ensure_private_parent(root.parent)
    root.mkdir(mode=0o700)


def _require_complete_or_empty(root: Path) -> None:
    present = {path.name for path in root.iterdir()} - {LEGACY_AUTHORITY_STATE}
    if present and not all(
        (root / name).is_file() for name in (_ROOT_KEY, _ROOT_CERTIFICATE)
    ):
        raise OwnerDomainAssetError(
            "AUTHORITY_RECOVERY_REQUIRED: existing Owner identity is incomplete; restore its backup"
        )


def _bootstrap_document(lineage: Mapping[str, object], *, established: bool) -> bytes:
    """The one-shot capability an empty Hub consumes, or the tombstone of one it did.

    Hub takes the pending form only into an empty database and deletes it on
    use.  A Host that has established its Authority is sent the consumed form
    naming exactly what it established, so the file on the Host agrees with
    the Host rather than with anything this side remembers.
    """

    value = {
        "contract_version": 1,
        "operation": (
            "owner-authority.bootstrap-consumed"
            if established
            else "owner-authority.bootstrap"
        ),
        "owner_domain_id": lineage["owner_domain_id"],
        "owner_domain_generation": lineage["owner_domain_generation"],
        "state_id": lineage["state_id"],
    }
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()


def _owner_root(root: Path, now: datetime) -> tuple[ec.EllipticCurvePrivateKey, x509.Certificate]:
    key_path = root / _ROOT_KEY
    certificate_path = root / _ROOT_CERTIFICATE
    present = key_path.is_file(), certificate_path.is_file()
    if any(present) and not all(present):
        raise OwnerDomainAssetError("Owner root material is incomplete")
    if all(present):
        key = _private_key(key_path)
        certificate = _certificate(certificate_path)
        _require_key_pair(key, certificate, "Owner root")
        constraints = certificate.extensions.get_extension_for_class(x509.BasicConstraints).value
        if not constraints.ca:
            raise OwnerDomainAssetError("Owner root certificate is not a CA")
        return key, certificate
    key = ec.generate_private_key(ec.SECP256R1())
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Eidolon Owner Domain Root")])
    certificate = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=5))
        .not_valid_after(now + timedelta(days=3650))
        .add_extension(x509.BasicConstraints(ca=True, path_length=1), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=True,
                crl_sign=True,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .sign(key, hashes.SHA256())
    )
    _write_key(key_path, key)
    write_private_file(certificate_path, certificate.public_bytes(serialization.Encoding.PEM))
    return key, certificate


def _directory_signer(
    root: Path,
    root_key: ec.EllipticCurvePrivateKey,
    root_certificate: x509.Certificate,
    now: datetime,
) -> tuple[ec.EllipticCurvePrivateKey, x509.Certificate]:
    key_path = root / _SIGNER_KEY
    certificate_path = root / _SIGNER_CERTIFICATE
    present = key_path.is_file(), certificate_path.is_file()
    if any(present) and not all(present):
        raise OwnerDomainAssetError("authority signer material is incomplete")
    if all(present):
        key = _private_key(key_path)
        certificate = _certificate(certificate_path)
        _require_key_pair(key, certificate, "authority signer")
        return key, certificate
    key = ec.generate_private_key(ec.SECP256R1())
    certificate = (
        x509.CertificateBuilder()
        .subject_name(
            x509.Name(
                [x509.NameAttribute(NameOID.COMMON_NAME, "Eidolon Owner Directory Signer")]
            )
        )
        .issuer_name(root_certificate.subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=5))
        .not_valid_after(now + timedelta(days=3650))
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.CODE_SIGNING]), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=False,
                crl_sign=False,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .sign(root_key, hashes.SHA256())
    )
    _write_key(key_path, key)
    write_private_file(certificate_path, certificate.public_bytes(serialization.Encoding.PEM))
    return key, certificate


def _host_tls(
    root: Path,
    identity: HostLanIdentity,
    root_key: ec.EllipticCurvePrivateKey,
    root_certificate: x509.Certificate,
    now: datetime,
) -> tuple[bytes, bytes]:
    certificate_path = root / _TLS_CERTIFICATE
    key_path = root / _TLS_KEY
    present = certificate_path.is_file(), key_path.is_file()
    if any(present) and not all(present):
        raise OwnerDomainAssetError("Host TLS material is incomplete")
    if all(present):
        certificate = _certificate(certificate_path)
        key = _private_key(key_path)
        try:
            sans = certificate.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
            constraints = certificate.extensions.get_extension_for_class(x509.BasicConstraints).value
            usage = certificate.extensions.get_extension_for_class(x509.ExtendedKeyUsage).value
            _require_key_pair(key, certificate, "Host TLS")
            issuer = root_certificate.public_key()
            if not isinstance(issuer, ec.EllipticCurvePublicKey):
                raise OwnerDomainAssetError("Owner root public key must use P-256")
            issuer.verify(
                certificate.signature,
                certificate.tbs_certificate_bytes,
                ec.ECDSA(certificate.signature_hash_algorithm),
            )
        except (InvalidSignature, ValueError, x509.ExtensionNotFound) as exc:
            raise OwnerDomainAssetError("existing Host TLS material is invalid") from exc
        if constraints.ca or ExtendedKeyUsageOID.SERVER_AUTH not in usage:
            raise OwnerDomainAssetError("existing Host TLS certificate usage is invalid")
        names = sans.get_values_for_type(x509.DNSName)
        if names == [identity.hub_hostname]:
            if now < certificate.not_valid_before_utc:
                raise OwnerDomainAssetError("existing Host TLS certificate is not yet valid")
            if now < certificate.not_valid_after_utc:
                return certificate_path.read_bytes(), key_path.read_bytes()
        elif len(names) != 1:
            raise OwnerDomainAssetError("existing Host TLS certificate SAN is invalid")
        # A single different Host routing name denotes a Host replacement; an
        # expired leaf denotes ordinary renewal. Both keep the Owner root and
        # logical identity stable while issuing a new transport leaf below it.
    key = ec.generate_private_key(ec.SECP256R1())
    certificate = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, identity.hub_hostname)]))
        .issuer_name(root_certificate.subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=5))
        .not_valid_after(now + timedelta(days=825))
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(x509.SubjectAlternativeName([x509.DNSName(identity.hub_hostname)]), False)
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=True)
        .sign(root_key, hashes.SHA256())
    )
    certificate_pem = certificate.public_bytes(serialization.Encoding.PEM)
    key_pem = _key_bytes(key)
    write_private_file(certificate_path, certificate_pem)
    write_private_file(key_path, key_pem)
    return certificate_pem, key_pem


def _directory(
    root: Path,
    identity: HostLanIdentity,
    port: int,
    owner_domain_id: str,
    owner_domain_generation: int,
    root_certificate: x509.Certificate,
    signer_certificate: x509.Certificate,
    signer_key: ec.EllipticCurvePrivateKey,
    now: datetime,
    *,
    served: bytes | None = None,
) -> bytes:
    path = root / _DESCRIPTOR
    if served is not None:
        _adopt_served_directory(
            path,
            served,
            owner_domain_id,
            owner_domain_generation,
            root_certificate,
            signer_certificate,
        )
    origin = identity.hub_origin(port)
    # This Host serves the document at its own onboarding route, and it is the
    # only party that knows that route. Stating it inside the signed document is
    # what stops a consumer from inventing a path convention: firmware used to
    # derive one from the Admission base address, which no Host answers, so
    # commissioning rolled back at its last step with a bare 404 nobody logged.
    descriptor_uri = origin + "/api/device-onboarding/v1/descriptor"
    endpoints = [
        {
            "authority": "admission",
            "logical_audience": f"{owner_domain_id}:admission",
            "uri": origin + "/api/admission/v1",
            "transport_profile": "https-json",
            "priority": 0,
        },
        {
            "authority": "device-control",
            "logical_audience": f"{owner_domain_id}:device-control",
            "uri": origin + "/api/device-control/v1",
            "transport_profile": "https-json",
            "priority": 0,
        },
    ]
    revision = 1
    if path.is_file():
        try:
            current = OwnerDomainDescriptor.model_validate_json(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            # A document this Host wrote before the contract grew a field is not
            # invalid — it is older, and the answer to older is to reissue it at
            # the next revision. Refusing instead stops every deploy on a Host
            # that has ever been provisioned, which is every Host in the field:
            # adding `descriptor_uri` did exactly that until this read said so.
            #
            # What still cannot be salvaged is a document that does not name this
            # Owner Domain, or that has no revision to continue from. Reissuing
            # over either of those would either seize another Owner's directory
            # or restart a revision line devices have already seen.
            try:
                superseded = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError) as unreadable:
                raise OwnerDomainAssetError(
                    "existing Owner Domain descriptor is unreadable"
                ) from unreadable
            if (
                not isinstance(superseded, dict)
                or superseded.get("owner_domain_id") != owner_domain_id
                or not isinstance(superseded.get("directory_revision"), int)
                or superseded["directory_revision"] < 1
            ):
                raise OwnerDomainAssetError(
                    "existing Owner Domain descriptor names another Owner Domain "
                    "or has no revision to continue"
                ) from exc
            revision = superseded["directory_revision"] + 1
            current = None
        if current is not None:
            expected = [item.model_dump(mode="json") for item in current.endpoints]
            if (
                current.owner_domain_id == owner_domain_id
                and current.owner_domain_generation == owner_domain_generation
                and expected == endpoints
                and current.descriptor_uri == descriptor_uri
                and current.issued_at <= now < current.expires_at
            ):
                _validate_directory(current, root_certificate, signer_certificate, now)
                return path.read_bytes()
            if current.owner_domain_generation == owner_domain_generation:
                revision = current.directory_revision + 1
    source = {
        "owner_domain_id": owner_domain_id,
        "owner_domain_generation": owner_domain_generation,
        "trust_epoch": 1,
        "directory_revision": revision,
        "descriptor_uri": descriptor_uri,
        "endpoints": endpoints,
        "issued_at": now.isoformat().replace("+00:00", "Z"),
        "expires_at": (now + timedelta(days=365)).isoformat().replace("+00:00", "Z"),
    }
    descriptor = issue_descriptor(
        source,
        owner_root_certificate_pem=root_certificate.public_bytes(serialization.Encoding.PEM).decode(),
        authority_signing_certificate_pem=signer_certificate.public_bytes(
            serialization.Encoding.PEM
        ).decode(),
        authority_private_key_pem=_key_bytes(signer_key),
    )
    _validate_directory(descriptor, root_certificate, signer_certificate, now)
    encoded = (json.dumps(descriptor.model_dump(mode="json"), indent=2) + "\n").encode()
    write_private_file(path, encoded)
    return encoded


def _adopt_served_directory(
    path: Path,
    served: bytes,
    owner_domain_id: str,
    owner_domain_generation: int,
    root_certificate: x509.Certificate,
    signer_certificate: x509.Certificate,
) -> None:
    """Make the directory the Host serves the baseline of this material's revision line.

    Adopted rather than reissued, and byte for byte: every device that cached
    this document keeps holding exactly it, and no revision line restarts.  It
    is believed only because this material's own Owner root signed it — which
    is also what stops a Host from naming a generation this Owner never issued.

    One thing is never walked back: a newer revision at the same generation
    that this side issued and has not yet delivered.  Between issuing it and
    the deploy that carries it, the Host still serves the older one, and taking
    that would silently undo the change the operator just made.
    """

    descriptor = _parse_directory(served, label="the directory this Host serves")
    if descriptor.owner_domain_id != owner_domain_id:
        raise OwnerDomainAssetError(
            "the directory this Host serves names another Owner Domain; this material cannot adopt it"
        )
    _require_signed_by_this_owner(descriptor, root_certificate, signer_certificate)
    if descriptor.owner_domain_generation != owner_domain_generation:
        raise OwnerDomainAssetError(
            "AUTHORITY_RECOVERY_REQUIRED: this Host serves a directory at generation "
            f"{descriptor.owner_domain_generation} but has established generation "
            f"{owner_domain_generation}; its own copies disagree"
        )
    if path.is_file():
        if path.read_bytes() == served:
            return
        try:
            current = OwnerDomainDescriptor.model_validate_json(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            current = None
        if (
            current is not None
            and current.owner_domain_id == owner_domain_id
            and current.owner_domain_generation == owner_domain_generation
            and current.directory_revision > descriptor.directory_revision
        ):
            return
    write_private_file(path, served)


def _parse_directory(raw: bytes, *, label: str) -> OwnerDomainDescriptor:
    try:
        return OwnerDomainDescriptor.model_validate_json(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise OwnerDomainAssetError(f"{label} is not a readable document") from exc


def _trust_anchor(
    descriptor: OwnerDomainDescriptor,
    root_certificate: x509.Certificate,
    signer_certificate: x509.Certificate,
) -> OwnerDomainTrustAnchor:
    return OwnerDomainTrustAnchor(
        owner_domain_id=descriptor.owner_domain_id,
        owner_root_certificate_pem=root_certificate.public_bytes(serialization.Encoding.PEM).decode(),
        authority_signing_certificate_pem=signer_certificate.public_bytes(
            serialization.Encoding.PEM
        ).decode(),
        trust_epoch=1,
    )


def _require_signed_by_this_owner(
    descriptor: OwnerDomainDescriptor,
    root_certificate: x509.Certificate,
    signer_certificate: x509.Certificate,
) -> None:
    try:
        verify_descriptor(descriptor, _trust_anchor(descriptor, root_certificate, signer_certificate))
    except (AuthorityLocatorError, ValueError) as exc:
        raise OwnerDomainAssetError(
            f"the directory this Host serves was not signed by this Owner root: {exc}"
        ) from exc


def _validate_directory(
    descriptor: OwnerDomainDescriptor,
    root_certificate: x509.Certificate,
    signer_certificate: x509.Certificate,
    now: datetime,
) -> None:
    AuthorityLocator(_trust_anchor(descriptor, root_certificate, signer_certificate)).accept(
        descriptor, now=now
    )


def _owner_domain_id(certificate: x509.Certificate) -> str:
    pem = certificate.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    ).decode()
    return "owner-" + descriptor_key_id(pem)[7:27]


def _certificate(path: Path) -> x509.Certificate:
    try:
        return x509.load_pem_x509_certificate(path.read_bytes())
    except (OSError, ValueError) as exc:
        raise OwnerDomainAssetError(f"certificate is invalid: {path.name}") from exc


def _private_key(path: Path) -> ec.EllipticCurvePrivateKey:
    try:
        key = serialization.load_pem_private_key(path.read_bytes(), password=None)
    except (OSError, TypeError, ValueError) as exc:
        raise OwnerDomainAssetError(f"private key is invalid: {path.name}") from exc
    if not isinstance(key, ec.EllipticCurvePrivateKey) or not isinstance(
        key.curve, ec.SECP256R1
    ):
        raise OwnerDomainAssetError(f"private key must use P-256: {path.name}")
    return key


def _require_key_pair(
    key: ec.EllipticCurvePrivateKey, certificate: x509.Certificate, label: str
) -> None:
    public = certificate.public_key()
    if not isinstance(public, ec.EllipticCurvePublicKey) or not isinstance(
        public.curve, ec.SECP256R1
    ):
        raise OwnerDomainAssetError(f"{label} certificate must use P-256")
    if public.public_numbers() != key.public_key().public_numbers():
        raise OwnerDomainAssetError(f"{label} certificate and private key do not match")


def _key_bytes(key: ec.EllipticCurvePrivateKey) -> bytes:
    return key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )


def _write_key(path: Path, key: ec.EllipticCurvePrivateKey) -> None:
    write_private_file(path, _key_bytes(key))
