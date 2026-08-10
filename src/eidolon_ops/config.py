"""Strict, secret-free operator configuration."""

from __future__ import annotations

import re
import stat
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from types import MappingProxyType

from eidolon_ops.foundation import FOUNDATION_PROFILE

SOURCE_IDS = (
    "eidolon_kernel",
    "eidolon_data",
    "eidolon_hub",
    "eidolon_admin",
    "eidolon_agent",
    "eidolon_channel",
    "eidolon_memory",
    "eidolon_sdk",
)
PRODUCT_UNITS = (
    "eidolon-bootstrapd.service",
    "eidolond.service",
    "eidolon-data.service",
    "eidolon-data-workspace.service",
    "eidolon-hub.service",
    "eidolon-kernel.service",
    "eidolon-local-api.service",
    "eidolon-admin.service",
    "eidolon-nats.service",
    "eidolon-livekit.service",
    "eidolon-memory-supervisor.service",
    "eidolon-memory-discovery.service",
    "eidolon-agent.service",
    "eidolon-channel.service",
)
INSTALL_FILE_NAMES = (
    "data_env",
    "hub_env",
    "kernel_env",
    "admin_env",
    "local_api_env",
    "bootstrap_env",
    "host_identity",
    "agent_env",
    "channel_env",
    "memory_env",
    "livekit_env",
    "agent_settings",
    "channel_settings",
    "memory_settings",
)
EXPANSION_FILE_NAMES = (
    "agent_env",
    "channel_env",
    "memory_env",
    "livekit_env",
    "agent_settings",
    "channel_settings",
    "memory_settings",
)
FIXED_DATA_PATHS = {
    "system_database": Path("/var/lib/eidolon/eidolon-system.sqlite3"),
    "object_store": Path("/var/lib/eidolon/objects"),
    "bootstrap_database": Path("/var/lib/eidolon-bootstrap/bootstrap.sqlite3"),
    "deployment_evidence": Path("/var/lib/eidolon/deployments"),
}

_REVISION = re.compile(r"^[0-9a-f]{40}$")
_HOST = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?$")
_USER = re.compile(r"^[a-z_][a-z0-9_-]{0,31}$")
_RELEASE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


class ConfigurationError(ValueError):
    """The operator configuration is ambiguous, incomplete, or unsafe."""


@dataclass(frozen=True, slots=True)
class HostConfig:
    user: str
    hostname: str
    port: int
    identity_file: Path
    known_hosts_file: Path
    connect_timeout_seconds: int
    remote_uv: Path

    @property
    def target(self) -> str:
        return f"{self.user}@{self.hostname}"


@dataclass(frozen=True, slots=True)
class WorkspaceConfig:
    bundle_root: Path
    release_cli: Path


@dataclass(frozen=True, slots=True)
class SourceConfig:
    path: Path
    revision: str


@dataclass(frozen=True, slots=True)
class DataConfig:
    system_database: Path
    object_store: Path
    bootstrap_database: Path
    deployment_evidence: Path


@dataclass(frozen=True, slots=True)
class OperationsConfig:
    path: Path
    foundation_profile: str
    host: HostConfig
    workspace: WorkspaceConfig
    sources: Mapping[str, SourceConfig]
    units: tuple[str, ...]
    data: DataConfig
    install_files: Mapping[str, Path]

    def with_revision_overrides(self, values: tuple[str, ...]) -> OperationsConfig:
        sources = dict(self.sources)
        seen: set[str] = set()
        for value in values:
            source_id, separator, revision = value.partition("=")
            if not separator or source_id not in sources or source_id in seen:
                raise ConfigurationError(
                    "revision override must be one unique source=40-hex assignment"
                )
            _require_revision(revision, f"revision override for {source_id}")
            sources[source_id] = replace(sources[source_id], revision=revision)
            seen.add(source_id)
        return replace(self, sources=MappingProxyType(sources))


def validate_release_id(value: str) -> str:
    if _RELEASE_ID.fullmatch(value) is None:
        raise ConfigurationError("release id is invalid")
    return value


