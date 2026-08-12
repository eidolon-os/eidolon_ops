"""The three boundaries a Host adapter is composed from.

Each has exactly two real implementations — local/ssh, supervisord/systemd,
none/apt — which is what makes them ports rather than modules. Nothing here
anticipates a third: a platform that is another Linux/systemd/apt board needs
a platform profile, not a new implementation.
"""

from __future__ import annotations

from collections.abc import Sequence
from enum import StrEnum
from typing import Protocol, runtime_checkable

from eidolon_ops.model import Capability
from eidolon_ops.process import ProcessResult


class TransportKind(StrEnum):
    LOCAL = "local"
    SSH = "ssh"


class SupervisorKind(StrEnum):
    SUPERVISORD = "supervisord"
    SYSTEMD = "systemd"


class PackageManagerKind(StrEnum):
    #: The operator's own machine; its packages are not Ops's to install.
    NONE = "none"
    APT = "apt"


@runtime_checkable
class Transport(Protocol):
    """How a command reaches the Host this profile describes."""

    kind: TransportKind

    def describe(self) -> str:
        """The link this transport settled on, for an operator's report."""

    def run(
        self,
        command: Sequence[str],
        *,
        timeout: float = 120,
        operation: str = "command",
    ) -> ProcessResult: ...


@runtime_checkable
class Supervisor(Protocol):
    """Whatever owns service lifetime on the Host."""

    kind: SupervisorKind

    @property
    def capabilities(self) -> frozenset[Capability]: ...

    def status(self) -> dict[str, object]: ...

    def doctor(self, *, release_id: str | None = None) -> dict[str, object]: ...

    def app_ready(self) -> dict[str, object]: ...

    def lifecycle(self, action: str, *, dry_run: bool) -> dict[str, object]: ...

    def logs(self, *, service: str | None, lines: int, since: str | None) -> dict[str, object]: ...

    def commissioning_code(self, *, ttl_seconds: int) -> dict[str, object]: ...


@runtime_checkable
class PackageManager(Protocol):
    """Whatever installs the non-Eidolon foundation the release assumes."""

    kind: PackageManagerKind

    @property
    def capabilities(self) -> frozenset[Capability]: ...

    def provision(self, *, apply: bool) -> dict[str, object]: ...

    def doctor(self) -> dict[str, object]: ...


__all__ = [
    "PackageManager",
    "PackageManagerKind",
    "Supervisor",
    "SupervisorKind",
    "Transport",
    "TransportKind",
]
