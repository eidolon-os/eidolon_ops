"""Workstation orchestration that composes, but does not replace, eidolon-release."""

from __future__ import annotations

import json
import os
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
    validate_private_local_file,
    validate_release_id,
)
from eidolon_ops.process import ProcessRunner, checked
from eidolon_ops.transport import SSHTransport

_STAGED_INSTALL_NAMES = {
    "data_env": "data.env",
    "hub_env": "hub.env",
    "kernel_env": "kernel.env",
    "admin_env": "admin.env",
    "local_api_env": "local-api.env",
    "bootstrap_env": "bootstrap.env",
    "host_identity": "host_identity.ed25519",
}


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
    ) -> None:
        self.config = config
        self.runner = runner
        self.transport = transport or SSHTransport(config.host, runner)
        self.git = git

    def status(self) -> dict[str, object]:
        self._validate_ssh_material()
        return self.transport.run_agent("status", self._target_payload())

    def doctor(self, *, release_id: str | None = None) -> dict[str, object]:
        local = self.local_preflight(require_install_files=False)
        payload = self._target_payload()
        payload["remote_uv"] = str(self.config.host.remote_uv)
        if release_id is not None:
            payload["release_id"] = validate_release_id(release_id)
        remote = self.transport.run_agent("doctor-host", payload, timeout=240)
        healthy = remote.get("status") == "healthy"
        return {
            "status": "healthy" if healthy else "degraded",
            "local": local,
            "remote": remote,
        }

    def local_preflight(self, *, require_install_files: bool) -> dict[str, object]:
        self._validate_ssh_material()
        required_commands = (self.git, "ssh", "scp")
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
            source_evidence[source_id] = source.revision
        if require_install_files:
            if set(self.config.install_files) != set(INSTALL_FILE_NAMES):
                raise ConfigurationError("install.files is required for first install")
            for name in INSTALL_FILE_NAMES:
                validate_private_local_file(
                    self.config.install_files[name], label=f"install.files.{name}"
                )
        return {
            "release_cli": str(release_cli),
            "sources": source_evidence,
            "ssh": {
                "target": self.config.host.target,
                "port": self.config.host.port,
                "batch_mode": True,
                "strict_host_key_checking": True,
            },
            "install_prerequisites_checked": require_install_files,
        }

    def deploy(
        self,
        *,
        release_id: str,
        resume: bool,
        activate: bool,
    ) -> dict[str, object]:
        release_id = validate_release_id(release_id)
        local = self.local_preflight(require_install_files=False)
        phases: list[dict[str, object]] = []
        if not resume:
            phases.extend(self._bundle_upload_prepare(release_id))
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
        doctor = self._remote_json(
            "release doctor",
            (cli, "doctor", descriptor),
            timeout=300,
        )
        phases.append({"phase": "doctor", "result": doctor})
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
    ) -> dict[str, object]:
        release_id = validate_release_id(release_id)
        if not apply:
            local = self.local_preflight(require_install_files=False)
            payload = self._target_payload()
            payload["remote_uv"] = str(self.config.host.remote_uv)
            remote = self.transport.run_agent("doctor-host", payload)
            return {
                "status": "planned",
                "release_id": release_id,
                "local": local,
                "remote_preflight": remote,
                "mutations": [
                    "prepare exact commit-pinned native release",
                    "create/reuse dedicated service identities and directories",
                    "install seven operator-supplied prerequisite files without overwrite",
                    "create a fresh Data V2 baseline",
                    "install descriptor-allowlisted assets and component links",
                    "enable Bootstrap/eidolond/Local API/Admin and require release doctor",
                ],
                "next": "rerun with --apply after confirming this is a new Eidolon namespace",
            }
        local = self.local_preflight(require_install_files=True)
        phases: list[dict[str, object]] = []
        if not resume:
            phases.extend(self._bundle_upload_prepare(release_id))
        stage = f"/var/tmp/eidolon-secrets-{release_id}"
        self._stage_install_files(release_id, stage)
        payload = self._target_payload()
        payload["release_id"] = release_id
        python = f"/srv/eidolon/releases/{release_id}/eidolon_kernel/.venv/bin/python"
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
            "local": local,
            "phases": phases,
        }

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
        python = "/srv/eidolon/current/eidolon_kernel/.venv/bin/python"
        return self.transport.run_agent(
            action,
            self._target_payload(),
            python=python,
            timeout=300,
        )

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

    def _bundle_upload_prepare(self, release_id: str) -> list[dict[str, object]]:
        output = self.config.workspace.bundle_root / release_id
        output.parent.mkdir(parents=True, exist_ok=True)
        if output.exists():
            raise OperationsError(
                f"bundle output already exists; use --resume or a new ID: {output}"
            )
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
        bundle = checked(
            "commit-pinned source bundle",
            self.runner.run(command, timeout=300),
        )
        bundle_result = self._parse_json(bundle.stdout, "bundle")
        guard = self.transport.run_agent(
            "guard-upload",
            {"release_id": release_id},
        )
        remote_bundle = f"/var/tmp/eidolon-release-{release_id}"
        self.transport.upload(output, remote_bundle, recursive=True)
        prepare = self._remote_json(
            "target-native release preparation",
            (
                "/usr/bin/python3",
                f"{remote_bundle}/prepare_target.py",
                remote_bundle,
                "--uv",
                str(self.config.host.remote_uv),
            ),
            timeout=1800,
        )
        return [
            {"phase": "bundle", "result": bundle_result},
            {"phase": "upload_guard", "result": guard},
            {"phase": "prepare", "result": prepare},
        ]

    def _stage_install_files(self, release_id: str, stage: str) -> None:
        self.transport.run_agent(
            "cleanup-stage",
            {"release_id": release_id},
        )
        self.transport.run(
            ("/usr/bin/install", "-d", "-m", "0700", stage),
            sudo=False,
            operation="private secret staging directory creation",
        )
        for name in INSTALL_FILE_NAMES:
            self.transport.upload(
                self.config.install_files[name],
                f"{stage}/{_STAGED_INSTALL_NAMES[name]}",
            )

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
        return {
            "units": list(self.config.units),
            "data": {
                "system_database": str(self.config.data.system_database),
                "object_store": str(self.config.data.object_store),
                "bootstrap_database": str(self.config.data.bootstrap_database),
                "deployment_evidence": str(self.config.data.deployment_evidence),
            },
        }

    @staticmethod
    def _remote_release_cli(release_id: str) -> str:
        return f"/srv/eidolon/releases/{release_id}/eidolon_kernel/.venv/bin/eidolon-release"

    @staticmethod
    def _remote_descriptor(release_id: str) -> str:
        return f"/srv/eidolon/releases/{release_id}/release.json"
