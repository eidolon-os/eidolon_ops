#!/usr/bin/env python3
"""Standalone target-side operations sent over SSH stdin.

Keep this module standard-library-only until an action deliberately imports the
prepared release's ``eidolon_deploy`` package.
"""

from __future__ import annotations

import base64
import fcntl
import grp
import hashlib
import json
import os
import platform
import pwd
import re
import shutil
import stat
import subprocess
import sys
import time
import uuid
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from pathlib import Path

PRODUCT_UNITS = (
    "eidolon-bootstrapd.service",
    "eidolond.service",
    "eidolon-data.service",
    "eidolon-data-workspace.service",
    "eidolon-hub.service",
    "eidolon-kernel.service",
    "eidolon-local-api.service",
    "eidolon-admin.service",
)
DIRECT_ENABLE_UNITS = (
    "eidolon-bootstrapd.service",
    "eidolond.service",
    "eidolon-local-api.service",
    "eidolon-admin.service",
)
CURRENT_LINKS = {
    "eidolon_kernel": Path("/srv/eidolon/current/eidolon_kernel"),
    "eidolon_data": Path("/srv/eidolon/current/eidolon_data"),
    "eidolon_hub": Path("/srv/eidolon/current/eidolon_hub"),
    "eidolon_admin": Path("/srv/eidolon/current/eidolon_admin"),
}
SECRET_INPUTS = {
    "data.env": (Path("/etc/eidolon/data.env"), "root", "root"),
    "hub.env": (Path("/etc/eidolon/hub.env"), "root", "root"),
    "kernel.env": (Path("/etc/eidolon/kernel.env"), "root", "root"),
    "admin.env": (Path("/etc/eidolon/admin.env"), "root", "root"),
    "local-api.env": (Path("/etc/eidolon/local-api.env"), "root", "root"),
    "bootstrap.env": (Path("/etc/eidolon/bootstrap.env"), "root", "root"),
    "host_identity.ed25519": (
        Path("/var/lib/eidolon-bootstrap/host_identity.ed25519"),
        "eidolon-bootstrap",
        "eidolon-bootstrap",
    ),
}
FIXED_DATA = {
    "system_database": Path("/var/lib/eidolon/eidolon-system.sqlite3"),
    "object_store": Path("/var/lib/eidolon/objects"),
    "bootstrap_database": Path("/var/lib/eidolon-bootstrap/bootstrap.sqlite3"),
    "deployment_evidence": Path("/var/lib/eidolon/deployments"),
}
_RELEASE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_STAGING_NAME = re.compile(r"^eidolon-(?:release|secrets)-[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_VAR_TMP = Path("/var/tmp")
_RELEASES = Path("/srv/eidolon/releases")
_CURRENT_KERNEL = Path("/srv/eidolon/current/eidolon_kernel")
_PHASES = (
    "validated",
    "identities",
    "prerequisites",
    "data_baseline",
    "assets",
    "started",
    "completed",
)


class TargetError(RuntimeError):
    """A target invariant or fixed operation failed closed."""


def _run(
    command: Sequence[str],
    *,
    timeout: float = 120,
    cwd: Path | None = None,
    env: Mapping[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            tuple(command),
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=cwd,
            env=env,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise TargetError(f"command could not run: {command[0]}: {exc}") from exc


def _checked(
    operation: str,
    command: Sequence[str],
    *,
    timeout: float = 120,
    cwd: Path | None = None,
    env: Mapping[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    result = _run(command, timeout=timeout, cwd=cwd, env=env)
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or "no diagnostic output"
        raise TargetError(f"{operation} failed: {detail}")
    return result


def _host_path(root: Path, path: Path) -> Path:
    if not path.is_absolute():
        raise TargetError(f"target path must be absolute: {path}")
    return path if root == Path("/") else root / path.relative_to("/")


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _atomic_json(path: Path, document: object, *, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    payload = (json.dumps(document, indent=2, sort_keys=True) + "\n").encode("utf-8")
    try:
        with temporary.open("xb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


@contextmanager
def _exclusive(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, 0o600)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise TargetError("another first-install transaction is in progress") from exc
        yield
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def _payload(value: str) -> dict[str, object]:
    try:
        document = json.loads(base64.urlsafe_b64decode(value.encode("ascii")))
    except (ValueError, UnicodeError, json.JSONDecodeError) as exc:
        raise TargetError("operator payload is invalid") from exc
    if not isinstance(document, dict):
        raise TargetError("operator payload must be an object")
    return document


def _fixed_units(payload: Mapping[str, object]) -> tuple[str, ...]:
    value = payload.get("units")
    if value != list(PRODUCT_UNITS):
        raise TargetError("unit set differs from the reviewed product topology")
    return PRODUCT_UNITS


def _fixed_data(payload: Mapping[str, object]) -> dict[str, Path]:
    value = payload.get("data")
    if not isinstance(value, dict) or set(value) != set(FIXED_DATA):
        raise TargetError("data path set is invalid")
    result = {name: Path(item) for name, item in value.items() if isinstance(item, str)}
    if result != FIXED_DATA:
        raise TargetError("data paths differ from reviewed system assets")
    return result


def _release_id(payload: Mapping[str, object], *, required: bool = True) -> str | None:
    value = payload.get("release_id")
    if value is None and not required:
        return None
    if not isinstance(value, str) or _RELEASE_ID.fullmatch(value) is None:
        raise TargetError("release id is invalid")
    return value


def _unit_status(unit: str) -> dict[str, object]:
    result = _run(
        (
            "/usr/bin/systemctl",
            "show",
            unit,
            "--property=LoadState,ActiveState,SubState,NRestarts",
        )
    )
    if result.returncode != 0:
        return {"error": result.stderr.strip() or result.stdout.strip() or "systemctl failed"}
    values: dict[str, object] = {}
    for line in result.stdout.splitlines():
        key, separator, value = line.partition("=")
        if separator and key in {"LoadState", "ActiveState", "SubState", "NRestarts"}:
            values[key] = int(value) if key == "NRestarts" and value.isdecimal() else value
    return values


def status(payload: Mapping[str, object]) -> dict[str, object]:
    units = _fixed_units(payload)
    links: dict[str, str | None] = {}
    for component_id, path in CURRENT_LINKS.items():
        links[component_id] = os.readlink(path) if path.is_symlink() else None
    evidence = FIXED_DATA["deployment_evidence"]
    receipts: list[dict[str, object]] = []
    installations: list[dict[str, object]] = []
    if evidence.is_dir():
        for receipt in sorted(
            evidence.glob("*/receipt.json"), key=lambda item: item.stat().st_mtime
        )[-10:]:
            try:
                document = json.loads(receipt.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if isinstance(document, dict):
                receipts.append(
                    {
                        "path": str(receipt),
                        "release_id": document.get("release_id"),
                        "status": document.get("status"),
                        "transaction_id": document.get("transaction_id"),
                    }
                )
        for journal in sorted(
            evidence.glob("install-*/install.json"), key=lambda item: item.stat().st_mtime
        )[-10:]:
            try:
                document = json.loads(journal.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if isinstance(document, dict):
                installations.append(
                    {
                        "path": str(journal),
                        "release_id": document.get("release_id"),
                        "status": document.get("status"),
                        "phase": document.get("phase"),
                    }
                )
    return {
        "status": "observed",
        "host": platform.node(),
        "system": platform.system().lower(),
        "machine": platform.machine().lower(),
        "units": {unit: _unit_status(unit) for unit in units},
        "current_links": links,
        "recent_receipts": receipts,
        "installations": installations,
    }


def doctor_host(payload: Mapping[str, object]) -> dict[str, object]:
    _fixed_units(payload)
    _fixed_data(payload)
    remote_uv = payload.get("remote_uv")
    if not isinstance(remote_uv, str) or not Path(remote_uv).is_absolute():
        raise TargetError("remote uv path is invalid")
    checks = {
        "system": platform.system().lower() == "linux",
        "machine": platform.machine().lower() == "aarch64",
        "root": os.geteuid() == 0,
        "systemctl": Path("/usr/bin/systemctl").is_file(),
        "systemd_analyze": Path("/usr/bin/systemd-analyze").is_file(),
        "python3": Path("/usr/bin/python3").is_file(),
        "uv": Path(remote_uv).is_file() and os.access(remote_uv, os.X_OK),
    }
    release_id = _release_id(payload, required=False)
    release_doctor: object = None
    if release_id is not None:
        descriptor = _RELEASES / release_id / "release.json"
        cli = _RELEASES / release_id / "eidolon_kernel/.venv/bin/eidolon-release"
        result = _run((str(cli), "doctor", str(descriptor)), timeout=180)
        if result.returncode != 0:
            release_doctor = {
                "healthy": False,
                "error": result.stderr.strip() or result.stdout.strip(),
            }
        else:
            try:
                release_doctor = {"healthy": True, "result": json.loads(result.stdout)}
            except json.JSONDecodeError:
                release_doctor = {"healthy": False, "error": "doctor output is not JSON"}
    return {
        "status": "healthy"
        if all(checks.values())
        and not (isinstance(release_doctor, dict) and not release_doctor.get("healthy"))
        else "degraded",
        "checks": checks,
        "release": release_doctor,
    }


def guard_upload(payload: Mapping[str, object]) -> dict[str, object]:
    release_id = _release_id(payload)
    path = _VAR_TMP / f"eidolon-release-{release_id}"
    if path.exists() or path.is_symlink():
        raise TargetError(f"remote bundle path already exists: {path}")
    return {"status": "ready_for_upload", "path": str(path)}


def cleanup_stage(payload: Mapping[str, object]) -> dict[str, object]:
    release_id = _release_id(payload)
    path = _VAR_TMP / f"eidolon-secrets-{release_id}"
    if path.parent != _VAR_TMP or _STAGING_NAME.fullmatch(path.name) is None:
        raise TargetError("secret staging path is unsafe")
    if path.exists():
        if path.is_symlink() or not path.is_dir():
            raise TargetError("secret staging path is not a directory")
        shutil.rmtree(path)
    return {"status": "cleaned", "path": str(path)}


class _DeploymentRunner:
    def __init__(self, command_result_type: type) -> None:
        self._command_result_type = command_result_type

    def run(self, *command: str):
        result = _run(command)
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
        command: Callable[..., subprocess.CompletedProcess[str]] = _run,
        manage_ownership: bool = True,
    ) -> None:
        self.release = release
        self.secret_stage = secret_stage
        self.data = data
        self.host = host
        self.root = root.resolve()
        self.command = command
        self.manage_ownership = manage_ownership
        self.release_id = str(release.release_id)
        self.evidence_dir = _host_path(
            self.root,
            data["deployment_evidence"] / f"install-{self.release_id}",
        )
        self.journal_path = self.evidence_dir / "install.json"
        self.lock_path = _host_path(self.root, Path("/run/lock/eidolon-install.lock"))

    def install(self) -> dict[str, object]:
        if os.geteuid() != 0 and self.root == Path("/"):
            raise TargetError("first install requires root")
        inputs = self._input_digests()
        with _exclusive(self.lock_path):
            journal = self._load_or_begin(inputs)
            if journal.get("status") == "completed":
                result = self.host.doctor(self.release)
                return {"status": "already_installed", "release_id": self.release_id, **result}
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
                    phase = self._record(journal, "started")
                result = self.host.doctor(self.release)
                phase = self._record(journal, "completed", status="completed")
                return {
                    "status": "installed",
                    "release_id": self.release_id,
                    "phase": phase,
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
                _atomic_json(self.journal_path, journal)
                raise

    def _input_digests(self) -> dict[str, str]:
        if not self.secret_stage.is_dir() or self.secret_stage.is_symlink():
            raise TargetError("secret staging directory is missing or unsafe")
        actual = {path.name for path in self.secret_stage.iterdir()}
        if actual != set(SECRET_INPUTS):
            raise TargetError("secret staging file set is invalid")
        values: dict[str, str] = {}
        for name in SECRET_INPUTS:
            path = self.secret_stage / name
            if not path.is_file() or path.is_symlink():
                raise TargetError(f"secret staging input is unsafe: {name}")
            values[name] = _file_sha256(path)
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
                or document.get("phase") not in _PHASES
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
            "updated_at": int(time.time()),
        }
        _atomic_json(self.journal_path, document)
        return document

    def _assert_clean_namespace(self) -> None:
        conflicts: list[str] = []
        for component in self.release.components:
            link = _host_path(self.root, component.current_link)
            if link.exists() or link.is_symlink():
                conflicts.append(str(component.current_link))
        for path in (self.data["system_database"], self.data["bootstrap_database"]):
            if _host_path(self.root, path).exists():
                conflicts.append(str(path))
        for destination, _user, _group in SECRET_INPUTS.values():
            if _host_path(self.root, destination).exists():
                conflicts.append(str(destination))
        for asset in self.release.system_assets:
            if _host_path(self.root, asset.destination).exists():
                conflicts.append(str(asset.destination))
        for namespace in (
            Path("/var/lib/eidolon"),
            Path("/var/lib/eidolon-bootstrap"),
            Path("/var/lib/eidolon-admin"),
            Path("/etc/eidolon"),
        ):
            path = _host_path(self.root, namespace)
            if path.is_dir() and any(path.iterdir()):
                conflicts.append(f"{namespace}/*")
        if conflicts:
            raise TargetError(
                "first install refuses an existing unowned namespace: "
                + ", ".join(sorted(conflicts))
            )

    @staticmethod
    def _before(current: str, target: str) -> bool:
        return _PHASES.index(current) < _PHASES.index(target)

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
        _atomic_json(self.journal_path, journal)
        return phase

    def _ensure_identities_and_directories(self) -> None:
        if self.root == Path("/"):
            self._ensure_service_identity("eidolon")
            self._ensure_service_identity("eidolon-bootstrap")
        directories = (
            (Path("/srv/eidolon/current"), 0o755, "root", "root"),
            (Path("/var/lib/eidolon"), 0o750, "eidolon", "eidolon"),
            (self.data["object_store"], 0o750, "eidolon", "eidolon"),
            (Path("/var/lib/eidolon-admin"), 0o750, "eidolon", "eidolon"),
            (Path("/var/lib/eidolon-bootstrap"), 0o710, "eidolon-bootstrap", "eidolon-bootstrap"),
            (Path("/etc/eidolon"), 0o750, "root", "root"),
        )
        for value, mode, user, group in directories:
            path = _host_path(self.root, value)
            path.mkdir(parents=True, exist_ok=True)
            os.chmod(path, mode)
            self._chown(path, user, group)

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

    def _install_prerequisites(self, inputs: Mapping[str, str]) -> None:
        for name, (destination_value, user, group) in SECRET_INPUTS.items():
            source = self.secret_stage / name
            destination = _host_path(self.root, destination_value)
            if destination.exists():
                if (
                    destination.is_symlink()
                    or not destination.is_file()
                    or _file_sha256(destination) != inputs[name]
                    or stat.S_IMODE(destination.stat().st_mode) != 0o600
                ):
                    raise TargetError(
                        f"existing prerequisite differs during resume: {destination_value}"
                    )
                continue
            destination.parent.mkdir(parents=True, exist_ok=True)
            temporary = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.tmp")
            try:
                shutil.copyfile(source, temporary)
                os.chmod(temporary, 0o600)
                self._chown(temporary, user, group)
                os.replace(temporary, destination)
            finally:
                temporary.unlink(missing_ok=True)

    def _create_data_v2_baseline(self) -> None:
        data_component = self.release.components_by_id["eidolon_data"]
        data_root = _host_path(self.root, data_component.release_path)
        database = _host_path(self.root, self.data["system_database"])
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
            _host_path(self.root, self.data["object_store"])
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
            ("/usr/bin/systemctl", "enable", *DIRECT_ENABLE_UNITS),
        )

    def _command_checked(self, operation: str, command: Sequence[str]):
        result = self.command(command, timeout=120)
        if result.returncode != 0:
            detail = result.stderr.strip() or result.stdout.strip() or "no diagnostic output"
            raise TargetError(f"{operation} failed: {detail}")
        return result

    def _chown(self, path: Path, user: str, group: str) -> None:
        if not self.manage_ownership or self.root != Path("/"):
            return
        try:
            uid = pwd.getpwnam(user).pw_uid
            gid = grp.getgrnam(group).gr_gid
        except KeyError as exc:
            raise TargetError(f"required service identity is missing: {user}:{group}") from exc
        os.chown(path, uid, gid)


def install(payload: Mapping[str, object]) -> dict[str, object]:
    release_id = _release_id(payload)
    data = _fixed_data(payload)
    descriptor = _RELEASES / release_id / "release.json"
    secret_stage = _VAR_TMP / f"eidolon-secrets-{release_id}"
    if secret_stage.parent != _VAR_TMP or _STAGING_NAME.fullmatch(secret_stage.name) is None:
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
        runner=_DeploymentRunner(CommandResult),
        readiness_timeout_seconds=90,
    )
    return TargetInstaller(
        release=release,
        secret_stage=secret_stage,
        data=data,
        host=host,
    ).install()


def lifecycle(action: str, payload: Mapping[str, object]) -> dict[str, object]:
    _fixed_units(payload)
    try:
        from eidolon_deploy.linux import LinuxDeploymentHost
        from eidolon_deploy.manifest import load_release_descriptor
    except ImportError as exc:
        raise TargetError("active release deployment package is unavailable") from exc
    active_kernel = _CURRENT_KERNEL.resolve()
    descriptor = active_kernel.parent / "release.json"
    release = load_release_descriptor(descriptor)
    host = LinuxDeploymentHost(readiness_timeout_seconds=90)
    with host.exclusive_activation():
        host.preflight(release)
        if action in {"stop", "restart"}:
            host.quiesce(release)
        if action in {"start", "restart"}:
            host.start_release(release)
            host.wait_ready(release)
    return {
        "status": action + "ed" if action != "stop" else "stopped",
        "release_id": release.release_id,
    }


def rollback_plan(payload: Mapping[str, object]) -> dict[str, object]:
    release_id = _release_id(payload)
    snapshot_value = payload.get("snapshot")
    if not isinstance(snapshot_value, str):
        raise TargetError("snapshot path is invalid")
    snapshot = Path(snapshot_value)
    expected_parent = FIXED_DATA["deployment_evidence"]
    if not snapshot.is_absolute() or snapshot.parent != expected_parent or not snapshot.is_dir():
        raise TargetError("snapshot is outside the fixed deployment evidence root or missing")
    descriptor = _RELEASES / release_id / "release.json"
    if not descriptor.is_file():
        raise TargetError("release descriptor is missing")
    return {
        "status": "rollback_planned",
        "release_id": release_id,
        "snapshot": str(snapshot),
        "descriptor": str(descriptor),
    }


def logs(payload: Mapping[str, object]) -> dict[str, object]:
    units = _fixed_units(payload)
    requested = payload.get("unit")
    if requested is not None and requested not in units:
        raise TargetError("requested log unit is outside the product topology")
    lines = payload.get("lines", 200)
    if type(lines) is not int or not 1 <= lines <= 5000:
        raise TargetError("log line count is invalid")
    since = payload.get("since")
    if since is not None and (
        not isinstance(since, str)
        or not since
        or len(since) > 80
        or any(ord(char) < 32 for char in since)
    ):
        raise TargetError("journal since value is invalid")
    selected = (requested,) if isinstance(requested, str) else units
    entries: dict[str, str] = {}
    for unit in selected:
        command = [
            "/usr/bin/journalctl",
            "--no-pager",
            "--output=short-iso",
            f"--lines={lines}",
            f"--unit={unit}",
        ]
        if since is not None:
            command.append(f"--since={since}")
        result = _run(command, timeout=60)
        entries[unit] = (
            result.stdout
            if result.returncode == 0
            else (result.stderr.strip() or "journalctl failed")
        )
    return {"status": "collected", "entries": entries}


def diagnose(payload: Mapping[str, object]) -> dict[str, object]:
    observed = status(payload)
    root_usage = shutil.disk_usage("/")
    journal = logs({**payload, "lines": 100, "since": None})
    return {
        "status": "diagnosed",
        "observed": observed,
        "platform": platform.platform(),
        "boot_id": _read_text(Path("/proc/sys/kernel/random/boot_id")),
        "uptime_seconds": _read_uptime(),
        "disk": {
            "total_bytes": root_usage.total,
            "used_bytes": root_usage.used,
            "free_bytes": root_usage.free,
        },
        "journal": journal["entries"],
        "redaction": "No env content, private key content, database content, or process environment collected.",
    }


def _read_text(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return None


def _read_uptime() -> float | None:
    value = _read_text(Path("/proc/uptime"))
    if value is None:
        return None
    try:
        return float(value.split()[0])
    except (ValueError, IndexError):
        return None


def main(argv: Sequence[str] | None = None) -> int:
    arguments = tuple(argv or sys.argv[1:])
    if len(arguments) != 2:
        print(
            json.dumps({"status": "failed", "error": "expected action and payload"}),
            file=sys.stderr,
        )
        return 2
    action, encoded = arguments
    try:
        payload = _payload(encoded)
        if action == "status":
            result = status(payload)
        elif action == "doctor-host":
            result = doctor_host(payload)
        elif action == "guard-upload":
            result = guard_upload(payload)
        elif action == "cleanup-stage":
            result = cleanup_stage(payload)
        elif action == "install":
            result = install(payload)
        elif action in {"start", "stop", "restart"}:
            result = lifecycle(action, payload)
        elif action == "rollback-plan":
            result = rollback_plan(payload)
        elif action == "logs":
            result = logs(payload)
        elif action == "diagnose":
            result = diagnose(payload)
        else:
            raise TargetError("unknown target action")
    except Exception as exc:
        print(json.dumps({"status": "failed", "error": str(exc)}, sort_keys=True), file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
