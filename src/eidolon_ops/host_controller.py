"""One lifecycle interface over a composed Host adapter.

Every operation declares a ``Plan`` and returns ``Evidence``. Nothing here asks
what driver a Host has: it asks the adapter whether it holds the capability,
and the adapter answers from what it is composed of.
"""

from __future__ import annotations

from pathlib import Path

from eidolon_ops import lan_observation, plans
from eidolon_ops.errors import OperationsError
from eidolon_ops.host import HostAdapter, build_adapter
from eidolon_ops.model import Capability, Evidence, Outcome, Plan, steps_from_phases
from eidolon_ops.paths import HostProfile, is_product_board
from eidolon_ops.process import ProcessError, ProcessRunner
from eidolon_ops.progress import ProgressSink
from eidolon_ops.source_assets import status_ports

_LIFECYCLE_ACTIONS = frozenset({"start", "stop", "restart"})
#: Flags that survived from a lifecycle model this tool no longer has. Every
#: adapter refused them, so they are refused once here instead of twice.
_RETIRED_LIFECYCLE_FLAGS = ("--force-cleanup", "--strict", "--no-wait-ready")


class HostController:
    def __init__(
        self,
        profile: HostProfile,
        runner: ProcessRunner,
        *,
        revision_overrides: tuple[str, ...] = (),
        allow_dirty: bool = False,
        progress: ProgressSink | None = None,
    ) -> None:
        self.profile = profile
        self.runner = runner
        self.revision_overrides = revision_overrides
        self.allow_dirty = allow_dirty
        self.progress = progress
        self._adapter: HostAdapter | None = None

    @property
    def adapter(self) -> HostAdapter:
        if self._adapter is None:
            self._adapter = build_adapter(
                self.profile,
                self.runner,
                revision_overrides=self.revision_overrides,
                allow_dirty=self.allow_dirty,
                progress=self.progress,
            )
        return self._adapter

    # -- read-only -----------------------------------------------------------

    def status(self) -> Evidence:
        plan = plans.status(self.profile.host_id)
        self.adapter.require(Capability.STATUS)
        report = self.adapter.supervisor.status()
        app = self.profile.app
        context: dict[str, object] = {
            "host_id": self.profile.host_id,
            "platform": str(self.profile.platform),
            "driver": str(self.profile.driver),
            "ports": status_ports(hub_https_port=app.hub_https_port if app else None),
        }
        if app is not None and not is_product_board(self.profile.platform):
            context["network"] = self._local_status_network()
        report = {**report, **context}
        return self._observed(plan, report, healthy=report.get("status") != "degraded")

    def _local_status_network(self) -> dict[str, object]:
        """Current Mac addresses; status must survive a disconnected machine."""

        try:
            addresses = lan_observation.interface_addresses(self.runner)
        except (OSError, ProcessError):
            addresses = set()
        app = self.profile.app
        declared = str(app.lan_ipv4) if app is not None and app.lan_ipv4 is not None else ""
        if declared:
            lan_ipv4 = declared
        else:
            try:
                lan_ipv4 = lan_observation.observed_lan_address(self.runner, addresses)
            except (OSError, ProcessError):
                lan_ipv4 = ""
        visible = sorted(address for address in addresses if not address.startswith("127."))
        if lan_ipv4 and lan_ipv4 not in visible:
            visible.insert(0, lan_ipv4)
        return {"lan_ipv4": lan_ipv4 or None, "addresses": visible}

    def app_ready(self) -> Evidence:
        plan = plans.app_ready(self.profile.host_id)
        self.adapter.require(Capability.APP_READY)
        report = self.adapter.supervisor.app_ready()
        return self._observed(plan, report, healthy=report.get("status") == "app_ready")

    def pending(self) -> Evidence:
        """Which commits are here and not on the Host.

        Gated on ``DEPLOY`` rather than a capability of its own: a Host that
        receives releases is exactly a Host that can be behind one, and a source
        run has nothing to be behind — it executes the checkouts themselves.
        """

        plan = plans.pending(self.profile.host_id)
        release = self.adapter.require_release(Capability.DEPLOY)
        report = release.pending()
        return self._observed(plan, report, healthy=not report.get("pending"))

    def doctor(self, *, release_id: str | None = None) -> Evidence:
        plan = plans.doctor(self.profile.host_id)
        adapter = self.adapter
        adapter.require(Capability.DOCTOR)
        paths = self._path_report()
        foundation = adapter.packages.doctor()
        host = adapter.supervisor.doctor(release_id=release_id)
        healthy = host.get("status") == "healthy" and foundation.get("status") in {
            "healthy",
            "unmanaged",
        }
        report = {
            "status": "healthy" if healthy else "degraded",
            "host_id": self.profile.host_id,
            "platform": str(self.profile.platform),
            "driver": str(self.profile.driver),
            "adapter": adapter.describe(),
            "paths": paths,
            "foundation": foundation,
            "host": host,
        }
        return self._observed(plan, report, healthy=healthy)

    def logs(self, *, service: str | None, lines: int, since: str | None) -> Evidence:
        plan = plans.logs(self.profile.host_id)
        adapter = self.adapter
        adapter.require(Capability.LOGS)
        if since is not None:
            adapter.require(Capability.LOG_HISTORY)
        report = adapter.supervisor.logs(service=service, lines=lines, since=since)
        return self._observed(plan, report, healthy=True)

    # -- boundary actions ----------------------------------------------------

    def lifecycle(
        self,
        operation: str,
        *,
        dry_run: bool = False,
        force_cleanup: bool = False,
        strict: bool = False,
        wait_ready: bool = True,
    ) -> Evidence:
        if operation not in _LIFECYCLE_ACTIONS:
            raise OperationsError(f"unsupported lifecycle operation: {operation}")
        if force_cleanup or strict or not wait_ready:
            raise OperationsError(
                "legacy Mac lifecycle flags are not valid for any adapter: "
                + ", ".join(_RETIRED_LIFECYCLE_FLAGS)
            )
        plan = plans.lifecycle(self.profile.host_id, operation, dry_run=dry_run)
        self.adapter.require(Capability.LIFECYCLE)
        report = self.adapter.supervisor.lifecycle(operation, dry_run=dry_run)
        if dry_run:
            return Evidence(plan=plan, outcome=Outcome.PLANNED, report=report)
        return self._applied(plan, report)

    def commissioning_code(self, *, ttl_seconds: int, setup_code: str | None = None) -> Evidence:
        """Issue the Setup code a phone types, on whichever Host this profile is.

        ``setup_code`` names the value for this one run. Left out, the profile's
        ``app.setup_code`` is used if it pins one, and otherwise the Host draws
        a code — which is the only difference a pinned value makes: an operator
        who already knows the digits never has to read this command's output.
        """

        if not 60 <= ttl_seconds <= 86400:
            raise OperationsError("commissioning-code TTL must be between 60 and 86400 seconds")
        plan = plans.commissioning_code(self.profile.host_id)
        self.adapter.require(Capability.COMMISSIONING_CODE)
        report = self.adapter.supervisor.commissioning_code(
            ttl_seconds=ttl_seconds, setup_code=setup_code
        )
        return self._applied(plan, report)

    def local_profile(
        self, profile_name: str, operation: str, *, arguments: tuple[str, ...] = ()
    ) -> Evidence:
        from eidolon_ops.adapters.supervisord import PROFILE

        plan = plans.source_profile(self.profile.host_id, operation)
        adapter = self.adapter
        adapter.require(Capability.SOURCE_PROFILE)
        if profile_name != PROFILE:
            raise OperationsError(f"unsupported local profile: {profile_name}")
        report = adapter.supervisor.profile_operation(operation, arguments=arguments)
        return self._applied(plan, report)

    # -- release and authority operations ------------------------------------

    def provision(self, *, apply: bool) -> Evidence:
        plan = plans.provision(self.profile.host_id, apply=apply)
        release = self.adapter.require_release(Capability.PROVISION)
        report = release.provision(apply=apply)
        return self._planned_or_applied(plan, report, applied=apply)

    def initialize_inputs(self, *, new_identity: bool = False) -> Evidence:
        plan = plans.initialize_inputs(self.profile.host_id, new_identity=new_identity)
        release = self.adapter.require_release(Capability.INIT_INPUTS)
        return self._applied(plan, release.initialize_inputs(new_identity=new_identity))

    def converge_inputs(self, *, apply: bool = False) -> Evidence:
        """Give an already-installed Host the credentials the product grew since.

        This method's absence is worth recording. The verb existed in the CLI and
        the implementation existed on the release executor, and the only thing
        missing was this — the line that connects them — so running it raised
        ``AttributeError: 'HostController' object has no attribute
        'add_missing_input_credentials'``. The one supported way to fix a Host
        with a missing credential could not be invoked at all, on any Host, and
        nothing failed until somebody typed it.

        A dispatch table and a gate over it now make that unrepresentable; see
        ``host_cli.OPERATIONS`` and ``tests/test_cli_operations.py``.
        """

        plan = plans.converge_inputs(self.profile.host_id, apply=apply)
        release = self.adapter.require_release(Capability.CONVERGE_INPUTS)
        report = release.converge_inputs(apply=apply)
        return self._planned_or_applied(plan, report, applied=apply)

    def install(
        self,
        *,
        release_id: str,
        resume: bool,
        apply: bool,
        reset_existing: bool = False,
        wipe_authority_data: bool = False,
    ) -> Evidence:
        plan = plans.install(
            self.profile.host_id,
            apply=apply,
            reset_existing=reset_existing,
            wipe_authority_data=wipe_authority_data,
        )
        release = self.adapter.require_release(Capability.INSTALL)
        report = release.install(
            release_id=release_id,
            resume=resume,
            apply=apply,
            reset_existing=reset_existing,
            wipe_authority_data=wipe_authority_data,
        )
        return self._planned_or_applied(plan, report, applied=apply)

    def deploy(
        self,
        *,
        release_id: str,
        resume: bool,
        activate: bool,
        cutover_mode: str = "reversible",
    ) -> Evidence:
        plan = plans.deploy(self.profile.host_id, activate=activate)
        release = self.adapter.require_release(Capability.DEPLOY)
        report = release.deploy(
            release_id=release_id,
            resume=resume,
            activate=activate,
            cutover_mode=cutover_mode,
        )
        return self._planned_or_applied(plan, report, applied=activate)

    def abandon(self, *, release_id: str) -> Evidence:
        plan = plans.abandon(self.profile.host_id)
        release = self.adapter.require_release(Capability.DEPLOY)
        return self._planned_or_applied(plan, release.abandon(release_id=release_id), applied=True)

    def rollback(self, *, release_id: str, snapshot: Path, apply: bool) -> Evidence:
        plan = plans.rollback(self.profile.host_id, apply=apply)
        release = self.adapter.require_release(Capability.ROLLBACK)
        report = release.rollback(release_id=release_id, snapshot=snapshot, apply=apply)
        return self._planned_or_applied(plan, report, applied=apply)

    def backup(self, *, output: Path) -> Evidence:
        plan = plans.backup(self.profile.host_id)
        release = self.adapter.require_release(Capability.BACKUP)
        return self._applied(plan, release.backup(output=output))

    def restore(self, *, source: Path, apply: bool) -> Evidence:
        plan = plans.restore(self.profile.host_id, apply=apply)
        release = self.adapter.require_release(Capability.RESTORE)
        report = release.restore(source=source, apply=apply)
        return self._planned_or_applied(plan, report, applied=apply)

    def authority_backup(self, *, output: Path) -> Evidence:
        plan = plans.authority_backup(self.profile.host_id)
        release = self.adapter.require_release(Capability.BACKUP)
        return self._applied(plan, release.authority_backup(output=output))

    def authority_restore(self, *, source: Path, apply: bool) -> Evidence:
        plan = plans.authority_restore(self.profile.host_id, apply=apply)
        release = self.adapter.require_release(Capability.RESTORE)
        report = release.authority_restore(source=source, apply=apply)
        return self._planned_or_applied(plan, report, applied=apply)

    def reset(self, *, wipe_authority_data: bool, apply: bool) -> Evidence:
        # Asked of the supervisor rather than the release: on a product Host the
        # release installed the namespace and its supervisor hands this straight
        # to it, while a source run has no release at all and the namespace it
        # clears is one it generated. Both Hosts answer the same question about
        # their own namespace; only one of them got to be asked before.
        plan = plans.reset(
            self.profile.host_id, apply=apply, wipe_authority_data=wipe_authority_data
        )
        self.adapter.require(Capability.RESET)
        report = self.adapter.supervisor.reset(wipe_authority_data=wipe_authority_data, apply=apply)
        return self._planned_or_applied(plan, report, applied=apply)

    def controller_reset(self, *, apply: bool) -> Evidence:
        plan = plans.controller_reset(self.profile.host_id, apply=apply)
        release = self.adapter.require_release(Capability.CONTROLLER_RESET)
        report = release.controller_reset(apply=apply)
        return self._planned_or_applied(plan, report, applied=apply)

    def authority_reset(self, *, apply: bool) -> Evidence:
        plan = plans.authority_reset(self.profile.host_id, apply=apply)
        release = self.adapter.require_release(Capability.AUTHORITY_RESET)
        report = release.authority_reset(apply=apply)
        return self._planned_or_applied(plan, report, applied=apply)

    def diagnose(self, *, output: Path) -> Evidence:
        plan = plans.diagnose(self.profile.host_id)
        release = self.adapter.require_release(Capability.DIAGNOSE)
        return self._applied(plan, release.diagnose(output=output))

    # -- evidence ------------------------------------------------------------

    def _path_report(self) -> dict[str, object]:
        paths = self.profile.paths
        items = (
            ("install", paths.install_root),
            ("current", paths.current_root),
            ("config", paths.config_root),
            ("state", paths.state_root),
            ("runtime", paths.runtime_root),
            ("logs", paths.log_root),
            ("cache", paths.cache_root),
            ("bootstrap_state", paths.bootstrap_state_root),
            ("bootstrap_runtime", paths.bootstrap_runtime_root),
        )
        # A path on this machine can be looked at; one on a remote Host cannot,
        # and reporting a workstation's answer for it would be a lie.
        local = self.adapter.release is None
        return {
            name: (
                {"path": str(value), "exists": value.exists()} if local else {"path": str(value)}
            )
            for name, value in items
        }

    @staticmethod
    def _observed(plan: Plan, report: dict[str, object], *, healthy: bool) -> Evidence:
        return Evidence(
            plan=plan,
            outcome=Outcome.OBSERVED if healthy else Outcome.DEGRADED,
            steps=steps_from_phases(plan, report),
            report=report,
        )

    @staticmethod
    def _applied(plan: Plan, report: dict[str, object]) -> Evidence:
        return Evidence(
            plan=plan,
            outcome=Outcome.APPLIED,
            steps=steps_from_phases(plan, report),
            report=report,
        )

    @staticmethod
    def _planned_or_applied(plan: Plan, report: dict[str, object], *, applied: bool) -> Evidence:
        return Evidence(
            plan=plan,
            outcome=Outcome.APPLIED if applied else Outcome.PLANNED,
            steps=steps_from_phases(plan, report),
            report=report,
        )
