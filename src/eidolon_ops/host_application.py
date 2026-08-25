"""Render deployment-owned, Host-bound LAN application assets."""

from __future__ import annotations

import base64
import binascii
import json
import os
import stat
from dataclasses import dataclass
from pathlib import Path

from eidolon_ops import environment
from eidolon_ops.config import OperationsConfig
from eidolon_ops.host_identity import (
    HostIdentityError,
    HostLanIdentity,
    derive_host_lan_identity,
)
from eidolon_ops.hub_assets import render_hub_settings
from eidolon_ops.owner_domain_assets import (
    OwnerDomainAssetError,
    OwnerDomainAssets,
    ensure_owner_domain_assets,
)
from eidolon_ops.paths import AppAccess


class HostApplicationError(ValueError):
    """Host application assets are incomplete, ambiguous, or unsafe."""


HOST_APPLICATION_STAGE_NAMES = (
    "hub.generated.yaml",
    "hub.crt",
    "hub.key",
    "owner-domain-descriptor.json",
    "owner-domain-root-ca.pem",
    "authority-signing-certificate.pem",
    "authority-bootstrap.json",
    "hub-ingress.py",
    "hub-ingress.service",
    "hub-service-override.conf",
)
DEVELOPMENT_COMMISSIONING_STAGE_NAME = "commissioning-secrets.json"
DEVELOPMENT_COMMISSIONING_TARGET = "/etc/eidolon/commissioning-secrets.json"
_DEVELOPMENT_COMMISSIONING_PROFILE = "eidolon-development-hmac-commissioning-v2"
_MINIMUM_DEVELOPMENT_SECRET_BYTES = 32
_MAXIMUM_DEVELOPMENT_REGISTRY_BYTES = 64 * 1024


@dataclass(frozen=True, slots=True)
class HostApplicationAssets:
    identity: HostLanIdentity
    owner_domain_id: str
    files: dict[str, bytes]


