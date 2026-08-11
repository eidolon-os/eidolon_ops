"""Materialize the commit-pinned Pi topology for isolated macOS source runs."""

from __future__ import annotations

import os
import re
import socket
import ssl
import stat
import time
import urllib.error
import urllib.request
from collections.abc import Mapping
from pathlib import Path
from urllib.parse import urlparse

from eidolon_ops.config import OperationsConfig
from eidolon_ops.controller import OperationsError
from eidolon_ops.host_identity import (
    HostIdentityError,
    HostLanIdentity,
    derive_host_lan_identity,
    generate_hub_tls_identity,
    validate_hub_tls_identity,
)
from eidolon_ops.install_inputs import InstallInputError, validate_install_input_contract
from eidolon_ops.paths import HostProfile
from eidolon_ops.process import ProcessRunner, checked

_ENV_NAMES = (
    "data.env",
    "hub.env",
    "kernel.env",
    "admin.env",
    "local-api.env",
    "bootstrap.env",
    "agent.env",
    "channel.env",
    "memory.env",
    "livekit.env",
)
_SETTING_INPUT_NAMES = ("agent.yaml", "channel.yaml", "memory.yaml")
_KERNEL_SETTINGS = {
    "kernel.yaml": "config/kernel.systemd.example.yaml",
    "hub.yaml": "config/hub.systemd.example.yaml",
    "system-services.yaml": "config/system-services.yaml",
}


