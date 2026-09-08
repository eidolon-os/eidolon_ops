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


def livekit_client_url_at(configured: str, observed: str) -> str:
    """The configured LiveKit URL as a device would receive it, for a report.

    `livekit_client_url` may leave the host out, because Ops does not know it
    and the Channel provider fills it in per binding. That is the right thing
    to write into configuration and the wrong thing to show an operator, who
    reads `ws://:7880` as a bug. So a report resolves it the same way, against
    the address it has just observed this Host answering on.
    """

    scheme, _, rest = configured.partition("://")
    if not rest.startswith(":"):
        return configured
    return f"{scheme}://{observed}{rest}"


def livekit_client_url(
    identity: HostLanIdentity,
    *,
    lan_ipv4: object | None,
    allow_insecure: bool,
    port: int,
) -> str:
    """Where this Host tells a device to reach its LiveKit.

    Derived, because it never carried anything Ops did not already know. The
    profile used to spell it out, and the validation around that field had
    squeezed it to exactly two legal values: the declared `lan_ipv4` when there
    was one, or this Host's own derived hostname when there was not. What the
    field added was a chance to type the wrong one — the Pi's suffix went onto
    the RK3588 profile and travelled all the way to the board before anything
    refused it.

    The second of those values was also wrong on its own terms. A `.local` name
    is resolved on Android by getaddrinfo, which does not resolve mDNS names at
    all, so the board shipped
    `ws://eidolon-hub-f89c0ecca5d0070a7989.local:7880` — a URL no Android phone
    could turn into an address. And the first is a deploy-time observation
    frozen into an environment file: a Host that changed networks afterwards
    went on handing out the address it used to have.

    So when nothing is declared, nothing is claimed: the host is left out
    (`ws://:7880`), and the Channel provider answers it per binding from the
    address the kernel would leave this machine by. Ops writing an address here
    would be Ops answering a question that is only answerable later, which is
    how both of the old values came to be wrong.

    A declared `lan_ipv4` still wins, because an operator who pins an address
    has said something Ops cannot derive.
    """

    scheme = "ws" if allow_insecure else "wss"
    host = "" if lan_ipv4 is None else str(lan_ipv4)
    return f"{scheme}://{host}:{port}"
