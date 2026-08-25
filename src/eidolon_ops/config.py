"""Strict, secret-free operator configuration."""

from __future__ import annotations

import re
import stat
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from types import MappingProxyType
from urllib.parse import urlsplit

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
    "eidolon-lifecycle-workflow.service",
    "eidolon-admin.service",
    "eidolon-nats.service",
    "eidolon-livekit.service",
    "eidolon-memory-supervisor.service",
    "eidolon-memory-discovery.service",
    "eidolon-agent.service",
    "eidolon-channel-provider.service",
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
FIXED_DATA_PATHS = {
    "system_database": Path("/var/lib/eidolon/eidolon-system.sqlite3"),
    "object_store": Path("/var/lib/eidolon/objects"),
    "bootstrap_database": Path("/var/lib/eidolon-bootstrap/bootstrap.sqlite3"),
    "deployment_evidence": Path("/var/lib/eidolon/deployments"),
}

_REVISION = re.compile(r"^[0-9a-f]{40}$")
_TAG = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,127}$")
_HOST = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?$")
_USER = re.compile(r"^[a-z_][a-z0-9_-]{0,31}$")
_RELEASE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_PYTHON_INDEX_URL = re.compile(r"^https://[A-Za-z0-9.-]+(?::[0-9]{1,5})?/[A-Za-z0-9_./:=+@,-]*$")


#: Directories the operating system empties on its own schedule. Anything a
#: release must be able to find again cannot be pinned inside one.
#:
#: Resolved, because ``/tmp`` is a symlink to ``/private/tmp`` on macOS and one
#: directory answering to two names would otherwise be half-guarded.
#:
#: Known gap: macOS also sweeps ``$TMPDIR``, a per-user directory under
#: ``/var/folders``, which is not named here. It is deliberately not added —
#: that is also where every test builds its workspace, so including it would
#: refuse the fixtures rather than a real operator's path. Nobody types a
#: ``/var/folders`` path into a profile by hand; the directory an operator
#: actually reaches for, and the one that took two prepared worktrees, is
#: ``/tmp``.
_EPHEMERAL_ROOTS = tuple(
    dict.fromkeys(
        Path(candidate).resolve()
        for candidate in ("/tmp", "/private/tmp", "/var/tmp", "/private/var/tmp")
    )
)


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
    #: How long this Host's own services may take to answer after an
    #: activation. A board is not a laptop — the Channel worker alone spends
    #: its stop timeout shutting down and then loads an ONNX model coming up —
    #: and a deadline too short turns a healthy release into a rolled-back one.
    #: A platform property, so it is stated per Host rather than compiled in.
    readiness_timeout_seconds: int = 240

    @property
    def target(self) -> str:
        return f"{self.user}@{self.hostname}"


@dataclass(frozen=True, slots=True)
class WorkspaceConfig:
    bundle_root: Path
    release_cli: Path
    #: Where Ops keeps the pinned workstation build tools it materializes.
    #: A version and a digest decide what goes here; nothing outside Ops has to
    #: have built anything for a release to be sealable.
    toolchain_root: Path
    #: An override for the pinned uv, for a workstation that must use its own.
    #: Absent means the pinned one, which is the case worth defaulting to: a
    #: path someone has to keep alive is not a pin.
    uv: Path | None
    python_index_url: str
    python_http_timeout_seconds: int
    python_http_retries: int
    python_concurrent_downloads: int