class HostApplicationMaterializer:
    """Keep TLS stable across retries while rendering public assets deterministically."""

    def __init__(self, config: OperationsConfig, app: AppAccess, ingress_source: bytes) -> None:
        self.config = config
        self.app = app
        self.ingress_source = ingress_source

    @property
    def material_root(self) -> Path:
        input_root = self.config.install_files["host_identity"].parent
        return input_root.parent / "owner-domain"

    def prepare(self, hub_template: str) -> HostApplicationAssets:
        identity = self.identity()
        try:
            owner = self.owner_assets(identity=identity)
        except OwnerDomainAssetError as exc:
            raise HostApplicationError(str(exc)) from exc
        commissioning_registry = self._development_commissioning_registry()
        settings = self._render_hub_settings(
            hub_template,
            owner.owner_domain_id,
            owner.owner_domain_generation,
            identity,
        )
        if commissioning_registry is not None:
            settings = self._enable_development_commissioning(settings)
        files = {
            "hub.generated.yaml": settings.encode(),
            "hub.crt": owner.tls_certificate,
            "hub.key": owner.tls_private_key,
            "owner-domain-descriptor.json": owner.descriptor,
            "owner-domain-root-ca.pem": owner.owner_root_certificate,
            "authority-signing-certificate.pem": owner.authority_signing_certificate,
            "authority-bootstrap.json": owner.authority_bootstrap,
            "hub-ingress.py": self.ingress_source,
            "hub-ingress.service": self._ingress_service().encode(),
            "hub-service-override.conf": self._hub_service_override().encode(),
        }
        if commissioning_registry is not None:
            files[DEVELOPMENT_COMMISSIONING_STAGE_NAME] = commissioning_registry
        expected = set(HOST_APPLICATION_STAGE_NAMES)
        if commissioning_registry is not None:
            expected.add(DEVELOPMENT_COMMISSIONING_STAGE_NAME)
        if set(files) != expected:
            raise HostApplicationError("Host application asset set is incomplete")
        return HostApplicationAssets(
            identity=identity, owner_domain_id=owner.owner_domain_id, files=files
        )

    def owner_assets(
        self, *, identity: HostLanIdentity | None = None
    ) -> OwnerDomainAssets:
        """Return the controller-held Authority contract, never its signing keys."""

        return ensure_owner_domain_assets(
            self.material_root,
            identity or self.identity(),
            self.app.hub_https_port,
        )

    def identity(self) -> HostLanIdentity:
        path = self.config.install_files["host_identity"]
        try:
            if (
                path.is_symlink()
                or not path.is_file()
                or stat.S_IMODE(path.stat().st_mode) != 0o600
            ):
                raise HostApplicationError("Host identity input is unsafe or missing")
            return derive_host_lan_identity(path.read_bytes())
        except (OSError, HostIdentityError) as exc:
            raise HostApplicationError("Host identity input cannot define LAN identity") from exc

    def render_environment(self, name: str, value: str) -> str:
        identity = self.identity()
        replacements: dict[str, str]
        if name == "local-api.env":
            try:
                owner = ensure_owner_domain_assets(
                    self.material_root, identity, self.app.hub_https_port
                )
            except OwnerDomainAssetError as exc:
                raise HostApplicationError(str(exc)) from exc
            replacements = {
                "EIDOLON_LOCAL_API_OWNER_DOMAIN_ID": owner.owner_domain_id,
                "EIDOLON_LOCAL_API_OWNER_DOMAIN_DESCRIPTOR_URI": (
                    identity.hub_origin(self.app.hub_https_port)
                    + "/api/device-onboarding/v1/descriptor"
                ),
                "EIDOLON_LOCAL_API_OWNER_DOMAIN_DESCRIPTOR": (
                    "/etc/eidolon/owner-domain/owner_domain_descriptor.json"
                ),
                "EIDOLON_LOCAL_API_OWNER_ROOT_CERTIFICATE": (
                    "/etc/eidolon/owner-domain/owner_domain_root_ca.pem"
                ),
                "EIDOLON_LOCAL_API_AUTHORITY_SIGNING_CERTIFICATE": (
                    "/etc/eidolon/owner-domain/authority_signing_certificate.pem"
                ),
            }
        elif name == "channel.env":
            replacements = {
                "EIDOLON_LIVEKIT_CLIENT_URL": self.app.livekit_client_url,
                "EIDOLON_CHANNEL_PROVIDER_ALLOW_INSECURE_LAN_CLIENT_URL": (
                    "1" if self.app.allow_insecure_livekit else "0"
                ),
            }
        else:
            return value
        return environment.merge(value, replacements, label="Host application environment")

    def public_contract(self) -> dict[str, object]:
        identity = self.identity()
        try:
            owner = ensure_owner_domain_assets(
                self.material_root, identity, self.app.hub_https_port
            )
        except OwnerDomainAssetError as exc:
            raise HostApplicationError(str(exc)) from exc
        return {
            "host_id": identity.host_id,
            "owner_domain_id": owner.owner_domain_id,
            "hub_hostname": identity.hub_hostname,
            "hub_https_port": self.app.hub_https_port,
            "hub_origin": identity.hub_origin(self.app.hub_https_port),
            **(
                {"lan_ipv4": str(self.app.lan_ipv4)}
                if self.app.lan_ipv4 is not None
                else {}
            ),
            "livekit_client_url": self.app.livekit_client_url,
            "allow_insecure_livekit": self.app.allow_insecure_livekit,
        }

    def _render_hub_settings(
        self,
        template: str,
        owner_domain_id: str,
        owner_domain_generation: int,
        identity: HostLanIdentity,
    ) -> str:
        return render_hub_settings(
            template,
            owner_domain_id,
            owner_domain_generation,
            identity,
            self.app.hub_https_port,
        )

    def _development_commissioning_registry(self) -> bytes | None:
        """Validate one explicit HIL registry without exposing its contents or digest."""

        path = self.app.development_commissioning_registry
        if path is None:
            return None
        try:
            if path.is_symlink():
                raise HostApplicationError(
                    "development commissioning registry must be a mode-0600 non-symlink "
                    "file no larger than 65536 bytes"
                )
            descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
            try:
                metadata = os.fstat(descriptor)
                if (
                    not stat.S_ISREG(metadata.st_mode)
                    or stat.S_IMODE(metadata.st_mode) != 0o600
                    or metadata.st_size > _MAXIMUM_DEVELOPMENT_REGISTRY_BYTES
                ):
                    raise HostApplicationError(
                        "development commissioning registry must be a mode-0600 non-symlink "
                        "file no larger than 65536 bytes"
                    )
                with os.fdopen(descriptor, "rb", closefd=False) as stream:
                    value = stream.read()
            finally:
                os.close(descriptor)
            document = json.loads(value)
            if not isinstance(document, dict) or set(document) != {"profile", "devices"}:
                raise HostApplicationError(
                    "development commissioning registry has unknown or missing fields"
                )
            if document["profile"] != _DEVELOPMENT_COMMISSIONING_PROFILE:
                raise HostApplicationError(
                    "development commissioning registry has the wrong profile"
                )
            devices = document["devices"]
            if not isinstance(devices, dict) or not devices:
                raise HostApplicationError(
                    "development commissioning registry must contain at least one device"
                )
            for hardware_lookup_id, entry in devices.items():
                # An entry pre-shares a secret and states nothing else. The v1
                # format also carried a hand-typed hardware_identity_ref, and a
                # Waveshare AMOLED board was installed as "hardware-box3-..."
                # for the rest of its life: an unverifiable board type welded
                # into an immutable identity. The Hub derives that identity from
                # the lookup id the secret is bound to, so staging a file that
                # asserts one would ship a lie to a Host.
                if (
                    not isinstance(hardware_lookup_id, str)
                    or not hardware_lookup_id.strip()
                    or len(hardware_lookup_id.encode()) > 128
                    or not isinstance(entry, dict)
                    or set(entry) != {"setup_secret"}
                    or not isinstance(entry["setup_secret"], str)
                    or not entry["setup_secret"]
                ):
                    raise HostApplicationError(
                        "development commissioning registry contains an invalid device entry"
                    )
                encoded = entry["setup_secret"]
                padding = "=" * (-len(encoded) % 4)
                secret = base64.b64decode(
                    encoded + padding,
                    altchars=b"-_",
                    validate=True,
                )
                canonical = base64.urlsafe_b64encode(secret).rstrip(b"=").decode()
                if (
                    len(secret) < _MINIMUM_DEVELOPMENT_SECRET_BYTES
                    or canonical != encoded
                ):
                    raise HostApplicationError(
                        "development commissioning registry contains a weak or non-canonical secret"
                    )
            return value
        except HostApplicationError:
            raise
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, binascii.Error) as exc:
            raise HostApplicationError(
                "development commissioning registry is unreadable or invalid"
            ) from exc

    @staticmethod
    def _enable_development_commissioning(settings: str) -> str:
        if "\ncommissioning_proof:" in settings:
            raise HostApplicationError(
                "Hub settings template already selects a commissioning proof profile"
            )
        return (
            settings.rstrip()
            + "\n\ncommissioning_proof:\n"
            + "  profile: development-hmac\n"
            + f"  setup_secret_registry_path: {DEVELOPMENT_COMMISSIONING_TARGET}\n"
        )

    def _ingress_service(self) -> str:
        return f"""\
[Unit]
Description=Eidolon Host-bound Hub TLS ingress
After=network-online.target eidolon-hub.service
Wants=network-online.target
Requires=eidolon-hub.service
PartOf=eidolon-hub.service

[Service]
Type=simple
User=eidolon
Group=eidolon
ExecStart=/usr/bin/python3 /usr/local/libexec/eidolon-hub-lan-ingress --listen-host 0.0.0.0 --listen-port {self.app.hub_https_port} --upstream-host 127.0.0.1 --upstream-port 8082 --certificate /etc/eidolon/tls/hub.crt --private-key /etc/eidolon/tls/hub.key
Restart=on-failure
RestartSec=3s
NoNewPrivileges=yes
PrivateTmp=yes
ProtectSystem=strict
ProtectHome=yes
ProtectKernelTunables=yes
ProtectKernelModules=yes
ProtectControlGroups=yes
LockPersonality=yes
CapabilityBoundingSet=
RestrictSUIDSGID=yes
RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6

[Install]
WantedBy=multi-user.target
"""

    @staticmethod
    def _hub_service_override() -> str:
        # The ingress declares PartOf=, which carries stop and restart downward
        # but never start. Activation stopped the Hub, took the ingress with it,
        # started the Hub again and left the LAN closed — the App gate saw a
        # refused connection on the Hub port and rolled the release back. Wants=
        # completes the pair, so whoever starts the Hub opens the LAN with it.
        #
        # The settings path repeats what the current unit already says by
        # default, and is kept for the one case where the unit does not say it:
        # a rollback restores the release's own unit file, and a release from
        # before that default changed points the Hub at /etc/eidolon/hub.yaml —
        # a path this deployer removes. This drop-in survives the rollback and
        # is read after the unit, so the restored Hub still starts against the
        # settings rendered for this Host.
        return """\
[Unit]
Wants=eidolon-hub-ingress.service

[Service]
Environment=EIDOLON_HUB_SETTINGS_YAML=/etc/eidolon/generated/hub.yaml
"""
