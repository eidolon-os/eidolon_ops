"""Workstation orchestration that composes, but does not replace, eidolon-release."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import tarfile
import tempfile
from pathlib import Path

from eidolon_ops.config import (
    INSTALL_FILE_NAMES,
    SOURCE_IDS,
    ConfigurationError,
    OperationsConfig,
    SourceConfig,
    validate_private_local_file,
    validate_release_id,
)
from eidolon_ops.foundation import (
    FOUNDATION_PROFILE,
    foundation_payload,
    python_bootstrap_script,
    python_probe_script,
)
from eidolon_ops.host_application import (
    HOST_APPLICATION_STAGE_NAMES,
    HostApplicationError,
    HostApplicationMaterializer,
)
from eidolon_ops.install_inputs import (
    InstallInputError,
    initialize_install_inputs,
    validate_install_input_contract,
)
from eidolon_ops.paths import AppAccess
from eidolon_ops.process import ProcessRunner, checked
from eidolon_ops.release_matrix import (
    ReleaseMatrixError,
    validate_release_systemd_matrix,
)
from eidolon_ops.transport import SSHTransport

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

#: Release formats this deployer speaks. The activator reports its own versions
#: through `eidolon-release contract`, so interoperability is proven against a
#: declared contract rather than against the Kernel repository layout.
_RELEASE_TOOL_CONTRACT = {
    "tool": "eidolon-release",
    "cli_contract_version": 1,
    "bundle_schema_version": 2,
    "descriptor_schema_version": 2,
    "snapshot_schema_version": 2,
}

#: Component-neutral operator entries published inside every sealed release.
_RELEASE_ACTIVATOR = ".release/bin/eidolon-release"
_RELEASE_INTERPRETER = ".release/bin/python"
#: Dependencies this workstation has already fetched, kept between builds so a
#: release costs the network only what actually changed. Named a dot entry so
#: it cannot be mistaken for a release id when the bundle root is listed.
_DEPENDENCY_CACHE_SEED = ".uv-cache-seed"

#: Sealing runs from the release's own activator, so the one this deployer runs
#: must be byte-identical to the one the pinned Kernel commit ships.
_DEPLOY_PACKAGE = "eidolon_deploy"
_DEPLOY_DIGEST_SUFFIXES = (".py", ".json")


class OperationsError(RuntimeError):
    """An orchestration invariant or phase failed."""


class EidolonPiController:
    def __init__(
        self,
        config: OperationsConfig,
        runner: ProcessRunner,
        *,
        transport: SSHTransport | None = None,
        git: str = "git",
        app: AppAccess | None = None,
    ) -> None:
        self.config = config
        self.runner = runner
        self.transport = transport or SSHTransport(config.host, runner)
        self.git = git
        self.app = app

    def status(self) -> dict[str, object]:
        self._validate_ssh_material()
        return self.transport.run_agent("status", self._target_payload())

    def initialize_inputs(self) -> dict[str, object]:
        """Create the private local first-install input set without contacting the Pi."""

        for source_id in ("eidolon_agent", "eidolon_channel", "eidolon_memory"):
            source = self.config.sources[source_id]
            result = checked(
                f"exact settings commit verification for {source_id}",
                self.runner.run(
                    (
                        self.git,
                        "-C",
                        str(source.path),
                        "rev-parse",
                        "--verify",
                        f"{source.revision}^{{commit}}",
                    )
                ),
            )
            if result.stdout.strip() != source.revision:
                raise OperationsError(
                    f"settings source revision is not the exact commit object: {source_id}"
                )
        try:
            result = initialize_install_inputs(self.config, self._read_exact_source_file)
            if self.app is None:
                return result
            self._prepare_host_application()
            # The contract describes the Host binding, which the materializer
            # owns; the assets it produced are the private material itself.
            return {**result, "host_application": self._host_application().public_contract()}
        except (InstallInputError, HostApplicationError) as exc:
            raise OperationsError(str(exc)) from exc

    def app_ready(self) -> dict[str, object]:
        self._validate_ssh_material()
        return self.transport.run_agent("app-ready", self._target_payload(), timeout=300)

    def doctor(self, *, release_id: str | None = None) -> dict[str, object]:
        local = self.local_preflight(require_install_files=False)
        foundation = self.provision(apply=False)
        if foundation["status"] == "planned_bootstrap":
            return {
                "status": "degraded",
                "local": local,
                "foundation": foundation,
                "remote": {"status": "unavailable", "reason": "python3 is missing"},
            }
        payload = self._target_payload()
        payload["remote_uv"] = str(self.config.host.remote_uv)
        if release_id is not None:
            payload["release_id"] = validate_release_id(release_id)
        remote = self.transport.run_agent("doctor-host", payload, timeout=240)
        healthy = remote.get("status") == "healthy" and foundation.get("status") == "healthy"
        return {
            "status": "healthy" if healthy else "degraded",
            "local": local,
            "foundation": foundation,
            "remote": remote,
        }

    def provision(self, *, apply: bool) -> dict[str, object]:
        """Detect or install the pinned non-Eidolon Raspberry Pi foundation."""

        self._validate_ssh_material()
        missing_commands = [command for command in ("ssh", "scp") if shutil.which(command) is None]
        if missing_commands:
            raise OperationsError(
                "required workstation command is missing: " + ", ".join(missing_commands)
            )
        python_available = self._remote_python_available()
        phases: list[dict[str, object]] = [{"phase": "python_probe", "available": python_available}]
        if not python_available:
            if not apply:
                return {
                    "status": "planned_bootstrap",
                    "profile": FOUNDATION_PROFILE,
                    "phases": phases,
                    "next": "rerun provision --apply to bootstrap Python and the pinned foundation",
                }
            bootstrap = self.transport.run(
                ("/bin/sh", "-s"),
                input_bytes=python_bootstrap_script(),
                sudo=True,
                timeout=1800,
                operation="remote foundation Python bootstrap",
            )
            try:
                bootstrap_result = json.loads(bootstrap.stdout)
            except json.JSONDecodeError as exc:
                raise OperationsError("foundation bootstrap did not return JSON") from exc
            phases.append({"phase": "python_bootstrap", "result": bootstrap_result})
        payload = {"foundation": foundation_payload()}
        observed = self.transport.run_agent(
            "foundation-doctor",
            payload,
            timeout=300,
        )
        phases.append({"phase": "doctor", "result": observed})
        if observed.get("status") == "healthy":
            return {
                "status": "healthy",
                "profile": FOUNDATION_PROFILE,
                "changed": False,
                "phases": phases,
            }
        if not apply:
            return {
                "status": "degraded",
                "profile": FOUNDATION_PROFILE,
                "changed": False,
                "phases": phases,
                "next": "rerun provision --apply after reviewing missing packages and capacity gates",
            }
        installed = self.transport.run_agent(
            "foundation-install",
            payload,
            timeout=3600,
        )
        phases.append({"phase": "install", "result": installed})
        return {
            "status": "installed",
            "profile": FOUNDATION_PROFILE,
            "changed": True,
            "phases": phases,
        }

    def _remote_python_available(self) -> bool:
        result = self.transport.run(
            ("/bin/sh", "-s"),
            input_bytes=python_probe_script(),
            timeout=30,
            operation="remote Python probe",
        )
        try:
            document = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise OperationsError("remote Python probe did not return JSON") from exc
        return document == {"python3": True}

    def local_preflight(self, *, require_install_files: bool) -> dict[str, object]:
        self._validate_ssh_material()
        required_commands = (self.git, "git-lfs", "ssh", "scp", "rsync")
        missing_commands = [
            command for command in required_commands if shutil.which(command) is None
        ]
        if missing_commands:
            raise OperationsError(
                "required workstation command is missing: " + ", ".join(missing_commands)
            )
        release_cli = self.config.workspace.release_cli
        if not release_cli.is_file() or not os.access(release_cli, os.X_OK):
            raise OperationsError(
                f"eidolon-release CLI is missing or not executable: {release_cli}"
            )
        local_uv = self.config.workspace.uv
        if not local_uv.is_file() or not os.access(local_uv, os.X_OK):
            raise OperationsError(f"pinned local uv executable is missing: {local_uv}")
        local_uv_version = checked(
            "pinned local uv verification",
            self.runner.run((str(local_uv), "--version")),
        ).stdout.strip()
        if local_uv_version != "uv 0.11.15" and not local_uv_version.startswith("uv 0.11.15 "):
            raise OperationsError(
                f"pinned local uv must be 0.11.15, got: {local_uv_version or 'no version'}"
            )
        release_contract = self._release_tool_contract(release_cli)
        source_evidence: dict[str, str] = {}
        for source_id in SOURCE_IDS:
            source = self.config.sources[source_id]
            if not source.path.is_dir() or not (source.path / ".git").exists():
                raise OperationsError(f"source repository is missing: {source_id}: {source.path}")
            result = checked(
                f"exact commit verification for {source_id}",
                self.runner.run(
                    (
                        self.git,
                        "-C",
                        str(source.path),
                        "rev-parse",
                        "--verify",
                        f"{source.revision}^{{commit}}",
                    )
                ),
            )
            if result.stdout.strip() != source.revision:
                raise OperationsError(
                    f"source revision is not the exact commit object: {source_id}"
                )
            self._require_tag_resolves(source_id, source)
            source_evidence[source_id] = source.revision
        try:
            release_matrix = validate_release_systemd_matrix(
                source_evidence,
                self._read_exact_source_file,
            )
        except ReleaseMatrixError as exc:
            raise OperationsError(str(exc)) from exc
        install_input_contract = None
        if require_install_files:
            install_input_contract = self._validate_install_inputs(INSTALL_FILE_NAMES)
        return {
            "release_cli": str(release_cli),
            "release_tool_contract": release_contract,
            "local_uv": str(local_uv),
            "local_uv_version": local_uv_version,
            "sources": source_evidence,
            "release_matrix": release_matrix,
            "ssh": {
                "target": self.config.host.target,
                "port": self.config.host.port,
                "batch_mode": True,
                "strict_host_key_checking": True,
            },
            "python_resolver": {
                "index_url": self.config.workspace.python_index_url,
                "http_timeout_seconds": self.config.workspace.python_http_timeout_seconds,
                "http_retries": self.config.workspace.python_http_retries,
                "concurrent_downloads": self.config.workspace.python_concurrent_downloads,
                "locked": True,
            },
            "install_prerequisites_checked": require_install_files,
            "install_input_contract": install_input_contract,
        }

    def _require_tag_resolves(self, source_id: str, source: SourceConfig) -> None:
        """Prove an annotated tag still names the commit this release pins.

        A tag is a movable reference, so it cannot define a release. It can
        still make one reviewable — as long as moving it is reported instead of
        silently followed.
        """

        if source.tag is None:
            return
        resolved = checked(
            f"tag resolution for {source_id}",
            self.runner.run(
                (
                    self.git,
                    "-C",
                    str(source.path),
                    "rev-parse",
                    "--verify",
                    f"{source.tag}^{{commit}}",
                )
            ),
        ).stdout.strip()
        if resolved != source.revision:
            raise OperationsError(
                f"tag no longer names the pinned commit: {source_id} "
                f"{source.tag} -> {resolved[:12]}, expected {source.revision[:12]}"
            )

    def _release_tool_contract(self, release_cli: Path) -> dict[str, object]:
        """Prove the activator speaks this deployer's release formats.

        The activator reports its own contract, so a stale or modified copy is
        rejected without this deployer knowing where it lives in its repository.
        """

        result = checked(
            "eidolon-release contract verification",
            self.runner.run((str(release_cli), "contract"), timeout=60),
        )
        try:
            document = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise OperationsError("eidolon-release contract output is not JSON") from exc
        if not isinstance(document, dict):
            raise OperationsError("eidolon-release contract output is not an object")
        mismatched = sorted(
            name for name, expected in _RELEASE_TOOL_CONTRACT.items() if document.get(name) != expected
        )
        if mismatched:
            raise OperationsError(
                "eidolon-release does not speak this deployer's release contract: "
                + ", ".join(mismatched)
            )
        published = {
            "activator_relative_path": _RELEASE_ACTIVATOR,
            "interpreter_relative_path": _RELEASE_INTERPRETER,
        }
        if any(document.get(name) != value for name, value in published.items()):
            raise OperationsError("eidolon-release publishes unexpected operator entries")
        self._require_shipped_activator(document.get("package_digest"))
        return document

    def _require_shipped_activator(self, reported: object) -> None:
        """Refuse to build a release with an activator that will not ship in it.

        Sealing runs on the target from the release's own copy, so a fix made
        here does nothing unless the Kernel pin moves with it. Without this
        check that mismatch is invisible until an install has already run.
        """

        source = self.config.sources["eidolon_kernel"]
        listing = checked(
            "pinned eidolon_deploy listing",
            self.runner.run(
                (
                    self.git,
                    "-C",
                    str(source.path),
                    "ls-tree",
                    "-r",
                    source.revision,
                    "--",
                    _DEPLOY_PACKAGE,
                )
            ),
        ).stdout
        entries: list[tuple[str, str]] = []
        for line in listing.splitlines():
            metadata, separator, path = line.partition("\t")
            if not separator:
                continue
            relative = path[len(_DEPLOY_PACKAGE) + 1 :]
            if relative.endswith(_DEPLOY_DIGEST_SUFFIXES):
                entries.append((relative, metadata.split()[2]))
        if not entries:
            raise OperationsError(
                "the pinned Kernel commit ships no eidolon_deploy package"
            )
        digest = hashlib.sha256()
        for relative, blob in sorted(entries):
            digest.update(f"{relative}:{blob}\n".encode())
        expected = digest.hexdigest()
        if reported != expected:
            raise OperationsError(
                "the configured eidolon-release is not the one this release will "
                "ship: move the eidolon_kernel pin to the commit that contains it"
            )

    def _read_exact_source_file(self, source_id: str, revision: str, path: str) -> str:
        source = self.config.sources[source_id]
        if source.revision != revision:
            raise OperationsError(f"release revision drifted during matrix validation: {source_id}")
        result = checked(
            f"exact systemd asset verification for {source_id}:{path}",
            self.runner.run(
                (
                    self.git,
                    "-C",
                    str(source.path),
                    "show",
                    f"{revision}:{path}",
                )
            ),
        )
        return result.stdout

    def _validate_install_inputs(self, names: tuple[str, ...]) -> dict[str, object]:
        if set(self.config.install_files) != set(INSTALL_FILE_NAMES):
            raise ConfigurationError("install.files is required for install or expansion")
        for name in names:
            validate_private_local_file(
                self.config.install_files[name], label=f"install.files.{name}"
            )
        try:
            return validate_install_input_contract(
                self.config,
                self._read_exact_source_file,
            )
        except InstallInputError as exc:
            raise OperationsError(str(exc)) from exc

    def deploy(
        self,
        *,
        release_id: str,
        resume: bool,
        activate: bool,
        _skip_prepare: bool = False,
    ) -> dict[str, object]:
        release_id = validate_release_id(release_id)
        local = self.local_preflight(require_install_files=False)
        phases: list[dict[str, object]] = []
        if not _skip_prepare:
            phases.extend(self._bundle_upload_prepare(release_id, reuse=resume))
        descriptor = self._remote_descriptor(release_id)
        cli = self._remote_release_cli(release_id)
        dry_run = self._remote_json(
            "release activation dry-run",
            (cli, "deploy", descriptor, "--dry-run"),
            timeout=300,
        )
        phases.append({"phase": "dry_run", "result": dry_run})
        if not activate:
            return {
                "status": "dry_run",
                "release_id": release_id,
                "local": local,
                "phases": phases,
                "next": "rerun with --resume --activate after reviewing previous_targets",
            }
        activation = self._remote_json(
            "release activation",
            (cli, "deploy", descriptor),
            timeout=600,
        )
        phases.append({"phase": "activate", "result": activation})
        transaction_id = activation.get("transaction_id")
        if (
            activation.get("status") != "activated"
            or not isinstance(transaction_id, str)
            or re.fullmatch(r"[0-9a-f]{32}", transaction_id) is None
        ):
            raise OperationsError("release activation returned invalid transaction evidence")
        snapshot = self.config.data.deployment_evidence / f"{release_id}-{transaction_id}"
        gate_error: Exception | None = None
        try:
            doctor = self._remote_json(
                "release doctor",
                (cli, "doctor", descriptor),
                timeout=300,
            )
            phases.append({"phase": "doctor", "result": doctor})
            if doctor.get("status") != "healthy":
                raise OperationsError("release doctor degraded after activation")
            app = self.app_ready()
            phases.append({"phase": "app_ready", "result": app})
            if app.get("status") != "app_ready":
                raise OperationsError("mobile App gate degraded after activation")
        except Exception as exc:
            gate_error = exc
        if gate_error is not None:
            try:
                restored = self._remote_json(
                    "post-activation gate release rollback",
                    (cli, "rollback", descriptor, str(snapshot)),
                    timeout=600,
                )
                if restored.get("status") != "restored":
                    raise OperationsError("release rollback returned invalid recovery evidence")
            except Exception as rollback_exc:
                raise OperationsError(
                    f"post-activation health gate failed ({gate_error}) and rollback failed: "
                    f"{rollback_exc}"
                ) from rollback_exc
            phases.append({"phase": "health_gate_rollback", "result": restored})
            raise OperationsError(
                f"post-activation health gate failed ({gate_error}); the exact release snapshot "
                "was restored"
            )
        return {
            "status": "activated",
            "release_id": release_id,
            "local": local,
            "phases": phases,
        }

    def install(
        self,
        *,
        release_id: str,
        resume: bool,
        apply: bool,
        reset_existing: bool = False,
        wipe_authority_data: bool = False,
    ) -> dict[str, object]:
        release_id = validate_release_id(release_id)
        if wipe_authority_data and not reset_existing:
            raise OperationsError("--wipe-authority-data requires --reset-existing")
        if reset_existing and not wipe_authority_data:
            raise OperationsError(
                "clean reinstall requires both --reset-existing and --wipe-authority-data"
            )
        if not apply:
            local = self.local_preflight(require_install_files=False)
            foundation = self.provision(apply=False)
            reset = self.reset(wipe_authority_data=True, apply=False) if reset_existing else None
            return {
                "status": "planned",
                "release_id": release_id,
                "local": local,
                "foundation": foundation,
                "reset": reset,
                "mutations": [
                    *(
                        [
                            "stop and remove the existing Eidolon deployment",
                            "permanently wipe Eidolon and Bootstrap authority data",
                        ]
                        if reset_existing
                        else []
                    ),
                    "install the pinned non-Eidolon Raspberry Pi foundation",
                    "prepare exact commit-pinned native release",
                    "create/reuse dedicated service identities and directories",
                    "install the fixed secret, identity and product-settings inputs without overwrite",
                    "create a fresh Data V2 baseline",
                    "install descriptor-allowlisted assets and component links",
                    "enable Bootstrap/eidolond/Local API/Admin and require release doctor",
                ],
                "next": "rerun with --apply after reviewing every planned mutation",
            }
        local = self.local_preflight(require_install_files=True)
        phases: list[dict[str, object]] = []
        if reset_existing:
            reset = self.reset(wipe_authority_data=True, apply=True)
            phases.append({"phase": "reset_existing", "result": reset})
        foundation = self.provision(apply=True)
        phases.extend(self._bundle_upload_prepare(release_id, reuse=resume))
        stage = f"/var/tmp/eidolon-secrets-{release_id}"
        self._stage_install_files(release_id, stage)
        payload = self._target_payload()
        payload["release_id"] = release_id
        python = f"/opt/eidolon/releases/{release_id}/{_RELEASE_INTERPRETER}"
        primary_error: Exception | None = None
        try:
            result = self.transport.run_agent(
                "install",
                payload,
                python=python,
                timeout=1200,
            )
            phases.append({"phase": "install", "result": result})
        except Exception as exc:
            primary_error = exc
        try:
            cleanup = self.transport.run_agent(
                "cleanup-stage",
                {"release_id": release_id},
                timeout=120,
            )
            phases.append({"phase": "secret_cleanup", "result": cleanup})
        except Exception as cleanup_exc:
            if primary_error is not None:
                raise OperationsError(
                    f"install failed ({primary_error}); secret staging cleanup also failed: {cleanup_exc}"
                ) from cleanup_exc
            raise
        if primary_error is not None:
            raise primary_error
        return {
            "status": "installed",
            "release_id": release_id,
            "foundation": foundation,
            "local": local,
            "phases": phases,
        }

    def reset(
        self,
        *,
        wipe_authority_data: bool,
        apply: bool,
    ) -> dict[str, object]:
        """Plan or remove only the fixed Eidolon Host deployment namespace."""

        self._validate_ssh_material()
        payload = self._target_payload()
        payload["wipe_authority_data"] = wipe_authority_data
        plan = self.transport.run_agent("reset-plan", payload, timeout=180)
        if plan.get("status") != "planned":
            raise OperationsError("Host reset plan returned invalid evidence")
        if not apply:
            return {
                **plan,
                "next": "rerun reset --apply after reviewing the detected paths",
            }
        result = self.transport.run_agent("reset-host", payload, timeout=600)
        if result.get("status") != "reset":
            raise OperationsError("Host reset returned invalid evidence")
        return result

    def controller_reset(self, *, apply: bool) -> dict[str, object]:
        """Return a claimed Host to unclaimed so a new phone can manage it.

        The Owner keeps everything else: Host identity, Owner binding, saved
        Wi-Fi and all component data. Only the managing phones lose access.
        """

        self._validate_ssh_material()
        if not apply:
            return {
                "status": "planned",
                "host": self.config.host.target,
                "revokes": "every Controller Grant; managing phones lose access immediately",
                "preserves": [
                    "Host identity and pinned TLS",
                    "Owner binding, Companions and Persona",
                    "saved Wi-Fi profiles and the current connection",
                    "admitted Devices and their Kernel mounts",
                ],
                "next": "rerun controller-reset --apply, then claim the Host from a new phone",
            }
        result = self.transport.run_agent(
            "controller-reset",
            self._target_payload(),
            timeout=180,
        )
        if result.get("status") != "reset":
            raise OperationsError("Controller reset returned invalid evidence")
        return result

    def lifecycle(self, action: str, *, dry_run: bool) -> dict[str, object]:
        if action not in {"start", "stop", "restart"}:
            raise OperationsError("unknown lifecycle action")
        self._validate_ssh_material()
        if dry_run:
            return {
                "status": "planned",
                "action": action,
                "units": self.config.units,
                "authority": "eidolond remains the Data/Hub/Kernel desired-state owner",
            }
        return self.transport.run_agent(
            action,
            self._target_payload(),
            python=self._active_release_interpreter(),
            timeout=300,
        )

    def _active_release_interpreter(self) -> str:
        """Ask the target which interpreter can drive its active release."""

        active = self.transport.run_agent(
            "active-release",
            self._target_payload(),
            timeout=120,
        )
        interpreter = active.get("interpreter")
        if active.get("status") != "observed" or not isinstance(interpreter, str):
            raise OperationsError("target did not report an active release interpreter")
        return interpreter

    def rollback(
        self,
        *,
        release_id: str,
        snapshot: Path,
        apply: bool,
    ) -> dict[str, object]:
        release_id = validate_release_id(release_id)
        if not snapshot.is_absolute() or snapshot.parent != self.config.data.deployment_evidence:
            raise OperationsError("snapshot must be one direct child of deployment_evidence")
        self._validate_ssh_material()
        plan = self.transport.run_agent(
            "rollback-plan",
            {"release_id": release_id, "snapshot": str(snapshot)},
        )
        if not apply:
            return {**plan, "next": "rerun with --apply to restore this exact snapshot"}
        result = self._remote_json(
            "explicit release rollback",
            (
                self._remote_release_cli(release_id),
                "rollback",
                self._remote_descriptor(release_id),
                str(snapshot),
            ),
            timeout=600,
        )
        return {"status": "restored", "release_id": release_id, "result": result}

    def logs(
        self,
        *,
        unit: str | None,
        lines: int,
        since: str | None,
    ) -> dict[str, object]:
        self._validate_ssh_material()
        return self.transport.run_agent(
            "logs",
            {
                **self._target_payload(),
                "unit": unit,
                "lines": lines,
                "since": since,
            },
            timeout=180,
        )

    def diagnose(self, *, output: Path) -> dict[str, object]:
        if not output.is_absolute() or output.suffixes[-2:] != [".tar", ".gz"]:
            raise OperationsError("diagnostic output must be an absolute .tar.gz path")
        if output.exists():
            raise OperationsError("diagnostic output already exists")
        self._validate_ssh_material()
        document = self.transport.run_agent(
            "diagnose",
            self._target_payload(),
            timeout=300,
        )
        output.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="eidolon-diagnostics-") as directory:
            root = Path(directory)
            (root / "target.json").write_text(
                json.dumps(document, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            (root / "release-inputs.json").write_text(
                json.dumps(
                    {
                        "target": self.config.host.target,
                        "port": self.config.host.port,
                        "sources": {
                            source_id: self.config.sources[source_id].revision
                            for source_id in SOURCE_IDS
                        },
                        "units": self.config.units,
                        "redaction": "Local secret paths/values and SSH key bytes omitted.",
                    },
                    indent=2,
                    sort_keys=True,
                )
                + "\n",
                encoding="utf-8",
            )
            with tarfile.open(output, "w:gz") as archive:
                archive.add(root / "target.json", arcname="target.json")
                archive.add(root / "release-inputs.json", arcname="release-inputs.json")
        return {
            "status": "diagnosed",
            "output": str(output),
            "bytes": output.stat().st_size,
            "redacted": True,
        }

    def _validate_ssh_material(self) -> None:
        validate_private_local_file(self.config.host.identity_file, label="host.identity_file")
        known_hosts = self.config.host.known_hosts_file
        if not known_hosts.is_file() or known_hosts.is_symlink() or known_hosts.stat().st_size == 0:
            raise ConfigurationError(
                f"host.known_hosts_file must be a non-empty regular file: {known_hosts}"
            )
        if stat.S_IMODE(known_hosts.stat().st_mode) & 0o022:
            raise ConfigurationError("host.known_hosts_file must not be group/world writable")

    def _bundle_upload_prepare(
        self, release_id: str, *, reuse: bool = False
    ) -> list[dict[str, object]]:
        output = self.config.workspace.bundle_root / release_id
        output.parent.mkdir(parents=True, exist_ok=True)
        if reuse and not output.exists():
            # Backward-compatible activation of a release prepared by another
            # workstation. The subsequent sealed descriptor dry-run is still
            # authoritative and fails closed when the target is not prepared.
            return []
        if output.exists():
            if not reuse:
                raise OperationsError(
                    f"bundle output already exists; use --resume or a new ID: {output}"
                )
            transfer_id = self._validate_existing_bundle(output, release_id)
            bundle_result = {
                "status": "reused_validated_bundle",
                "manifest": str(output / "bundle.json"),
                "sha256": transfer_id,
            }
        else:
            command = [
                str(self.config.workspace.release_cli),
                "bundle",
                release_id,
                str(output),
            ]
            for source_id in SOURCE_IDS:
                flag = source_id.removeprefix("eidolon_").replace("eidolon-", "")
                command.extend((f"--{flag}-repo", str(self.config.sources[source_id].path)))
            for source_id in SOURCE_IDS:
                flag = source_id.removeprefix("eidolon_").replace("eidolon-", "")
                command.extend((f"--{flag}-revision", self.config.sources[source_id].revision))
            command.extend(("--uv", str(self.config.workspace.uv)))
            environment = os.environ.copy()
            environment.update(
                {
                    "UV_DEFAULT_INDEX": self.config.workspace.python_index_url,
                    "UV_HTTP_TIMEOUT": str(self.config.workspace.python_http_timeout_seconds),
                    "UV_HTTP_RETRIES": str(self.config.workspace.python_http_retries),
                    "UV_CONCURRENT_DOWNLOADS": str(
                        self.config.workspace.python_concurrent_downloads
                    ),
                    # Beside the bundles on purpose: copy-on-write clones only
                    # work within one volume, and that is the whole saving.
                    "EIDOLON_RELEASE_UV_CACHE_SEED": str(
                        self.config.workspace.bundle_root / _DEPENDENCY_CACHE_SEED
                    ),
                }
            )
            bundle = checked(
                "commit-pinned source bundle",
                self.runner.run(command, env=environment, timeout=1800),
            )
            bundle_result = self._parse_json(bundle.stdout, "bundle")
            transfer_id = self._validate_existing_bundle(output, release_id)
        guard = self.transport.run_agent(
            "guard-upload",
            {"release_id": release_id, "transfer_id": transfer_id},
            sudo=False,
        )
        remote_bundle = f"/var/tmp/eidolon-release-{release_id}"
        if guard.get("status") == "already_prepared":
            return [
                {"phase": "bundle", "result": bundle_result},
                {"phase": "upload_guard", "result": guard},
            ]
        if guard.get("status") not in {
            "ready_for_upload",
            "resume_upload",
            "ready_for_prepare",
        }:
            raise OperationsError("remote upload guard returned invalid evidence")
        if guard.get("status") != "ready_for_prepare":
            self.transport.upload_directory_resumable(output, remote_bundle)
        finalized = self.transport.run_agent(
            "finalize-upload",
            {"release_id": release_id, "transfer_id": transfer_id},
            sudo=False,
        )
        if finalized.get("status") not in {"finalized", "already_finalized"}:
            raise OperationsError("remote upload finalization returned invalid evidence")
        prepare = self._remote_json(
            "target-native release preparation",
            (
                "/usr/bin/env",
                f"UV_DEFAULT_INDEX={self.config.workspace.python_index_url}",
                f"UV_HTTP_TIMEOUT={self.config.workspace.python_http_timeout_seconds}",
                f"UV_HTTP_RETRIES={self.config.workspace.python_http_retries}",
                f"UV_CONCURRENT_DOWNLOADS={self.config.workspace.python_concurrent_downloads}",
                "/usr/bin/python3",
                f"{remote_bundle}/prepare_target.py",
                remote_bundle,
                "--uv",
                str(self.config.host.remote_uv),
            ),
            timeout=3600,
        )
        return [
            {"phase": "bundle", "result": bundle_result},
            {"phase": "upload_guard", "result": guard},
            {"phase": "upload_finalize", "result": finalized},
            {"phase": "prepare", "result": prepare},
        ]

    def _validate_existing_bundle(self, output: Path, release_id: str) -> str:
        manifest = output / "bundle.json"
        try:
            document = json.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise OperationsError("existing bundle manifest is unreadable") from exc
        sources = document.get("sources") if isinstance(document, dict) else None
        if (
            not isinstance(document, dict)
            or document.get("release_id") != release_id
            or not isinstance(sources, list)
            or len(sources) != len(SOURCE_IDS)
        ):
            raise OperationsError("existing bundle identity or source set is invalid")
        for source_id, item in zip(SOURCE_IDS, sources, strict=True):
            expected_revision = self.config.sources[source_id].revision
            if (
                not isinstance(item, dict)
                or item.get("source_id") != source_id
                or item.get("revision") != expected_revision
                or item.get("archive") != f"sources/{source_id}.tar"
                or not isinstance(item.get("sha256"), str)
            ):
                raise OperationsError(f"existing bundle source record drifted: {source_id}")
            archive = output / str(item["archive"])
            if self._file_sha256(archive) != item["sha256"]:
                raise OperationsError(f"existing bundle source digest drifted: {source_id}")
        preparer = document.get("preparer")
        if (
            not isinstance(preparer, dict)
            or preparer.get("path") != "prepare_target.py"
            or not isinstance(preparer.get("sha256"), str)
            or self._file_sha256(output / "prepare_target.py") != preparer["sha256"]
        ):
            raise OperationsError("existing bundle preparer digest drifted")
        dependencies = document.get("python_dependencies")
        if (
            document.get("schema_version") != 2
            or not isinstance(dependencies, dict)
            or dependencies.get("path") != "python-dependencies.tar.gz"
            or dependencies.get("uv_version") != "0.11.15"
            or dependencies.get("python_version") != "3.13"
            or dependencies.get("platform") != "aarch64-manylinux_2_40"
            or dependencies.get("index_url") != self.config.workspace.python_index_url
            or not isinstance(dependencies.get("sha256"), str)
            or self._file_sha256(output / "python-dependencies.tar.gz") != dependencies["sha256"]
        ):
            raise OperationsError("existing bundle Python dependency cache drifted")
        return self._file_sha256(manifest)

    @staticmethod
    def _file_sha256(path: Path) -> str:
        digest = hashlib.sha256()
        try:
            with path.open("rb") as stream:
                for block in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(block)
        except OSError as exc:
            raise OperationsError(f"bundle file is unreadable: {path}") from exc
        return digest.hexdigest()

    def _stage_install_files(
        self,
        release_id: str,
        stage: str,
        *,
        names: tuple[str, ...] = INSTALL_FILE_NAMES,
    ) -> None:
        self.transport.run_agent(
            "cleanup-stage",
            {"release_id": release_id},
        )
        self.transport.run(
            ("/usr/bin/install", "-d", "-m", "0700", stage),
            sudo=False,
            operation="private secret staging directory creation",
        )
        full_install = names == INSTALL_FILE_NAMES
        application = self._prepare_host_application() if full_install and self.app else None
        with tempfile.TemporaryDirectory(prefix="eidolon-host-application-") as temporary_value:
            temporary = Path(temporary_value)
            for name in names:
                source = self.config.install_files[name]
                if application is not None and name in {"local_api_env", "channel_env"}:
                    rendered = self._host_application().render_environment(
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

    def _remote_json(
        self,
        operation: str,
        command: tuple[str, ...],
        *,
        timeout: float,
    ) -> dict[str, object]:
        result = self.transport.run(
            command,
            sudo=True,
            timeout=timeout,
            operation=operation,
        )
        return self._parse_json(result.stdout, operation)

    @staticmethod
    def _parse_json(value: str, operation: str) -> dict[str, object]:
        try:
            document = json.loads(value)
        except json.JSONDecodeError as exc:
            raise OperationsError(f"{operation} did not return one JSON document") from exc
        if not isinstance(document, dict):
            raise OperationsError(f"{operation} returned a non-object JSON document")
        return document

    def _target_payload(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "units": list(self.config.units),
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
                payload["app"] = self._host_application().public_contract()
            except HostApplicationError as exc:
                raise OperationsError(str(exc)) from exc
        return payload

    def _require_app_access(self) -> AppAccess:
        if self.app is None:
            raise OperationsError("Pi Host operations require the unified [app] access contract")
        return self.app

    def _host_application(self) -> HostApplicationMaterializer:
        app = self._require_app_access()
        ingress = Path(__file__).with_name("lan_ingress.py")
        try:
            source = ingress.read_bytes()
        except OSError as exc:
            raise OperationsError("deployment-owned LAN ingress source is missing") from exc
        return HostApplicationMaterializer(self.config, app, source)

    def _prepare_host_application(self):
        materializer = self._host_application()
        kernel = self.config.sources["eidolon_kernel"]
        template = self._read_exact_source_file(
            "eidolon_kernel", kernel.revision, "config/hub.systemd.example.yaml"
        )
        try:
            return materializer.prepare(template)
        except HostApplicationError as exc:
            raise OperationsError(str(exc)) from exc

    @staticmethod
    def _remote_release_cli(release_id: str) -> str:
        return f"/opt/eidolon/releases/{release_id}/{_RELEASE_ACTIVATOR}"

    @staticmethod
    def _remote_descriptor(release_id: str) -> str:
        return f"/opt/eidolon/releases/{release_id}/release.json"
