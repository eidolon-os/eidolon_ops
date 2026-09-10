"""Materialize and attest the commit-pinned topology for a macOS source run."""

from __future__ import annotations

import json
import os
import shutil
import stat
from collections.abc import Callable, Mapping, Sequence
from dataclasses import fields
from pathlib import Path
from urllib.parse import urlparse

from cryptography import x509
from cryptography.hazmat.primitives.asymmetric import ec

from eidolon_ops import environment, lan_observation, probes, source_assets
from eidolon_ops.config import OperationsConfig
from eidolon_ops.errors import InstallInputError, OperationsError
from eidolon_ops.host_identity import (
    HostIdentityError,
    HostLanIdentity,
    derive_host_lan_identity,
    livekit_client_url,
    livekit_client_url_at,
)
from eidolon_ops.hostagent.authority_reset import lineage_evidence
from eidolon_ops.hostagent.kernel_schema import (
    absent_document,
    acknowledged_selections,
    forgets_uncounted,
    kernel_evidence,
    plan_document,
    refuse_unless_warranted,
    require_regular_file,
    set_aside,
    set_aside_suffix,
)
from eidolon_ops.hostagent.primitives import TargetError
from eidolon_ops.hub_assets import (
    hub_settings_are_bound,
    hub_settings_template,
    render_hub_settings,
)
from eidolon_ops.install_inputs import validate_install_input_contract
from eidolon_ops.owner_domain_assets import (
    AuthorityDecision,
    OwnerDomainAssetError,
    OwnerDomainAssets,
    authority_lineage,
    authority_recovery_required,
    decide_owner_authority,
    ensure_owner_domain_assets,
    mark_authority_bootstrapped,
)
from eidolon_ops.owner_domain_assets import (
    reset_owner_authority as advance_owner_authority,
)
from eidolon_ops.paths import AppAccess, HostProfile
from eidolon_ops.private_files import atomic_private_file
from eidolon_ops.process import ProcessRunner, checked
from eidolon_ops.readiness import (
    CHANNEL_AGENT_NAME,
    DEFAULT_CHANNEL_SETTLE_SECONDS,
    SETUP_READINESS_SETTLE_SECONDS,
    HostKind,
    ReadinessFact,
    is_ready,
    setup_answer_is_settled,
    setup_readiness_evidence,
)
from eidolon_ops.source_resolution import SourceResolver
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
        self._sources: SourceResolver | None = None

    @property
    def sources(self) -> SourceResolver:
        """Which commit each checkout is on, resolved once for this run.

        Built on first use rather than in the constructor: the Mac adapter
        composes the profile before it has an operations config to hand, and a
        resolver bound to nothing is worse than one bound late.
        """

        if self._sources is None:
            self._sources = SourceResolver(self.config, self.runner, git=self.git)
        return self._sources

    # -- materialization -----------------------------------------------------

    def prepare(self) -> dict[str, object]:
        self._validate_exact_worktrees()
        try:
            # ``refresh_derived`` because this Host tracks its checkouts' HEAD:
            # any commit to a component's own `config/settings.yaml` moves what
            # the three derived settings inputs should say, and comparing them to
            # it instead of following it stopped every operation here until
            # someone ran a Pi-only command.
            contract = validate_install_input_contract(
                self.sources.resolved_config(),
                self._read_exact_file,
                verify_provider_sources=False,
                refresh_derived=True,
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
            **(
                {"refreshed_inputs": contract["refreshed"]}
                if isinstance(contract, dict) and contract.get("refreshed")
                else {}
            ),
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
            self._authority_bootstrap_path(),
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

    # -- kernel schema --------------------------------------------------------

    def kernel_schema_reset(
        self,
        *,
        apply: bool,
        forget_selections: int | None,
        forget_uncounted_selections: bool = False,
        quiesce: Callable[[], object] | None = None,
    ) -> dict[str, object]:
        """Set aside a Kernel authority this checkout's Kernel refuses to open.

        The narrow half of ``reset --wipe-authority-data``, and the reason this
        profile needed one. That flag is what a Host behind the Kernel schema had
        to reach for, and it destroys every authority on the machine and advances
        the Owner Domain generation — voiding every Claim — to move one file. Here
        the same Host loses one file, and keeps it.

        The workstation runs the Kernel out of its own worktree, so "the installed
        Kernel" is that checkout's interpreter. Same question, same answer, same
        gate: this refuses unless that Kernel says it will not open this database.
        """

        database = self._kernel_database_path()
        if not database.exists():
            return {"profile": "product-source", **absent_document(database)}
        try:
            require_regular_file(database, display=database)
            report = kernel_evidence(
                database=database, interpreter=self._kernel_interpreter_path()
            )
            plan = {
                "profile": "product-source",
                **plan_document(database=database, display=database, report=report),
            }
            if not apply:
                return plan
            refuse_unless_warranted(
                report,
                acknowledged=acknowledged_selections(
                    {"forget_selections": forget_selections}
                ),
                forget_uncounted=forgets_uncounted(
                    {"forget_uncounted_selections": forget_uncounted_selections}
                ),
            )
        except TargetError as exc:
            raise OperationsError(str(exc)) from exc
        suffix = set_aside_suffix()
        # Quiesced between the gate and the rename, never before it: supervisord's
        # children hold this file open, so renaming it underneath a running Kernel
        # leaves a process writing to a path nobody will read again — but taking a
        # Host down and *then* refusing would be the worse of the two orders.
        # Whoever knows how to stop this Host passes that in; this method knows
        # only which file moves.
        stopped = quiesce() if quiesce is not None else None
        try:
            renamed = set_aside(database, suffix=suffix)
        except TargetError as exc:
            raise OperationsError(str(exc)) from exc
        return {
            **plan,
            "status": "kernel_schema_reset",
            "set_aside_suffix": suffix,
            "renamed": renamed,
            "selections_destroyed": report.get("selections"),
            **({"stopped": stopped} if stopped is not None else {}),
            "next": "start this Host again; the Kernel builds an empty authority at the "
            "current schema and replays device mounts from the Hub Claim stream",
        }

    def _kernel_database_path(self) -> Path:
        """Where this profile's Kernel keeps its authority.

        The product location resolved against this profile's state root, the way
        ``_hub_database_path`` resolves Hub's — a second opinion about either
        would name a file no service writes.
        """

        return self.profile.paths.state_root / "eidolon-kernel.sqlite3"

    def _kernel_interpreter_path(self) -> Path:
        """The Kernel this Host actually runs, which here is a worktree.

        Asked rather than assumed for the same reason the product Host asks the
        release's interpreter: the answer to "will this Kernel open this file"
        has to come from the Kernel that is going to try.
        """

        source = self.config.sources.get("eidolon_kernel")
        if source is None:
            raise OperationsError(
                "this profile does not declare an eidolon_kernel source, so no Kernel "
                "can be asked what setting its authority aside would cost"
            )
        return Path(source.path) / ".venv/bin/python"

    # -- reset ---------------------------------------------------------------

    def reset(self, *, wipe_authority_data: bool, apply: bool) -> dict[str, object]:
        """Clear what this profile generated, and only that.

        The product Host has had this since the day it could be installed, and
        it is the missing half of a source run: Kernel and Hub both refuse to
        migrate a database they do not recognize — deliberately, since a guessed
        migration of an authority is worse than a refusal — so a Host that falls
        behind the schema cannot start and cannot be repaired. The only way out
        was to know which sqlite files to move aside by hand.

        What is cleared and what is kept are different questions from the Pi's,
        because the two Hosts hold different things in the same roles:

        * The code is the operator's own worktrees, not an installed release.
          Nothing here may touch them, and that is asserted rather than assumed.
        * ``owner-domain-private`` is this Host's Owner root key — the workstation
          material, which on a Pi install lives on the workstation and no reset
          deletes. Keeping it means a reset re-derives the same Owner Domain
          instead of quietly minting a new one that every enrolled device would
          then fail to recognize.
        * The Host identity under the Bootstrap roots is a separate ownership
          boundary on both Hosts, and stays. Retiring it is what
          ``init-inputs --new-identity`` is for.

        ``wipe_authority_data`` adds the state root: the system database, Hub,
        Kernel, Agent, Memory, NATS and the rest. That is the flag for a Host
        behind the schema, and it is irreversible.

        Nothing in the Bootstrap root has to be reached for this to leave a
        coherent Host, which is a property rather than an accident: Bootstrap
        records only what it can know by itself, so destroying the Data
        authority cannot leave it asserting something about a Workspace. It
        used to keep an Owner binding through exactly this operation, and a
        Host that had been through it refused every phone at setup forever.

        Irreversible in one more way than the file list shows. The state root is
        where this Host keeps both halves of its Owner Authority lineage
        evidence, so wiping it *is* a ``ResetAuthority``, and the next
        ``prepare`` advances ``owner_domain_generation`` accordingly — see
        ``_decided_owner_authority``. Every Claim and running credential issued
        under the destroyed generation is thereby void, which is the point of
        advancing rather than the cost of it: fenced off, they cannot be
        mistaken for the new authority's.
        """

        paths = self.profile.paths
        generated = [
            paths.config_root / "env",
            paths.config_root / "settings",
            paths.config_root / "tls",
            paths.config_root / "owner-domain",
            paths.config_root / "product-source.env",
            paths.runtime_root,
        ]
        authority = [paths.state_root] if wipe_authority_data else []
        targets = [*generated, *authority]
        self._require_removable(targets)
        present = [path for path in targets if path.exists() or path.is_symlink()]
        report: dict[str, object] = {
            "profile": "product-source",
            "wipe_authority_data": wipe_authority_data,
            "targets": [str(path) for path in targets],
            "present": [str(path) for path in present],
            "kept": [
                str(self._owner_material_root()),
                str(paths.bootstrap_state_root),
                str(paths.bootstrap_runtime_root),
                str(paths.log_root),
                str(paths.cache_root),
                *(
                    []
                    if wipe_authority_data
                    else [str(paths.state_root)]
                ),
            ],
            "note": (
                "the Owner Domain private material and the Host identity are kept, so "
                "prepare re-derives the same Owner Domain and the same Host name"
                + (
                    "; the Authority state behind them is destroyed, so prepare "
                    "advances owner_domain_generation and every existing device "
                    "Claim and credential becomes void"
                    if wipe_authority_data
                    else ""
                )
            ),
        }
        if not apply:
            return {**report, "status": "planned"}
        for path in present:
            if path.is_dir() and not path.is_symlink():
                shutil.rmtree(path)
            else:
                path.unlink()
        return {**report, "status": "reset", "removed": [str(path) for path in present]}

    def _require_removable(self, targets: Sequence[Path]) -> None:
        """Prove nothing here can reach code or an unrelated tree.

        A source run's roots live inside the workspace that holds the eight
        checkouts, so "under the install root" cannot be the test — it is true of
        everything. What must hold is that no target is a source worktree, or a
        parent of one, or a root the profile itself is anchored on.
        """

        paths = self.profile.paths
        protected = [
            paths.install_root,
            paths.current_root,
            self._owner_material_root(),
            paths.bootstrap_state_root,
            paths.bootstrap_runtime_root,
            *(Path(source.path) for source in self.config.sources.values()),
        ]
        for target in targets:
            for keep in protected:
                if target == keep or keep.is_relative_to(target):
                    raise OperationsError(
                        f"a source-run reset would remove something it does not own: "
                        f"{target} contains or is {keep}"
                    )
            if not any(
                target == root or target.is_relative_to(root)
                for root in (paths.config_root, paths.runtime_root, paths.state_root)
            ):
                raise OperationsError(
                    f"a source-run reset target is outside this profile's roots: {target}"
                )

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
            # Channel and LiveKit used to have their key pair replaced here with
            # whatever the already-running LiveKit had issued itself, because a
            # source run shared a server it did not start. Ops starts it now, and
            # renders its config from `livekit.env` at every start, so the pair
            # travels one way: install inputs -> env files -> server. Reading it
            # back out of the file Ops just wrote would be a cycle, and it made
            # the whole profile depend on that file already existing — which on a
            # clean Host it does not.
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
                        "EIDOLON_LIVEKIT_CLIENT_URL": self._livekit_client_url(app),
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
                    self.sources.revision("eidolon_kernel"),
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
                self.sources.revision("eidolon_channel"),
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
        # Asked over loopback, not over the LAN address: this is the Host being
        # asked about itself, and the answer must not depend on which interface
        # the question arrived on.
        setup = probes.settle(
            lambda: setup_readiness_evidence(
                probes.http_json(
                    f"https://127.0.0.1:{ports['local_api']}"
                    "/api/local/v1/setup/readiness"
                )
            ),
            setup_answer_is_settled,
            seconds=SETUP_READINESS_SETTLE_SECONDS,
        )
        hub = probes.http_health(f"https://{address}:{app.hub_https_port}/health")
        livekit_origin = urlparse(self._livekit_client_url(app))
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
        media_service = probes.unix_http_json(
            self.profile.paths.runtime_root / "system.sock",
            "/api/system/v1/services/livekit",
        ) or {}
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
            str(ReadinessFact.HOST_SETUP_COMPLETABLE): bool(setup["healthy"]),
            str(ReadinessFact.LIVEKIT_CLIENT_ORIGIN): (
                channel_values.get("EIDOLON_LIVEKIT_CLIENT_URL")
                == self._livekit_client_url(app)
                and channel_values.get("EIDOLON_CHANNEL_PROVIDER_ALLOW_INSECURE_LAN_CLIENT_URL")
                == ("1" if app.allow_insecure_livekit else "0")
            ),
            str(ReadinessFact.LIVEKIT_LAN_REACHABLE): bool(livekit["healthy"]),
            # The lifecycle owner reports whether the running process has
            # consumed current network inputs. YAML and TCP alone cannot.
            str(ReadinessFact.LIVEKIT_NETWORK_CURRENT): (
                media_service.get("service_id") == "livekit"
                and media_service.get("runtime_state") == "ready"
                and media_service.get("network_current") is True
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
            "setup": setup,
            "endpoints": {
                "local_api": f"https://{address}:{ports['local_api']}",
                "hub": identity.hub_origin(app.hub_https_port),
                "livekit": livekit_client_url_at(
                    self._livekit_client_url(app), address
                ),
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
        """Resolve what this source run is about to execute.

        This method was the other half of the same job the release path does —
        it already resolved HEAD; it just compared the answer to a written pin
        and called a difference an error. It now shares the release path's
        resolution, and keeps the one comparison that means something here: a
        source run starts processes out of the worktree, so a pinned commit that
        is not the worktree's HEAD describes something that is not running.
        """

        self.sources.resolve()
        self.sources.require_worktree_is_the_selection()
        # Deliberately not requiring a clean worktree. A source run *is* the
        # worktree, so there is no gap between what was tested and what runs —
        # which is the only thing the release path's refusal is about. Refusing
        # here would break the loop this profile exists for.

    def _read_exact_file(self, source_id: str, revision: str, path: str) -> str:
        if self.sources.revision(source_id) != revision:
            raise OperationsError(f"source revision drifted while reading {source_id}:{path}")
        return checked(
            f"exact Mac product source file {source_id}:{path}",
            self.runner.run(
                (
                    self.git,
                    "-C",
                    str(self.config.sources[source_id].path),
                    "show",
                    f"{revision}:{path}",
                )
            ),
        ).stdout

    def _source_revisions(self) -> dict[str, str]:
        return self.sources.revisions()

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

    def _livekit_client_url(self, app) -> str:
        return livekit_client_url(
            self._host_lan_identity(),
            lan_ipv4=app.lan_ipv4,
            allow_insecure=app.allow_insecure_livekit,
            port=source_assets.PORTS["livekit"],
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

    def _authority_bootstrap_path(self) -> Path:
        """Where Hub looks for its first-install capability, on either Host.

        The same relative location the product template names, so this is the
        path the generated ``hub.yaml`` already points at rather than a second
        opinion about it.
        """

        return self.profile.paths.state_root / "hub/authority-bootstrap.json"

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
        assets = self._decided_owner_authority(self._issue_owner_domain_assets())
        targets = {
            "tls_certificate": self._hub_certificate_path(),
            "tls_private_key": self._hub_private_key_path(),
            "descriptor": self._owner_descriptor_path(),
            "owner_root_certificate": self._owner_root_certificate_path(),
            "authority_signing_certificate": self._authority_signing_certificate_path(),
            # Hub refuses to initialize an empty authority database without
            # this, and refuses to migrate a stale one — so a source run that
            # did not place it could not start Hub at all, from either state.
            # The product install has always sent it (one of
            # ``HOST_APPLICATION_STAGE_NAMES``); this path simply never did.
            "authority_bootstrap": self._authority_bootstrap_path(),
        }
        self._require_every_owner_domain_asset_is_placed(assets, targets)
        for name, path in targets.items():
            atomic_private_file(path, getattr(assets, name))
        return assets

    def _issue_owner_domain_assets(self) -> OwnerDomainAssets:
        """Read or mint this Host's Owner material, placing none of it."""

        try:
            return ensure_owner_domain_assets(
                self._owner_material_root(),
                self._host_lan_identity(),
                self._require_app_access().hub_https_port,
            )
        except OwnerDomainAssetError as exc:
            raise OperationsError(f"Mac Owner Domain material is {exc}") from exc

    # -- Owner Authority lineage ---------------------------------------------

    def _hub_database_path(self) -> Path:
        """The database Hub's generated settings name, so the same file is read.

        Both this and the anchor below are the product locations resolved
        against this profile's state root, which is what ``translate_fhs``
        makes of Hub's own ``$EIDOLON_STATE_ROOT`` template. A second opinion
        about either would read a file no Hub writes.
        """

        return self.profile.paths.state_root / "hub/eidolon-hub.sqlite3"

    def _authority_anchor_path(self) -> Path:
        return self.profile.paths.state_root / "hub/authority-lineage.json"

    def _observed_authority_lineage(self) -> dict[str, object]:
        """What this Host's Hub holds today, read before anything is placed."""

        try:
            return lineage_evidence(
                database=self._hub_database_path(),
                anchor=self._authority_anchor_path(),
            )
        except TargetError as exc:
            raise OperationsError(f"Mac Owner Authority lineage is unreadable: {exc}") from exc

    def _decided_owner_authority(self, current: OwnerDomainAssets) -> OwnerDomainAssets:
        """Advance the Owner Authority generation when this Host's state is gone.

        ``reset --wipe-authority-data`` destroys the state root, which is where
        this Host keeps both halves of its Authority lineage evidence — the Hub
        database marker and the external anchor — while deliberately keeping the
        Owner material that issued them. That combination is a
        ``ResetAuthority`` in fact, and nothing here used to say so: the Owner
        material stayed ``bootstrap_pending`` at generation 1 forever, so every
        wipe-and-start re-derived a byte-identical capability and an empty Hub
        wrote back the very marker that had just been destroyed. 《设备生命周期
        状态机与恢复边》§3.6.1 forbids exactly that — a second, empty Authority
        state behind the anti-rollback fence every prior Claim, credential and
        database backup was issued under — and says so for any operation that
        destroys Hub Authority state, whatever the command is called.

        The rules are the ones :func:`decide_owner_authority` states for every
        such operation. Only two of them do work here, and both do it before a
        descriptor or capability naming the result is rendered, let alone
        placed: recording a capability this Host can prove it used, and
        advancing past one whose state is gone. There is no ``will_wipe`` on
        this path — a source run wipes and starts as two commands, so by the
        time this runs the database really is absent rather than doomed.
        """

        observed = self._observed_authority_lineage()
        decision = decide_owner_authority(
            current, marker=observed["marker"], established=observed["established"]
        )
        if decision is AuthorityDecision.RECORD_CONSUMED:
            current = self._recorded_authority_bootstrap(current)
            decision = decide_owner_authority(
                current, marker=observed["marker"], established=observed["established"]
            )
        if decision is AuthorityDecision.RECOVERY_REQUIRED:
            raise OperationsError(
                self._authority_recovery_required(observed["marker"], current)
            )
        if decision is not AuthorityDecision.ADVANCE_GENERATION:
            return current
        try:
            # The same CAS primitive authority-reset uses: the new lineage is
            # persisted before anything naming it is issued.
            advance_owner_authority(
                self._owner_material_root(),
                expected_owner_domain_id=current.owner_domain_id,
                expected_generation=current.owner_domain_generation,
            )
        except OwnerDomainAssetError as exc:
            raise OperationsError(f"Mac Owner Authority reset failed: {exc}") from exc
        advanced = self._issue_owner_domain_assets()
        if not advanced.bootstrap_pending:
            raise OperationsError(
                "Mac Owner Authority rebuild has no pending bootstrap capability"
            )
        return advanced

    def commit_owner_authority(self) -> dict[str, object]:
        """Record the one-shot capability as spent, against this Host's proof.

        The other half of the decision above, and not optional: consumption has
        to be recorded when it happens, because it is the only thing that later
        tells a wipe apart from a retry. Without it a first ``start`` leaves the
        Owner material ``bootstrap_pending`` — which is the state
        ``authority-backup`` refuses — and the next ``reset
        --wipe-authority-data`` reads that pending capability as a failed
        bootstrap to retry and hands the empty Hub the destroyed state id back.

        Evidence-driven, so it is safe to call after any start: a Host that
        cannot prove the lineage twice over has nothing to record, and the next
        ``prepare`` will make the same decision from the same pair of files.
        Advancing a generation is not done here — only ``prepare`` can render
        the inputs a new generation needs.
        """

        current = self._issue_owner_domain_assets()
        observed = self._observed_authority_lineage()
        decision = decide_owner_authority(
            current, marker=observed["marker"], established=observed["established"]
        )
        if decision is AuthorityDecision.RECOVERY_REQUIRED:
            raise OperationsError(
                self._authority_recovery_required(observed["marker"], current)
            )
        if decision is not AuthorityDecision.RECORD_CONSUMED:
            return {
                "status": "authority_bootstrap_unproven",
                "decision": str(decision),
                "observed": observed,
            }
        consumed = self._recorded_authority_bootstrap(current)
        return {
            "status": "authority_bootstrap_consumed",
            "authority": authority_lineage(consumed),
            "observed": observed,
        }

    def _recorded_authority_bootstrap(self, current: OwnerDomainAssets) -> OwnerDomainAssets:
        try:
            mark_authority_bootstrapped(
                self._owner_material_root(),
                owner_domain_id=current.owner_domain_id,
                owner_domain_generation=current.owner_domain_generation,
                authority_state_id=current.authority_state_id,
            )
        except OwnerDomainAssetError as exc:
            raise OperationsError(
                f"Mac Owner Authority bootstrap cannot be recorded: {exc}"
            ) from exc
        return self._issue_owner_domain_assets()

    @staticmethod
    def _authority_recovery_required(marker: object, current: OwnerDomainAssets) -> str:
        return authority_recovery_required(
            marker,
            authority_lineage(current),
            remedy=(
                "A source run must not start past that. Restore the matching Hub "
                "state, or rebuild deliberately with reset --wipe-authority-data "
                "--apply, which advances the Owner Authority generation at the "
                "next start."
            ),
        )

    @staticmethod
    def _require_every_owner_domain_asset_is_placed(
        assets: OwnerDomainAssets, targets: Mapping[str, Path]
    ) -> None:
        """Fail closed when the issuer produces material this Host drops.

        The product install path states its asset set by name and refuses to
        proceed if what it built is not exactly that set. This path listed its
        writes inline instead, so when ``authority-bootstrap.json`` joined the
        Owner Domain bundle, nothing here noticed it had been left on the floor.
        A missing capability is not a smaller version of the feature; it is a
        Host that cannot start. The next asset added must break this instead.
        """

        issued = {
            field.name
            for field in fields(assets)
            if isinstance(getattr(assets, field.name), bytes)
        }
        dropped = issued.difference(targets)
        if dropped:
            raise OperationsError(
                "Mac Owner Domain material is issued but never placed: "
                + ", ".join(sorted(dropped))
            )

    # Kept as an internal call alias until the local-product tests complete the
    # same coordinated cutover; it now issues Owner-scoped material, never a
    # self-signed Host trust anchor.
    def _ensure_hub_tls_identity(self) -> None:
        self._ensure_owner_domain_assets()
