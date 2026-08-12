"""Host-neutral filesystem contract for Eidolon operations.

The names in this module describe lifecycle and durability, not one operating
system's directory convention.  A host profile maps the same roles to macOS or
Linux paths.  Components consume the exported environment variables and never
derive product state from ``HOME``.
"""

from __future__ import annotations

import os
import re
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from ipaddress import IPv4Address, ip_address
from pathlib import Path
from types import MappingProxyType
from typing import Any, Literal
from urllib.parse import urlparse

from eidolon_ops.config import SOURCE_IDS, SourceConfig


class HostProfileError(ValueError):
    """A host profile is incomplete, ambiguous, or unsafe."""


class HostPlatform(StrEnum):
    """The machine a Host profile describes."""

    MACOS = "macos"
    RASPBERRY_PI = "raspberry-pi"


class HostDriver(StrEnum):
    """How Ops reaches a Host and drives its services.

    A closed enumeration rather than a string, because this value used to be
    compared literally in nine places to decide what an operation was allowed
    to do. What a Host can do is now derived from the adapter this selects.
    """

    LOCAL_SUPERVISORD = "local-supervisord"
    SSH_SYSTEMD = "ssh-systemd"


_HOST_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
#: Which driver each platform is reachable through. One entry per real Host;
#: a pair that is not in this table is not a Host this tool has an adapter for.
_PLATFORM_DRIVERS = {
    HostPlatform.MACOS: HostDriver.LOCAL_SUPERVISORD,
    HostPlatform.RASPBERRY_PI: HostDriver.SSH_SYSTEMD,
}
_FOUNDATION_MODES = {"external"}
_IPV4_LITERAL = re.compile(r"^\d{1,3}(?:\.\d{1,3}){3}$")
_PATH_FIELDS = (
    "install_root",
    "current_root",
    "config_root",
    "state_root",
    "runtime_root",
    "log_root",
    "cache_root",
    "bootstrap_state_root",
    "bootstrap_runtime_root",
)


@dataclass(frozen=True, slots=True)
class HostPaths:
    """Absolute locations classified by ownership and lifetime."""

    install_root: Path
    current_root: Path
    config_root: Path
    state_root: Path
    runtime_root: Path
    log_root: Path
    cache_root: Path
    bootstrap_state_root: Path
    bootstrap_runtime_root: Path

    def environment(self) -> dict[str, str]:
        return {
            "EIDOLON_INSTALL_ROOT": str(self.install_root),
            "EIDOLON_WORKSPACE_ROOT": str(self.current_root),
            # Compatibility for components that have not yet renamed this
            # long-standing variable.  It has exactly the same value and is
            # not a second source of truth.
            "EIDOLON_ROOT": str(self.current_root),
            "EIDOLON_CONFIG_ROOT": str(self.config_root),
            "EIDOLON_STATE_ROOT": str(self.state_root),
            "EIDOLON_RUNTIME_ROOT": str(self.runtime_root),
            "EIDOLON_LOG_ROOT": str(self.log_root),
            "EIDOLON_CACHE_ROOT": str(self.cache_root),
            "EIDOLON_BOOTSTRAP_STATE_ROOT": str(self.bootstrap_state_root),
            "EIDOLON_BOOTSTRAP_RUNTIME_ROOT": str(self.bootstrap_runtime_root),
            # Bootstrap predates the host profile contract and consumes DIR
            # names.  They are aliases, not independently configurable paths.
            "EIDOLON_BOOTSTRAP_STATE_DIR": str(self.bootstrap_state_root),
            "EIDOLON_BOOTSTRAP_RUNTIME_DIR": str(self.bootstrap_runtime_root),
        }


@dataclass(frozen=True, slots=True)
class AppAccess:
    """Device-reachable application endpoints for one Host.

    ``lan_ipv4`` is optional because an address is observed state, not a
    decision. A Host that gets its address from DHCP has no stable value to
    declare, and a declaration that silently goes stale makes every reachability
    check ambiguous: you cannot tell a real network fault from an old config.
    Leave it out and the Host reports the address it currently has.
    """

    lan_ipv4: IPv4Address | None
    hub_https_port: int
    livekit_client_url: str
    allow_insecure_livekit: bool


@dataclass(frozen=True, slots=True)
class HostProfile:
    path: Path
    host_id: str
    platform: HostPlatform
    driver: HostDriver
    paths: HostPaths
    lifecycle_script: Path | None
    operations_config: Path | None
    foundation_mode: Literal["external"] | None = None
    external_livekit_config: Path | None = None
    app: AppAccess | None = None
    source_overrides: Mapping[str, SourceConfig] = field(
        default_factory=lambda: MappingProxyType({})
    )

    def environment(self) -> dict[str, str]:
        values = self.paths.environment()
        values.update(
            {
                "EIDOLON_HOST_ID": self.host_id,
                "EIDOLON_HOST_PLATFORM": str(self.platform),
                "EIDOLON_HOST_DRIVER": str(self.driver),
            }
        )
        if self.foundation_mode is not None:
            values["EIDOLON_FOUNDATION_MODE"] = self.foundation_mode
        return values


