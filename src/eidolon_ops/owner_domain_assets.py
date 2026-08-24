"""Offline Owner Domain issuer and public Host delivery bundle.

The Owner root and delegated directory signer live only in the controller-side
private material directory.  A Host receives the signed directory, public
certificates, and its own TLS leaf/key.  Host replacement therefore changes an
endpoint and directory revision, never the Owner trust anchor.
"""

from __future__ import annotations

import json
import secrets
import stat
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TypedDict, cast

from cryptography import x509
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID
from eidolon_sdk.device_foundation.v1 import (
    AuthorityLocator,
    OwnerDomainDescriptor,
    OwnerDomainTrustAnchor,
    descriptor_key_id,
)
from eidolon_sdk.device_foundation.v1.directory_tool import issue_descriptor

from eidolon_ops.host_identity import HostLanIdentity
from eidolon_ops.private_inputs import ensure_private_parent, write_private_file


class OwnerDomainAssetError(ValueError):
    """The offline trust material or issued public bundle is unsafe."""


class _AuthorityState(TypedDict):
    contract_version: int
    owner_domain_id: str
    owner_domain_generation: int
    authority_state_id: str
    bootstrap_pending: bool


_ROOT_KEY = "owner-domain-root.key.pem"
_ROOT_CERTIFICATE = "owner-domain-root-ca.pem"
_SIGNER_KEY = "authority-signing.key.pem"
_SIGNER_CERTIFICATE = "authority-signing-certificate.pem"
_DESCRIPTOR = "owner-domain-descriptor.json"
_TLS_CERTIFICATE = "hub.crt"
_TLS_KEY = "hub.key"
_STATE = "owner-domain-state.json"
_MATERIAL_NAMES = {
    _ROOT_KEY,
    _ROOT_CERTIFICATE,
    _SIGNER_KEY,
    _SIGNER_CERTIFICATE,
    _DESCRIPTOR,
    _TLS_CERTIFICATE,
    _TLS_KEY,
    _STATE,
}
OWNER_DOMAIN_MATERIAL_NAMES = frozenset(_MATERIAL_NAMES)


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


def ensure_owner_domain_assets(
    material_root: Path,
    identity: HostLanIdentity,
    port: int,
    *,
    now: datetime | None = None,
) -> OwnerDomainAssets:
    """Issue or reuse one Owner Domain bundle without exporting signing keys."""

    if not 1 <= port <= 65535:
        raise OwnerDomainAssetError("Owner Domain endpoint port is invalid")
    instant = (now or datetime.now(UTC)).astimezone(UTC)
    _require_private_material_root(material_root)
    root_key, root_certificate = _owner_root(material_root, instant)
    signer_key, signer_certificate = _directory_signer(
        material_root, root_key, root_certificate, instant
    )
    owner_domain_id = _owner_domain_id(root_certificate)
    authority_state = _authority_state(material_root, owner_domain_id)
    tls_certificate, tls_private_key = _host_tls(
        material_root, identity, root_key, root_certificate, instant
    )
    descriptor = _directory(
        material_root,
        identity,
        port,
        owner_domain_id,
        authority_state["owner_domain_generation"],
        root_certificate,
        signer_certificate,
        signer_key,
        instant,
    )
    return OwnerDomainAssets(
        owner_domain_id=owner_domain_id,
        owner_domain_generation=authority_state["owner_domain_generation"],
        authority_state_id=authority_state["authority_state_id"],
        bootstrap_pending=authority_state["bootstrap_pending"],
        authority_bootstrap=_bootstrap_document(authority_state),
        descriptor=descriptor,
        owner_root_certificate=root_certificate.public_bytes(serialization.Encoding.PEM),
        authority_signing_certificate=signer_certificate.public_bytes(
            serialization.Encoding.PEM
        ),
        tls_certificate=tls_certificate,
        tls_private_key=tls_private_key,
    )


def _require_private_material_root(root: Path) -> None:
    if root.exists():
        if root.is_symlink() or not root.is_dir() or stat.S_IMODE(root.stat().st_mode) != 0o700:
            raise OwnerDomainAssetError("Owner Domain material directory is unsafe")
        extra = {path.name for path in root.iterdir()} - _MATERIAL_NAMES
        if extra:
            raise OwnerDomainAssetError("Owner Domain material directory has extra files")
        for path in root.iterdir():
            if path.is_symlink() or not path.is_file() or stat.S_IMODE(path.stat().st_mode) != 0o600:
                raise OwnerDomainAssetError(f"Owner Domain material is unsafe: {path.name}")
        return
    ensure_private_parent(root.parent)
    root.mkdir(mode=0o700)


