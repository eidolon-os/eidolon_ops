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
                # Not a release capability here. There is no release on this
                # Host, and the namespace a reset clears is entirely generated;
                # what makes it necessary is that Kernel and Hub refuse to
                # migrate an authority database they do not recognize, so a Host
                # behind the schema had no supported way back.
                Capability.RESET,
                # The narrow answer to the same problem the comment above
                # describes. RESET is how this Host got out of it before,
                # at the price of every other authority on the machine.
                Capability.KERNEL_SCHEMA_RESET,
                # The narrower repair still. A source run reaches bootstrapctl
                # through its own profile script, the same way it mints a
                # Setup code, so this Host can offer it without a release.
                Capability.OWNER_RESET,
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

    def reset(self, *, wipe_authority_data: bool, apply: bool) -> dict[str, object]:
        """Clear the generated namespace, after stopping what is using it.

        Stopping first is not tidiness: supervisord and every child hold open
        handles into the runtime root and read their settings from the config
        root, so clearing those underneath a running stack leaves processes alive
        against files that no longer exist.
        """

        product = self._product()
        plan = product.reset(wipe_authority_data=wipe_authority_data, apply=False)
        if not apply:
            return plan
        phases = Journal(self.progress)
        phases.begin("stop")
        stopped = self.transport.run(
            (str(self.script()), PROFILE, "stop"),
            timeout=300,
            operation=f"local {PROFILE} stop",
        )
        phases.append({"phase": "stop", "result": {"output": stopped.stdout.strip()}})
        phases.begin("remove")
        removed = product.reset(wipe_authority_data=wipe_authority_data, apply=True)
        phases.append({"phase": "remove", "result": removed})
        return removed

    def kernel_schema_reset(
        self,
        *,
        apply: bool,
        forget_selections: int | None,
        forget_uncounted_selections: bool = False,
    ) -> dict[str, object]:
        """Plan without touching the Host; apply behind a stop it hands down.

        The stop is passed to the product rather than run here first, because the
        gate that can refuse this operation lives on the other side of it: taking
        a working Host down and then declining to do anything is the one ordering
        that is worse than the hand-done rename this replaces.
        """

        product = self._product()
        if not apply:
            return product.kernel_schema_reset(
                apply=False,
                forget_selections=forget_selections,
                forget_uncounted_selections=forget_uncounted_selections,
            )
        phases = Journal(self.progress)

        def quiesce() -> dict[str, object]:
            phases.begin("quiesce")
            stopped = self.transport.run(
                (str(self.script()), PROFILE, "stop"),
                timeout=300,
                operation=f"local {PROFILE} stop",
            )
            result = {"output": stopped.stdout.strip()}
            phases.append({"phase": "quiesce", "result": result})
            return result

        phases.begin("ask")
        applied = product.kernel_schema_reset(
            apply=True,
            forget_selections=forget_selections,
            forget_uncounted_selections=forget_uncounted_selections,
            quiesce=quiesce,
        )
        phases.begin("set-aside")
        phases.append({"phase": "set-aside", "result": {"renamed": applied.get("renamed")}})
        return applied

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

    def commissioning_code(
        self, *, ttl_seconds: int, setup_code: str | None = None
    ) -> dict[str, object]:
        # The Mac source run reaches bootstrapctl through its own profile
        # script, so the code travels as the same flag the Pi passes over SSH.
        arguments = ("--ttl", str(ttl_seconds))
        if setup_code is not None:
            arguments += ("--code", setup_code)
        return self.profile_operation("commissioning-code", arguments=arguments)

    def owner_reset(self, *, apply: bool) -> dict[str, object]:
        """Ask Bootstrap to forget which Owner this Host holds.

        Over the control socket, with the Host up, because that is the Host
        this repair is for: every service healthy, every phone refused at
        setup. The offline route exists too — ``reset --wipe-authority-data``
        takes it, having just stopped everything — but reaching a running
        Host's authority through its own daemon is the interface, and taking a
        working Host down to withdraw one row would be the wrong trade twice.
        """

        if not apply:
            return {
                "profile": PROFILE,
                "status": "planned",
                "releases": (
                    "Bootstrap's record that the Data plane holds a Workspace "
                    "for this Host's Owner"
                ),
                "preserves": [
                    "every Controller Grant; no phone has to claim this Host again",
                    "Host identity and pinned TLS",
                    "saved Wi-Fi profiles and the current connection",
                    "every component database, including the Data plane",
                ],
                "next": (
                    "rerun owner-reset --apply, then set this Host up again from "
                    "the phone that already holds it"
                ),
            }
        return self.profile_operation("owner-reset")

    def profile_operation(
        self, operation: str, *, arguments: tuple[str, ...] = ()
    ) -> dict[str, object]:
        """Run one operation of the source-run profile, and read its health.

        A source run has three parts an operator waits on separately —
        materializing the pinned topology, the supervisord command itself, and
        the health wait afterwards. The journal is local because this report
        has no ``phases`` key to add them to; announcing them is still what
        turns a two-minute silence into three named waits.

        A fourth part is not a wait. An operation that started this Host's Hub
        has to record the one-shot Owner Authority capability Hub just used,
        against the evidence Hub left behind — the product install does this
        from its own transaction, and this path is where a source run's
        equivalent belongs, because it is the only place that knows the stack
        has been started and settled. Skipping it is not a missing report: the
        next ``reset --wipe-authority-data`` would read the still-pending
        capability as a failed bootstrap to retry, and rebuild the destroyed
        Authority at its own generation and state id.
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
        if operation in _PREPARING_OPERATIONS:
            phases.begin("authority")
            authority = product.commit_owner_authority()
            response["authority"] = authority
            phases.append({"phase": "authority", "result": authority})
        return response

    def script(self) -> Path:
        script = self.profile.lifecycle_script
        if script is None or not script.is_file() or not os.access(script, os.X_OK):
            raise OperationsError(f"local lifecycle script is missing or not executable: {script}")
        return script