def load_host_profile(path: Path) -> HostProfile:
    resolved = path.expanduser().resolve()
    try:
        document = tomllib.loads(resolved.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise HostProfileError(f"host profile is unreadable: {resolved}") from exc
    allowed_root = {"schema_version", "host", "paths", "adapter", "app", "source_overrides"}
    if not {"schema_version", "host", "paths", "adapter"}.issubset(document) or not set(
        document
    ).issubset(allowed_root):
        raise HostProfileError(
            "host profile root must contain schema_version, host, paths and adapter, with only app "
            "and source_overrides optional"
        )
    if document["schema_version"] != 1:
        raise HostProfileError("host profile schema_version must be 1")

    host = _table(document["host"], "host")
    if set(host) != {"id", "platform", "driver"}:
        raise HostProfileError("host must contain exactly id, platform and driver")
    host_id = _text(host["id"], "host.id")
    if _HOST_ID.fullmatch(host_id) is None:
        raise HostProfileError("host.id is invalid")
    platform = _member(HostPlatform, host["platform"], "host.platform")
    driver = _member(HostDriver, host["driver"], "host.driver")
    if _PLATFORM_DRIVERS[platform] is not driver:
        raise HostProfileError("host platform and driver are incompatible")

    paths_wire = _table(document["paths"], "paths")
    if set(paths_wire) != set(_PATH_FIELDS):
        raise HostProfileError(f"paths must contain exactly {', '.join(_PATH_FIELDS)}")
    paths = HostPaths(
        **{name: _absolute_path(paths_wire[name], f"paths.{name}") for name in _PATH_FIELDS}
    )
    _validate_paths(paths, platform=platform)

    adapter = _table(document["adapter"], "adapter")
    base = resolved.parent
    lifecycle_script: Path | None = None
    operations_config: Path | None = None
    foundation_mode: Literal["external"] | None = None
    external_livekit_config: Path | None = None
    if driver is HostDriver.LOCAL_SUPERVISORD:
        if set(adapter) != {
            "lifecycle_script",
            "operations_config",
            "foundation_mode",
            "external_livekit_config",
        }:
            raise HostProfileError(
                "local adapter must contain lifecycle, operations and foundation settings"
            )
        lifecycle_script = _local_path(
            adapter["lifecycle_script"], base, "adapter.lifecycle_script"
        )
        operations_config = _local_path(
            adapter["operations_config"], base, "adapter.operations_config"
        )
        mode = _text(adapter["foundation_mode"], "adapter.foundation_mode")
        if mode not in _FOUNDATION_MODES:
            raise HostProfileError(
                f"adapter.foundation_mode must be one of {sorted(_FOUNDATION_MODES)}"
            )
        foundation_mode = mode  # type: ignore[assignment]
        external_livekit_config = _local_path(
            adapter["external_livekit_config"], base, "adapter.external_livekit_config"
        )
    else:
        if set(adapter) != {"operations_config"}:
            raise HostProfileError("ssh adapter must contain only operations_config")
        operations_config = _local_path(
            adapter["operations_config"], base, "adapter.operations_config"
        )

    source_overrides = _source_overrides(document.get("source_overrides"), base=base)
    if source_overrides and driver is not HostDriver.LOCAL_SUPERVISORD:
        raise HostProfileError("source_overrides are available only for local-supervisord hosts")
    app = _app_access(document.get("app"))
    return HostProfile(
        path=resolved,
        host_id=host_id,
        platform=platform,
        driver=driver,
        paths=paths,
        lifecycle_script=lifecycle_script,
        operations_config=operations_config,
        foundation_mode=foundation_mode,
        external_livekit_config=external_livekit_config,
        app=app,
        source_overrides=source_overrides,
    )


def merged_environment(profile: HostProfile) -> dict[str, str]:
    environment = os.environ.copy()
    environment.update(profile.environment())
    return environment


def _validate_paths(paths: HostPaths, *, platform: HostPlatform) -> None:
    if paths.current_root == paths.install_root:
        if platform is not HostPlatform.MACOS:
            raise HostProfileError("Pi current_root must be a link below install_root")
    elif paths.install_root not in paths.current_root.parents:
        raise HostProfileError("current_root must equal or be below install_root")

    lifecycle_roots = (
        paths.config_root,
        paths.state_root,
        paths.runtime_root,
        paths.log_root,
        paths.cache_root,
    )
    if len(set(lifecycle_roots)) != len(lifecycle_roots):
        raise HostProfileError("config/state/runtime/log/cache roots must be distinct")
    if paths.bootstrap_state_root == paths.state_root:
        raise HostProfileError("Bootstrap state must remain a distinct ownership boundary")
    if paths.bootstrap_runtime_root == paths.runtime_root:
        raise HostProfileError("Bootstrap runtime must remain a distinct ownership boundary")
    if platform is HostPlatform.RASPBERRY_PI:
        expected = {
            "install_root": Path("/opt/eidolon"),
            "current_root": Path("/opt/eidolon/current"),
            "config_root": Path("/etc/eidolon"),
            "state_root": Path("/var/lib/eidolon"),
            "runtime_root": Path("/run/eidolon"),
            "log_root": Path("/var/log/eidolon"),
            "cache_root": Path("/var/cache/eidolon"),
            "bootstrap_state_root": Path("/var/lib/eidolon-bootstrap"),
            "bootstrap_runtime_root": Path("/run/eidolon-bootstrap"),
        }
        for name, value in expected.items():
            if getattr(paths, name) != value:
                raise HostProfileError(f"Pi paths.{name} must be {value}")


def _table(value: object, label: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise HostProfileError(f"{label} must be a table")
    return value


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise HostProfileError(f"{label} must be a non-empty string")
    return value.strip()


def _member(enumeration: type[Any], value: object, label: str) -> Any:
    try:
        return enumeration(_text(value, label))
    except ValueError as exc:
        allowed = ", ".join(sorted(str(member) for member in enumeration))
        raise HostProfileError(f"{label} must be one of {allowed}") from exc


def _app_access(value: object | None) -> AppAccess | None:
    if value is None:
        return None
    document = _table(value, "app")
    required = {"hub_https_port", "livekit_client_url", "allow_insecure_livekit"}
    if not required <= set(document) or not set(document) <= (required | {"lan_ipv4"}):
        raise HostProfileError(
            f"app must contain exactly {', '.join(sorted(required))}, with lan_ipv4 optional"
        )
    address: IPv4Address | None = None
    if "lan_ipv4" in document:
        try:
            address = ip_address(_text(document["lan_ipv4"], "app.lan_ipv4"))
        except ValueError as exc:
            raise HostProfileError("app.lan_ipv4 must be a private IPv4 address") from exc
        if not isinstance(address, IPv4Address) or not address.is_private or address.is_loopback:
            raise HostProfileError("app.lan_ipv4 must be a private IPv4 address")
    port = document["hub_https_port"]
    if not isinstance(port, int) or isinstance(port, bool) or not 1 <= port <= 65535:
        raise HostProfileError("app.hub_https_port must be a valid TCP port")
    livekit_url = _text(document["livekit_client_url"], "app.livekit_client_url")
    parsed = urlparse(livekit_url)
    if (
        parsed.scheme not in {"ws", "wss"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise HostProfileError("app.livekit_client_url must be a plain ws/wss origin")
    allow_insecure = document["allow_insecure_livekit"]
    if not isinstance(allow_insecure, bool):
        raise HostProfileError("app.allow_insecure_livekit must be boolean")
    if parsed.scheme == "ws" and not allow_insecure:
        raise HostProfileError("an insecure LiveKit URL requires explicit development opt-in")
    if address is None and _IPV4_LITERAL.fullmatch(parsed.hostname or "") is not None:
        raise HostProfileError(
            "app.livekit_client_url must not embed a literal address when lan_ipv4 is "
            "discovered; use the Host-bound hostname so it cannot go stale either"
        )
    if address is not None and parsed.scheme == "ws" and parsed.hostname != str(address):
        raise HostProfileError("an insecure LiveKit URL must use app.lan_ipv4")
    return AppAccess(
        lan_ipv4=address,
        hub_https_port=port,
        livekit_client_url=livekit_url.rstrip("/"),
        allow_insecure_livekit=allow_insecure,
    )


def _source_overrides(value: object | None, *, base: Path) -> Mapping[str, SourceConfig]:
    if value is None:
        return MappingProxyType({})
    document = _table(value, "source_overrides")
    unknown = set(document).difference(SOURCE_IDS)
    if unknown:
        raise HostProfileError(
            "source_overrides contains unknown source: " + ", ".join(sorted(unknown))
        )
    overrides: dict[str, SourceConfig] = {}
    for source_id, raw in document.items():
        source = _table(raw, f"source_overrides.{source_id}")
        if set(source) != {"path", "revision"}:
            raise HostProfileError(
                f"source_overrides.{source_id} must contain exactly path and revision"
            )
        revision = _text(source["revision"], f"source_overrides.{source_id}.revision")
        if re.fullmatch(r"[0-9a-f]{40}", revision) is None:
            raise HostProfileError(
                f"source_overrides.{source_id}.revision must be exactly 40 lowercase hex"
            )
        overrides[source_id] = SourceConfig(
            path=_local_path(source["path"], base, f"source_overrides.{source_id}.path"),
            revision=revision,
        )
    return MappingProxyType(overrides)


def _absolute_path(value: object, label: str) -> Path:
    path = Path(_text(value, label)).expanduser()
    if not path.is_absolute() or ".." in path.parts:
        raise HostProfileError(f"{label} must be a safe absolute path")
    # Host paths can describe a remote machine.  Normalize lexically without
    # resolving symlinks against the workstation filesystem.
    return Path(os.path.abspath(path))


def _local_path(value: object, base: Path, label: str) -> Path:
    path = Path(_text(value, label)).expanduser()
    if not path.is_absolute():
        path = base / path
    return path.resolve(strict=False)