def load_config(path: Path) -> OperationsConfig:
    resolved = path.expanduser().resolve()
    try:
        document = tomllib.loads(resolved.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise ConfigurationError(f"configuration is unreadable: {resolved}") from exc
    _require_keys(
        document,
        required={
            "schema_version",
            "foundation",
            "host",
            "workspace",
            "sources",
            "services",
            "data",
        },
        optional={"install"},
        label="root",
    )
    if document["schema_version"] != 1:
        raise ConfigurationError("configuration schema_version must be 1")
    base = resolved.parent
    foundation_wire = _mapping(document["foundation"], "foundation")
    _require_keys(foundation_wire, required={"profile"}, label="foundation")
    foundation_profile = _string(foundation_wire["profile"], "foundation.profile")
    if foundation_profile != FOUNDATION_PROFILE:
        raise ConfigurationError(
            f"foundation.profile must be the reviewed profile: {FOUNDATION_PROFILE}"
        )
    host_wire = _mapping(document["host"], "host")
    _require_keys(
        host_wire,
        required={
            "user",
            "hostname",
            "port",
            "identity_file",
            "known_hosts_file",
            "connect_timeout_seconds",
            "remote_uv",
        },
        label="host",
    )
    user = _string(host_wire["user"], "host.user")
    hostname = _string(host_wire["hostname"], "host.hostname")
    if user == "root" or _USER.fullmatch(user) is None or _HOST.fullmatch(hostname) is None:
        raise ConfigurationError("host user or hostname is unsafe")
    port = _integer(host_wire["port"], "host.port", minimum=1, maximum=65535)
    timeout = _integer(
        host_wire["connect_timeout_seconds"],
        "host.connect_timeout_seconds",
        minimum=1,
        maximum=120,
    )
    remote_uv = _absolute_remote_path(host_wire["remote_uv"], "host.remote_uv")

    workspace_wire = _mapping(document["workspace"], "workspace")
    _require_keys(
        workspace_wire,
        required={"bundle_root", "release_cli"},
        label="workspace",
    )
    workspace = WorkspaceConfig(
        bundle_root=_local_path(workspace_wire["bundle_root"], base, "workspace.bundle_root"),
        release_cli=_local_path(workspace_wire["release_cli"], base, "workspace.release_cli"),
    )

    sources_wire = _mapping(document["sources"], "sources")
    if set(sources_wire) != set(SOURCE_IDS):
        raise ConfigurationError(
            "sources must be exactly Kernel/Data/Hub/Admin/Agent/Channel/Memory/SDK"
        )
    sources: dict[str, SourceConfig] = {}
    for source_id in SOURCE_IDS:
        source_wire = _mapping(sources_wire[source_id], f"sources.{source_id}")
        _require_keys(source_wire, required={"path", "revision"}, label=f"sources.{source_id}")
        revision = _string(source_wire["revision"], f"sources.{source_id}.revision")
        _require_revision(revision, f"sources.{source_id}.revision")
        sources[source_id] = SourceConfig(
            path=_local_path(source_wire["path"], base, f"sources.{source_id}.path"),
            revision=revision,
        )

    services_wire = _mapping(document["services"], "services")
    _require_keys(services_wire, required={"units"}, label="services")
    units_wire = services_wire["units"]
    if not isinstance(units_wire, list) or not all(isinstance(item, str) for item in units_wire):
        raise ConfigurationError("services.units must be an array of strings")
    units = tuple(units_wire)
    if units != PRODUCT_UNITS:
        raise ConfigurationError("services.units must equal the fixed reviewed product topology")

    data_wire = _mapping(document["data"], "data")
    _require_keys(data_wire, required=set(FIXED_DATA_PATHS), label="data")
    data_values: dict[str, Path] = {}
    for key, expected in FIXED_DATA_PATHS.items():
        value = _absolute_remote_path(data_wire[key], f"data.{key}")
        if value != expected:
            raise ConfigurationError(f"data.{key} must match reviewed system assets: {expected}")
        data_values[key] = value
    data = DataConfig(**data_values)

    install_files: dict[str, Path] = {}
    if "install" in document:
        install_wire = _mapping(document["install"], "install")
        _require_keys(install_wire, required={"files"}, label="install")
        files_wire = _mapping(install_wire["files"], "install.files")
        if set(files_wire) != set(INSTALL_FILE_NAMES):
            raise ConfigurationError("install.files must contain the fixed full-product inputs")
        for name in INSTALL_FILE_NAMES:
            install_files[name] = _local_path(files_wire[name], base, f"install.files.{name}")
        if len(set(install_files.values())) != len(install_files):
            raise ConfigurationError("install.files paths must be unique per security scope")

    return OperationsConfig(
        path=resolved,
        foundation_profile=foundation_profile,
        host=HostConfig(
            user=user,
            hostname=hostname,
            port=port,
            identity_file=_local_path(host_wire["identity_file"], base, "host.identity_file"),
            known_hosts_file=_local_path(
                host_wire["known_hosts_file"], base, "host.known_hosts_file"
            ),
            connect_timeout_seconds=timeout,
            remote_uv=remote_uv,
        ),
        workspace=workspace,
        sources=MappingProxyType(sources),
        units=units,
        data=data,
        install_files=MappingProxyType(install_files),
    )


def validate_private_local_file(path: Path, *, label: str) -> None:
    if not path.is_file() or path.is_symlink():
        raise ConfigurationError(f"{label} is missing or is not a regular file: {path}")
    mode = stat.S_IMODE(path.stat().st_mode)
    if mode & 0o077:
        raise ConfigurationError(f"{label} must not be group/world accessible: {path}")


def _mapping(value: object, label: str) -> dict:
    if not isinstance(value, dict):
        raise ConfigurationError(f"{label} must be a table")
    return value


def _require_keys(
    value: Mapping[str, object],
    *,
    required: set[str],
    optional: set[str] | None = None,
    label: str,
) -> None:
    allowed = required | (optional or set())
    missing = sorted(required - set(value))
    extra = sorted(set(value) - allowed)
    if missing or extra:
        raise ConfigurationError(
            f"{label} keys are invalid; missing={missing or 'none'}, extra={extra or 'none'}"
        )


def _string(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise ConfigurationError(f"{label} must be a non-empty string")
    return value


def _integer(value: object, label: str, *, minimum: int, maximum: int) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise ConfigurationError(f"{label} must be between {minimum} and {maximum}")
    return value


def _local_path(value: object, base: Path, label: str) -> Path:
    text = _string(value, label)
    path = Path(text).expanduser()
    if not path.is_absolute():
        path = base / path
    return path.resolve()


def _absolute_remote_path(value: object, label: str) -> Path:
    path = Path(_string(value, label))
    if not path.is_absolute() or ".." in path.parts:
        raise ConfigurationError(f"{label} must be a safe absolute path")
    return path


def _require_revision(value: str, label: str) -> None:
    if _REVISION.fullmatch(value) is None:
        raise ConfigurationError(f"{label} must be a full lowercase 40-hex commit")