class LocalProductSource:
    """Generate and validate one source-run profile without touching sibling trees."""

    def __init__(
        self,
        profile: HostProfile,
        config: OperationsConfig,
        runner: ProcessRunner,
        *,
        git: str = "git",
    ) -> None:
        self.profile = profile
        self.config = config
        self.runner = runner
        self.git = git

    def prepare(self) -> dict[str, object]:
        self._validate_exact_worktrees()
        try:
            validate_install_input_contract(
                self.config,
                self._read_exact_file,
                verify_provider_sources=False,
            )
        except InstallInputError as exc:
            raise OperationsError(str(exc)) from exc

        paths = self.profile.paths
        for directory in (
            paths.config_root,
            paths.config_root / "env",
            paths.config_root / "settings",
            paths.config_root / "tls",
            paths.state_root,
            paths.runtime_root,
            paths.log_root,
            paths.cache_root,
            paths.bootstrap_state_root,
            paths.bootstrap_runtime_root,
        ):
            directory.mkdir(parents=True, exist_ok=True)
        os.chmod(paths.config_root, 0o700)
        os.chmod(paths.config_root / "env", 0o700)
        os.chmod(paths.config_root / "tls", 0o700)
        source_inputs = next(iter(self.config.install_files.values())).parent
        identity_destination = paths.bootstrap_state_root / "host_identity.ed25519"
        identity_source = source_inputs.joinpath("host_identity.ed25519").read_bytes()
        if not identity_destination.exists():
            _atomic_private_file(identity_destination, identity_source)
        elif (
            identity_destination.is_symlink()
            or not identity_destination.is_file()
            or stat.S_IMODE(identity_destination.stat().st_mode) != 0o600
            or identity_destination.stat().st_size != 32
        ):
            raise OperationsError("existing Mac Host Identity is unsafe or invalid")
        self._ensure_hub_tls_identity()

        expected: dict[Path, bytes] = {}
        for name in _ENV_NAMES:
            rendered = self._translate_fhs(source_inputs.joinpath(name).read_text(encoding="utf-8"))
            if self.profile.foundation_mode == "external" and name in {
                "channel.env",
                "livekit.env",
            }:
                rendered = _replace_environment_values(
                    rendered,
                    self._external_livekit_credentials(),
                )
            if name == "local-api.env":
                rendered = _merge_environment_values(
                    rendered,
                    {
                        "EIDOLON_LOCAL_API_HUB_ID": self._host_lan_identity().hub_id,
                        "EIDOLON_LOCAL_API_HUB_DESCRIPTOR_URI": (
                            self._hub_public_base_url() + "/api/device-onboarding/v1/descriptor"
                        ),
                        "EIDOLON_LOCAL_API_HUB_TLS_CERTIFICATE": str(self._hub_certificate_path()),
                    },
                )
            if name == "channel.env":
                app = self._require_app_access()
                rendered = _merge_environment_values(
                    rendered,
                    {
                        "EIDOLON_LIVEKIT_CLIENT_URL": app.livekit_client_url,
                        "EIDOLON_CHANNEL_PROVIDER_ALLOW_INSECURE_LAN_CLIENT_URL": (
                            "1" if app.allow_insecure_livekit else "0"
                        ),
                    },
                )
            expected[paths.config_root / "env" / name] = rendered.encode("utf-8")
        for name in _SETTING_INPUT_NAMES:
            rendered = self._translate_fhs(source_inputs.joinpath(name).read_text(encoding="utf-8"))
            if name == "memory.yaml" and "\nsupervisor:\n" not in rendered:
                rendered = (
                    rendered.rstrip()
                    + "\n\nsupervisor:\n  eager_init: true\n"
                    + f"  admin_http_port: {self._ports()['memory_admin']}\n"
                )
            expected[paths.config_root / "settings" / name] = rendered.encode("utf-8")
        for name, source_path in _KERNEL_SETTINGS.items():
            rendered = self._translate_fhs(
                self._read_exact_file(
                    "eidolon_kernel",
                    self.config.sources["eidolon_kernel"].revision,
                    source_path,
                )
            )
            if name == "hub.yaml":
                identity = self._host_lan_identity()
                rendered = _replace_exactly_once(
                    rendered,
                    "hub_id: eidolon-hub-local",
                    f"hub_id: {identity.hub_id}",
                    label="Host-bound Hub identity",
                )
                rendered = _replace_exactly_once(
                    rendered,
                    "public_base_url: https://eidolon-hub.local",
                    f"public_base_url: {self._hub_public_base_url()}",
                    label="Hub public base URL",
                )
            expected[paths.config_root / "settings" / name] = rendered.encode("utf-8")
        provider_settings = self._read_exact_file(
            "eidolon_channel",
            self.config.sources["eidolon_channel"].revision,
            "config/channel-provider.yaml",
        )
        expected[paths.config_root / "settings/channel-provider.yaml"] = self._translate_fhs(
            provider_settings
        ).encode("utf-8")
        expected[paths.config_root / "settings/eidolond.yaml"] = self._eidolond_settings().encode(
            "utf-8"
        )
        expected[paths.config_root / "settings/ports.yaml"] = self._admin_ports_yaml().encode(
            "utf-8"
        )
        expected[paths.config_root / "settings/services.yaml"] = self._admin_services_yaml().encode(
            "utf-8"
        )
        livekit_template = Path(__file__).resolve().parents[2] / "deploy/livekit/livekit.yaml"
        expected[paths.config_root / "settings/livekit.yaml"] = self._translate_ports(
            livekit_template.read_text(encoding="utf-8")
        ).encode("utf-8")
        expected[paths.config_root / "product-source.env"] = self._profile_environment().encode(
            "utf-8"
        )
        for destination, content in expected.items():
            _atomic_private_file(destination, content)
        self._migrate_data_schema()
        result = self.validate()
        return {
            **result,
            "status": "prepared",
            "source_count": len(self.config.sources),
            "generated_root": str(paths.config_root),
        }

    def validate(self) -> dict[str, object]:
        self._validate_exact_worktrees()
        root = self.profile.paths.config_root
        required = [
            *(root / "env" / name for name in _ENV_NAMES),
            *(root / "settings" / name for name in _SETTING_INPUT_NAMES),
            *(root / "settings" / name for name in _KERNEL_SETTINGS),
            root / "settings/channel-provider.yaml",
            root / "settings/eidolond.yaml",
            root / "settings/ports.yaml",
            root / "settings/services.yaml",
            root / "settings/livekit.yaml",
            root / "product-source.env",
            self.profile.paths.bootstrap_state_root / "host_identity.ed25519",
            self._hub_certificate_path(),
            self._hub_private_key_path(),
        ]
        missing = [str(path) for path in required if not path.is_file() or path.is_symlink()]
        if missing:
            raise OperationsError("Mac product-source inputs are missing: " + ", ".join(missing))
        unsafe = [str(path) for path in required if stat.S_IMODE(path.stat().st_mode) != 0o600]
        if unsafe:
            raise OperationsError(
                "Mac product-source inputs have unsafe modes: " + ", ".join(unsafe)
            )
        identity = self.profile.paths.bootstrap_state_root / "host_identity.ed25519"
        if identity.stat().st_size != 32:
            raise OperationsError("Mac Host Identity must contain exactly 32 bytes")
        return {
            "status": "compatible",
            "profile": "product-source",
            "generated_root": str(root),
            "services": 15,
            "redaction": "generated credentials are not returned",
        }

    def health(self, *, wait_seconds: float = 0) -> dict[str, object]:
        deadline = time.monotonic() + max(wait_seconds, 0)
        checks: dict[str, dict[str, object]] = {}
        while True:
            ports = self._ports()
            endpoints = {
                "nats": f"http://127.0.0.1:{ports['nats_http']}/healthz",
                "livekit": f"http://127.0.0.1:{ports['livekit']}/",
                "data": f"http://127.0.0.1:{ports['data']}/health",
                "data-workspace": f"http://127.0.0.1:{ports['data_workspace']}/health",
                "hub": f"http://127.0.0.1:{ports['hub']}/health",
                "kernel": f"http://127.0.0.1:{ports['kernel']}/health",
                "admin": f"http://127.0.0.1:{ports['admin']}/healthz",
                "local-api": f"https://127.0.0.1:{ports['local_api']}/healthz",
                "memory": (
                    f"http://127.0.0.1:{ports['memory_discovery']}/api/discovery/agent-routing"
                ),
                "agent": f"http://127.0.0.1:{ports['agent_http']}/readyz",
                "channel-provider": (f"http://127.0.0.1:{ports['channel_provider']}/health"),
            }
            checks = {name: _http_health(url) for name, url in endpoints.items()}
            checks["eidolond"] = _unix_http_health(self.profile.paths.runtime_root / "system.sock")
            if all(item["healthy"] for item in checks.values()) or time.monotonic() >= deadline:
                break
            time.sleep(0.5)
        healthy = all(item["healthy"] for item in checks.values())
        return {
            "status": "healthy" if healthy else "degraded",
            "foundation_mode": self.profile.foundation_mode,
            "checks": checks,
        }

    def app_ready(self) -> dict[str, object]:
        app = self._require_app_access()
        ports = self._ports()
        backend = self.health()
        interface_result = self.runner.run(("ifconfig",), timeout=10)
        interface_addresses = set(
            re.findall(r"\binet\s+(\d+\.\d+\.\d+\.\d+)\b", interface_result.stdout)
        )
        local_api = _http_health(f"https://{app.lan_ipv4}:{ports['local_api']}/healthz")
        hub = _http_health(f"https://{app.lan_ipv4}:{app.hub_https_port}/health")
        livekit_origin = urlparse(app.livekit_client_url)
        livekit = _tcp_health(
            str(app.lan_ipv4),
            livekit_origin.port or (443 if livekit_origin.scheme == "wss" else 80),
        )
        local_api_env = _read_environment_file(self.profile.paths.config_root / "env/local-api.env")
        channel_env = _read_service_environment_file(
            self.profile.paths.config_root / "env/channel.env"
        )
        settings = (self.profile.paths.config_root / "settings/hub.yaml").read_text(
            encoding="utf-8"
        )
        identity = self._host_lan_identity()
        try:
            validate_hub_tls_identity(
                self._hub_certificate_path().read_bytes(),
                self._hub_private_key_path().read_bytes(),
                identity,
            )
            hub_tls_identity = True
        except (OSError, HostIdentityError):
            hub_tls_identity = False
        generated_livekit = self.profile.external_livekit_config
        livekit_node_ip = ""
        if generated_livekit is not None and generated_livekit.is_file():
            match = re.search(
                r"(?m)^\s*node_ip:\s*(\d+\.\d+\.\d+\.\d+)\s*$",
                generated_livekit.read_text(encoding="utf-8"),
            )
            livekit_node_ip = match.group(1) if match else ""
        mdns_log = self.profile.paths.log_root / "admin/local-api-mdns.log"
        mdns_registered = mdns_log.is_file() and "Name now registered" in mdns_log.read_text(
            encoding="utf-8", errors="replace"
        )
        contract = {
            "lan_address_present": str(app.lan_ipv4) in interface_addresses,
            "local_api_target": (
                local_api_env.get("EIDOLON_LOCAL_API_HUB_ID") == self._host_lan_identity().hub_id
                and local_api_env.get("EIDOLON_LOCAL_API_HUB_DESCRIPTOR_URI")
                == self._hub_public_base_url() + "/api/device-onboarding/v1/descriptor"
                and local_api_env.get("EIDOLON_LOCAL_API_HUB_TLS_CERTIFICATE")
                == str(self._hub_certificate_path())
            ),
            "hub_public_url": f"public_base_url: {self._hub_public_base_url()}" in settings,
            "hub_identity": (
                f"hub_id: {identity.hub_id}" in settings
                and "hub_id: eidolon-hub-local" not in settings
            ),
            "hub_tls_identity": hub_tls_identity,
            "livekit_client_url": (
                channel_env.get("EIDOLON_LIVEKIT_CLIENT_URL") == app.livekit_client_url
            ),
            "livekit_development_opt_in": (
                channel_env.get("EIDOLON_CHANNEL_PROVIDER_ALLOW_INSECURE_LAN_CLIENT_URL")
                == ("1" if app.allow_insecure_livekit else "0")
            ),
            "livekit_rtc_node_ip": livekit_node_ip == str(app.lan_ipv4),
            "local_api_mdns_registered": mdns_registered,
        }
        checks = {
            "backend": backend["status"] == "healthy",
            "local_api_lan_https": bool(local_api["healthy"]),
            "hub_lan_https": bool(hub["healthy"]),
            "livekit_lan_tcp": bool(livekit["healthy"]),
            **contract,
        }
        healthy = all(checks.values())
        return {
            "status": "app_ready" if healthy else "degraded",
            "host_id": self.profile.host_id,
            "lan_ipv4": str(app.lan_ipv4),
            "checks": checks,
            "endpoints": {
                "local_api": f"https://{app.lan_ipv4}:{ports['local_api']}",
                "hub": self._hub_public_base_url(),
                "livekit": app.livekit_client_url,
            },
            "scope": (
                "Host-side LAN contract only; a Pad conversation remains the final external gate"
            ),
        }

    def _validate_exact_worktrees(self) -> None:
        for source_id, source in self.config.sources.items():
            if not source.path.is_dir():
                raise OperationsError(f"source worktree is missing: {source_id}")
            result = checked(
                f"Mac product source revision for {source_id}",
                self.runner.run((self.git, "-C", str(source.path), "rev-parse", "HEAD")),
            )
            if result.stdout.strip() != source.revision:
                raise OperationsError(
                    f"Mac product source revision does not match the release pin: {source_id}"
                )

    def _read_exact_file(self, source_id: str, revision: str, path: str) -> str:
        source = self.config.sources[source_id]
        if source.revision != revision:
            raise OperationsError(f"source revision drifted while reading {source_id}:{path}")
        return checked(
            f"exact Mac product source file {source_id}:{path}",
            self.runner.run((self.git, "-C", str(source.path), "show", f"{revision}:{path}")),
        ).stdout

    def _translate_fhs(self, value: str) -> str:
        paths = self.profile.paths
        replacements = (
            ("/var/lib/eidolon-bootstrap", str(paths.bootstrap_state_root)),
            ("/run/eidolon-bootstrap", str(paths.bootstrap_runtime_root)),
            ("/var/lib/eidolon", str(paths.state_root)),
            ("/var/log/eidolon", str(paths.log_root)),
            ("/var/cache/eidolon", str(paths.cache_root)),
            ("/run/eidolon", str(paths.runtime_root)),
        )
        for old, new in replacements:
            value = value.replace(old, new)
        return self._translate_ports(value)

    def _translate_ports(self, value: str) -> str:
        if "supervisor:\n  eager_init: true" in value and "admin_http_port:" not in value:
            value = value.replace(
                "supervisor:\n  eager_init: true",
                "supervisor:\n  eager_init: true\n"
                f"  admin_http_port: {self._ports()['memory_admin']}",
            )
        return value

    def _ports(self) -> dict[str, int]:
        return {
            "nats": 4222,
            "nats_http": 8222,
            "livekit": 7880,
            "memory_admin": 8019,
            "memory_discovery": 8020,
            "agent_admin": 8081,
            "agent_http": 8180,
            "hub": 8082,
            "kernel": 8083,
            "data": 8084,
            "data_workspace": 8085,
            "eidolond": 8090,
            "channel_worker": 8766,
            "channel_provider": 8767,
            "admin": 9000,
            "admin_web": 9001,
            "local_api": 9002,
        }

    def _require_app_access(self):
        if self.profile.app is None:
            raise OperationsError("Mac product-source profile requires an app access contract")
        return self.profile.app

    def _hub_public_base_url(self) -> str:
        app = self._require_app_access()
        return self._host_lan_identity().hub_origin(app.hub_https_port)

    def _host_lan_identity(self) -> HostLanIdentity:
        path = self.profile.paths.bootstrap_state_root / "host_identity.ed25519"
        try:
            if (
                path.is_symlink()
                or not path.is_file()
                or stat.S_IMODE(path.stat().st_mode) != 0o600
            ):
                raise OperationsError("Mac Host Identity is unsafe or missing")
            return derive_host_lan_identity(path.read_bytes())
        except (OSError, HostIdentityError) as exc:
            raise OperationsError("Mac Host Identity cannot define its LAN identity") from exc

    def _hub_certificate_path(self) -> Path:
        return self.profile.paths.config_root / "tls/hub.crt"

    def _hub_private_key_path(self) -> Path:
        return self.profile.paths.config_root / "tls/hub.key"

    def _ensure_hub_tls_identity(self) -> None:
        identity = self._host_lan_identity()
        certificate = self._hub_certificate_path()
        private_key = self._hub_private_key_path()
        existing = (certificate.exists(), private_key.exists())
        if any(existing) and not all(existing):
            raise OperationsError("Mac Hub TLS identity is incomplete")
        replace_identity = not all(existing)
        if all(existing):
            for path in (certificate, private_key):
                if path.is_symlink() or not path.is_file():
                    raise OperationsError("Mac Hub TLS identity is unsafe")
            try:
                validate_hub_tls_identity(
                    certificate.read_bytes(), private_key.read_bytes(), identity
                )
            except (OSError, HostIdentityError):
                replace_identity = True
        if replace_identity:
            certificate_pem, private_key_pem = generate_hub_tls_identity(identity)
            previous_certificate = certificate.read_bytes() if certificate.is_file() else None
            previous_key = private_key.read_bytes() if private_key.is_file() else None
            try:
                _atomic_private_file(certificate, certificate_pem)
                _atomic_private_file(private_key, private_key_pem)
                validate_hub_tls_identity(
                    certificate.read_bytes(), private_key.read_bytes(), identity
                )
            except (OSError, HostIdentityError) as exc:
                if previous_certificate is None:
                    certificate.unlink(missing_ok=True)
                else:
                    _atomic_private_file(certificate, previous_certificate)
                if previous_key is None:
                    private_key.unlink(missing_ok=True)
                else:
                    _atomic_private_file(private_key, previous_key)
                raise OperationsError("Mac Hub TLS identity rotation failed") from exc
        for path in (certificate, private_key):
            if path.is_symlink() or not path.is_file():
                raise OperationsError("Mac Hub TLS identity is unsafe")
            os.chmod(path, 0o600)
        try:
            validate_hub_tls_identity(certificate.read_bytes(), private_key.read_bytes(), identity)
        except (OSError, HostIdentityError) as exc:
            raise OperationsError("Mac Hub TLS identity is invalid") from exc

    def _eidolond_settings(self) -> str:
        paths = self.profile.paths
        port = self._ports()["eidolond"]
        supervisorctl = Path(__file__).resolve().parents[2] / ".venv/bin/supervisorctl"
        supervisor_config = (
            Path(__file__).resolve().parents[2] / "deploy/dev/supervisord.profile.conf"
        )
        return f"""\
manifest:
  path: {paths.config_root / "settings/system-services.yaml"}
persistence:
  path: {paths.state_root / "eidolond.sqlite3"}
host:
  driver: supervisord
  supervisorctl: {supervisorctl}
  supervisor_config: {supervisor_config}
  command_timeout_seconds: 20
reconciliation:
  interval_seconds: 5
  readiness_timeout_seconds: 3
interface:
  host: 127.0.0.1
  port: {port}
  uds: {paths.runtime_root / "system.sock"}
  uds_mode: "0600"
"""

    def _profile_environment(self) -> str:
        ports = self._ports()
        values: dict[str, str] = {
            **self.profile.environment(),
            "EIDOLON_PRODUCT_ENV_ROOT": str(self.profile.paths.config_root / "env"),
            "EIDOLON_PRODUCT_SETTINGS_ROOT": str(self.profile.paths.config_root / "settings"),
            "EIDOLON_LIVEKIT_TEMPLATE_CONFIG": str(
                self.profile.paths.config_root / "settings/livekit.yaml"
            ),
            "EIDOLON_ADMIN_API_HOST": "127.0.0.1",
            "EIDOLON_ADMIN_API_PORT": str(ports["admin"]),
            "EIDOLON_ADMIN_WEB_PORT": str(ports["admin_web"]),
            "EIDOLON_PRODUCT_NATS_PORT": str(ports["nats"]),
            "EIDOLON_PRODUCT_NATS_HTTP_PORT": str(ports["nats_http"]),
            "EIDOLON_PRODUCT_LIVEKIT_PORT": str(ports["livekit"]),
            "EIDOLON_PRODUCT_MEMORY_ADMIN_PORT": str(ports["memory_admin"]),
            "EIDOLON_PRODUCT_MEMORY_DISCOVERY_PORT": str(ports["memory_discovery"]),
            "EIDOLON_PRODUCT_AGENT_ADMIN_PORT": str(ports["agent_admin"]),
            "EIDOLON_PRODUCT_AGENT_HTTP_PORT": str(ports["agent_http"]),
            "EIDOLON_PRODUCT_HUB_PORT": str(ports["hub"]),
            "EIDOLON_PRODUCT_KERNEL_PORT": str(ports["kernel"]),
            "EIDOLON_PRODUCT_DATA_PORT": str(ports["data"]),
            "EIDOLON_PRODUCT_DATA_WORKSPACE_PORT": str(ports["data_workspace"]),
            "EIDOLON_PRODUCT_EIDOLOND_PORT": str(ports["eidolond"]),
            "EIDOLON_PRODUCT_CHANNEL_WORKER_PORT": str(ports["channel_worker"]),
            "EIDOLON_PRODUCT_CHANNEL_PROVIDER_PORT": str(ports["channel_provider"]),
            "EIDOLON_PRODUCT_LOCAL_API_PORT": str(ports["local_api"]),
            "EIDOLON_APP_LAN_IPV4": str(self._require_app_access().lan_ipv4),
            "EIDOLON_APP_HUB_HTTPS_PORT": str(self._require_app_access().hub_https_port),
            "EIDOLON_APP_HUB_TLS_CERTIFICATE": str(self._hub_certificate_path()),
            "EIDOLON_APP_HUB_TLS_PRIVATE_KEY": str(self._hub_private_key_path()),
            "EIDOLON_SOURCE_KERNEL": str(self.config.sources["eidolon_kernel"].path),
            "EIDOLON_SOURCE_DATA": str(self.config.sources["eidolon_data"].path),
            "EIDOLON_SOURCE_HUB": str(self.config.sources["eidolon_hub"].path),
            "EIDOLON_SOURCE_ADMIN": str(self.config.sources["eidolon_admin"].path),
            "EIDOLON_SOURCE_AGENT": str(self.config.sources["eidolon_agent"].path),
            "EIDOLON_SOURCE_CHANNEL": str(self.config.sources["eidolon_channel"].path),
            "EIDOLON_SOURCE_MEMORY": str(self.config.sources["eidolon_memory"].path),
        }
        if self.profile.external_livekit_config is not None:
            values["EIDOLON_LIVEKIT_GENERATED_CONFIG"] = str(self.profile.external_livekit_config)
        return _serialize_plain_environment(values)

    def _admin_ports_yaml(self) -> str:
        port = self._ports()
        return f"""\
admin:
  api: {{host: 127.0.0.1, port: {port["admin"]}}}
  web: {{port: {port["admin_web"]}}}
hub:
  api: {{host: 127.0.0.1, port: {port["hub"]}}}
data:
  api: {{host: 127.0.0.1, port: {port["data"]}}}
  workspace_api: {{host: 127.0.0.1, port: {port["data_workspace"]}}}
kernel:
  api: {{host: 127.0.0.1, port: {port["kernel"]}}}
eidolond:
  api: {{host: 127.0.0.1, port: {port["eidolond"]}}}
agent:
  http: {{port: {port["agent_http"]}}}
  admin: {{port: {port["agent_admin"]}}}
  grpc: {{port: 45051}}
memory:
  discovery: {{host: 127.0.0.1, port: {port["memory_discovery"]}}}
  mcp: {{port: 10030}}
  supervisor_http: {{host: 127.0.0.1, port: {port["memory_admin"]}}}
channel:
  worker: {{port: {port["channel_worker"]}}}
client_web: {{port: 3001}}
nats: {{port: {port["nats"]}, http_port: {port["nats_http"]}}}
livekit:
  port: {port["livekit"]}
  turn_udp_port: 3478
  rtc_port_start: 50000
  rtc_port_end: 60000
"""

    def _admin_services_yaml(self) -> str:
        port = self._ports()
        services = (
            (
                "admin",
                "Eidolon Admin",
                "admin",
                ("admin-api",),
                f"http://127.0.0.1:{port['admin']}/docs",
            ),
            (
                "admin-web",
                "Eidolon Admin Web",
                "admin-web",
                ("admin-web",),
                f"http://127.0.0.1:{port['admin_web']}/",
            ),
            ("bootstrap", "Eidolon Bootstrap", "bootstrap", ("bootstrapd",), None),
            ("local-api", "Eidolon Local API", "local-api", ("local-api",), None),
            ("eidolond", "Eidolon System Manager", "eidolond", ("eidolond",), None),
            (
                "data",
                "Eidolon Data V2 Authority",
                "data",
                ("data-api",),
                f"http://127.0.0.1:{port['data']}/health",
            ),
            (
                "data-workspace",
                "Eidolon Data Workspace Authority",
                "data",
                ("data-workspace-api",),
                f"http://127.0.0.1:{port['data_workspace']}/health",
            ),
            ("hub", "Eidolon Hub", "hub", ("hub-api",), f"http://127.0.0.1:{port['hub']}/health"),
            (
                "kernel",
                "Eidolon Kernel",
                "kernel",
                ("kernel-api",),
                f"http://127.0.0.1:{port['kernel']}/health",
            ),
            (
                "memory",
                "Eidolon Memory",
                "memory",
                ("memory-supervisor", "memory-discovery"),
                f"http://127.0.0.1:{port['memory_admin']}/api/admin/health",
            ),
            (
                "agent",
                "Eidolon Agent",
                "agent",
                ("agent",),
                f"http://127.0.0.1:{port['agent_http']}/readyz",
            ),
            (
                "channel-provider",
                "Eidolon Channel Provider",
                "channel-provider",
                ("channel-provider",),
                f"http://127.0.0.1:{port['channel_provider']}/health",
            ),
            ("channel", "Eidolon Channel Worker", "channel", ("channel-worker",), None),
        )
        lines = [
            "admin:",
            "  host: 127.0.0.1",
            f"  port: {port['admin']}",
            "  cors_origins:",
            f"    - http://127.0.0.1:{port['admin_web']}",
            f"    - http://localhost:{port['admin_web']}",
            "services:",
        ]
        for service_id, name, group, programs, health in services:
            lines.extend(
                [
                    f"  - id: {service_id}",
                    f"    name: {name}",
                    "    integration: infra",
                    "    base_url: ''",
                    "    upstream_prefix: ''",
                    "    auth: {type: none}",
                ]
            )
            if health is not None:
                lines.append(f"    health: {health}")
            lines.extend(
                [
                    "    supervisor:",
                    f"      group: {group}",
                    "      programs:",
                    *(f"        - {program}" for program in programs),
                    "    features: []",
                ]
            )
        for service_id, name, health in (
            ("nats", "NATS (external)", f"http://127.0.0.1:{port['nats_http']}/varz"),
            ("livekit", "LiveKit (external)", f"http://127.0.0.1:{port['livekit']}/"),
            ("client-web", "Eidolon Client Web (external)", "http://127.0.0.1:3001/"),
        ):
            lines.extend(
                [
                    f"  - id: {service_id}",
                    f"    name: {name}",
                    "    integration: infra",
                    "    base_url: ''",
                    "    upstream_prefix: ''",
                    "    auth: {type: none}",
                    f"    health: {health}",
                    "    features: []",
                ]
            )
        return "\n".join(lines) + "\n"

    def _external_livekit_credentials(self) -> dict[str, str]:
        path = self.profile.external_livekit_config
        if path is None or not path.is_file() or path.is_symlink():
            raise OperationsError(f"external LiveKit config is missing or unsafe: {path}")
        text = path.read_text(encoding="utf-8")
        block = re.search(r"(?m)^keys:\n((?:[ \t].*(?:\n|$))*)", text)
        keys_block = block.group(1) if block else ""
        matches = re.findall(
            r"(?m)^\s{2}([A-Za-z0-9_-]+):\s*([A-Za-z0-9_-]+)\s*$",
            keys_block,
        )
        if len(matches) != 1:
            raise OperationsError("external LiveKit config must contain exactly one key pair")
        key, secret = matches[0]
        if not 3 <= len(key) <= 64 or not 8 <= len(secret) <= 128:
            raise OperationsError("external LiveKit credential shape is invalid")
        return {"LIVEKIT_API_KEY": key, "LIVEKIT_API_SECRET": secret}

    def _migrate_data_schema(self) -> None:
        source = self.config.sources["eidolon_data"].path
        alembic = source / ".venv/bin/alembic"
        if not os.access(alembic, os.X_OK):
            raise OperationsError(f"Data migration entrypoint is missing: {alembic}")
        environment = os.environ.copy()
        environment.update(self.profile.environment())
        environment.update(_read_environment_file(self.profile.paths.config_root / "env/data.env"))
        checked(
            "Mac product-source Data schema gate",
            self.runner.run(
                (str(alembic), "-c", "alembic.ini", "upgrade", "head"),
                cwd=source,
                env=environment,
                timeout=120,
            ),
        )


