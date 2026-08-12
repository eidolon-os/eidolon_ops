"""The layer Ops owns on top of a release, and the payload every Host gets.

A release brings components. What fronts them on the LAN — the ingress unit,
the Hub settings rendered for this Host, the TLS material bound to its identity
— is derived here from the operator's own inputs, staged privately, and
refreshed without a reinstall.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

from eidolon_ops.config import INSTALL_FILE_NAMES, OperationsConfig
from eidolon_ops.environment import EnvironmentFileError
from eidolon_ops.errors import InstallInputError, OperationsError
from eidolon_ops.host_application import (
    HOST_APPLICATION_STAGE_NAMES,
    HostApplicationError,
    HostApplicationMaterializer,
)
from eidolon_ops.hub_assets import HubAssetError
from eidolon_ops.paths import AppAccess
from eidolon_ops.readiness import product_payload
from eidolon_ops.transport import SSHTransport

#: Everything the Host-application and install-input materializers refuse with.
#: They are separate modules with separate concerns; to an operator they are one
#: failure — this deployer could not produce a Host's assets.
ASSET_ERRORS = (
    EnvironmentFileError,
    HostApplicationError,
    HubAssetError,
    InstallInputError,
)

_STAGED_INSTALL_NAMES = {
    "data_env": "data.env",
    "hub_env": "hub.env",
    "kernel_env": "kernel.env",
    "admin_env": "admin.env",
    "local_api_env": "local-api.env",
    "bootstrap_env": "bootstrap.env",
    "host_identity": "host_identity.ed25519",
    "agent_env": "agent.env",
    "channel_env": "channel.env",
    "memory_env": "memory.env",
    "livekit_env": "livekit.env",
    "agent_settings": "agent.yaml",
    "channel_settings": "channel.yaml",
    "memory_settings": "memory.yaml",
}
#: Which port each component binds. Ops owns this file — Admin builds its
#: service catalog from it and interpolates the EIDOLON_* variables its
#: services.yaml names — so a Host is sent this one rather than carrying a
#: restatement of it that could drift.
_PORT_REGISTRY = Path(__file__).resolve().parents[2] / "config" / "ports.yaml"
#: Environment files whose values name this Host rather than a credential.
_HOST_BOUND_INPUTS = frozenset({"local_api_env", "channel_env"})


class HostLayer:
    def __init__(
        self,
        config: OperationsConfig,
        transport: SSHTransport,
        app: AppAccess | None,
        *,
        read_exact_source_file,
    ) -> None:
        self.config = config
        self.transport = transport
        self.app = app
        self._read_exact_source_file = read_exact_source_file

    def target_payload(self) -> dict[str, object]:
        """Everything a Host is told about itself, in one reviewed shape."""

        try:
            port_registry = _PORT_REGISTRY.read_text(encoding="utf-8")
        except OSError as exc:
            raise OperationsError(
                f"Ops-owned port registry is unreadable: {_PORT_REGISTRY}"
            ) from exc
        payload: dict[str, object] = {
            "units": list(self.config.units),
            "port_registry": port_registry,
            # What this Host is asked to attest, and what it needs to attest
            # it. The check set has one author; a copy compiled into the
            # injected agent would be the one nobody thinks to update.
            "readiness": product_payload(
                settle_seconds=self.config.host.readiness_timeout_seconds
            ),
            "readiness_timeout_seconds": self.config.host.readiness_timeout_seconds,
            "data": {
                "system_database": str(self.config.data.system_database),
                "object_store": str(self.config.data.object_store),
                "bootstrap_database": str(self.config.data.bootstrap_database),
                "deployment_evidence": str(self.config.data.deployment_evidence),
            },
        }
        identity_path = self.config.install_files.get("host_identity")
        if self.app is not None and identity_path is not None and identity_path.is_file():
            try:
                payload["app"] = self.materializer().public_contract()
            except ASSET_ERRORS as exc:
                raise OperationsError(str(exc)) from exc
        return payload

    def public_contract(self) -> dict[str, object]:
        return self.materializer().public_contract()

    def materializer(self) -> HostApplicationMaterializer:
        if self.app is None:
            raise OperationsError("Pi Host operations require the unified [app] access contract")
        ingress = Path(__file__).with_name("lan_ingress.py")
        try:
            source = ingress.read_bytes()
        except OSError as exc:
            raise OperationsError("deployment-owned LAN ingress source is missing") from exc
        return HostApplicationMaterializer(self.config, self.app, source)

    def prepare(self):
        materializer = self.materializer()
        kernel = self.config.sources["eidolon_kernel"]
        template = self._read_exact_source_file(
            "eidolon_kernel", kernel.revision, "config/hub.systemd.example.yaml"
        )
        try:
            return materializer.prepare(template)
        except ASSET_ERRORS as exc:
            raise OperationsError(str(exc)) from exc

    def stage_install_files(
        self,
        release_id: str,
        stage: str,
        *,
        names: tuple[str, ...] = INSTALL_FILE_NAMES,
    ) -> None:
        self.transport.run_agent("cleanup-stage", {"release_id": release_id})
        self.transport.run(
            ("/usr/bin/install", "-d", "-m", "0700", stage),
            sudo=False,
            operation="private secret staging directory creation",
        )
        # The full name set gates the private inputs, which are written once.
        # The Host layer is derived rather than kept, so it is staged whenever
        # this profile has one — a refresh asks for it without the credentials.
        application = self.prepare() if self.app else None
        with tempfile.TemporaryDirectory(prefix="eidolon-host-application-") as temporary_value:
            temporary = Path(temporary_value)
            for name in names:
                source = self.config.install_files[name]
                if application is not None and name in _HOST_BOUND_INPUTS:
                    rendered = self.materializer().render_environment(
                        _STAGED_INSTALL_NAMES[name], source.read_text(encoding="utf-8")
                    )
                    source = temporary / _STAGED_INSTALL_NAMES[name]
                    source.write_text(rendered, encoding="utf-8")
                    os.chmod(source, 0o600)
                self.transport.upload(source, f"{stage}/{_STAGED_INSTALL_NAMES[name]}")
            if application is not None:
                for name in HOST_APPLICATION_STAGE_NAMES:
                    source = temporary / name
                    source.write_bytes(application.files[name])
                    os.chmod(source, 0o600)
                    self.transport.upload(source, f"{stage}/{name}")

    def refresh(self, release_id: str) -> dict[str, object]:
        stage = f"/var/tmp/eidolon-secrets-{release_id}"
        self.stage_install_files(release_id, stage, names=("host_identity",))
        return self.transport.run_agent(
            "refresh-host-application",
            {**self.target_payload(), "release_id": release_id},
            timeout=180,
        )
