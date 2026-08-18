"""Which Hosts this console manages, and what each says it can do.

A console entry is a Host profile file, nothing more. The set is discovered the
way an operator already thinks of it — the profiles in ``config/hosts`` — and a
profile that does not load is listed with its error rather than hidden, because
a Host missing from the console looks like a Host that does not exist.

Capabilities are asked of the composed adapter, never derived from the platform
here. That is the whole point of ``HostAdapter.describe``: the console offers
``install`` on a board and ``debug`` on a workstation because those Hosts say
so, and a third platform would need no change on this side.
"""

from __future__ import annotations

import time
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from eidolon_ops.config import ConfigurationError, load_config
from eidolon_ops.console.errors import ConsoleError
from eidolon_ops.errors import OperationsError
from eidolon_ops.host import build_adapter
from eidolon_ops.host_controller import HostController
from eidolon_ops.model import Capability
from eidolon_ops.paths import HostDriver, HostProfile, HostProfileError, load_host_profile
from eidolon_ops.process import SubprocessRunner
from eidolon_ops.progress import ProgressSink

__all__ = ["HostEntry", "HostRegistry", "discover"]

#: A profile named as an example is a template for an operator to copy, not a
#: Host. Listing it would offer operations against a hostname nobody owns.
_TEMPLATE_SUFFIX = ".example.toml"
_LOAD_ERRORS = (HostProfileError, ConfigurationError, OperationsError, OSError, KeyError)


def discover(location: Path) -> tuple[Path, ...]:
    """Every Host profile in one directory, or the one file named."""

    if location.is_file():
        return (location,)
    if not location.is_dir():
        raise ConsoleError(f"no Host profile or profile directory at {location}", status=404)
    return tuple(
        sorted(
            path
            for path in location.glob("*.toml")
            if not path.name.endswith(_TEMPLATE_SUFFIX)
        )
    )


@dataclass(frozen=True, slots=True)
class HostEntry:
    """One profile, loaded or not."""

    path: Path
    profile: HostProfile | None
    capabilities: frozenset[Capability]
    adapter: dict[str, object] | None
    error: str | None

    @property
    def host_id(self) -> str:
        return self.profile.host_id if self.profile is not None else self.path.stem

    def to_json(self) -> dict[str, object]:
        document: dict[str, object] = {
            "host_id": self.host_id,
            "profile_path": str(self.path),
            "error": self.error,
            "capabilities": sorted(str(item) for item in self.capabilities),
            "adapter": self.adapter,
        }
        if self.profile is not None:
            document.update(
                platform=str(self.profile.platform),
                driver=str(self.profile.driver),
                paths=_paths(self.profile),
                app=_app(self.profile),
                source_overrides=sorted(self.profile.source_overrides),
            )
        return document


class HostRegistry:
    """The profiles this console was started with."""

    def __init__(self, paths: Iterable[Path]) -> None:
        self._paths = tuple(dict.fromkeys(Path(path).expanduser().resolve() for path in paths))
        if not self._paths:
            raise ConsoleError("this console was given no Host profile to manage")

    @property
    def paths(self) -> tuple[Path, ...]:
        return self._paths

    def entries(self) -> tuple[HostEntry, ...]:
        """Load every profile now.

        Deliberately not cached. A profile is a file an operator edits between
        two operations — pinning a different source override, moving a port —
        and a console that answered from a snapshot would be describing a Host
        that no longer exists. Loading is a TOML parse and some dataclasses.
        """

        return tuple(_load(path) for path in self._paths)

    def entry(self, host_id: str) -> HostEntry:
        for item in self.entries():
            if item.host_id == host_id:
                return item
        raise ConsoleError(f"no such Host: {host_id}", status=404)

    def profile(self, host_id: str) -> HostProfile:
        entry = self.entry(host_id)
        if entry.profile is None:
            raise ConsoleError(f"{host_id} does not load: {entry.error}", status=409)
        return entry.profile

    def controller(self, host_id: str, *, progress: ProgressSink | None = None) -> HostController:
        """The same controller the CLI builds, told where to report progress."""

        return HostController(self.profile(host_id), SubprocessRunner(), progress=progress)

    def suggestions(self, host_id: str) -> dict[str, list[str]]:
        """Values worth offering for a free-text field, per Host.

        Suggestions, not choices: ``logs`` accepts a component name, a unit
        name, or a relative log path depending on the supervisor, and the
        supervisor is the one that decides — this only saves the typing.
        """

        entry = self.entry(host_id)
        profile = entry.profile
        if profile is None:
            return {}
        if profile.driver is HostDriver.LOCAL_SUPERVISORD:
            root = profile.paths.log_root
            if not root.is_dir():
                return {"service": []}
            names = sorted(
                str(path.relative_to(root))
                for path in root.iterdir()
                if path.is_dir() or path.suffix == ".log"
            )
            return {"service": names}
        config_path = profile.operations_config
        if config_path is None:
            return {"service": []}
        try:
            return {"service": list(load_config(config_path).units)}
        except _LOAD_ERRORS:
            return {"service": []}

    def artifact_default(self, host_id: str, kind: str, suffix: str) -> str:
        """A local path to suggest for an artifact this console asks a Host for.

        On this workstation, always: a Pi profile's path roles describe the
        board, so its cache root is somewhere the operator cannot open. The
        stamp is part of the name because a backup that silently replaced
        yesterday's would be the one time it mattered.
        """

        profile = self.profile(host_id)
        root: Path | None = None
        config_path = profile.operations_config
        if config_path is not None:
            try:
                root = load_config(config_path).workspace.bundle_root.parent
            except _LOAD_ERRORS:
                root = None
        if root is None:
            root = profile.paths.cache_root
        stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime())
        return str(root / "eidolon-ops-console" / f"{host_id}-{kind}-{stamp}{suffix}")


def _load(path: Path) -> HostEntry:
    try:
        profile = load_host_profile(path)
    except _LOAD_ERRORS as exc:
        return HostEntry(
            path=path,
            profile=None,
            capabilities=frozenset(),
            adapter=None,
            error=f"{type(exc).__name__}: {exc}",
        )
    try:
        adapter = build_adapter(profile, SubprocessRunner())
    except _LOAD_ERRORS as exc:
        # The profile is readable but the Host it describes cannot be composed —
        # usually an operations config that does not load. Still a Host, still
        # listed, with the reason it can be asked nothing.
        return HostEntry(
            path=path,
            profile=profile,
            capabilities=frozenset(),
            adapter=None,
            error=f"{type(exc).__name__}: {exc}",
        )
    return HostEntry(
        path=path,
        profile=profile,
        capabilities=adapter.capabilities,
        # ``describe`` reads the composition; it never asks the transport to
        # resolve a link, which would put a DNS lookup behind a page load.
        adapter=adapter.describe(),
        error=None,
    )


def _paths(profile: HostProfile) -> dict[str, str]:
    paths = profile.paths
    return {
        "install": str(paths.install_root),
        "current": str(paths.current_root),
        "config": str(paths.config_root),
        "state": str(paths.state_root),
        "runtime": str(paths.runtime_root),
        "logs": str(paths.log_root),
        "cache": str(paths.cache_root),
        "bootstrap_state": str(paths.bootstrap_state_root),
        "bootstrap_runtime": str(paths.bootstrap_runtime_root),
    }


def _app(profile: HostProfile) -> dict[str, object] | None:
    app = profile.app
    if app is None:
        return None
    return {
        "hub_https_port": app.hub_https_port,
        "livekit_client_url": app.livekit_client_url,
        "allow_insecure_livekit": app.allow_insecure_livekit,
    }