def _serialize_plain_environment(values: Mapping[str, str]) -> str:
    lines: list[str] = []
    for key in sorted(values):
        value = values[key]
        if not key.replace("_", "").isalnum() or not key.startswith("EIDOLON_"):
            raise OperationsError(f"unsafe generated profile key: {key}")
        if not value or any(character in value for character in "\n\r\0"):
            raise OperationsError(f"unsafe generated profile value: {key}")
        lines.append(f"{key}={value}\n")
    return "".join(lines)


def _read_environment_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        if not raw:
            continue
        key, separator, value = raw.partition("=")
        if (
            not separator
            or not key.replace("_", "").isalnum()
            or not key.startswith("EIDOLON_")
            or not value
            or key in values
        ):
            raise OperationsError(f"generated env file is invalid: {path.name}")
        values[key] = value
    return values


def _read_service_environment_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        if not raw:
            continue
        key, separator, value = raw.partition("=")
        if (
            not separator
            or re.fullmatch(r"[A-Z][A-Z0-9_]*", key) is None
            or not value
            or key in values
        ):
            raise OperationsError(f"generated service env file is invalid: {path.name}")
        values[key] = value
    return values


def _replace_environment_values(value: str, replacements: Mapping[str, str]) -> str:
    parsed: dict[str, str] = {}
    for raw in value.splitlines():
        if not raw:
            continue
        key, separator, current = raw.partition("=")
        if not separator or not key or key in parsed or not current:
            raise OperationsError("generated product environment is invalid")
        parsed[key] = current
    for key, replacement in replacements.items():
        if key not in parsed:
            raise OperationsError(f"generated product environment lacks {key}")
        parsed[key] = replacement
    return "".join(f"{key}={parsed[key]}\n" for key in sorted(parsed))


