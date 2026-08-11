"""Render deployment-owned, Host-bound LAN application assets."""

from __future__ import annotations

import os
import stat
from dataclasses import dataclass
from pathlib import Path

from eidolon_ops.config import OperationsConfig
from eidolon_ops.host_identity import (
    HostIdentityError,
    HostLanIdentity,
    derive_host_lan_identity,
    generate_hub_tls_identity,
    validate_hub_tls_identity,
)
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
        return _merge_environment(value, replacements)

    def public_contract(self) -> dict[str, object]:
        identity = self.identity()
        return {
            "host_id": identity.host_id,
            "hub_id": identity.hub_id,
            "hub_hostname": identity.hub_hostname,
            "hub_https_port": self.app.hub_https_port,
            "hub_origin": identity.hub_origin(self.app.hub_https_port),
            "lan_ipv4": str(self.app.lan_ipv4),
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
        certificate_path = root / "hub.crt"
        private_key_path = root / "hub.key"
        existing = (certificate_path.exists(), private_key_path.exists())
        if any(existing) and not all(existing):
            raise HostApplicationError("Host application TLS identity is incomplete")
        if all(existing):
            for path in (certificate_path, private_key_path):
                if (
                    path.is_symlink()
                    or not path.is_file()
                    or stat.S_IMODE(path.stat().st_mode) != 0o600
                ):
                    raise HostApplicationError("Host application TLS identity is unsafe")
            certificate = certificate_path.read_bytes()
            private_key = private_key_path.read_bytes()
            try:
                validate_hub_tls_identity(certificate, private_key, identity)
            except HostIdentityError as exc:
                raise HostApplicationError(
                    "existing Host application TLS identity does not match the Host identity"
                ) from exc
            return certificate, private_key
        certificate, private_key = generate_hub_tls_identity(identity)
        _atomic_private_file(certificate_path, certificate)
        try:
            _atomic_private_file(private_key_path, private_key)
        except Exception:
            certificate_path.unlink(missing_ok=True)
            raise
        return certificate, private_key

    def _render_hub_settings(self, template: str, identity: HostLanIdentity) -> str:
        rendered = _replace_once(
            template,
            "hub_id: eidolon-hub-local",
            f"hub_id: {identity.hub_id}",
            "Hub ID",
        )
        rendered = _replace_once(
            rendered,
            "public_base_url: https://eidolon-hub.local",
            f"public_base_url: {identity.hub_origin(self.app.hub_https_port)}",
            "Hub public base URL",
        )
        return rendered

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
        return """\
[Service]
Environment=EIDOLON_HUB_SETTINGS_YAML=/etc/eidolon/generated/hub.yaml
"""


def _replace_once(value: str, old: str, new: str, label: str) -> str:
    if value.count(old) != 1:
        raise HostApplicationError(f"{label} template drifted")
    return value.replace(old, new)


def _merge_environment(value: str, replacements: dict[str, str]) -> str:
    parsed: dict[str, str] = {}
    for raw in value.splitlines():
        if not raw:
            continue
        key, separator, current = raw.partition("=")
        if not separator or not key or key in parsed or not current:
            raise HostApplicationError("Host application environment seed is invalid")
        parsed[key] = current
    for key, replacement in replacements.items():
        if not replacement or any(character in replacement for character in "\n\r\0"):
            raise HostApplicationError("Host application environment value is unsafe")
        parsed[key] = replacement
    return "".join(f"{key}={parsed[key]}\n" for key in sorted(parsed))


def _atomic_private_file(path: Path, content: bytes) -> None:
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        os.chmod(path, 0o600)
    finally:
        temporary.unlink(missing_ok=True)