def _authority_state(root: Path, owner_domain_id: str) -> _AuthorityState:
    path = root / _STATE
    if path.is_file():
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise OwnerDomainAssetError("Owner Authority state is invalid") from exc
        expected_keys = {
            "contract_version",
            "owner_domain_id",
            "owner_domain_generation",
            "authority_state_id",
            "bootstrap_pending",
        }
        if (
            not isinstance(value, dict)
            or set(value) != expected_keys
            or value.get("contract_version") != 1
            or value.get("owner_domain_id") != owner_domain_id
            or not isinstance(value.get("owner_domain_generation"), int)
            or value["owner_domain_generation"] < 1
            or not isinstance(value.get("authority_state_id"), str)
            or not value["authority_state_id"].startswith("authority-state_")
            or not isinstance(value.get("bootstrap_pending"), bool)
        ):
            raise OwnerDomainAssetError("Owner Authority state does not match its root")
        return cast(_AuthorityState, value)
    value: _AuthorityState = {
        "contract_version": 1,
        "owner_domain_id": owner_domain_id,
        "owner_domain_generation": 1,
        "authority_state_id": "authority-state_" + secrets.token_urlsafe(24),
        "bootstrap_pending": True,
    }
    write_private_file(
        path,
        (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode(),
    )
    return value


def reset_owner_authority(
    material_root: Path,
    *,
    expected_owner_domain_id: str,
    expected_generation: int,
) -> tuple[int, str]:
    """Advance the Authority lineage under an exact, explicit CAS.

    This does not touch a Host or a device. The caller must separately execute
    the destructive target reset and must not expose the new generation until
    every release input has been regenerated from this state.
    """

    _require_private_material_root(material_root)
    root_certificate = _certificate(material_root / _ROOT_CERTIFICATE)
    owner_domain_id = _owner_domain_id(root_certificate)
    state = _authority_state(material_root, owner_domain_id)
    if (
        owner_domain_id != expected_owner_domain_id
        or state["owner_domain_generation"] != expected_generation
    ):
        raise OwnerDomainAssetError("Owner Authority reset target changed")
    next_state: _AuthorityState = {
        "contract_version": 1,
        "owner_domain_id": owner_domain_id,
        "owner_domain_generation": expected_generation + 1,
        "authority_state_id": "authority-state_" + secrets.token_urlsafe(24),
        "bootstrap_pending": True,
    }
    write_private_file(
        material_root / _STATE,
        (json.dumps(next_state, sort_keys=True, separators=(",", ":")) + "\n").encode(),
    )
    return next_state["owner_domain_generation"], next_state["authority_state_id"]


def mark_authority_bootstrapped(
    material_root: Path,
    *,
    owner_domain_id: str,
    owner_domain_generation: int,
    authority_state_id: str,
) -> None:
    """Consume a bootstrap capability only after target marker proof."""

    _require_private_material_root(material_root)
    state = _authority_state(material_root, owner_domain_id)
    if (
        state["owner_domain_generation"] != owner_domain_generation
        or state["authority_state_id"] != authority_state_id
    ):
        raise OwnerDomainAssetError("Authority bootstrap proof does not match")
    if not state["bootstrap_pending"]:
        return
    state["bootstrap_pending"] = False
    write_private_file(
        material_root / _STATE,
        (json.dumps(state, sort_keys=True, separators=(",", ":")) + "\n").encode(),
    )


def _bootstrap_document(state: _AuthorityState) -> bytes:
    value = {
        "contract_version": 1,
        "operation": (
            "owner-authority.bootstrap"
            if state["bootstrap_pending"]
            else "owner-authority.bootstrap-consumed"
        ),
        "owner_domain_id": state["owner_domain_id"],
        "owner_domain_generation": state["owner_domain_generation"],
        "state_id": state["authority_state_id"],
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
) -> bytes:
    path = root / _DESCRIPTOR
    origin = identity.hub_origin(port)
    endpoints = [
        {
            "authority": "admission",
            "logical_audience": f"{owner_domain_id}:admission",
            "uri": origin + "/api/device-onboarding/v1",
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
            try:
                legacy = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError) as legacy_exc:
                raise OwnerDomainAssetError(
                    "existing Owner Domain descriptor is invalid"
                ) from legacy_exc
            if (
                not isinstance(legacy, dict)
                or "owner_domain_generation" in legacy
                or legacy.get("owner_domain_id") != owner_domain_id
                or not isinstance(legacy.get("directory_revision"), int)
                or legacy["directory_revision"] < 1
            ):
                raise OwnerDomainAssetError(
                    "existing Owner Domain descriptor is invalid"
                ) from exc
            revision = legacy["directory_revision"] + 1
            current = None
        if current is not None:
            expected = [item.model_dump(mode="json") for item in current.endpoints]
            if (
                current.owner_domain_id == owner_domain_id
                and current.owner_domain_generation == owner_domain_generation
                and expected == endpoints
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


def _validate_directory(
    descriptor: OwnerDomainDescriptor,
    root_certificate: x509.Certificate,
    signer_certificate: x509.Certificate,
    now: datetime,
) -> None:
    trust = OwnerDomainTrustAnchor(
        owner_domain_id=descriptor.owner_domain_id,
        owner_root_certificate_pem=root_certificate.public_bytes(serialization.Encoding.PEM).decode(),
        authority_signing_certificate_pem=signer_certificate.public_bytes(
            serialization.Encoding.PEM
        ).decode(),
        trust_epoch=1,
    )
    AuthorityLocator(trust).accept(descriptor, now=now)


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
