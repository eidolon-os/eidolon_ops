"""Stable Host-bound LAN routing names.

These names locate the current Host candidate; they are never trust anchors.
Owner Domain certificates and signed logical-authority directories are issued
only by :mod:`eidolon_ops.owner_domain_assets`.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519


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
