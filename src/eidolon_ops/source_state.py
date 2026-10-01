"""Operator-owned source roots, with a reversible transfer from legacy roles.

Call transfers only with the Host stopped. Copy bytes rather than ownership or
ACL metadata; retain the original directory, including SQLite WAL and identity.
No account management, chown, ACL grants, or privileged subprocesses.
"""

from __future__ import annotations

import configparser
import json
import os
import pwd
import shutil
import stat
import tempfile
import uuid
from contextlib import suppress
from pathlib import Path

from eidolon_ops.errors import OperationsError
from eidolon_ops.hostagent.primitives import file_sha256
from eidolon_ops.paths import HostProfile
from eidolon_ops.private_files import atomic_private_file
from eidolon_ops.source_runtime import SourceService, service_directories, source_operator


def _journal(path: Path) -> Path:
    return path.with_name(f".{path.name}.source-transfer.json")


def migration_pending(services: tuple[SourceService, ...]) -> bool:
    uid = os.getuid()
    return any(
        _journal(path).exists() or (path.exists() and path.stat().st_uid != uid)
        for service in services for path, _mode in service_directories(service)
    )


def _safe(path: Path, profile: HostProfile) -> None:
    roots = (
        profile.paths.state_root, profile.paths.runtime_root,
        profile.paths.bootstrap_state_root, profile.paths.bootstrap_runtime_root,
    )
    if not any(path == root or path.is_relative_to(root) for root in roots):
        raise OperationsError(f"source service root is outside declared Host paths: {path}")
    if path in (profile.paths.state_root, profile.paths.runtime_root):
        raise OperationsError(f"service cannot own a shared Host root: {path}")
    if any(parent.is_symlink() for parent in (path, *path.parents)):
        raise OperationsError(f"source service root contains a symlink: {path}")


def _recover(path: Path, profile: HostProfile) -> None:
    journal = _journal(path)
    if not journal.exists():
        return
    if journal.is_symlink() or journal.stat().st_uid != os.getuid():
        raise OperationsError(f"source transfer journal is unsafe: {journal}")
    record = json.loads(journal.read_text())
    staging, backup = (Path(record[key]) for key in ("staging", "backup"))
    for candidate in (staging, backup):
        if candidate.parent != path.parent or not candidate.name.startswith(f".{path.name}.source-"):
            raise OperationsError("source transfer journal names an unrelated directory")
        if any(parent.is_symlink() for parent in (candidate, *candidate.parents)):
            raise OperationsError("source transfer journal names a symlink")
    # A crash between the two renames restores the original before trying again.
    if not path.exists() and backup.is_dir():
        backup.rename(path)
    if staging.exists():
        if not staging.is_dir() or staging.stat().st_uid != os.getuid():
            raise OperationsError("source transfer staging directory is unsafe")
        shutil.rmtree(staging)
    journal.unlink()


def _transfer(path: Path, profile: HostProfile, *, runtime: bool, mode: int) -> Path:
    staging = Path(tempfile.mkdtemp(prefix=f".{path.name}.source-", dir=path.parent))
    backup = staging.with_name(staging.name + "-original")
    journal = _journal(path)
    try:
        for item in path.rglob("*"):
            _safe(item, profile)
            destination = staging / item.relative_to(path)
            details = item.lstat()
            if stat.S_ISDIR(details.st_mode):
                destination.mkdir(parents=True, exist_ok=True, mode=0o700)
            elif stat.S_ISREG(details.st_mode):
                destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                shutil.copyfile(item, destination)
                destination.chmod(stat.S_IMODE(details.st_mode))
                if file_sha256(item) != file_sha256(destination):
                    raise OperationsError(f"source state copy did not preserve bytes: {item}")
                with destination.open("rb") as stream:
                    os.fsync(stream.fileno())
            elif not (runtime and stat.S_ISSOCK(details.st_mode)):
                raise OperationsError(f"source state contains an unsupported file: {item}")
        staging.chmod(mode)
        atomic_private_file(journal, json.dumps({"staging": str(staging), "backup": str(backup)}).encode())
        path.rename(backup)
        try:
            staging.rename(path)
        except BaseException:
            backup.rename(path)
            raise
        journal.unlink()
        return backup
    finally:
        if staging.exists():
            shutil.rmtree(staging)


def prepare_roots(
    services: tuple[SourceService, ...], profile: HostProfile, *, migrate: bool = False,
) -> list[Path]:
    operator = source_operator(profile)
    plans = {}
    # Validate every destination and legacy owner before any ownership transition.
    for service in services:
        for path, mode in service_directories(service):
            _safe(path, profile)
            if path.exists() and not path.is_dir():
                raise OperationsError(f"source service root is not a directory: {path}")
            if path.exists() and path.stat().st_uid != operator.pw_uid:
                try:
                    legacy_uid = pwd.getpwnam(service.declared_user).pw_uid
                except KeyError as exc:
                    raise OperationsError(f"source root has an unknown owner: {path}") from exc
                if path.stat().st_uid != legacy_uid or not migrate:
                    raise OperationsError(f"source root requires a stopped ownership transfer: {path}")
            plans[path] = (mode, path in service.runtime_paths)
    backups = []
    for path, (mode, runtime) in plans.items():
        if _journal(path).exists():
            if not migrate:
                raise OperationsError(f"source root has an interrupted transfer: {path}")
            _recover(path, profile)
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if path.parent.stat().st_uid != operator.pw_uid:
            raise OperationsError(f"source root parent is not owned by the operator: {path.parent}")
        if path.exists() and path.stat().st_uid != operator.pw_uid:
            backups.append(_transfer(path, profile, runtime=runtime, mode=mode))
        else:
            path.mkdir(exist_ok=True, mode=mode)
            if stat.S_IMODE(path.stat().st_mode) != mode:
                path.chmod(mode)
    return backups


def prepare_logs(profile: HostProfile, services: tuple[SourceService, ...]) -> list[Path]:
    """Preserve legacy root-written log files; let the operator own new logs."""
    operator = source_operator(profile)
    legacy_uids = {0}
    for service in services:
        with suppress(KeyError):
            legacy_uids.add(pwd.getpwnam(service.declared_user).pw_uid)
    parser = configparser.ConfigParser(interpolation=None)
    parser.read(profile.paths.config_root / "supervisor.conf")
    targets = set()
    for section in parser.sections():
        if not section.startswith("program:"):
            continue
        for key in ("stdout_logfile", "stderr_logfile"):
            value = parser[section].get(key)
            if value:
                path = Path(value.replace("%(ENV_EIDOLON_LOG_ROOT)s", str(profile.paths.log_root)))
                if not path.is_relative_to(profile.paths.log_root) or any(
                    parent.is_symlink() for parent in (path, *path.parents)
                ):
                    raise OperationsError(f"source log target is unsafe: {path}")
                if path.exists() and (not path.is_file() or path.stat().st_uid not in legacy_uids | {operator.pw_uid}):
                    raise OperationsError(f"source log target has an unexpected owner: {path}")
                targets.add(path)
    backups = []
    for path in sorted(targets):
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if path.parent.stat().st_uid != operator.pw_uid:
            raise OperationsError(f"source log parent is not owned by the operator: {path.parent}")
        if path.exists() and path.stat().st_uid != operator.pw_uid:
            backup = path.with_name(f"{path.name}.before-operator-{uuid.uuid4().hex}")
            path.rename(backup)
            backups.append(backup)
        if not path.exists():
            atomic_private_file(path, b"")
    return backups
