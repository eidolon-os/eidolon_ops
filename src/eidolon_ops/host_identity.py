"""Stable Host-bound LAN identities and pinned Hub TLS material."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, ed25519
from cryptography.x509.oid import NameOID


class HostIdentityError(ValueError):
    """Host identity or its derived LAN material is invalid."""


@dataclass(frozen=True, slots=True)
class HostLanIdentity:
    """Names bound to the same Ed25519 Host identity consumed by Local API."""

    host_id: str
    hub_id: str
    hub_hostname: str

    def hub_origin(self, port: int) -> str:
        if not 1 <= port <= 65535:
            raise HostIdentityError("Hub HTTPS port is invalid")
        return f"https://{self.hub_hostname}:{port}"


def derive_host_lan_identity(raw_private_key: bytes) -> HostLanIdentity:
    """Use Admin's authoritative Host-ID derivation for deployment-owned LAN names."""

    if len(raw_private_key) != 32:
        raise HostIdentityError("Host identity must contain 32 raw Ed25519 private bytes")
    try:
        private_key = ed25519.Ed25519PrivateKey.from_private_bytes(raw_private_key)
    except ValueError as exc:
        raise HostIdentityError("Host identity is not a valid Ed25519 private key") from exc
    public_key = private_key.public_key().public_bytes(
        serialization.Encoding.Raw,
        serialization.PublicFormat.Raw,
    )
    suffix = hashlib.sha256(public_key).hexdigest()[:20]
    return HostLanIdentity(
        host_id=f"ehost-{suffix}",
        hub_id=f"eidolon-hub-{suffix}",
        hub_hostname=f"eidolon-hub-{suffix}.local",
    )


def generate_hub_tls_identity(
    identity: HostLanIdentity,
    *,
    now: datetime | None = None,
    validity_days: int = 3650,
) -> tuple[bytes, bytes]:
    """Create one self-signed P-256 leaf pinned by Local API, not a public PKI identity."""

    if validity_days < 1:
        raise HostIdentityError("Hub TLS validity must be positive")
    instant = (now or datetime.now(UTC)).astimezone(UTC)
    private_key = ec.generate_private_key(ec.SECP256R1())
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, identity.hub_hostname)])
    certificate = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(private_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(instant - timedelta(minutes=5))
        .not_valid_after(instant + timedelta(days=validity_days))
        .add_extension(
            x509.SubjectAlternativeName([x509.DNSName(identity.hub_hostname)]),
            critical=False,
        )
        .sign(private_key, hashes.SHA256())
    )
    return (
        certificate.public_bytes(serialization.Encoding.PEM),
        private_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ),
    )


def validate_hub_tls_identity(
    certificate_pem: bytes,
    private_key_pem: bytes,
    identity: HostLanIdentity,
    *,
    now: datetime | None = None,
) -> None:
    """Prove hostname, validity, curve and key pairing without disclosing key material."""

    try:
        certificate = x509.load_pem_x509_certificate(certificate_pem)
        private_key = serialization.load_pem_private_key(private_key_pem, password=None)
        sans = certificate.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    except (TypeError, ValueError, x509.ExtensionNotFound) as exc:
        raise HostIdentityError("Hub TLS identity is unreadable") from exc
    if not isinstance(private_key, ec.EllipticCurvePrivateKey) or not isinstance(
        private_key.curve, ec.SECP256R1
    ):
        raise HostIdentityError("Hub TLS private key must use P-256")
    if certificate.public_key().public_numbers() != private_key.public_key().public_numbers():
        raise HostIdentityError("Hub TLS certificate and private key do not match")
    if sans.get_values_for_type(x509.DNSName) != [identity.hub_hostname]:
        raise HostIdentityError("Hub TLS certificate SAN does not match the Host identity")
    instant = (now or datetime.now(UTC)).astimezone(UTC)
    not_before = certificate.not_valid_before_utc
    not_after = certificate.not_valid_after_utc
    if instant < not_before or instant >= not_after:
        raise HostIdentityError("Hub TLS certificate is outside its validity period")
