"""Materialize and attest the commit-pinned topology for a macOS source run."""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path
from urllib.parse import urlparse

from cryptography import x509
from cryptography.hazmat.primitives.asymmetric import ec

from eidolon_ops import environment, lan_observation, probes, source_assets
from eidolon_ops.config import OperationsConfig
from eidolon_ops.errors import InstallInputError, OperationsError
from eidolon_ops.host_identity import HostIdentityError, HostLanIdentity, derive_host_lan_identity
from eidolon_ops.hub_assets import (
    hub_settings_are_bound,
    hub_settings_template,
    render_hub_settings,
)
from eidolon_ops.install_inputs import validate_install_input_contract
from eidolon_ops.owner_domain_assets import (
    OwnerDomainAssetError,
    OwnerDomainAssets,
    ensure_owner_domain_assets,
)
from eidolon_ops.paths import AppAccess, HostProfile
from eidolon_ops.private_files import atomic_private_file
from eidolon_ops.process import ProcessRunner, checked
from eidolon_ops.readiness import (
    CHANNEL_AGENT_NAME,
    DEFAULT_CHANNEL_SETTLE_SECONDS,
    HostKind,
    ReadinessFact,
    is_ready,
)
from eidolon_ops.source_schema import migrate_data_schema


class LocalProductSource:
    """Generate, validate and attest one source-run profile."""

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

    # -- materialization -----------------------------------------------------

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
        self._make_roots()
        source_inputs = next(iter(self.config.install_files.values())).parent
        self._adopt_host_identity(source_inputs)
        self._ensure_owner_domain_assets()

        expected = {
            **self._rendered_environment(source_inputs),
            **self._rendered_settings(source_inputs),
        }
        for destination, content in expected.items():
            atomic_private_file(destination, content)
        migrate_data_schema(self.profile, self.config, self.runner)
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
            *(root / "env" / name for name in source_assets.ENV_NAMES),
            *(root / "settings" / name for name in source_assets.SETTING_INPUT_NAMES),
            *(root / "settings" / name for name in source_assets.KERNEL_SETTINGS),
            root / "settings" / source_assets.HUB_SETTINGS_NAME,
            root / "settings/channel-provider.yaml",
            root / "settings/eidolond.yaml",
            root / "settings/ports.yaml",
            root / "settings/services.yaml",
            root / "settings/livekit.yaml",
            root / "product-source.env",
            self._host_identity_path(),
            self._hub_certificate_path(),
            self._hub_private_key_path(),
            self._owner_descriptor_path(),
            self._owner_root_certificate_path(),
            self._authority_signing_certificate_path(),
        ]
        missing = [str(path) for path in required if not path.is_file() or path.is_symlink()]
        if missing:
            raise OperationsError("Mac product-source inputs are missing: " + ", ".join(missing))
        unsafe = [str(path) for path in required if stat.S_IMODE(path.stat().st_mode) != 0o600]
        if unsafe:
            raise OperationsError(
                "Mac product-source inputs have unsafe modes: " + ", ".join(unsafe)
            )
        if self._host_identity_path().stat().st_size != 32:
            raise OperationsError("Mac Host Identity must contain exactly 32 bytes")
        return {
            "status": "compatible",
            "profile": "product-source",
            "generated_root": str(root),
            "services": 15,
            "redaction": "generated credentials are not returned",
        }

    def _make_roots(self) -> None:
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
        for directory in (paths.config_root, paths.config_root / "env", paths.config_root / "tls"):
            os.chmod(directory, 0o700)

    def _adopt_host_identity(self, source_inputs: Path) -> None:
        destination = self._host_identity_path()
        if not destination.exists():
            atomic_private_file(
                destination, source_inputs.joinpath("host_identity.ed25519").read_bytes()
            )
            return
        if (
            destination.is_symlink()
            or not destination.is_file()
            or stat.S_IMODE(destination.stat().st_mode) != 0o600
            or destination.stat().st_size != 32
        ):
            raise OperationsError("existing Mac Host Identity is unsafe or invalid")

    def _rendered_environment(self, source_inputs: Path) -> dict[Path, bytes]:
        root = self.profile.paths.config_root
        app = self._require_app_access()
        owner_domain_id = self._owner_domain_id()
        rendered: dict[Path, bytes] = {}
        for name in source_assets.ENV_NAMES:
            value = source_assets.translate_fhs(
                self.profile, source_inputs.joinpath(name).read_text(encoding="utf-8")
            )
            if self.profile.foundation_mode == "external" and name in {
                "channel.env",
                "livekit.env",
            }:
                value = environment.replace(
                    value,
                    source_assets.external_livekit_credentials(
                        self.profile.external_livekit_config
                    ),
                    label="generated environment",
                )
            if name == "local-api.env":
                value = environment.merge(
                    value,
                    {
                        "EIDOLON_LOCAL_API_OWNER_DOMAIN_ID": owner_domain_id,
                        "EIDOLON_LOCAL_API_OWNER_DOMAIN_DESCRIPTOR_URI": self._descriptor_uri(),
                        "EIDOLON_LOCAL_API_OWNER_DOMAIN_DESCRIPTOR": str(
                            self._owner_descriptor_path()
                        ),
                        "EIDOLON_LOCAL_API_OWNER_ROOT_CERTIFICATE": str(
                            self._owner_root_certificate_path()
                        ),
                        "EIDOLON_LOCAL_API_AUTHORITY_SIGNING_CERTIFICATE": str(
                            self._authority_signing_certificate_path()
                        ),
                    },
                    label="generated environment",
                )
            if name == "channel.env":
                value = environment.merge(
                    value,
                    {
                        "EIDOLON_LIVEKIT_CLIENT_URL": app.livekit_client_url,
                        "EIDOLON_CHANNEL_PROVIDER_ALLOW_INSECURE_LAN_CLIENT_URL": (
                            "1" if app.allow_insecure_livekit else "0"
                        ),
                    },
                    label="generated environment",
                )
            rendered[root / "env" / name] = value.encode("utf-8")
        rendered[root / "product-source.env"] = source_assets.profile_environment(
            self.profile,
            self.config,
            hub_certificate=self._hub_certificate_path(),
            hub_private_key=self._hub_private_key_path(),
            lan_ipv4=(
                str(app.lan_ipv4)
                if app.lan_ipv4 is not None
                else lan_observation.observed_lan_address(self.runner)
            ),
            hub_https_port=app.hub_https_port,
        ).encode("utf-8")
        return rendered

    def _rendered_settings(self, source_inputs: Path) -> dict[Path, bytes]:
        root = self.profile.paths.config_root
        app = self._require_app_access()
        identity = self._host_lan_identity()
        owner_domain_id = self._owner_domain_id()
        owner_domain_generation = self._owner_domain_generation()
        rendered: dict[Path, bytes] = {}
        for name in source_assets.SETTING_INPUT_NAMES:
            value = source_assets.translate_fhs(
                self.profile, source_inputs.joinpath(name).read_text(encoding="utf-8")
            )
            if name == "memory.yaml" and "\nsupervisor:\n" not in value:
                value = value.rstrip() + source_assets.memory_supervisor_block()
            rendered[root / "settings" / name] = value.encode("utf-8")
        for name, source_path in source_assets.KERNEL_SETTINGS.items():
            rendered[root / "settings" / name] = source_assets.translate_fhs(
                self.profile,
                self._read_exact_file(
                    "eidolon_kernel",
                    self.config.sources["eidolon_kernel"].revision,
                    source_path,
                ),
            ).encode("utf-8")
        # The same template a product Host is sent, resolved from the same
        # pinned commits, so a source run cannot be started from a Hub profile
        # no Host would get.
        template = hub_settings_template(self._source_revisions(), self._read_exact_file)
        rendered[root / "settings" / source_assets.HUB_SETTINGS_NAME] = source_assets.translate_fhs(
            self.profile,
            render_hub_settings(
                template.text,
                owner_domain_id,
                owner_domain_generation,
                identity,
                app.hub_https_port,
            ),
        ).encode("utf-8")
        rendered[root / "settings/channel-provider.yaml"] = source_assets.translate_fhs(
            self.profile,
            self._read_exact_file(
                "eidolon_channel",
                self.config.sources["eidolon_channel"].revision,
                "config/channel-provider.yaml",
            ),
        ).encode("utf-8")
        rendered[root / "settings/eidolond.yaml"] = source_assets.eidolond_settings(
            self.profile
        ).encode("utf-8")
        rendered[root / "settings/ports.yaml"] = source_assets.admin_ports_yaml().encode("utf-8")
        rendered[root / "settings/services.yaml"] = source_assets.admin_services_yaml().encode(
            "utf-8"
        )
        livekit_template = source_assets.ASSETS / "livekit.yaml"
        rendered[root / "settings/livekit.yaml"] = source_assets.translate_ports(
            livekit_template.read_text(encoding="utf-8")
        ).encode("utf-8")
        return rendered

    # -- health --------------------------------------------------------------

    def health(self, *, wait_seconds: float = 0) -> dict[str, object]:
        ports = source_assets.PORTS
        endpoints = {
            "nats": f"http://127.0.0.1:{ports['nats_http']}/healthz",
            "livekit": f"http://127.0.0.1:{ports['livekit']}/",
            "data": f"http://127.0.0.1:{ports['data']}/health",
            "data-workspace": f"http://127.0.0.1:{ports['data_workspace']}/health",
            "hub": f"http://127.0.0.1:{ports['hub']}/health",
            "kernel": f"http://127.0.0.1:{ports['kernel']}/health",
            "admin": f"http://127.0.0.1:{ports['admin']}/healthz",
            "local-api": f"https://127.0.0.1:{ports['local_api']}/healthz",
            "memory": f"http://127.0.0.1:{ports['memory_discovery']}/api/discovery/agent-routing",
            "agent": f"http://127.0.0.1:{ports['agent_http']}/readyz",
            "channel-provider": f"http://127.0.0.1:{ports['channel_provider']}/health",
        }
        socket_path = self.profile.paths.runtime_root / "system.sock"

        def observe() -> dict[str, object]:
            checks: dict[str, object] = {
                name: probes.http_health(url) for name, url in endpoints.items()
            }
            checks["eidolond"] = probes.unix_http_health(socket_path)
            return {"healthy": all(bool(item["healthy"]) for item in checks.values()), **checks}

        report = probes.settle(
            observe, lambda item: bool(item["healthy"]), seconds=wait_seconds
        )
        healthy = bool(report.pop("healthy"))
        return {
            "status": "healthy" if healthy else "degraded",
            "foundation_mode": self.profile.foundation_mode,
            "checks": report,
        }

    def app_ready(self) -> dict[str, object]:
        """Attest every fact the readiness contract expects of a source run."""

        app = self._require_app_access()
        identity = self._host_lan_identity()
        owner_domain_id = self._owner_domain_id()
        owner_domain_generation = self._owner_domain_generation()
        ports = source_assets.PORTS
        backend = self.health()
        interface_addresses = lan_observation.interface_addresses(self.runner)
        address = (
            str(app.lan_ipv4)
            if app.lan_ipv4 is not None
            else lan_observation.observed_lan_address(self.runner, interface_addresses)
        )
        local_api = probes.http_health(f"https://{address}:{ports['local_api']}/healthz")
        hub = probes.http_health(f"https://{address}:{app.hub_https_port}/health")
        livekit_origin = urlparse(app.livekit_client_url)
        livekit = probes.tcp_health(
            address,
            livekit_origin.port or (443 if livekit_origin.scheme == "wss" else 80),
        )
        channel = probes.settle(
            lambda: probes.channel_worker_report(
                ports["channel_worker"], agent_name=CHANNEL_AGENT_NAME
            ),
            lambda report: bool(report["healthy"]) and bool(report["dispatch_identity"]),
            seconds=DEFAULT_CHANNEL_SETTLE_SECONDS,
        )
        local_api_values = environment.parse(
            (self.profile.paths.config_root / "env/local-api.env").read_text(encoding="utf-8"),
            label="generated env file",
            key=environment.OPS_KEY,
        )
        channel_values = environment.parse(
            (self.profile.paths.config_root / "env/channel.env").read_text(encoding="utf-8"),
            label="generated service env file",
            key=environment.SERVICE_KEY,
        )
        settings = (self.profile.paths.config_root / "settings/hub.yaml").read_text(
            encoding="utf-8"
        )
        checks = {
            str(ReadinessFact.BACKEND_HEALTHY): backend["status"] == "healthy",
            # A declared address must still be one this Host has; a discovered
            # one is true by construction, so what carries meaning is whether
            # devices can find the Host under its published name.
            str(ReadinessFact.LAN_ADDRESS_OBSERVED): (
                bool(address) and address in interface_addresses
            ),
            str(ReadinessFact.LAN_NAME_RESOLVES): lan_observation.name_resolves_to(
                self.runner, identity.hub_hostname, address
            ),
            str(ReadinessFact.HOST_IDENTITY_MATERIAL): self._host_identity_is_private(),
            str(ReadinessFact.HUB_TLS_IDENTITY): self._hub_tls_matches(identity),
            str(ReadinessFact.HUB_SETTINGS_BOUND): hub_settings_are_bound(
                settings,
                owner_domain_id,
                owner_domain_generation,
                identity,
                app.hub_https_port,
            ),
            str(ReadinessFact.HUB_LAN_REACHABLE): bool(hub["healthy"]),
            str(ReadinessFact.LOCAL_API_REACHABLE): bool(local_api["healthy"]),
            str(ReadinessFact.LOCAL_API_TARGETS_HUB): (
                local_api_values.get("EIDOLON_LOCAL_API_OWNER_DOMAIN_ID")
                == owner_domain_id
                and local_api_values.get("EIDOLON_LOCAL_API_OWNER_DOMAIN_DESCRIPTOR_URI")
                == self._descriptor_uri()
                and local_api_values.get("EIDOLON_LOCAL_API_OWNER_DOMAIN_DESCRIPTOR")
                == str(self._owner_descriptor_path())
                and local_api_values.get("EIDOLON_LOCAL_API_OWNER_ROOT_CERTIFICATE")
                == str(self._owner_root_certificate_path())
                and local_api_values.get("EIDOLON_LOCAL_API_AUTHORITY_SIGNING_CERTIFICATE")
                == str(self._authority_signing_certificate_path())
            ),
            str(ReadinessFact.LIVEKIT_CLIENT_ORIGIN): (
                channel_values.get("EIDOLON_LIVEKIT_CLIENT_URL") == app.livekit_client_url
                and channel_values.get("EIDOLON_CHANNEL_PROVIDER_ALLOW_INSECURE_LAN_CLIENT_URL")
                == ("1" if app.allow_insecure_livekit else "0")
            ),
            str(ReadinessFact.LIVEKIT_LAN_REACHABLE): bool(livekit["healthy"]),
            # WebRTC has to advertise a reachable address, so this compares
            # against the one the Host actually answers on, not a declaration.
            str(ReadinessFact.LIVEKIT_RTC_ADVERTISED): (
                lan_observation.livekit_node_ip(self.profile.external_livekit_config) == address
            ),
            str(ReadinessFact.CHANNEL_WORKER_HEALTHY): bool(channel["healthy"]),
            str(ReadinessFact.CHANNEL_WORKER_DISPATCH_IDENTITY): bool(
                channel["dispatch_identity"]
            ),
        }
        healthy = is_ready(HostKind.SOURCE, checks)
        return {
            "status": "app_ready" if healthy else "degraded",
            "host_id": self.profile.host_id,
            "lan_ipv4": address,
            "checks": checks,
            "channel_worker": channel,
            "endpoints": {
                "local_api": f"https://{address}:{ports['local_api']}",
                "hub": identity.hub_origin(app.hub_https_port),
                "livekit": app.livekit_client_url,
            },
            "scope": (
                "Host-side LAN contract only; a Pad conversation remains the final external gate"
            ),
        }

    # -- observation ---------------------------------------------------------

    def _hub_tls_matches(self, identity: HostLanIdentity) -> bool:
        try:
            leaf = x509.load_pem_x509_certificate(self._hub_certificate_path().read_bytes())
            root = x509.load_pem_x509_certificate(
                self._owner_root_certificate_path().read_bytes()
            )
            sans = leaf.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
            public = root.public_key()
            if not isinstance(public, ec.EllipticCurvePublicKey):
                return False
            public.verify(
                leaf.signature,
                leaf.tbs_certificate_bytes,
                ec.ECDSA(leaf.signature_hash_algorithm),
            )
        except (OSError, ValueError, x509.ExtensionNotFound):
            return False
        return sans.get_values_for_type(x509.DNSName) == [identity.hub_hostname]

    def _host_identity_is_private(self) -> bool:
        path = self._host_identity_path()
        return (
            not path.is_symlink()
            and path.is_file()
            and stat.S_IMODE(path.stat().st_mode) == 0o600
            and path.stat().st_size == 32
        )

    # -- source and identity -------------------------------------------------

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

    def _source_revisions(self) -> dict[str, str]:
        return {source_id: source.revision for source_id, source in self.config.sources.items()}

    def _require_app_access(self) -> AppAccess:
        if self.profile.app is None:
            raise OperationsError("Mac product-source profile requires an app access contract")
        return self.profile.app

    def _descriptor_uri(self) -> str:
        app = self._require_app_access()
        return (
            self._host_lan_identity().hub_origin(app.hub_https_port)
            + "/api/device-onboarding/v1/descriptor"
        )

    def _host_identity_path(self) -> Path:
        return self.profile.paths.bootstrap_state_root / "host_identity.ed25519"

    def _host_lan_identity(self) -> HostLanIdentity:
        path = self._host_identity_path()
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

    def _owner_material_root(self) -> Path:
        return self.profile.paths.config_root / "owner-domain-private"

    def _owner_descriptor_path(self) -> Path:
        return self.profile.paths.config_root / "owner-domain/owner_domain_descriptor.json"

    def _owner_root_certificate_path(self) -> Path:
        return self.profile.paths.config_root / "owner-domain/owner_domain_root_ca.pem"

    def _authority_signing_certificate_path(self) -> Path:
        return self.profile.paths.config_root / "owner-domain/authority_signing_certificate.pem"

    def _owner_domain_id(self) -> str:
        try:
            value = json.loads(self._owner_descriptor_path().read_text(encoding="utf-8"))
            owner_domain_id = value["owner_domain_id"]
        except (OSError, KeyError, TypeError, ValueError) as exc:
            raise OperationsError("Mac Owner Domain descriptor is unreadable") from exc
        if not isinstance(owner_domain_id, str) or not owner_domain_id:
            raise OperationsError("Mac Owner Domain descriptor has no Owner identity")
        return owner_domain_id

    def _owner_domain_generation(self) -> int:
        try:
            value = json.loads(self._owner_descriptor_path().read_text(encoding="utf-8"))
            generation = value["owner_domain_generation"]
        except (OSError, KeyError, TypeError, ValueError) as exc:
            raise OperationsError("Mac Owner Domain generation is unreadable") from exc
        if not isinstance(generation, int) or generation < 1:
            raise OperationsError("Mac Owner Domain generation is invalid")
        return generation

    def _ensure_owner_domain_assets(self) -> OwnerDomainAssets:
        try:
            assets = ensure_owner_domain_assets(
                self._owner_material_root(),
                self._host_lan_identity(),
                self._require_app_access().hub_https_port,
            )
        except OwnerDomainAssetError as exc:
            raise OperationsError(f"Mac Owner Domain material is {exc}") from exc
        for path, value in (
            (self._hub_certificate_path(), assets.tls_certificate),
            (self._hub_private_key_path(), assets.tls_private_key),
            (self._owner_descriptor_path(), assets.descriptor),
            (self._owner_root_certificate_path(), assets.owner_root_certificate),
            (
                self._authority_signing_certificate_path(),
                assets.authority_signing_certificate,
            ),
        ):
            atomic_private_file(path, value)
        return assets

    # Kept as an internal call alias until the local-product tests complete the
    # same coordinated cutover; it now issues Owner-scoped material, never a
    # self-signed Host trust anchor.
    def _ensure_hub_tls_identity(self) -> None:
        self._ensure_owner_domain_assets()