@dataclass(frozen=True, slots=True)
class SourceConfig:
    path: Path
    #: An *intentional* commit, or nothing at all.
    #:
    #: This field used to be required, and that requirement is what shipped a
    #: release nobody meant to ship: a cross-repository change was written and
    #: tested on every HEAD, ``deploy`` reported success, and the board kept
    #: running the commits written down here — then three deploys failed
    #: because a new release's virtualenv held the old SDK. Two copies of one
    #: fact were kept by hand, so they were only ever accidentally equal.
    #:
    #: The repository HEAD is now the fact, and this is a deliberate exception
    #: to it: writing a commit here says "reproduce this exact combination",
    #: the same thing ``--revision`` says for one run. Nothing writes it back.
    revision: str | None = None
    #: Optional human label for the commit. The commit is the identity — a tag
    #: is a movable reference, so it annotates the release rather than defining
    #: it, and Ops proves it still resolves to the commit that was resolved.
    tag: str | None = None


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

    def with_source_overrides(self, values: Mapping[str, SourceConfig]) -> OperationsConfig:
        sources = dict(self.sources)
        unknown = set(values).difference(sources)
        if unknown:
            raise ConfigurationError(
                "source override contains unknown source: " + ", ".join(sorted(unknown))
            )
        for source_id, source in values.items():
            if source.revision is not None:
                _require_revision(source.revision, f"source override for {source_id}")
            sources[source_id] = source
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
        optional={"readiness_timeout_seconds"},
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
    readiness = _integer(
        host_wire.get("readiness_timeout_seconds", 240),
        "host.readiness_timeout_seconds",
        minimum=30,
        maximum=1800,
    )

    workspace_wire = _mapping(document["workspace"], "workspace")
    _require_keys(
        workspace_wire,
        required={
            "bundle_root",
            "release_cli",
            "python_index_url",
            "python_http_timeout_seconds",
            "python_http_retries",
            "python_concurrent_downloads",
        },
        optional={"uv", "toolchain_root"},
        label="workspace",
    )
    workspace = WorkspaceConfig(
        bundle_root=_local_path(workspace_wire["bundle_root"], base, "workspace.bundle_root"),
        release_cli=_durable_local_path(
            workspace_wire["release_cli"], base, "workspace.release_cli"
        ),
        toolchain_root=_durable_local_path(
            workspace_wire.get("toolchain_root", "../.eidolon-ops/toolchain"),
            base,
            "workspace.toolchain_root",
        ),
        uv=(
            _durable_local_path(workspace_wire["uv"], base, "workspace.uv")
            if "uv" in workspace_wire
            else None
        ),
        python_index_url=_https_index_url(
            workspace_wire["python_index_url"], "workspace.python_index_url"
        ),
        python_http_timeout_seconds=_integer(
            workspace_wire["python_http_timeout_seconds"],
            "workspace.python_http_timeout_seconds",
            minimum=10,
            maximum=600,
        ),
        python_http_retries=_integer(
            workspace_wire["python_http_retries"],
            "workspace.python_http_retries",
            minimum=0,
            maximum=20,
        ),
        python_concurrent_downloads=_integer(
            workspace_wire["python_concurrent_downloads"],
            "workspace.python_concurrent_downloads",
            minimum=1,
            maximum=16,
        ),
    )

    sources_wire = _mapping(document["sources"], "sources")
    if set(sources_wire) != set(SOURCE_IDS):
        raise ConfigurationError(
            "sources must be exactly Kernel/Data/Hub/Admin/Agent/Channel/Memory/SDK"
        )
    sources: dict[str, SourceConfig] = {}
    for source_id in SOURCE_IDS:
        source_wire = _mapping(sources_wire[source_id], f"sources.{source_id}")
        _require_keys(
            source_wire,
            required={"path"},
            optional={"revision", "tag"},
            label=f"sources.{source_id}",
        )
        revision: str | None = None
        if "revision" in source_wire:
            revision = _string(source_wire["revision"], f"sources.{source_id}.revision")
            _require_revision(revision, f"sources.{source_id}.revision")
        raw_tag = source_wire.get("tag")
        tag = None if raw_tag is None else _string(raw_tag, f"sources.{source_id}.tag")
        if tag is not None and _TAG.fullmatch(tag) is None:
            raise ConfigurationError(f"sources.{source_id}.tag is invalid")
        sources[source_id] = SourceConfig(
            path=_durable_local_path(
                source_wire["path"], base, f"sources.{source_id}.path"
            ),
            revision=revision,
            tag=tag,
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
            readiness_timeout_seconds=readiness,
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


def _durable_local_path(value: object, base: Path, label: str) -> Path:
    """A path a release input is allowed to be pinned to.

    The system temp directory is swept without warning, and both a pinned
    build tool and a pinned source repository were once kept there. Neither
    announced anything when it went: the release line simply stopped, days
    later, with an error naming a file rather than the reason it was gone.

    Pinning something to a directory the operating system may empty is not
    pinning it, so it is refused where it is written rather than where it is
    eventually missed. Outputs are a different matter — a bundle is rebuilt
    from its inputs, and temp is the right place for it.
    """

    path = _local_path(value, base, label)
    for temporary in _EPHEMERAL_ROOTS:
        if path == temporary or temporary in path.parents:
            raise ConfigurationError(
                f"{label} must not live under {temporary}: the system empties it, "
                "and a release input that can vanish is not pinned"
            )
    return path


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


def _https_index_url(value: object, label: str) -> str:
    text = _string(value, label)
    if len(text) > 2048 or any(character.isspace() for character in text):
        raise ConfigurationError(f"{label} must be a bounded HTTPS URL")
    try:
        parsed = urlsplit(text)
        port = parsed.port
    except ValueError as exc:
        raise ConfigurationError(f"{label} must be a valid HTTPS URL") from exc
    if (
        parsed.scheme != "https"
        or parsed.hostname is None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or not parsed.path.startswith("/")
        or (port is not None and not 1 <= port <= 65535)
        or _PYTHON_INDEX_URL.fullmatch(text) is None
    ):
        raise ConfigurationError(
            f"{label} must be HTTPS without credentials, query parameters, or fragments"
        )
    return text


def _require_revision(value: str, label: str) -> None:
    if _REVISION.fullmatch(value) is None:
        raise ConfigurationError(f"{label} must be a full lowercase 40-hex commit")
