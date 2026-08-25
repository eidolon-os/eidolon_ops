"""Compose a Host from its ports, and derive what it can be asked to do.

``HostController`` used to answer "is this operation allowed here" by comparing
the profile's driver string, in nine places, to one of two literals. A Host now
says what it can do: the composition below is the answer, and ``doctor``
publishes it so an operator — and later a console — can read the operation set
off the Host instead of off this source file.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from eidolon_ops.adapters.local_transport import LocalTransport
from eidolon_ops.adapters.packages import AptPackages, UnmanagedPackages
from eidolon_ops.adapters.supervisord import SupervisordSupervisor
from eidolon_ops.adapters.systemd import SystemdSupervisor
from eidolon_ops.config import load_config
from eidolon_ops.controller import EidolonPiController
from eidolon_ops.errors import OperationsError
from eidolon_ops.model import Capability
from eidolon_ops.paths import (
    HostDriver,
    HostPlatform,
    HostProfile,
    HostProfileError,
    merged_environment,
)
from eidolon_ops.ports import PackageManager, Supervisor, Transport
from eidolon_ops.process import ProcessRunner
from eidolon_ops.progress import ProgressSink
from eidolon_ops.transport import SSHTransport


@dataclass(frozen=True, slots=True)
class PlatformProfile:
    """Data, not code: what this kind of machine is, and what it always offers."""

    id: str
    platform: HostPlatform


#: One entry per real platform. A third board that is also Linux/systemd/apt
#: belongs here as another row, not as another adapter.
PLATFORM_PROFILES = {
    HostPlatform.MACOS: PlatformProfile(id="macos-dev", platform=HostPlatform.MACOS),
    HostPlatform.RASPBERRY_PI: PlatformProfile(
        id="linux-debian13-arm64", platform=HostPlatform.RASPBERRY_PI
    ),
}


@dataclass(frozen=True, slots=True)
class HostAdapter:
    """One Host, composed of a platform and three ports."""

    platform: PlatformProfile
    transport: Transport
    supervisor: Supervisor
    packages: PackageManager
    #: The release transaction executor, present only where releases are
    #: installed. One implementation, so a module rather than a port.
    release: EidolonPiController | None

    @property
    def capabilities(self) -> frozenset[Capability]:
        return self.supervisor.capabilities | self.packages.capabilities

    def require(self, capability: Capability) -> None:
        if capability not in self.capabilities:
            raise OperationsError(
                f"{capability} is not available on this Host "
                f"({self.platform.id}/{self.supervisor.kind}/{self.packages.kind})"
            )

    def require_release(self, capability: Capability) -> EidolonPiController:
        self.require(capability)
        if self.release is None:
            raise OperationsError(f"{capability} has no release executor on this Host")
        return self.release

    def describe(self) -> dict[str, object]:
        return {
            "platform_profile": self.platform.id,
            "transport": str(self.transport.kind),
            "supervisor": str(self.supervisor.kind),
            "packages": str(self.packages.kind),
            "capabilities": sorted(str(capability) for capability in self.capabilities),
        }


def build_adapter(
    profile: HostProfile,
    runner: ProcessRunner,
    *,
    revision_overrides: tuple[str, ...] = (),
    allow_dirty: bool = False,
    progress: ProgressSink | None = None,
) -> HostAdapter:
    """Assemble the adapter this profile describes."""

    platform = PLATFORM_PROFILES[profile.platform]
    if profile.driver is HostDriver.LOCAL_SUPERVISORD:
        return _source_adapter(profile, runner, platform, revision_overrides, progress)
    return _product_adapter(
        profile, runner, platform, revision_overrides, progress, allow_dirty=allow_dirty
    )


def _source_adapter(
    profile: HostProfile,
    runner: ProcessRunner,
    platform: PlatformProfile,
    revision_overrides: tuple[str, ...],
    progress: ProgressSink | None = None,
) -> HostAdapter:
    transport = LocalTransport(
        runner,
        cwd=_workspace_root(profile),
        env=merged_environment(profile),
    )
    supervisor = SupervisordSupervisor(
        profile,
        transport,
        _product_factory(profile, runner, revision_overrides),
        progress,
    )
    return HostAdapter(
        platform=platform,
        transport=transport,
        supervisor=supervisor,
        packages=UnmanagedPackages(),
        release=None,
    )


def _product_adapter(
    profile: HostProfile,
    runner: ProcessRunner,
    platform: PlatformProfile,
    revision_overrides: tuple[str, ...],
    progress: ProgressSink | None = None,
    *,
    allow_dirty: bool = False,
) -> HostAdapter:
    config_path = profile.operations_config
    if config_path is None:
        raise OperationsError("Pi host profile does not reference an operations config")
    config = load_config(config_path).with_revision_overrides(revision_overrides)
    transport = SSHTransport(config.host, runner)
    release = EidolonPiController(
        config,
        runner,
        transport=transport,
        app=profile.app,
        progress=progress,
        allow_dirty=allow_dirty,
    )
    return HostAdapter(
        platform=platform,
        transport=transport,
        supervisor=SystemdSupervisor(release),
        packages=AptPackages(release),
        release=release,
    )


def _product_factory(
    profile: HostProfile,
    runner: ProcessRunner,
    revision_overrides: tuple[str, ...],
) -> Callable[[], object]:
    def build() -> object:
        from eidolon_ops.local_product import LocalProductSource

        config_path = profile.operations_config
        if config_path is None:
            raise OperationsError("Mac product-source profile has no operations config")
        config = (
            load_config(config_path)
            .with_source_overrides(profile.source_overrides)
            .with_revision_overrides(revision_overrides)
        )
        return LocalProductSource(profile, config, runner)

    return build


def _workspace_root(profile: HostProfile) -> Path:
    """Where the lifecycle script expects to be run from.

    The profile answers this now; the wrapper stays because the failure an
    operator sees here should be an operations error, not a profile one.
    """

    try:
        return profile.workspace_root
    except HostProfileError as exc:
        raise OperationsError(str(exc)) from exc
