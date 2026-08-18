"""Render deployment-owned, Host-bound LAN application assets."""

from __future__ import annotations

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
from eidolon_ops.hub_assets import ensure_hub_tls_identity, render_hub_settings
from eidolon_ops.paths import AppAccess


class HostApplicationError(ValueError):
    """Host application assets are incomplete, ambiguous, or unsafe."""


HOST_APPLICATION_STAGE_NAMES = (
    "hub.generated.yaml",
    "hub.crt",
    "hub.key",
    "hub-ingress.py",
    "hub-ingress.service",
    "hub-service-override.conf",
)


@dataclass(frozen=True, slots=True)
class HostApplicationAssets:
    identity: HostLanIdentity
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
        return input_root.with_name(f"{input_root.name}-host-application")

    def prepare(self, hub_template: str) -> HostApplicationAssets:
        identity = self.identity()
        certificate, private_key = self._ensure_tls(identity)
        files = {
            "hub.generated.yaml": self._render_hub_settings(hub_template, identity).encode(),
            "hub.crt": certificate,
            "hub.key": private_key,
            "hub-ingress.py": self.ingress_source,
            "hub-ingress.service": self._ingress_service().encode(),
            "hub-service-override.conf": self._hub_service_override().encode(),
        }
        if set(files) != set(HOST_APPLICATION_STAGE_NAMES):
            raise HostApplicationError("Host application asset set is incomplete")
        return HostApplicationAssets(identity=identity, files=files)

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
            replacements = {
                "EIDOLON_LOCAL_API_HUB_ID": identity.hub_id,
                "EIDOLON_LOCAL_API_HUB_DESCRIPTOR_URI": (
                    identity.hub_origin(self.app.hub_https_port)
                    + "/api/device-onboarding/v1/descriptor"
                ),
                "EIDOLON_LOCAL_API_HUB_TLS_CERTIFICATE": "/etc/eidolon/tls/hub.crt",
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
        return {
            "host_id": identity.host_id,
            "hub_id": identity.hub_id,
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

    def _ensure_tls(self, identity: HostLanIdentity) -> tuple[bytes, bytes]:
        root = self.material_root
        if root.exists():
            if root.is_symlink() or not root.is_dir() or stat.S_IMODE(root.stat().st_mode) != 0o700:
                raise HostApplicationError("Host application material directory is unsafe")
            if {path.name for path in root.iterdir()} - {"hub.crt", "hub.key"}:
                raise HostApplicationError("Host application material directory has extra files")
        else:
            root.mkdir(mode=0o700, parents=False)
        # A product Host refuses a mismatch rather than rotating: material is
        # written once, and a pair that stopped matching means something
        # replaced it, which is an operator's decision and not this code's.
        return ensure_hub_tls_identity(
            root / "hub.crt",
            root / "hub.key",
            identity,
            rotate_on_mismatch=False,
        )

    def _render_hub_settings(self, template: str, identity: HostLanIdentity) -> str:
        return render_hub_settings(template, identity, self.app.hub_https_port)

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


