"""Drive a macOS source run through its supervisord lifecycle script."""

from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path

from eidolon_ops.adapters.local_transport import LocalTransport
from eidolon_ops.errors import OperationsError
from eidolon_ops.model import Capability
from eidolon_ops.paths import HostProfile
from eidolon_ops.ports import SupervisorKind
from eidolon_ops.progress import Journal, ProgressSink

#: Every profile the lifecycle script is allowed to be asked about. One entry:
#: the implementation-level profiles this replaced are gone, and a second one
#: would be a second product topology on the same machine.
PROFILE = "product-source"
_PREPARING_OPERATIONS = frozenset({"start", "restart", "web-start", "web-restart"})
_HEALTH_REPORTING_OPERATIONS = frozenset({"start", "restart", "status"})
#: supervisord prints a per-program table; these mark a program that is not
#: running even though the group command itself returned success.
_UNHEALTHY_MARKERS = (" FATAL ", " BACKOFF ", " EXITED ")


class SupervisordSupervisor:
    """The Mac adapter: one product topology, driven by one script."""

    kind = SupervisorKind.SUPERVISORD

    def __init__(
        self,
        profile: HostProfile,
        transport: LocalTransport,
        product: Callable[[], object],
        progress: ProgressSink | None = None,
    ) -> None:
        self.profile = profile
        self.transport = transport
        self._product = product
        self.progress = progress

    @property
    def capabilities(self) -> frozenset[Capability]:
        return frozenset(
            {
                Capability.STATUS,
                Capability.DOCTOR,
                Capability.APP_READY,
                Capability.LIFECYCLE,
                Capability.LOGS,
                Capability.COMMISSIONING_CODE,
                Capability.SOURCE_PROFILE,
            }
        )

    def status(self) -> dict[str, object]:
        return self.profile_operation("status")

    def doctor(self, *, release_id: str | None = None) -> dict[str, object]:
        script = self.script()
        healthy = self.profile.paths.current_root.is_dir() and os.access(script, os.X_OK)
        return {
            "status": "healthy" if healthy else "degraded",
            "lifecycle_script": str(script),
        }

    def app_ready(self) -> dict[str, object]:
        return self._product().app_ready()

    def lifecycle(self, action: str, *, dry_run: bool) -> dict[str, object]:
        if dry_run:
            return {
                "status": "dry_run",
                "driver": str(self.profile.driver),
                "command": [str(self.script()), PROFILE, action],
                "environment": self.profile.environment(),
            }
        return self.profile_operation(action)

    def logs(self, *, service: str | None, lines: int, since: str | None) -> dict[str, object]:
        if lines < 1 or lines > 5000:
            raise OperationsError("lines must be between 1 and 5000")
        root = self.profile.paths.log_root
        target = root / service if service else root
        if root not in target.resolve(strict=False).parents and target != root:
            raise OperationsError("service log selector escapes the log root")
        files = [target] if target.is_file() else sorted(target.rglob("*.log"))
        output: dict[str, list[str]] = {}
        for path in files:
            if not path.is_file():
                continue
            content = path.read_text(encoding="utf-8", errors="replace").splitlines()
            output[str(path.relative_to(root))] = content[-lines:]
        return {"status": "ok", "host_id": self.profile.host_id, "logs": output}

    def commissioning_code(self, *, ttl_seconds: int) -> dict[str, object]:
        return self.profile_operation(
            "commissioning-code", arguments=("--ttl", str(ttl_seconds))
        )

    def profile_operation(
        self, operation: str, *, arguments: tuple[str, ...] = ()
    ) -> dict[str, object]:
        """Run one operation of the source-run profile, and read its health.

        A source run has three parts an operator waits on separately —
        materializing the pinned topology, the supervisord command itself, and
        the health wait afterwards. The journal is local because this report
        has no ``phases`` key to add them to; announcing them is still what
        turns a two-minute silence into three named waits.
        """

        script = self.script()
        product = self._product()
        phases = Journal(self.progress)
        if operation in {"prepare", "validate"}:
            phases.begin(operation)
            direct = product.prepare() if operation == "prepare" else product.validate()
            phases.append({"phase": operation, "result": direct})
            return direct
        if operation in _PREPARING_OPERATIONS:
            phases.begin("prepare")
            phases.append({"phase": "prepare", "result": product.prepare()})
        phases.begin(operation)
        result = self.transport.run(
            (str(script), PROFILE, operation, *arguments),
            timeout=300,
            operation=f"local {PROFILE} {operation}",
        )
        response: dict[str, object] = {
            "status": "ok",
            "host_id": self.profile.host_id,
            "profile": PROFILE,
            "operation": operation,
            "output": result.stdout.strip(),
        }
        phases.append({"phase": operation, "result": {"output": response["output"]}})
        if operation in _HEALTH_REPORTING_OPERATIONS:
            phases.begin("health")
            health = product.health(wait_seconds=120 if operation != "status" else 0)
            unhealthy_process = any(marker in result.stdout for marker in _UNHEALTHY_MARKERS)
            response["health"] = health
            response["status"] = (
                "healthy" if health["status"] == "healthy" and not unhealthy_process else "degraded"
            )
            phases.append({"phase": "health", "result": health})
        return response

    def script(self) -> Path:
        script = self.profile.lifecycle_script
        if script is None or not script.is_file() or not os.access(script, os.X_OK):
            raise OperationsError(f"local lifecycle script is missing or not executable: {script}")
        return script
