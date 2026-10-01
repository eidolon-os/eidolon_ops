"""Execution policy, independent of the Host's operating system and hardware.

Only compositions with an implemented lifecycle are accepted. Legacy driver
names are compatibility aliases for these compositions, never platform defaults.
"""

from dataclasses import dataclass
from enum import StrEnum

from eidolon_ops.ports import PackageManagerKind, SupervisorKind, TransportKind


class ExecutionIdentity(StrEnum):
    CURRENT_USER = "current-user"
    SERVICE_ACCOUNTS = "service-accounts"


@dataclass(frozen=True, slots=True)
class HostExecution:
    transport: TransportKind
    supervisor: SupervisorKind
    packages: PackageManagerKind
    identity: ExecutionIdentity

    @property
    def source_run(self) -> bool:
        return self.supervisor is SupervisorKind.SUPERVISORD

    def describe(self) -> dict[str, str]:
        return {
            "transport": str(self.transport), "supervisor": str(self.supervisor),
            "packages": str(self.packages), "identity": str(self.identity),
        }


DRIVER_EXECUTIONS = {
    "local-supervisord": HostExecution(
        TransportKind.LOCAL, SupervisorKind.SUPERVISORD,
        PackageManagerKind.NONE, ExecutionIdentity.CURRENT_USER,
    ),
    "ssh-systemd": HostExecution(
        TransportKind.SSH, SupervisorKind.SYSTEMD,
        PackageManagerKind.APT, ExecutionIdentity.SERVICE_ACCOUNTS,
    ),
}


def compatible_driver(execution: HostExecution) -> str:
    for driver, supported in DRIVER_EXECUTIONS.items():
        if execution == supported:
            return driver
    raise ValueError(
        "execution composition has no implemented lifecycle: "
        + ", ".join(f"{key}={value}" for key, value in execution.describe().items())
    )