def _merge_environment_values(value: str, replacements: Mapping[str, str]) -> str:
    parsed: dict[str, str] = {}
    for raw in value.splitlines():
        if not raw:
            continue
        key, separator, current = raw.partition("=")
        if not separator or not key or key in parsed or not current:
            raise OperationsError("generated product environment is invalid")
        parsed[key] = current
    for key, replacement in replacements.items():
        if (
            not key.replace("_", "").isalnum()
            or not replacement
            or any(character in replacement for character in "\n\r\0")
        ):
            raise OperationsError("generated product environment merge is unsafe")
        parsed[key] = replacement
    return "".join(f"{key}={parsed[key]}\n" for key in sorted(parsed))


def _replace_exactly_once(value: str, old: str, new: str, *, label: str) -> str:
    if value.count(old) != 1:
        raise OperationsError(f"{label} template drifted")
    return value.replace(old, new)


def _atomic_private_file(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
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


def _http_health(url: str) -> dict[str, object]:
    context = ssl._create_unverified_context() if url.startswith("https://") else None
    handlers: list[urllib.request.BaseHandler] = [urllib.request.ProxyHandler({})]
    if context is not None:
        handlers.append(urllib.request.HTTPSHandler(context=context))
    opener = urllib.request.build_opener(*handlers)
    try:
        with opener.open(url, timeout=1.5) as response:
            status = response.status
    except (OSError, urllib.error.URLError):
        return {"healthy": False, "http_status": None}
    return {"healthy": status == 200, "http_status": status}


def _unix_http_health(path: Path) -> dict[str, object]:
    request = b"GET /health HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n"
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(1.5)
            client.connect(str(path))
            client.sendall(request)
            response = client.recv(128)
    except OSError:
        return {"healthy": False, "http_status": None}
    match = re.match(rb"HTTP/\d(?:\.\d)? (\d{3})(?: |\r)", response)
    status = int(match.group(1)) if match else None
    return {"healthy": status == 200, "http_status": status}


def _tcp_health(host: str, port: int) -> dict[str, object]:
    try:
        with socket.create_connection((host, port), timeout=1.5):
            pass
    except OSError:
        return {"healthy": False}
    return {"healthy": True}
