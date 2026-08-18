"""Install, activate and roll back a release, with the gate that decides.

Activation is the Host's own activator's job; what belongs here is the order of
the phases, the evidence each one produced, and the rule that a release which
does not leave the Host able to serve the App is put back.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from pathlib import Path

from eidolon_ops.config import OperationsConfig, validate_release_id
from eidolon_ops.errors import OperationsError
from eidolon_ops.host_layer import HostLayer
from eidolon_ops.progress import Journal, ProgressSink
from eidolon_ops.readiness import describe_failures
from eidolon_ops.release_bundle import BundleTransfer, parse_json
from eidolon_ops.release_preflight import (
    RELEASE_ACTIVATOR,
    RELEASE_INTERPRETER,
    ReleasePreflight,
)
from eidolon_ops.transport import SSHTransport

_TRANSACTION_ID = re.compile(r"[0-9a-f]{32}")
_INSTALL_MUTATIONS = (
    "install the pinned non-Eidolon Raspberry Pi foundation",
    "prepare exact commit-pinned native release",
    "create/reuse dedicated service identities and directories",
    "install the fixed secret, identity and product-settings inputs without overwrite",
    "create a fresh Data V2 baseline",
    "install descriptor-allowlisted assets and component links",
    "enable Bootstrap/eidolond/Local API/Admin and require release doctor",
)
_RESET_MUTATIONS = (
    "stop and remove the existing Eidolon deployment",
    "permanently wipe Eidolon and Bootstrap authority data",
)


class ReleaseTransaction:
    def __init__(
        self,
        config: OperationsConfig,
        transport: SSHTransport,
        preflight: ReleasePreflight,
        bundles: BundleTransfer,
        host_layer: HostLayer,
        *,
        provision: Callable[..., dict[str, object]],
        reset: Callable[..., dict[str, object]],
        app_ready: Callable[[], dict[str, object]],
        progress: ProgressSink | None = None,
    ) -> None:
        self.config = config
        self.transport = transport
        self.preflight = preflight
        self.bundles = bundles
        self.host_layer = host_layer
        self._provision = provision
        self._reset = reset
        self._app_ready = app_ready
        self.progress = progress

    def deploy(
        self,
        *,
        release_id: str,
        resume: bool,
        activate: bool,
        _skip_prepare: bool = False,
    ) -> dict[str, object]:
        release_id = validate_release_id(release_id)
        local = self.preflight.run(require_install_files=False)
        phases = Journal(self.progress)
        if not _skip_prepare:
            self.bundles.prepare(release_id, reuse=resume, journal=phases)
        descriptor = remote_descriptor(release_id)
        cli = remote_release_cli(release_id)
        phases.begin("dry_run")
        dry_run = self._remote_json(
            "release activation dry-run",
            (cli, "deploy", descriptor, "--dry-run"),
            timeout=300,
        )
        phases.append({"phase": "dry_run", "result": dry_run})
        if not activate:
            return {
                "status": "dry_run",
                "release_id": release_id,
                "local": local,
                "phases": phases,
                "next": "rerun with --resume --activate after reviewing previous_targets",
            }
        if self.host_layer.app is not None:
            # The Host layer is an input to the new component graph, not a
            # post-activation decoration. In particular, Hub validates its
            # strict settings model while importing the ASGI app; starting the
            # new Hub against the previous settings schema can never become
            # ready. Prestage atomically while the old processes still hold
            # their already-loaded configuration, then switch components.
            phases.begin("host_application")
            phases.append(
                {"phase": "host_application", "result": self.host_layer.refresh(release_id)}
            )
        phases.begin("activate")
        activation = self._remote_json(
            "release activation",
            (cli, "deploy", descriptor),
            timeout=600,
        )
        phases.append({"phase": "activate", "result": activation})
        transaction_id = activation.get("transaction_id")
        if (
            activation.get("status") != "activated"
            or not isinstance(transaction_id, str)
            or _TRANSACTION_ID.fullmatch(transaction_id) is None
        ):
            raise OperationsError("release activation returned invalid transaction evidence")
        snapshot = self.config.data.deployment_evidence / f"{release_id}-{transaction_id}"
        gate_error = self._run_health_gate(cli, descriptor, phases)
        if gate_error is not None:
            self._restore(cli, descriptor, snapshot, phases, gate_error)
        return {
            "status": "activated",
            "release_id": release_id,
            "local": local,
            "phases": phases,
        }

    def _run_health_gate(self, cli: str, descriptor: str, phases: Journal) -> Exception | None:
        try:
            phases.begin("doctor")
            doctor = self._remote_json(
                "release doctor",
                (cli, "doctor", descriptor),
                timeout=300,
            )
            phases.append({"phase": "doctor", "result": doctor})
            if doctor.get("status") != "healthy":
                raise OperationsError("release doctor degraded after activation")
            phases.begin("app_ready")
            app = self._app_ready()
            phases.append({"phase": "app_ready", "result": app})
            if app.get("status") != "app_ready":
                raise OperationsError(
                    "mobile App gate degraded after activation: " + describe_failures(app)
                )
        except Exception as exc:
            return exc
        return None

    def _restore(
        self,
        cli: str,
        descriptor: str,
        snapshot: Path,
        phases: Journal,
        gate_error: Exception,
    ) -> None:
        try:
            phases.begin("health_gate_rollback")
            restored = self._remote_json(
                "post-activation gate release rollback",
                (cli, "rollback", descriptor, str(snapshot)),
                timeout=600,
            )
            if restored.get("status") != "restored":
                raise OperationsError("release rollback returned invalid recovery evidence")
        except Exception as rollback_exc:
            raise OperationsError(
                f"post-activation health gate failed ({gate_error}) and rollback failed: "
                f"{rollback_exc}"
            ) from rollback_exc
        phases.append({"phase": "health_gate_rollback", "result": restored})
        raise OperationsError(
            f"post-activation health gate failed ({gate_error}); the exact release snapshot "
            "was restored"
        )

    def install(
        self,
        *,
        release_id: str,
        resume: bool,
        apply: bool,
        reset_existing: bool = False,
        wipe_authority_data: bool = False,
    ) -> dict[str, object]:
        release_id = validate_release_id(release_id)
        if wipe_authority_data and not reset_existing:
            raise OperationsError("--wipe-authority-data requires --reset-existing")
        if reset_existing and not wipe_authority_data:
            raise OperationsError(
                "clean reinstall requires both --reset-existing and --wipe-authority-data"
            )
        if not apply:
            return {
                "status": "planned",
                "release_id": release_id,
                "local": self.preflight.run(require_install_files=False),
                "foundation": self._provision(apply=False),
                "reset": (
                    self._reset(wipe_authority_data=True, apply=False) if reset_existing else None
                ),
                "mutations": [
                    *(_RESET_MUTATIONS if reset_existing else ()),
                    *_INSTALL_MUTATIONS,
                ],
                "next": "rerun with --apply after reviewing every planned mutation",
            }
        local = self.preflight.run(require_install_files=True)
        phases = Journal(self.progress)
        if reset_existing:
            phases.begin("reset_existing")
            phases.append(
                {
                    "phase": "reset_existing",
                    "result": self._reset(wipe_authority_data=True, apply=True),
                }
            )
        # The foundation is installed here but reported under its own key, so
        # it is announced as work in flight and never recorded as a phase: the
        # phase list is the plan's vocabulary, and the plan does not name it.
        phases.begin("foundation")
        foundation = self._provision(apply=True)
        self.bundles.prepare(release_id, reuse=resume, journal=phases)
        self.host_layer.stage_install_files(
            release_id, f"/var/tmp/eidolon-secrets-{release_id}"
        )
        payload = {**self.host_layer.target_payload(), "release_id": release_id}
        primary_error: Exception | None = None
        try:
            phases.begin("install")
            phases.append(
                {
                    "phase": "install",
                    "result": self.transport.run_agent(
                        "install",
                        payload,
                        python=f"/opt/eidolon/releases/{release_id}/{RELEASE_INTERPRETER}",
                        timeout=1200,
                    ),
                }
            )
        except Exception as exc:
            primary_error = exc
        try:
            phases.begin("secret_cleanup")
            cleanup = self.transport.run_agent(
                "cleanup-stage",
                {"release_id": release_id},
                timeout=120,
            )
            phases.append({"phase": "secret_cleanup", "result": cleanup})
        except Exception as cleanup_exc:
            if primary_error is not None:
                raise OperationsError(
                    f"install failed ({primary_error}); secret staging cleanup also failed: "
                    f"{cleanup_exc}"
                ) from cleanup_exc
            raise
        if primary_error is not None:
            raise primary_error
        return {
            "status": "installed",
            "release_id": release_id,
            "foundation": foundation,
            "local": local,
            "phases": phases,
        }

    def rollback(self, *, release_id: str, snapshot: Path, apply: bool) -> dict[str, object]:
        release_id = validate_release_id(release_id)
        if not snapshot.is_absolute() or snapshot.parent != self.config.data.deployment_evidence:
            raise OperationsError("snapshot must be one direct child of deployment_evidence")
        self.preflight.validate_ssh_material()
        plan = self.transport.run_agent(
            "rollback-plan",
            {"release_id": release_id, "snapshot": str(snapshot)},
        )
        if not apply:
            return {**plan, "next": "rerun with --apply to restore this exact snapshot"}
        result = self._remote_json(
            "explicit release rollback",
            (
                remote_release_cli(release_id),
                "rollback",
                remote_descriptor(release_id),
                str(snapshot),
            ),
            timeout=600,
        )
        return {"status": "restored", "release_id": release_id, "result": result}

    def _remote_json(
        self,
        operation: str,
        command: tuple[str, ...],
        *,
        timeout: float,
    ) -> dict[str, object]:
        result = self.transport.run(command, sudo=True, timeout=timeout, operation=operation)
        return parse_json(result.stdout, operation)


def remote_release_cli(release_id: str) -> str:
    return f"/opt/eidolon/releases/{release_id}/{RELEASE_ACTIVATOR}"


def remote_descriptor(release_id: str) -> str:
    return f"/opt/eidolon/releases/{release_id}/release.json"
