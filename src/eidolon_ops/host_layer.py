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

from eidolon_ops.component_contract import read_component_contracts
from eidolon_ops.config import INSTALL_FILE_NAMES, OperationsConfig
from eidolon_ops.environment import EnvironmentFileError
from eidolon_ops.errors import InstallInputError, OperationsError
from eidolon_ops.host_application import (
    HostApplicationError,
    HostApplicationMaterializer,
)
from eidolon_ops.hub_assets import HubAssetError, hub_settings_template
from eidolon_ops.paths import AppAccess
from eidolon_ops.private_inputs import INSTALL_DESTINATION_NAMES
from eidolon_ops.readiness import product_payload
from eidolon_ops.source_assets import PORTS
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

#: The one filename each install input is staged and written under. Imported
#: rather than restated: this used to be a second copy, identical byte for
#: byte, and two copies of a mapping are a mapping that will eventually
#: disagree with itself in a way no test was watching for.
_STAGED_INSTALL_NAMES = INSTALL_DESTINATION_NAMES

#: The staged name of an input the operator does not supply.
#:
#: Kept out of `INSTALL_DESTINATION_NAMES` because that table has a second job:
#: it *is* the set every profile must provide a file for
#: (`install_inputs.py:180`). Adding an optional entry there turned it into a
#: requirement, which is the opposite of optional.
_FACTORY_SETUP_CODE_STAGED_NAME = "factory_setup_code"
#: Which port each component binds. Ops owns this file — Admin builds its
#: service catalog from it and interpolates the EIDOLON_* variables its
#: services.yaml names — so a Host is sent this one rather than carrying a
#: restatement of it that could drift.
#: Inside the package, addressed by name rather than by walking up to a
#: repository root. It is an asset Ops ships, so it travels with Ops whether
#: this is a checkout or an installed wheel.
_PORT_REGISTRY = Path(__file__).with_name("assets") / "ports.yaml"
#: Environment files whose values name this Host rather than a credential.
HOST_BOUND_INPUTS = frozenset({"local_api_env", "channel_env"})
# Non-secret product settings are derived from the exact component revisions
# selected for this release. They belong to every cutover, not only first
# install; the Host snapshot makes replacing them reversible with the code.
PRODUCT_SETTINGS_INPUTS = ("agent_settings", "channel_settings", "memory_settings")


