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
from dataclasses import dataclass
from pathlib import Path
from typing import Literal


class HostProfileError(ValueError):
    """A host profile is incomplete, ambiguous, or unsafe."""


_HOST_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_PLATFORMS = {"macos", "raspberry-pi"}
_DRIVERS = {"local-supervisord", "ssh-systemd"}
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
class HostProfile:
    path: Path
    host_id: str
    platform: Literal["macos", "raspberry-pi"]
    driver: Literal["local-supervisord", "ssh-systemd"]
    paths: HostPaths
    lifecycle_script: Path | None
    operations_config: Path | None

    def environment(self) -> dict[str, str]:
        values = self.paths.environment()
        values.update(
            {
                "EIDOLON_HOST_ID": self.host_id,
                "EIDOLON_HOST_PLATFORM": self.platform,
                "EIDOLON_HOST_DRIVER": self.driver,
            }
        )
        return values


def load_host_profile(path: Path) -> HostProfile:
    resolved = path.expanduser().resolve()
    try:
        document = tomllib.loads(resolved.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise HostProfileError(f"host profile is unreadable: {resolved}") from exc
    if set(document) != {"schema_version", "host", "paths", "adapter"}:
        raise HostProfileError(
            "host profile root must contain only schema_version, host, paths and adapter"
        )
    if document["schema_version"] != 1:
        raise HostProfileError("host profile schema_version must be 1")

    host = _table(document["host"], "host")
    if set(host) != {"id", "platform", "driver"}:
        raise HostProfileError("host must contain exactly id, platform and driver")
    host_id = _text(host["id"], "host.id")
    platform = _text(host["platform"], "host.platform")
    driver = _text(host["driver"], "host.driver")
    if _HOST_ID.fullmatch(host_id) is None:
        raise HostProfileError("host.id is invalid")
    if platform not in _PLATFORMS:
        raise HostProfileError(f"host.platform must be one of {sorted(_PLATFORMS)}")
    if driver not in _DRIVERS:
        raise HostProfileError(f"host.driver must be one of {sorted(_DRIVERS)}")
    if (platform, driver) not in {
        ("macos", "local-supervisord"),
        ("raspberry-pi", "ssh-systemd"),
    }:
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
    if driver == "local-supervisord":
        if set(adapter) != {"lifecycle_script"}:
            raise HostProfileError("local adapter must contain only lifecycle_script")
        lifecycle_script = _local_path(
            adapter["lifecycle_script"], base, "adapter.lifecycle_script"
        )
    else:
        if set(adapter) != {"operations_config"}:
            raise HostProfileError("ssh adapter must contain only operations_config")
        operations_config = _local_path(
            adapter["operations_config"], base, "adapter.operations_config"
        )

    return HostProfile(
        path=resolved,
        host_id=host_id,
        platform=platform,  # type: ignore[arg-type]
        driver=driver,  # type: ignore[arg-type]
        paths=paths,
        lifecycle_script=lifecycle_script,
        operations_config=operations_config,
    )


def merged_environment(profile: HostProfile) -> dict[str, str]:
    environment = os.environ.copy()
    environment.update(profile.environment())
    return environment


def _validate_paths(paths: HostPaths, *, platform: str) -> None:
    if paths.current_root == paths.install_root:
        if platform != "macos":
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
    if platform == "raspberry-pi":
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
