"""Drive a product Host through the systemd units its release installs."""

from __future__ import annotations

from eidolon_ops.controller import EidolonPiController
from eidolon_ops.model import Capability
from eidolon_ops.ports import SupervisorKind

_UNIT_SUFFIX = ".service"
_UNIT_PREFIX = "eidolon-"
#: The one product unit that does not carry the component prefix.
_MANAGER_UNIT = "eidolond"


class SystemdSupervisor:
    """The Pi adapter: units, a journal, and the release that installed them."""

    kind = SupervisorKind.SYSTEMD

    def __init__(self, release: EidolonPiController) -> None:
        self.release = release

    @property
    def capabilities(self) -> frozenset[Capability]:
        return frozenset(
            {
                Capability.STATUS,
                Capability.DOCTOR,
                Capability.APP_READY,
                Capability.LIFECYCLE,
                Capability.LOGS,
                Capability.LOG_HISTORY,
                Capability.COMMISSIONING_CODE,
                Capability.INIT_INPUTS,
                Capability.CONVERGE_INPUTS,
                Capability.INSTALL,
                Capability.DEPLOY,
                Capability.ROLLBACK,
                Capability.BACKUP,
                Capability.RESTORE,
                Capability.RESET,
                Capability.CONTROLLER_RESET,
                Capability.AUTHORITY_RESET,
                Capability.DIAGNOSE,
            }
        )

    def status(self) -> dict[str, object]:
        return self.release.status()

    def doctor(self, *, release_id: str | None = None) -> dict[str, object]:
        return self.release.doctor(release_id=release_id)

    def app_ready(self) -> dict[str, object]:
        return self.release.app_ready()

    def converge_inputs(self, *, apply: bool) -> dict[str, object]:
        return self.release.converge_inputs(apply=apply)

    def lifecycle(self, action: str, *, dry_run: bool) -> dict[str, object]:
        return self.release.lifecycle(action, dry_run=dry_run)

    def logs(self, *, service: str | None, lines: int, since: str | None) -> dict[str, object]:
        return self.release.logs(unit=unit_name(service), lines=lines, since=since)

    def commissioning_code(self, *, ttl_seconds: int) -> dict[str, object]:
        return self.release.commissioning_code(ttl_seconds=ttl_seconds)

    def reset(self, *, wipe_authority_data: bool, apply: bool) -> dict[str, object]:
        # Unchanged behaviour, reached the way this Host's other operations are:
        # the release installed the namespace, so the release clears it.
        return self.release.reset(wipe_authority_data=wipe_authority_data, apply=apply)


def unit_name(service: str | None) -> str | None:
    """Accept the component name an operator types, not only the unit name."""

    if service is None or service.endswith(_UNIT_SUFFIX):
        return service
    if not service.startswith(_UNIT_PREFIX) and service != _MANAGER_UNIT:
        service = f"{_UNIT_PREFIX}{service}"
    return f"{service}{_UNIT_SUFFIX}"
