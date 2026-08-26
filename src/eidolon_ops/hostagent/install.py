"""Resumable first install of an otherwise unowned Host namespace."""

from __future__ import annotations

import grp
import json
import os
import pwd
import shutil
import stat
import subprocess
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path

from . import contract, host_application, primitives, probe
from .primitives import TargetError


class DeploymentRunner:
    def __init__(self, command_result_type: type) -> None:
        self._command_result_type = command_result_type

    def run(self, *command: str):
        result = primitives.run(command)
        return self._command_result_type(result.returncode, result.stdout, result.stderr)

class TargetInstaller:
    """Resumable first-install state machine for an otherwise unowned namespace."""

    def __init__(
        self,
        *,
        release: object,
        secret_stage: Path,
        data: Mapping[str, Path],
        host: object,
        root: Path = Path("/"),
        command: Callable[..., subprocess.CompletedProcess[str]] = primitives.run,
        manage_ownership: bool = True,
        app_check: Callable[[], dict[str, object]] | None = None,
        port_registry: str = "",
        sources: Mapping[str, object] | None = None,
    ) -> None:
        self.port_registry = port_registry
        #: Which commit of each repository this Host was installed from. A first
        #: install writes no cutover document, so this journal is the only place
        #: the founding combination survives the release directory being
        #: reclaimed.
        self.sources = sources
        self.release = release
        self.secret_stage = secret_stage
        self.data = data
        self.host = host
        self.root = root.resolve()
        self.command = command
        self.manage_ownership = manage_ownership
        self.app_check = app_check
        self.release_id = str(release.release_id)
        self.evidence_dir = primitives.host_path(
            self.root,
            data["deployment_evidence"] / f"install-{self.release_id}",
        )
        self.journal_path = self.evidence_dir / "install.json"
        self.lock_path = primitives.host_path(self.root, Path("/run/lock/eidolon-install.lock"))

    def install(self) -> dict[str, object]:
        if os.geteuid() != 0 and self.root == Path("/"):
            raise TargetError("first install requires root")
        inputs = self._input_digests()
        with primitives.exclusive(self.lock_path):
            journal = self._load_or_begin(inputs)
            if journal.get("status") == "completed":
                result = self.host.doctor(self.release)
                return {
                    "status": "already_installed",
                    "release_id": self.release_id,
                    "app": self._require_app_ready(),
                    **result,
                }
            phase = str(journal["phase"])
            try:
                if self._before(phase, "identities"):
                    self._ensure_identities_and_directories()
                    phase = self._record(journal, "identities")
                if self._before(phase, "prerequisites"):
                    self._install_prerequisites(inputs)
                    phase = self._record(journal, "prerequisites")
                else:
                    # A resumed transaction must re-prove the private inputs it
                    # installed before it is allowed to start or switch again.
                    self._install_prerequisites(inputs)
                if self._before(phase, "data_baseline"):
                    self._create_data_v2_baseline()
                    phase = self._record(journal, "data_baseline")
                if self._before(phase, "assets"):
                    self.host.install_assets(self.release)
                    self.host.switch_components(self.release)
                    self.host.reload_systemd()
                    self._enable_product_units()
                    phase = self._record(journal, "assets")
                if self._before(phase, "started"):
                    self.host.start_release(self.release)
                    self.host.wait_ready(self.release)
                    self._await_host_layer()
                    result = self.host.doctor(self.release)
                    app_result = self._require_app_ready()
                    phase = self._record(journal, "started")
                else:
                    self._await_host_layer()
                    result = self.host.doctor(self.release)
                    app_result = self._require_app_ready()
                phase = self._record(journal, "completed", status="completed")
                return {
                    "status": "installed",
                    "release_id": self.release_id,
                    "phase": phase,
                    "app": app_result,
                    **result,
                }
            except Exception as exc:
                stop_error: str | None = None
                if not self._before(phase, "assets"):
                    try:
                        self.host.quiesce(self.release)
                    except Exception as stop_exc:
                        stop_error = str(stop_exc)
                journal.update(
                    {
                        "status": "failed",
                        "phase": phase,
                        "error": str(exc),
                        "stop_error": stop_error,
                        "updated_at": int(time.time()),
                    }
                )
                primitives.atomic_json(self.journal_path, journal)
                raise

    def _require_app_ready(self) -> dict[str, object] | None:
        if self.app_check is None:
            return None
        result = self.app_check()
        if result.get("status") != "app_ready":
            raise TargetError("mobile App commissioning gate is degraded")
        return result

    def _input_digests(self) -> dict[str, str]:
        if not self.secret_stage.is_dir() or self.secret_stage.is_symlink():
            raise TargetError("secret staging directory is missing or unsafe")
        actual = {path.name for path in self.secret_stage.iterdir()}
        if frozenset(actual) not in {
            frozenset(contract.SECRET_INPUTS),
            frozenset(contract.BASE_INSTALL_INPUTS),
            frozenset(contract.INSTALL_INPUTS),
        }:
            raise TargetError("secret staging file set is invalid")
        values: dict[str, str] = {}
        for name in sorted(actual):
            path = self.secret_stage / name
            if not path.is_file() or path.is_symlink():
                raise TargetError(f"secret staging input is unsafe: {name}")
            values[name] = (
                "private-input-redacted"
                if name in contract.OPTIONAL_HOST_APPLICATION_INPUTS
                else primitives.file_sha256(path)
            )
        return values

    def _load_or_begin(self, inputs: Mapping[str, str]) -> dict[str, object]:
        if self.journal_path.exists():
            try:
                document = json.loads(self.journal_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise TargetError("install journal is unreadable") from exc
            if (
                not isinstance(document, dict)
                or document.get("schema_version") != 1
                or document.get("release_id") != self.release_id
                or document.get("input_sha256") != dict(inputs)
                or document.get("phase") not in contract.PHASES
            ):
                raise TargetError("install journal identity or inputs do not match")
            return document
        self._assert_clean_namespace()
        self.evidence_dir.mkdir(parents=True, mode=0o700)
        os.chmod(self.evidence_dir, 0o700)
        document: dict[str, object] = {
            "schema_version": 1,
            "release_id": self.release_id,
            "status": "running",
            "phase": "validated",
            "input_sha256": dict(inputs),
            # Deliberately outside the identity comparison above: a resumed
            # install is the same install, and refusing to resume because a
            # sibling repository moved would strand a half-installed Host.
            "sources": self.sources,
            "updated_at": int(time.time()),
        }
        primitives.atomic_json(self.journal_path, document)
        return document

    def _assert_clean_namespace(self) -> None:
        """Refuse to install over somebody else's Eidolon — and only that.

        What makes a namespace "unowned" is that this install did not put it
        there. The encoder is the exception, and it is not a loophole: the same
        operation carries it in a step *before* this one, into a path the
        contract declares, precisely so a Host never has to reach the model hub
        itself. Counting it as a stranger's leftover deadlocked the one case
        this guard exists to protect — a genuinely fresh Host — because the
        install could not proceed past weights it had just placed correctly.
        """

        carried = {
            primitives.host_path(self.root, contract.HOST_EMBEDDING_MODEL_ROOT)
        }
        conflicts: list[str] = []
        for component in self.release.components:
            link = primitives.host_path(self.root, component.current_link)
            if link.exists() or link.is_symlink():
                conflicts.append(str(component.current_link))
        for path in (self.data["system_database"], self.data["bootstrap_database"]):
            if primitives.host_path(self.root, path).exists():
                conflicts.append(str(path))
        for destination, _user, _group, _mode in contract.INSTALL_INPUTS.values():
            if primitives.host_path(self.root, destination).exists():
                conflicts.append(str(destination))
        for asset in self.release.system_assets:
            if primitives.host_path(self.root, asset.destination).exists():
                conflicts.append(str(asset.destination))
        for namespace in (
            Path("/var/lib/eidolon"),
            Path("/var/lib/eidolon-bootstrap"),
            Path("/var/lib/eidolon/admin"),
            Path("/etc/eidolon"),
        ):
            path = primitives.host_path(self.root, namespace)
            if path.is_dir() and any(
                entry for entry in path.iterdir() if entry not in carried
            ):
                conflicts.append(f"{namespace}/*")
        if conflicts:
            raise TargetError(
                "first install refuses an existing unowned namespace: "
                + ", ".join(sorted(conflicts))
            )

    @staticmethod
    def _before(current: str, target: str) -> bool:
        return contract.PHASES.index(current) < contract.PHASES.index(target)

    def _record(
        self,
        journal: dict[str, object],
        phase: str,
        *,
        status: str = "running",
    ) -> str:
        journal.update(
            {
                "phase": phase,
                "status": status,
                "error": None,
                "stop_error": None,
                "updated_at": int(time.time()),
            }
        )
        primitives.atomic_json(self.journal_path, journal)
        return phase

    def _ensure_identities_and_directories(self) -> None:
        if self.root == Path("/"):
            self._ensure_service_group("eidolon-lifecycle-client")
            self._ensure_service_identity("eidolon")
            self._ensure_service_identity("eidolon-bootstrap")
            self._ensure_service_identity("eidolon-local-api")
            self._ensure_service_identity("eidolon-lifecycle")
            self._validate_service_identity_boundary()
        contract.ensure_host_path_contract(self.root, self._chown, self.port_registry)

    def _ensure_service_group(self, name: str) -> None:
        group = self.command(("/usr/bin/getent", "group", name), timeout=30)
        if group.returncode != 0:
            self._command_checked(
                "service group creation", ("/usr/sbin/groupadd", "--system", name)
            )

    def _ensure_service_identity(self, name: str) -> None:
        group = self.command(("/usr/bin/getent", "group", name), timeout=30)
        if group.returncode != 0:
            self._command_checked(
                "service group creation", ("/usr/sbin/groupadd", "--system", name)
            )
        user = self.command(("/usr/bin/id", "-u", name), timeout=30)
        if user.returncode != 0:
            self._command_checked(
                "service user creation",
                (
                    "/usr/sbin/useradd",
                    "--system",
                    "--gid",
                    name,
                    "--home-dir",
                    "/nonexistent",
                    "--shell",
                    "/usr/sbin/nologin",
                    name,
                ),
            )
        primary = self._command_checked("service identity inspection", ("/usr/bin/id", "-gn", name))
        if primary.stdout.strip() != name:
            raise TargetError(f"service identity has unexpected primary group: {name}")

    def _validate_service_identity_boundary(self) -> None:
        uids: list[int] = []
        for name in (
            "eidolon",
            "eidolon-bootstrap",
            "eidolon-local-api",
            "eidolon-lifecycle",
        ):
            result = self._command_checked(
                "service uid inspection", ("/usr/bin/id", "-u", name)
            )
            try:
                uid = int(result.stdout.strip())
            except ValueError as exc:
                raise TargetError("service uid inspection returned an invalid value") from exc
            if uid == 0:
                raise TargetError(f"service identity must not be root: {name}")
            uids.append(uid)
        if len(set(uids)) != len(uids):
            raise TargetError("service identities must have distinct UIDs")
        group = self._command_checked(
            "socket group inspection",
            ("/usr/bin/getent", "group", "eidolon-lifecycle-client"),
        ).stdout.strip()
        fields = group.split(":")
        if len(fields) != 4 or fields[3].strip():
            raise TargetError("socket group has persistent members")

    def _install_prerequisites(self, inputs: Mapping[str, str]) -> None:
        selected_inputs = {name: contract.INSTALL_INPUTS[name] for name in inputs}
        for name, (destination_value, user, group, mode) in selected_inputs.items():
            source = self.secret_stage / name
            destination = primitives.host_path(self.root, destination_value)
            if destination.exists():
                expected_ids: tuple[int, int] | None = None
                if self.manage_ownership and self.root == Path("/"):
                    try:
                        expected_ids = (
                            pwd.getpwnam(user).pw_uid,
                            grp.getgrnam(group).gr_gid,
                        )
                    except KeyError as exc:
                        raise TargetError(
                            f"required prerequisite identity is missing: {user}:{group}"
                        ) from exc
                metadata = destination.stat()
                if (
                    destination.is_symlink()
                    or not destination.is_file()
                    or (
                        destination.read_bytes() != source.read_bytes()
                        if name in contract.OPTIONAL_HOST_APPLICATION_INPUTS
                        else primitives.file_sha256(destination) != inputs[name]
                    )
                    or stat.S_IMODE(metadata.st_mode) != mode
                    or (
                        expected_ids is not None
                        and (metadata.st_uid, metadata.st_gid) != expected_ids
                    )
                ):
                    raise TargetError(
                        f"existing prerequisite differs during resume: {destination_value}"
                    )
                continue
            destination.parent.mkdir(parents=True, exist_ok=True)
            temporary = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.tmp")
            try:
                shutil.copyfile(source, temporary)
                os.chmod(temporary, mode)
                self._chown(temporary, user, group)
                os.replace(temporary, destination)
            finally:
                temporary.unlink(missing_ok=True)

    def _create_data_v2_baseline(self) -> None:
        data_component = self.release.components_by_id["eidolon_data"]
        data_root = primitives.host_path(self.root, data_component.release_path)
        database = primitives.host_path(self.root, self.data["system_database"])
        if database.exists() and (database.is_symlink() or not database.is_file()):
            raise TargetError("Data authority path is unsafe")
        command = (
            str(data_root / ".venv/bin/alembic"),
            "-c",
            str(data_root / "alembic.ini"),
            "upgrade",
            "head",
        )
        environment = dict(os.environ)
        environment["EIDOLON_DATA_SQLITE_PATH"] = str(database)
        environment["EIDOLON_DATA_OBJECT_STORE_PATH"] = str(
            primitives.host_path(self.root, self.data["object_store"])
        )
        result = self.command(command, timeout=300, cwd=data_root, env=environment)
        if result.returncode != 0:
            detail = result.stderr.strip() or result.stdout.strip() or "no diagnostic output"
            raise TargetError(f"Data V2 baseline failed: {detail}")
        if not database.is_file() or database.is_symlink():
            raise TargetError("Data V2 baseline did not create its authority database")
        os.chmod(database, 0o640)
        self._chown(database, "eidolon", "eidolon")

    def _enable_product_units(self) -> None:
        self._command_checked(
            "product unit enablement",
            ("/usr/bin/systemctl", "enable", *contract.DIRECT_ENABLE_UNITS),
        )
        ingress = primitives.host_path(self.root, contract.HOST_APPLICATION_INPUTS["hub-ingress.service"][0])
        if ingress.is_file():
            self._command_checked(
                "Host application unit enablement",
                ("/usr/bin/systemctl", "enable", "eidolon-hub-ingress.service"),
            )

    def _await_host_layer(self) -> None:
        host_application.await_host_application(self.command, self.root)

    def _command_checked(self, operation: str, command: Sequence[str]):
        result = self.command(command, timeout=120)
        if result.returncode != 0:
            detail = result.stderr.strip() or result.stdout.strip() or "no diagnostic output"
            raise TargetError(f"{operation} failed: {detail}")
        return result

    def _chown(self, path: Path, user: str, group: str) -> None:
        if not self.manage_ownership or self.root != Path("/"):
            return
        primitives.chown_path(path, user, group)

def install(payload: Mapping[str, object]) -> dict[str, object]:
    release_id = contract.fixed_release_id(payload)
    data = contract.fixed_data(payload)
    descriptor = contract.RELEASES / release_id / "release.json"
    secret_stage = contract.VAR_TMP / f"eidolon-secrets-{release_id}"
    if secret_stage.parent != contract.VAR_TMP or contract.STAGING_NAME.fullmatch(secret_stage.name) is None:
        raise TargetError("secret staging path is unsafe")
    try:
        from eidolon_deploy.linux import CommandResult, LinuxDeploymentHost
        from eidolon_deploy.manifest import load_release_descriptor
    except ImportError as exc:
        raise TargetError("prepared release deployment package is unavailable") from exc
    release = load_release_descriptor(descriptor)
    if release.release_id != release_id:
        raise TargetError("prepared release identity mismatch")
    host = LinuxDeploymentHost(
        runner=DeploymentRunner(CommandResult),
        readiness_timeout_seconds=contract.release_readiness_seconds(payload),
    )
    return TargetInstaller(
        release=release,
        secret_stage=secret_stage,
        data=data,
        host=host,
        app_check=lambda: probe.app_ready(payload),
        port_registry=contract.fixed_port_registry(payload),
        sources=contract.optional_source_provenance(payload),
    ).install()
