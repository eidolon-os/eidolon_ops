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
from eidolon_ops.install_inputs import declared_secret_env_keys
from eidolon_ops.process import ProcessError
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


#: The transport must never be the first thing to give up on a deploy. A Host
#: can legitimately spend its 300s readiness gate and then, if that gate fails,
#: another 300s waiting for the release its rollback restores. Anything shorter
#: turns a slow-but-correct Host into a stuck one: the remote keeps the flock
#: this side can no longer see, and the candidate marker outlives the operator
#: who could have cleared it.
_REMOTE_ACTIVATION_TIMEOUT_SECONDS = 1800.0


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

    def _require_declared_credentials(self) -> dict[str, object]:
        """Refuse to ship onto a Host that is short a declared credential.

        Asks the *Host*, not the workstation. The workstation's input set is the
        source a Host is converged from and is checked when it is written and
        when it is repaired; what decides whether this release will work is what
        is in ``/etc/eidolon`` on the machine receiving it. Those two drift
        independently — repairing the input set and delivering it are separate
        operations — and the gap between them is where a fixed workstation and a
        broken Host sat together for two weeks.

        Names the verb that fixes it. A gate that refuses without saying what to
        run is a gate people learn to work around.
        """

        # Only what the question needs. A read of which keys a Host holds does
        # not need the Host-layer payload, and asking for it would make this gate
        # depend on deriving the Host identity — which is a different failure to
        # report and one this check has no business raising.
        host = self.transport.run_agent(
            "converge-secret-inputs",
            {"declared": declared_secret_env_keys(), "apply": False},
            timeout=120,
        )
        outstanding = host.get("missing") or {}
        if outstanding:
            short = ", ".join(
                f"{name} is missing {', '.join(keys)}"
                for name, keys in sorted(outstanding.items())
            )
            raise OperationsError(
                "this Host does not hold every credential the product declares "
                f"({short}); run `converge-inputs --apply`, then `restart`, "
                "before shipping a release that expects them"
            )
        return {"host": host}

    def deploy(
        self,
        *,
        release_id: str,
        resume: bool,
        activate: bool,
        cutover_mode: str = "reversible",
        _skip_prepare: bool = False,
    ) -> dict[str, object]:
        release_id = validate_release_id(release_id)
        local = self.preflight.run(require_install_files=False)
        # Before anything is prepared or moved. A release that needs a credential
        # the Host does not hold is a release that ships green and does not work,
        # which is exactly what happened: two credentials were added to the
        # product on 2026-08-25 and the Host installed on 2026-08-10 could not be
        # given them, so every memory and conversation feature answered 503 while
        # deploy after deploy reported success. Refusing here rather than warning,
        # because a warning in a release log is a thing nobody reads twice.
        local["install_input_contract"] = self._require_declared_credentials()
        # Said before the bundle is sealed, not after something fails.
        #
        # A release is defined by what the repositories hold, which removed the
        # failure where a board ran commits nobody meant to ship. The failure it
        # leaves is the mirror image: with several people committing to these
        # eight repositories, HEAD is a combination that may never have run
        # together, and nothing said so until a readiness timeout was already
        # being explained. The same calculation that annotates that failure costs
        # one round trip here, where the answer can still change a decision.
        local["source_advance"] = self._advance_since_last_activation()
        phases = Journal(self.progress)
        candidate_prepared = False
        health_gates_passed = False
        host_snapshot: str | None = None
        host_restored = False
        persistent_barrier_crossed = False
        if not _skip_prepare:
            self.bundles.prepare(
                release_id,
                reuse=resume,
                cutover_mode=cutover_mode,
                journal=phases,
            )
            candidate_prepared = True
        try:
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
                phases.begin("release_reclaim_retain")
                retained = self.bundles.reclaim(release_id, phase="retain")
                self.bundles.require_reclamation(retained, "retained")
                phases.append({"phase": "release_reclaim_retain", "result": retained})
                return {
                    "status": "dry_run",
                    "release_id": release_id,
                    "local": local,
                    "phases": phases,
                    "next": "rerun with --resume --activate after reviewing previous_targets",
                }
            phases.begin("service_identities")
            identities = self.transport.run_agent(
                "ensure-service-identities",
                {"units": list(self.config.units)},
                timeout=120,
            )
            if identities.get("status") != "service_identities_ready":
                raise OperationsError("service identity cutover returned invalid evidence")
            phases.append({"phase": "service_identities", "result": identities})
            if self.host_layer.app is not None:
                phases.begin("host_cutover_snapshot")
                host_cutover = self.transport.run_agent(
                    "release-cutover-snapshot",
                    {
                        "release_id": release_id,
                        "cutover_mode": cutover_mode,
                        # The commits this release is, written where reclamation
                        # cannot reach and a later question can. Also the only
                        # place a --allow-dirty seal is still visible tomorrow.
                        "sources": self.preflight.sources.provenance(),
                    },
                    timeout=180,
                )
                host_snapshot_value = host_cutover.get("host_snapshot")
                if (
                    host_cutover.get("status") != "host_cutover_snapshotted"
                    or not isinstance(host_snapshot_value, str)
                ):
                    raise OperationsError("Host cutover snapshot returned invalid evidence")
                host_snapshot = host_snapshot_value
                phases.append({"phase": "host_cutover_snapshot", "result": host_cutover})
                # The Host layer is an input to the new component graph, not a
                # post-activation decoration. In particular, Hub validates its
                # strict settings model while importing the ASGI app; starting the
                # new Hub against the previous settings schema can never become
                # ready. Prestage atomically while the old processes still hold
                # their already-loaded configuration, then switch components.
                phases.begin("host_application")
                phases.append(
                    {
                        "phase": "host_application",
                        "result": self.host_layer.refresh(release_id),
                    }
                )
            phases.begin("activate")
            activation = self._activation_json(
                "release activation",
                (cli, "deploy", descriptor),
                # Deliberately longer than everything the Host can spend inside
                # one deploy: a 300s readiness gate, and — when that gate fails
                # — a rollback that waits the same 300s again for the release it
                # restores. At 600 the operator's side was the first to give up,
                # and a client that gives up first is worse than one that waits:
                # the remote transaction keeps running, keeps the flock, and the
                # Host is left holding its own upgrade lock with a candidate
                # marker no later release can clear.
                timeout=_REMOTE_ACTIVATION_TIMEOUT_SECONDS,
            )
            phases.append({"phase": "activate", "result": activation})
            if (
                activation.get("status") == "forward_fix_required"
                and activation.get("cutover_mode") == "forward-only"
                and activation.get("persistent_state_mutated") is True
            ):
                persistent_barrier_crossed = True
                health_gates_passed = True
                self._finalize_cutover(
                    release_id, cutover_mode, host_snapshot, activation, phases
                )
                self._commit_reclaim_after_barrier(release_id, phases)
                raise OperationsError(
                    "release crossed the forward-only persistent-state barrier; "
                    "old interpreters were not restored and this release requires "
                    "a same-schema forward fix"
                )
            transaction_id = activation.get("transaction_id")
            if (
                activation.get("status") != "activated"
                or not isinstance(transaction_id, str)
                or _TRANSACTION_ID.fullmatch(transaction_id) is None
                or activation.get("cutover_mode") != cutover_mode
                or activation.get("persistent_state_mutated")
                is not (cutover_mode == "forward-only")
            ):
                raise OperationsError("release activation returned invalid transaction evidence")
            if cutover_mode == "forward-only":
                # Starting the candidate is the durable barrier.  From here
                # neither candidate reclaim nor an old-interpretation restore
                # is permitted, even if later evidence recording itself fails.
                persistent_barrier_crossed = True
                health_gates_passed = True
            snapshot = self.config.data.deployment_evidence / f"{release_id}-{transaction_id}"
            gate_error = self._run_health_gate(cli, descriptor, phases)
            if gate_error is not None:
                if cutover_mode == "forward-only":
                    health_gates_passed = True
                    self._finalize_cutover(
                        release_id,
                        cutover_mode,
                        host_snapshot,
                        {**activation, "health_gate_error": str(gate_error)},
                        phases,
                    )
                    self._commit_reclaim_after_barrier(release_id, phases)
                    raise OperationsError(
                        "post-activation health gate failed after the forward-only "
                        f"persistent-state barrier ({gate_error}); old interpreters were "
                        "not restored and this release requires a same-schema forward fix"
                    )
                try:
                    self._restore(cli, descriptor, snapshot, phases, gate_error)
                finally:
                    if host_snapshot is not None:
                        self._restore_host_cutover(
                            release_id, cutover_mode, host_snapshot, phases
                        )
                        host_restored = True
            self._finalize_cutover(
                release_id, cutover_mode, host_snapshot, activation, phases
            )
            health_gates_passed = True
            phases.begin("release_reclaim_commit")
            committed = self.bundles.reclaim(release_id, phase="commit")
            self.bundles.require_reclamation(committed, "committed")
            phases.append({"phase": "release_reclaim_commit", "result": committed})
            return {
                "status": "activated",
                "release_id": release_id,
                "local": local,
                "phases": phases,
            }
        except Exception as exc:
            if (
                host_snapshot is not None
                and not host_restored
                and not persistent_barrier_crossed
                and not health_gates_passed
            ):
                try:
                    self._restore_host_cutover(
                        release_id, cutover_mode, host_snapshot, phases
                    )
                except Exception as host_restore_error:
                    raise OperationsError(
                        f"release transaction failed ({exc}); Host layer rollback also failed: "
                        f"{host_restore_error}"
                    ) from host_restore_error
            if candidate_prepared and not health_gates_passed:
                self._abort_candidate(release_id, phases, exc)
            raise self._explained(exc) from exc

    def _explained(self, failure: Exception) -> Exception:
        """Say which repositories moved since the release that last worked.

        A combination of commits that is not self-consistent cannot be caught
        before it runs — one component's new required field and another's old
        model are both valid on their own. What can be fixed is the report: the
        incident this comes from failed three times with `readiness timeout:
        hub, kernel, local-api` and nothing to start from, while the actual
        cause was one repository the release had never included.

        Returns the original failure untouched when there is nothing to compare
        against. A diagnostic that replaces the error it was explaining is
        worse than no diagnostic.
        """

        previous = self._last_activated_release()
        if previous is None:
            return failure
        release_id, sources = previous
        advance = self.preflight.sources.advance_from(sources)
        if not advance:
            return failure
        moved = ", ".join(
            f"{source_id} +{count}" for source_id, count in sorted(advance.items())
        )
        return OperationsError(
            f"{failure}\n\nsince release {release_id} — the last one this Host activated "
            f"— these sources advanced: {moved}. A release is only as consistent as the "
            "combination it was built from; suspect these before anything else."
        )

    def _advance_since_last_activation(self) -> dict[str, object]:
        """What moved since this Host last activated something, before sealing.

        Best effort by construction, like the annotation it shares a calculation
        with: a Host that cannot answer is not a reason to refuse a deploy, and a
        Host with no history has nothing to compare against.
        """

        previous = self._last_activated_release()
        if previous is None:
            return {"status": "no_previous_release"}
        release_id, sources = previous
        advance = self.preflight.sources.advance_from(sources)
        if not advance:
            return {"status": "unchanged", "since": release_id}
        return {
            "status": "advanced",
            "since": release_id,
            "commits": dict(sorted(advance.items())),
            "note": (
                f"these sources advanced since release {release_id}, the last one this "
                "Host activated. This combination has not run on it before."
            ),
        }

    def _last_activated_release(self) -> tuple[str, dict[str, object]] | None:
        try:
            history = self.transport.run_agent("release-sources", {}, timeout=60)
            releases = history.get("releases")
            newest = releases[0] if isinstance(releases, list) and releases else None
            release_id = newest.get("release_id") if isinstance(newest, dict) else None
            sources = newest.get("sources") if isinstance(newest, dict) else None
        except Exception:
            # The Host is already failing something; asking it a second question
            # is best-effort by construction.
            return None
        if not isinstance(release_id, str) or not isinstance(sources, dict):
            return None
        return (release_id, sources)

    def _restore_host_cutover(
        self,
        release_id: str,
        cutover_mode: str,
        host_snapshot: str,
        phases: Journal,
    ) -> None:
        phases.begin("host_cutover_rollback")
        restored = self.transport.run_agent(
            "release-cutover-restore",
            {
                "release_id": release_id,
                "cutover_mode": cutover_mode,
                "host_snapshot": host_snapshot,
            },
            timeout=180,
        )
        if restored.get("status") != "host_cutover_restored":
            raise OperationsError("Host layer rollback returned invalid evidence")
        phases.append({"phase": "host_cutover_rollback", "result": restored})

    def _commit_reclaim_after_barrier(
        self, release_id: str, phases: Journal
    ) -> None:
        """Clean private staging after a candidate became forward-only current."""

        phases.begin("release_reclaim_commit")
        committed = self.bundles.reclaim(release_id, phase="commit")
        self.bundles.require_reclamation(committed, "committed")
        phases.append({"phase": "release_reclaim_commit", "result": committed})

    def _finalize_cutover(
        self,
        release_id: str,
        cutover_mode: str,
        host_snapshot: str | None,
        activation: dict[str, object],
        phases: Journal,
    ) -> None:
        if host_snapshot is None:
            return
        phases.begin("cutover_receipt")
        recorded = self.transport.run_agent(
            "release-cutover-finalize",
            {
                "release_id": release_id,
                "cutover_mode": cutover_mode,
                "host_snapshot": host_snapshot,
                "activation": activation,
            },
            timeout=180,
        )
        if recorded.get("status") != "cutover_recorded":
            raise OperationsError("release cutover receipt returned invalid evidence")
        phases.append({"phase": "cutover_receipt", "result": recorded})

    def _abort_candidate(
        self, release_id: str, phases: Journal, primary_error: Exception
    ) -> None:
        try:
            phases.begin("release_reclaim_abort")
            aborted = self.bundles.reclaim(release_id, phase="abort")
            self.bundles.require_reclamation(aborted, "aborted")
            phases.append({"phase": "release_reclaim_abort", "result": aborted})
        except Exception as cleanup_error:
            raise OperationsError(
                f"release transaction failed ({primary_error}); candidate cleanup also failed: "
                f"{cleanup_error}"
            ) from cleanup_error

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
                timeout=_REMOTE_ACTIVATION_TIMEOUT_SECONDS,
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
        self.bundles.prepare(
            release_id, reuse=resume, cutover_mode="reversible", journal=phases
        )
        candidate_prepared = True
        try:
            self.host_layer.stage_install_files(
                release_id, f"/var/tmp/eidolon-secrets-{release_id}"
            )
        except Exception as stage_error:
            self._abort_candidate(release_id, phases, stage_error)
            raise
        payload = {
            **self.host_layer.target_payload(),
            "release_id": release_id,
            "sources": self.preflight.sources.provenance(),
        }
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
                combined = OperationsError(
                    f"install failed ({primary_error}); secret staging cleanup also failed: "
                    f"{cleanup_exc}"
                )
                self._abort_candidate(release_id, phases, combined)
                raise combined from cleanup_exc
            self._abort_candidate(release_id, phases, cleanup_exc)
            raise
        if primary_error is not None:
            if candidate_prepared:
                self._abort_candidate(release_id, phases, primary_error)
            raise primary_error
        phases.begin("release_reclaim_commit")
        committed = self.bundles.reclaim(release_id, phase="commit")
        self.bundles.require_reclamation(committed, "committed")
        phases.append({"phase": "release_reclaim_commit", "result": committed})
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
            timeout=_REMOTE_ACTIVATION_TIMEOUT_SECONDS,
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

    def _activation_json(
        self,
        operation: str,
        command: tuple[str, ...],
        *,
        timeout: float,
    ) -> dict[str, object]:
        """Preserve the activator's durable forward-fix receipt on exit 4."""

        try:
            return self._remote_json(operation, command, timeout=timeout)
        except ProcessError as exc:
            if exc.result.returncode != 4:
                raise
            receipt = parse_json(exc.result.stderr, operation)
            if receipt.get("status") != "forward_fix_required":
                raise
            return receipt


def remote_release_cli(release_id: str) -> str:
    return f"/opt/eidolon/releases/{release_id}/{RELEASE_ACTIVATOR}"


def remote_descriptor(release_id: str) -> str:
    return f"/opt/eidolon/releases/{release_id}/release.json"