class HostLayer:
    def __init__(
        self,
        config: OperationsConfig,
        transport: SSHTransport,
        app: AppAccess | None,
        *,
        read_exact_source_file,
        source_revisions,
    ) -> None:
        self.config = config
        self.transport = transport
        self.app = app
        self._read_exact_source_file = read_exact_source_file
        #: Asked rather than read off the configuration: which commit a source
        #: ships is resolved once per operation, and a second reader deriving
        #: its own answer is the shape of the bug this replaced.
        self._source_revisions = source_revisions

    def _port_registry(self) -> str:
        """The registry a Host is given: the reviewed baseline, plus the port
        roles this Host's capabilities actually bring.

        Two artifacts have been living in one file. The baseline is curated by
        hand and aggregated from sub-project settings, keyed the way Admin reads
        it (`hub.api.port`). The port *roles* are derived: a component's own
        `ops/component.toml` reserves one, and Ops selects it by capability —
        so `asr_stream` exists on a board with an NPU and on no other Host.

        Only the derived half is added here, under its own key, in the flat
        shape it is derived in. Folding it into the curated nesting would mean
        inventing a role-name-to-path rule, and there is none to invent: the
        existing keys are hand-chosen (`nats_http` lives at `nats.http_port`).
        Admin reads this file key by key and ignores what it does not know.

        Why it has to reach the Host at all: a component that reserves a port
        is the only thing that should state the number, and something on the
        Host has to be able to ask. Otherwise every consumer writes 8768 into
        its own configuration — which is the class of defect this repository
        spent a day removing.
        """

        try:
            baseline = _PORT_REGISTRY.read_text(encoding="utf-8")
        except OSError as exc:
            raise OperationsError(
                f"Ops-owned port registry is unreadable: {_PORT_REGISTRY}"
            ) from exc
        roles = self._capability_port_roles()
        if not roles:
            return baseline
        lines = [
            "",
            "# Derived, not curated: the port roles the components' own contracts",
            "# reserve for the capabilities this Host declares. Absent on a Host",
            "# that declares nothing, which is why this section is written here",
            "# rather than kept in the file above.",
            "port_roles:",
        ]
        lines.extend(f"  {role}: {port}" for role, port in sorted(roles.items()))
        return baseline.rstrip("\n") + "\n" + "\n".join(lines) + "\n"

    def _capability_port_roles(self) -> dict[str, int]:
        """The roles a capability adds, and only those.

        The baseline file already states every port every Host binds, so
        repeating those here would give two answers to one question. What it
        cannot state is a port that exists only on some Hosts.
        """

        sources = {source_id: source.path for source_id, source in self.config.sources.items()}
        without = read_component_contracts(sources, frozenset()).port_roles
        with_capabilities = read_component_contracts(
            sources, self.config.capabilities
        ).port_roles
        return {
            role: port
            for role, port in with_capabilities.items()
            if role not in without
        }

    def target_payload(self) -> dict[str, object]:
        """Everything a Host is told about itself, in one reviewed shape."""

        port_registry = self._port_registry()
        payload: dict[str, object] = {
            "units": list(self.config.units),
            # Sent so the agent can derive the unit set itself rather than
            # take the list on trust. It holds its own copy of what each
            # capability adds; agreeing is the check.
            "capabilities": sorted(self.config.capabilities),
            "port_registry": port_registry,
            # Where memory's supervisor answers. A backup asks it for a
            # snapshot of each space rather than copying a palace the agent
            # does not understand, and the injected agent carries no YAML
            # parser to read the registry above — so the assignment is sent
            # from the one place that owns it.
            "memory_admin_url": f"http://127.0.0.1:{PORTS['memory_admin']}",
            # What this Host is asked to attest, and what it needs to attest
            # it. The check set has one author; a copy compiled into the
            # injected agent would be the one nobody thinks to update.
            "readiness": product_payload(settle_seconds=self.config.host.readiness_timeout_seconds),
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
        try:
            template = hub_settings_template(
                self._source_revisions(),
                self._read_exact_source_file,
            )
            return materializer.prepare(template.text)
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
            factory_code = self._factory_setup_code()
            if factory_code is not None:
                # Rendered, not collected. The value already lives in the
                # profile as `app.setup_code`, and asking the operator for a
                # second copy of it in a file is the drift this whole change
                # exists to stop being possible.
                staged = temporary / _FACTORY_SETUP_CODE_STAGED_NAME
                staged.write_text(factory_code + "\n", encoding="utf-8")
                os.chmod(staged, 0o600)
                self.transport.upload(
                    staged, f"{stage}/{_FACTORY_SETUP_CODE_STAGED_NAME}"
                )
            for name in names:
                source = self.config.install_files[name]
                if application is not None and name in HOST_BOUND_INPUTS:
                    rendered = self.materializer().render_environment(
                        _STAGED_INSTALL_NAMES[name], source.read_text(encoding="utf-8")
                    )
                    source = temporary / _STAGED_INSTALL_NAMES[name]
                    source.write_text(rendered, encoding="utf-8")
                    os.chmod(source, 0o600)
                self.transport.upload(source, f"{stage}/{_STAGED_INSTALL_NAMES[name]}")
            if application is not None:
                for name in sorted(application.files):
                    source = temporary / name
                    source.write_bytes(application.files[name])
                    os.chmod(source, 0o600)
                    self.transport.upload(source, f"{stage}/{name}")

    def _factory_setup_code(self) -> str | None:
        """The code this Host was manufactured with, if this profile names one.

        Delivering it is what lets an unclaimed Host stand up the claim window
        the code on its chassis is for, instead of waiting for someone to reach
        its control socket (ADR-0007). A profile that names no code delivers no
        file, and such a Host behaves exactly as it did before — which is also
        how a development fleet sharing one code stays safe, since an
        unexpiring window plus a code everyone knows is an open door.
        """

        app = self.app
        return None if app is None else app.setup_code


    def refresh(self, release_id: str) -> dict[str, object]:
        stage = f"/var/tmp/eidolon-secrets-{release_id}"
        self.stage_install_files(
            release_id,
            stage,
            names=(
                "host_identity",
                *sorted(HOST_BOUND_INPUTS),
                *PRODUCT_SETTINGS_INPUTS,
            ),
        )
        return self.transport.run_agent(
            "refresh-host-application",
            {**self.target_payload(), "release_id": release_id},
            timeout=180,
        )
