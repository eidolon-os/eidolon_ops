"""Workstation orchestration that composes, but does not replace, eidolon-release.

This is the facade a Host adapter calls. The work is in the collaborators it
holds: proving the workstation, sealing and transferring a release, running the
release transaction, deriving the Host layer, and the read-only and boundary
operations the injected agent performs.
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import tarfile
import tempfile
import time
from collections.abc import Callable, Mapping
from pathlib import Path

from eidolon_ops.component_contract import read_component_contracts
from eidolon_ops.config import OperationsConfig, validate_release_id
from eidolon_ops.errors import OperationsError
from eidolon_ops.foundation import (
    FOUNDATION_PROFILE,
    foundation_payload,
    python_bootstrap_script,
    python_probe_script,
)
from eidolon_ops.host_layer import ASSET_ERRORS, HostLayer
from eidolon_ops.hostagent.contract import RESET_AUTHORITY_ROOTS
from eidolon_ops.install_inputs import (
    add_missing_install_credentials,
    declared_secret_env_keys,
    initialize_install_inputs,
)
from eidolon_ops.owner_domain_assets import (
    OWNER_DOMAIN_MATERIAL_NAMES,
    AuthorityDecision,
    OwnerDomainAssetError,
    authority_lineage,
    authority_recovery_required,
    decide_owner_authority,
    ensure_owner_domain_assets,
    mark_authority_bootstrapped,
)
from eidolon_ops.owner_domain_assets import (
    reset_owner_authority as advance_owner_authority,
)
from eidolon_ops.paths import AppAccess
from eidolon_ops.process import ProcessRunner
from eidolon_ops.progress import Journal, ProgressSink
from eidolon_ops.readiness import READINESS_TRANSPORT_TIMEOUT_SECONDS, describe_failures
from eidolon_ops.release_bundle import BundleTransfer, file_sha256, parse_json
from eidolon_ops.release_preflight import ReleasePreflight
from eidolon_ops.release_transaction import ReleaseTransaction
from eidolon_ops.transport import SSHTransport

__all__ = ["EidolonPiController", "OperationsError"]

#: Sources whose settings templates the private input set is derived from.
_SETTINGS_SOURCES = ("eidolon_agent", "eidolon_channel", "eidolon_memory")
_LIFECYCLE_ACTIONS = frozenset({"start", "stop", "restart"})

#: The staging directory credential convergence uses.
#:
#: A fixed name rather than a release id, because convergence is not tied to
#: a release: it delivers what the *product* declares, and a Host converges to
#: that whether or not anything is being shipped. Fixed also means a second
#: run cleans up after the first rather than accumulating directories of
#: secrets on the Host.
_CONVERGENCE_STAGE_ID = "credential-convergence"


#: What SQLite leaves beside a database it owns. A component that declared the
#: database has declared these; listing them as unclaimed would bury the one
#: entry that genuinely belongs to nobody under seven that obviously do not.
_SQLITE_SIDECARS = ("-shm", "-wal", ".lock", "-journal")

#: How many commits or edited paths a source names before the list is elided.
#: Enough to recognize the work, bounded so one busy repository cannot bury the
#: other seven.
_PENDING_SUBJECTS = 5


def _pending_detail(
    active: str,
    total: int,
    behind: Mapping[str, object],
    uncommitted: Mapping[str, object],
) -> str:
    """One sentence, naming both ways work fails to be on a Host."""

    parts = []
    if behind:
        parts.append(f"{total} commit(s) in {len(behind)} source(s) are not on this Host")
    if uncommitted:
        parts.append(
            f"{len(uncommitted)} source(s) hold uncommitted work, which no release can carry"
        )
    if not parts:
        return f"the Host runs {active} and every source matches it"
    return "; ".join(parts)


def _authority_recovery_required(
    marker: object, lineage: dict[str, object]
) -> str:
    return authority_recovery_required(
        marker,
        lineage,
        remedy=(
            "An install must not ship past that. Restore the matching Authority "
            "backup, or rebuild deliberately with authority-reset --apply or "
            "install --reset-existing --wipe-authority-data --apply."
        ),
    )


def _same_state(entry: str, declared: str) -> bool:
    """Whether a path on the Host is the state a component declared."""

    if entry == declared:
        return True
    for suffix in _SQLITE_SIDECARS:
        if entry == f"{declared}{suffix}":
            return True
    return Path(entry).is_relative_to(declared) or Path(declared).is_relative_to(entry)


def _wait_for_restored_authority_readiness(
    probe: Callable[[], dict[str, object]],
    *,
    timeout_seconds: float,
) -> dict[str, object]:
    """Require the restored graph to become App-ready before reporting success."""

    deadline = time.monotonic() + timeout_seconds
    while True:
        report = probe()
        if report.get("status") == "app_ready":
            return report
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise OperationsError(
                "AUTHORITY_RESTORE_FAILED: App readiness degraded after restore: "
                + describe_failures(report)
            )
        time.sleep(min(0.5, remaining))


def _finalize_authority_restore_stage(
    transport: SSHTransport, payload: dict[str, object]
) -> dict[str, object]:
    """Require positive proof that the sensitive upload is absent."""

    try:
        result = transport.run_agent(
            "authority-restore-stage-finalize", payload, timeout=120
        )
    except Exception as exc:
        raise OperationsError(
            "AUTHORITY_RESTORE_FAILED: sensitive restore staging cleanup failed"
        ) from exc
    if result.get("status") != "authority_restore_stage_absent":
        raise OperationsError(
            "AUTHORITY_RESTORE_FAILED: restore staging cleanup was not proven"
        )
    return result


class EidolonPiController:
    def __init__(
        self,
        config: OperationsConfig,
        runner: ProcessRunner,
        *,
        transport: SSHTransport | None = None,
        git: str = "git",
        app: AppAccess | None = None,
        progress: ProgressSink | None = None,
        allow_dirty: bool = False,
    ) -> None:
        self.config = config
        self.runner = runner
        self.transport = transport or SSHTransport(config.host, runner)
        self.git = git
        self.progress = progress
        self.preflight = ReleasePreflight(config, runner, git=git, allow_dirty=allow_dirty)
        self.bundles = BundleTransfer(config, runner, self.transport, self.preflight.sources)
        self.host_layer = HostLayer(
            config,
            self.transport,
            app,
            read_exact_source_file=self.preflight.read_exact_source_file,
            source_revisions=self.preflight.sources.revisions,
        )
        self.releases = ReleaseTransaction(
            config,
            self.transport,
            self.preflight,
            self.bundles,
            self.host_layer,
            provision=self._nested_provision,
            reset=self.reset,
            app_ready=self.app_ready,
            authority_capability=self.authority_capability,
            commit_authority_capability=self.commit_authority_capability,
            progress=progress,
        )

    @property
    def app(self) -> AppAccess | None:
        """The Host-bound app contract, kept in one place rather than two."""

        return self.host_layer.app

    @app.setter
    def app(self, value: AppAccess | None) -> None:
        self.host_layer.app = value

    # -- release transaction -------------------------------------------------

    def install(self, **arguments) -> dict[str, object]:
        return self.releases.install(**arguments)

    def deploy(self, **arguments) -> dict[str, object]:
        return self.releases.deploy(**arguments)

    def rollback(self, **arguments) -> dict[str, object]:
        return self.releases.rollback(**arguments)

    def abandon(self, *, release_id: str) -> dict[str, object]:
        """Give up on a prepared candidate nobody is going to finish.

        A driver process that dies mid-transaction leaves its candidate marker
        on the Host, and every later operation is refused because of it. The
        only sanctioned way out was to continue that exact candidate — which
        requires checking the workspace out at the commit it was sealed from —
        so a killed deploy could strand a Host with no forward move at all.

        This is not a rollback: the Host agent refuses to abandon a candidate
        the current links reference, so an activated release is never touched.
        """

        self.preflight.validate_ssh_material()
        reclaimed = self.releases.bundles.reclaim(release_id, phase="abort")
        self.releases.bundles.require_reclamation(reclaimed, "aborted")
        return {"status": "abandoned", "release_id": release_id, "result": reclaimed}

    def local_preflight(
        self, *, require_install_files: bool, require_clean_sources: bool = True
    ) -> dict[str, object]:
        return self.preflight.run(
            require_install_files=require_install_files,
            require_clean_sources=require_clean_sources,
        )

    # -- read-only -----------------------------------------------------------

    def status(self) -> dict[str, object]:
        self.preflight.validate_ssh_material()
        report = self.transport.run_agent("status", self.host_layer.target_payload())
        # Which link this ran over decides whether the next release takes two
        # seconds or three minutes, so the operator gets told rather than
        # having to infer it from how long they waited.
        #
        # The gap between these checkouts and the Host rides along because the
        # Host facts it needs are already in this report: asking costs local git
        # and no second round trip. Counts only — naming each commit is what
        # `pending` is for, and status is asked far more often than it is read
        # in full.
        return {
            **report,
            "endpoint": self.transport.describe(),
            "behind": self._pending_summary(report),
        }

    def _pending_summary(self, report: Mapping[str, object]) -> dict[str, object]:
        """How far this workstation is ahead of the Host, in one line's worth.

        Never raises: it is a footnote on a health report, and a Host that is
        answering must not be reported as unreachable because a sibling checkout
        could not be read.
        """

        try:
            answer = self._pending_commits(report, verbose=False)
        except Exception:  # pragma: no cover - a footnote must not fail a report
            return {}
        return {
            key: answer[key]
            for key in ("active_release", "pending_commits", "uncommitted", "detail")
            if key in answer
        } | {"sources": sorted(answer.get("pending") or {})}

    def pending(self) -> dict[str, object]:
        """Which commits exist here and are not on the Host.

        A release ships each checkout's HEAD as it stood when the run began, and
        several people commit to these repositories all day. So a commit made
        while a deploy was in flight — the window measured three to six minutes
        on this board — is simply in the next release, and a commit made after
        one is not on the Host at all. Neither is lost, and both look exactly
        like "my change is missing" if nothing says so.

        Both halves of the answer already existed and nothing put them together:
        the Host records the commits every activation shipped, and the local
        repositories know where they are now. This subtracts them.

        Read-only, and tolerant on purpose. It is the command someone runs when
        they already suspect something is wrong, so a source it cannot account
        for is reported as unknown rather than raising.
        """

        self.preflight.validate_ssh_material()
        report = self.transport.run_agent("status", self.host_layer.target_payload())
        return {
            "status": "observed",
            "endpoint": self.transport.describe(),
            **self._pending_commits(report),
        }

    def _pending_commits(
        self, report: Mapping[str, object], *, verbose: bool = True
    ) -> dict[str, object]:
        active = self._active_release_from_links(report)
        if active is None:
            return {
                "active_release": None,
                "detail": "the Host publishes no component links, so nothing says what it runs",
            }
        shipped = self._shipped_sources(report, active)
        if shipped is None:
            return {
                "active_release": active,
                "detail": (
                    f"the Host runs {active} but has no recorded provenance for it, so what "
                    "it shipped cannot be compared with what is here"
                ),
            }
        behind: dict[str, object] = {}
        uncommitted: dict[str, object] = {}
        for source_id, source in sorted(self.config.sources.items()):
            path = Path(source.path)
            recorded = shipped.get(source_id)
            revision = recorded.get("revision") if isinstance(recorded, Mapping) else None
            entry = self._distance_from_head(path, revision, verbose=verbose)
            if entry is not None:
                behind[source_id] = entry
            edits = self._uncommitted(path, verbose=verbose)
            if edits is not None:
                uncommitted[source_id] = edits
        total = sum(
            value["commits"]
            for value in behind.values()
            if isinstance(value, Mapping) and isinstance(value.get("commits"), int)
        )
        return {
            "active_release": active,
            "pending": behind,
            "pending_commits": total,
            # A separate fact, deliberately not folded into the count above.
            # Committed work is on its way to the Host and merely has not been
            # sent; uncommitted work cannot be sent at all, because a release is
            # sealed with `git archive` from a commit. Adding them together
            # would give one number that means neither thing.
            "uncommitted": uncommitted,
            "detail": _pending_detail(active, total, behind, uncommitted),
        }

    @staticmethod
    def _active_release_from_links(report: Mapping[str, object]) -> str | None:
        """The release the Host's own component links point at.

        Not the newest receipt: a receipt records that an activation happened,
        and the links record which one is being served. They disagree while a
        candidate is prepared and not activated, which is exactly the state this
        command exists to make visible.
        """

        links = report.get("current_links")
        if not isinstance(links, Mapping) or not links:
            return None
        names = {
            str(value).split("/releases/")[1].split("/")[0]
            for value in links.values()
            if isinstance(value, str) and "/releases/" in value
        }
        if len(names) != 1:
            return None
        return next(iter(names))

    @staticmethod
    def _shipped_sources(
        report: Mapping[str, object], release_id: str
    ) -> Mapping[str, object] | None:
        history = report.get("release_sources")
        if not isinstance(history, list):
            return None
        for item in history:
            if not isinstance(item, Mapping) or item.get("release_id") != release_id:
                continue
            sources = item.get("sources")
            if isinstance(sources, Mapping):
                return sources
        return None

    def _distance_from_head(
        self, path: Path, revision: object, *, verbose: bool = True
    ) -> dict[str, object] | None:
        """How far this checkout has moved past what the Host is running."""

        if not isinstance(revision, str) or not revision:
            return {"shipped": None, "head": None, "commits": None, "reason": "not in the release"}
        head = self.runner.run((self.git, "-C", str(path), "rev-parse", "HEAD"))
        if head.returncode != 0:
            return {"shipped": revision, "head": None, "commits": None, "reason": "unreadable"}
        current = head.stdout.strip()
        if current == revision:
            return None
        counted = self.runner.run(
            (self.git, "-C", str(path), "rev-list", "--count", f"{revision}..{current}")
        )
        commits = (
            int(counted.stdout.strip())
            if counted.returncode == 0 and counted.stdout.strip().isdigit()
            else None
        )
        entry: dict[str, object] = {"shipped": revision, "head": current, "commits": commits}
        if commits and verbose:
            entry["subjects"] = self._subjects(path, revision, current)
        if commits is None:
            # The Host is running something this checkout does not contain: a
            # release built elsewhere, or history that was rewritten here.
            entry["reason"] = "the shipped commit is not in this checkout"
        return entry

    def _subjects(self, path: Path, revision: str, head: str) -> list[str]:
        """The commits themselves, because a count does not say which one is missing."""

        listed = self.runner.run(
            (
                self.git,
                "-C",
                str(path),
                "log",
                "--no-decorate",
                "--format=%h %s",
                f"-{_PENDING_SUBJECTS + 1}",
                f"{revision}..{head}",
            )
        )
        if listed.returncode != 0:
            return []
        lines = [line for line in listed.stdout.splitlines() if line.strip()]
        if len(lines) > _PENDING_SUBJECTS:
            return [*lines[:_PENDING_SUBJECTS], "…"]
        return lines

    def _uncommitted(self, path: Path, *, verbose: bool = True) -> dict[str, object] | None:
        """Work that cannot reach the Host at all until it is committed.

        A release is sealed with ``git archive`` from a commit, so an edit that
        is not committed is not merely unsent — it is unsendable, and `deploy`
        refuses rather than shipping around it. Someone asking why their change
        is not on the board deserves to be told that here, next to the commits
        that simply have not gone yet.
        """

        listed = self.runner.run((self.git, "-C", str(path), "status", "--porcelain"))
        if listed.returncode != 0:
            return None
        entries = [line.strip() for line in listed.stdout.splitlines() if line.strip()]
        if not entries:
            return None
        edits: dict[str, object] = {"paths": len(entries)}
        if verbose:
            edits["sample"] = entries[:_PENDING_SUBJECTS]
        return edits

    def app_ready(self) -> dict[str, object]:
        self.preflight.validate_ssh_material()
        return self.transport.run_agent(
            "app-ready",
            self.host_layer.target_payload(),
            timeout=READINESS_TRANSPORT_TIMEOUT_SECONDS,
        )

    def doctor(self, *, release_id: str | None = None) -> dict[str, object]:
        # A dirty sibling repository is reported here, not refused: this is the
        # command an operator runs to find out what is wrong, and it must be
        # able to answer while the workspace is mid-edit.
        local = self.preflight.run(require_install_files=False, require_clean_sources=False)
        foundation = self.provision(apply=False)
        if foundation["status"] == "planned_bootstrap":
            return {
                "status": "degraded",
                "local": local,
                "foundation": foundation,
                "remote": {"status": "unavailable", "reason": "python3 is missing"},
            }
        payload = self.host_layer.target_payload()
        payload["remote_uv"] = str(self.config.host.remote_uv)
        if release_id is not None:
            payload["release_id"] = validate_release_id(release_id)
        remote = self.transport.run_agent("doctor-host", payload, timeout=240)
        healthy = remote.get("status") == "healthy" and foundation.get("status") == "healthy"
        return {
            "status": "healthy" if healthy else "degraded",
            "local": local,
            "foundation": foundation,
            "remote": remote,
        }

    def logs(self, *, unit: str | None, lines: int, since: str | None) -> dict[str, object]:
        self.preflight.validate_ssh_material()
        return self.transport.run_agent(
            "logs",
            {**self.host_layer.target_payload(), "unit": unit, "lines": lines, "since": since},
            timeout=180,
        )

    def diagnose(self, *, output: Path) -> dict[str, object]:
        if not output.is_absolute() or output.suffixes[-2:] != [".tar", ".gz"]:
            raise OperationsError("diagnostic output must be an absolute .tar.gz path")
        if output.exists():
            raise OperationsError("diagnostic output already exists")
        self.preflight.validate_ssh_material()
        document = self.transport.run_agent(
            "diagnose", self.host_layer.target_payload(), timeout=300
        )
        output.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="eidolon-diagnostics-") as directory:
            root = Path(directory)
            write_json(root / "target.json", document)
            write_json(
                root / "release-inputs.json",
                {
                    "target": self.config.host.target,
                    "port": self.config.host.port,
                    "sources": self.preflight.sources.provenance(),
                    "units": self.config.units,
                    "redaction": "Local secret paths/values and SSH key bytes omitted.",
                },
            )
            with tarfile.open(output, "w:gz") as archive:
                archive.add(root / "target.json", arcname="target.json")
                archive.add(root / "release-inputs.json", arcname="release-inputs.json")
        return {
            "status": "diagnosed",
            "output": str(output),
            "bytes": output.stat().st_size,
            "redacted": True,
        }

    # -- foundation ----------------------------------------------------------

    def _nested_provision(self, *, apply: bool) -> dict[str, object]:
        """Install the foundation as part of a larger operation.

        Silent: the release transaction announces one step of its own around
        this, and a foundation phase named ``install`` arriving in the middle
        of an ``install`` operation would name two different things the same.
        """

        return self.provision(apply=apply, journal=Journal())

    def provision(self, *, apply: bool, journal: Journal | None = None) -> dict[str, object]:
        """Detect or install the pinned non-Eidolon Raspberry Pi foundation."""

        self.preflight.validate_ssh_material()
        self.preflight.require_commands(("ssh", "scp"))
        phases = Journal(self.progress) if journal is None else journal
        phases.begin("python_probe")
        python_available = self._remote_python_available()
        phases.append({"phase": "python_probe", "available": python_available})
        if not python_available:
            if not apply:
                return {
                    "status": "planned_bootstrap",
                    "profile": FOUNDATION_PROFILE,
                    "phases": phases,
                    "next": "rerun provision --apply to bootstrap Python and the pinned foundation",
                }
            phases.begin("python_bootstrap")
            bootstrap = self.transport.run(
                ("/bin/sh", "-s"),
                input_bytes=python_bootstrap_script(),
                sudo=True,
                timeout=1800,
                operation="remote foundation Python bootstrap",
            )
            phases.append(
                {
                    "phase": "python_bootstrap",
                    "result": parse_json(bootstrap.stdout, "foundation bootstrap"),
                }
            )
        payload = {"foundation": foundation_payload()}
        phases.begin("doctor")
        observed = self.transport.run_agent("foundation-doctor", payload, timeout=300)
        phases.append({"phase": "doctor", "result": observed})
        if observed.get("status") == "healthy":
            return {
                "status": "healthy",
                "profile": FOUNDATION_PROFILE,
                "changed": False,
                "phases": phases,
            }
        if not apply:
            return {
                "status": "degraded",
                "profile": FOUNDATION_PROFILE,
                "changed": False,
                "phases": phases,
                "next": "rerun provision --apply after reviewing missing packages and capacity gates",
            }
        phases.begin("install")
        installed = self.transport.run_agent("foundation-install", payload, timeout=3600)
        phases.append({"phase": "install", "result": installed})
        return {
            "status": "installed",
            "profile": FOUNDATION_PROFILE,
            "changed": True,
            "phases": phases,
        }

    def _remote_python_available(self) -> bool:
        result = self.transport.run(
            ("/bin/sh", "-s"),
            input_bytes=python_probe_script(),
            timeout=30,
            operation="remote Python probe",
        )
        return parse_json(result.stdout, "remote Python probe") == {"python3": True}

    # -- private inputs ------------------------------------------------------

    def initialize_inputs(self, *, new_identity: bool = False) -> dict[str, object]:
        """Create the private local first-install input set without contacting the Pi.

        A machine keeps the identity it was given. `new_identity` retires it,
        which is what a machine changing hands means and what installing onto
        the same one again does not.
        """

        self.preflight.require_exact_commits(_SETTINGS_SOURCES)
        try:
            result = initialize_install_inputs(
                self.preflight.sources.resolved_config(),
                self.preflight.read_exact_source_file,
                new_identity=new_identity,
            )
            if self.app is None:
                return result
            self.host_layer.prepare()
            # The contract describes the Host binding, which the materializer
            # owns; the assets it produced are the private material itself.
            return {**result, "host_application": self.host_layer.public_contract()}
        except ASSET_ERRORS as exc:
            raise OperationsError(str(exc)) from exc

    def add_missing_input_credentials(self, *, apply: bool = False) -> dict[str, object]:
        """Give this machine's input set the credentials the product grew since.

        Not an install and not a reissue: it adds keys the set is missing and
        touches nothing that is already there. Without it, a Host installed
        before a credential existed has an input set the contract check refuses
        and no way to fix but reinitialising — which rotates every secret on the
        Host and re-anchors its identity.

        Dry unless asked, because whoever runs this is holding a Host that works.
        """

        try:
            return add_missing_install_credentials(
                self.preflight.sources.resolved_config(), apply=apply
            )
        except ASSET_ERRORS as exc:
            raise OperationsError(str(exc)) from exc

    def converge_inputs(self, *, apply: bool = False) -> dict[str, object]:
        """Make this Host hold the credential set the product declares.

        One operation rather than two, because two is how the gap stayed open.
        There was a verb that repaired the *workstation's* input set and no verb
        that delivered the result, so a Host installed before a credential
        existed could be diagnosed and never fixed: ``install`` refuses when an
        input on the Host differs from the staged one, and ``refresh`` carries
        only the two Host-bound files. The repair was reachable, correct, and
        pointless on its own.

        Both halves, in order:

        1. the local input set gains the keys the declaration says it is missing
           — copied from the file that already holds the other side of a shared
           secret, minted once for the pair when neither side has it;
        2. the Host gains the keys *it* is missing, from those files.

        Additive at both ends. Nothing already present is read, replaced, or
        rotated, which is what makes this safe to run on a Host that works —
        the only kind anybody runs it on.

        Dry unless asked. The report names keys, never values.
        """

        local = self.add_missing_input_credentials(apply=apply)
        payload: dict[str, object] = {
            "declared": declared_secret_env_keys(),
            "apply": apply,
        }
        if apply:
            # Staged with the Host layer's own renderer, so a Host-bound file
            # offers the values this Host should have rather than the template's.
            stage = f"/var/tmp/eidolon-secrets-{_CONVERGENCE_STAGE_ID}"
            self.host_layer.stage_install_files(_CONVERGENCE_STAGE_ID, stage)
            payload["release_id"] = _CONVERGENCE_STAGE_ID
        host = self.transport.run_agent(
            "converge-secret-inputs", payload, timeout=120
        )
        applied = bool(local.get("applied")) or bool(host.get("applied"))
        return {
            "status": "converged" if applied else "planned",
            "workstation": local,
            "host": host,
            # Named rather than performed: an env file is read at start, so the
            # units that read a changed file have to be restarted — and deciding
            # *when* a Host restarts is the operator's call, not this verb's.
            "restart_required": sorted(host.get("added") or {}),
            "next": (
                "run `restart` so the services read their new credentials"
                if applied
                else "rerun with --apply to write what is listed"
            ),
        }

    # -- boundary actions ----------------------------------------------------

    def lifecycle(self, action: str, *, dry_run: bool) -> dict[str, object]:
        if action not in _LIFECYCLE_ACTIONS:
            raise OperationsError("unknown lifecycle action")
        self.preflight.validate_ssh_material()
        if dry_run:
            return {
                "status": "planned",
                "action": action,
                "units": self.config.units,
                "authority": "eidolond remains the Data/Hub/Kernel desired-state owner",
            }
        return self.transport.run_agent(
            action,
            self.host_layer.target_payload(),
            python=self.active_release_interpreter(),
            timeout=300,
        )

    def commissioning_code(
        self,
        *,
        ttl_seconds: int,
        setup_code: str | None = None,
    ) -> dict[str, object]:
        """Mint the one-time Setup code a phone types to claim this Host.

        SSH to the Host is what authorises this, the same way it authorises
        controller-reset: both reach a root-owned local socket, and issuing a
        code is the lesser of the two acts.

        A profile may pin the value (``app.setup_code``) so the operator never
        has to look one up. Only the value is pinned: the Host still opens one
        ordinary session for it, which expires, is spent once, and supersedes
        any window before it.
        """

        self.preflight.validate_ssh_material()
        named = setup_code if setup_code is not None else self._configured_setup_code()
        payload: dict[str, object] = {
            **self.host_layer.target_payload(),
            "ttl_seconds": ttl_seconds,
        }
        if named is not None:
            payload["setup_code"] = named
        return self.transport.run_agent(
            "commissioning-code",
            payload,
            timeout=180,
        )

    def _configured_setup_code(self) -> str | None:
        app = self.app
        return None if app is None else app.setup_code

    def reset(self, *, wipe_authority_data: bool, apply: bool) -> dict[str, object]:
        """Plan or remove only the fixed Eidolon Host deployment namespace."""

        self.preflight.validate_ssh_material()
        payload = {
            **self.host_layer.target_payload(),
            "wipe_authority_data": wipe_authority_data,
        }
        # Everything that can refuse does so before the Host is touched.
        authority = self._authority_to_remove() if wipe_authority_data else None
        plan = self.transport.run_agent("reset-plan", payload, timeout=180)
        if plan.get("status") != "planned":
            raise OperationsError("Host reset plan returned invalid evidence")
        if authority is not None:
            authority = self._attribute_authority(authority, plan)
            plan = {**plan, "authority": authority}
        if not apply:
            return {**plan, "next": "rerun reset --apply after reviewing the detected paths"}
        result = self.transport.run_agent("reset-host", payload, timeout=600)
        if result.get("status") != "reset":
            raise OperationsError("Host reset returned invalid evidence")
        return result if authority is None else {**result, "authority": authority}

    @staticmethod
    def _attribute_authority(
        authority: dict[str, object], plan: dict[str, object]
    ) -> dict[str, object]:
        """Say who owns each thing this reset is about to take.

        The roots stay the unit of removal, because some of what lives under
        them belongs to no component: NATS keeps its JetStream store there and
        has no repository of ours to publish a contract. Narrowing the deletion
        to the declared paths would hand the next owner a Host that still holds
        the last one's message history.

        What was wrong was not the breadth — it was that the report described
        one list and the Host removed another. Now the report is about what the
        Host will actually remove, and every entry either carries a component's
        name or is called out as carrying nobody's.
        """

        contents = plan.get("authority_contents")
        if not isinstance(contents, list):
            return authority
        declared = authority.get("declared_by")
        owners: dict[str, str] = {}
        if isinstance(declared, dict):
            for component_id, paths in declared.items():
                for path in paths:
                    owners[str(path)] = str(component_id)

        claimed: dict[str, str] = {}
        unclaimed: list[str] = []
        for entry in sorted(str(item) for item in contents):
            owner = next(
                (
                    component_id
                    for path, component_id in owners.items()
                    if _same_state(entry, path)
                ),
                None,
            )
            if owner is None:
                unclaimed.append(entry)
            else:
                claimed[entry] = owner
        return {
            **authority,
            "will_be_removed": claimed,
            # Not an error. It is how an operator learns that a factory reset
            # also takes the message bus's stores, and how a directory left by
            # a component that has since left the product becomes visible
            # instead of vanishing quietly.
            "removed_but_unclaimed": unclaimed,
        }

    def _authority_to_remove(self) -> dict[str, object]:
        """What each component says a reset takes from it, checked against the roots.

        A reset that wipes authority is the one operation where being nearly
        right is worse than refusing, so it is the one that asks the components
        rather than trusting three coarse roots to have covered them. A
        component whose state moved outside those roots would leave the next
        owner holding the last one's data, and a wipe of /var/lib/eidolon
        would not have said so.

        Silence is read three ways, because a release pins exact commits and
        Ops has to be able to reset a Host running one from before any of this
        existed:

        * Every component declares — the answer is checked and reported.
        * None declares — a pre-contract release. The roots are what they
          always were, so the reset proceeds and the plan says the question
          was not asked rather than implying it was answered.
        * Some declare and some do not — refused. That mixture is the one that
          is actually dangerous: the set has started moving, and a component
          that has not been asked is exactly where the moved state would be.
        """

        sources = {
            source_id: source.path for source_id, source in self.config.sources.items()
        }
        topology = read_component_contracts(sources)
        if not topology.contracts:
            return {
                "contracts": "absent",
                "note": (
                    "no component in this release declares an operations contract, "
                    "so what a reset removes was not checked against them"
                ),
            }
        topology.requires_every_component("wiping authority data")

        roots = tuple(RESET_AUTHORITY_ROOTS)
        removed: dict[str, list[str]] = {}
        stranded: list[str] = []
        # The platform included: NATS and LiveKit hold state under these roots
        # and a reset removes it, so an operator should read that as a decision
        # someone made rather than as a path nobody accounted for.
        for contract_ in topology.declared:
            declared = sorted(str(path) for path in contract_.factory_reset_paths)
            removed[contract_.component_id] = declared
            stranded.extend(
                path
                for path in declared
                if not any(Path(path).is_relative_to(root) for root in roots)
            )
        if stranded:
            raise OperationsError(
                "these components declare state a reset would not reach: "
                + ", ".join(sorted(stranded))
                + ". The Host clears "
                + ", ".join(str(root) for root in roots)
                + " and nothing else, so this reset would leave data behind."
            )
        return {
            "contracts": "complete",
            "declared_by": removed,
            # Named here as well as in a backup report, because this is the
            # last moment anyone can decide not to lose it.
            "not_in_any_backup": sorted(
                f"{state.component_id}:{state.path}"
                for state in topology.authority
                if not state.is_covered
            ),
        }

    # -- Owner Authority capability an install carries -------------------------

    def _observed_authority_lineage(self) -> dict[str, object]:
        """What the Host holds today, asked before anything is shipped to it."""

        observed = self.transport.run_agent(
            "authority-lineage", self.host_layer.target_payload(), timeout=120
        )
        if observed.get("status") != "observed" or set(observed) != {
            "status",
            "marker",
            "anchor",
            "established",
        }:
            raise OperationsError("Owner Authority lineage observation is invalid")
        return observed

    def authority_capability(
        self, *, will_wipe: bool, apply: bool
    ) -> dict[str, object] | None:
        """Decide which Owner Authority capability an install must carry.

        The bootstrap capability in an install is one-shot: Hub accepts it only
        into an empty database, and deletes it on use.  The controller records
        that use in its own Owner material, so what an install ships is a
        *decision*, not a copy of a file — and until now nobody made it.  An
        install shipped whatever the material root last said, which after any
        successful Hub start is ``owner-authority.bootstrap-consumed``.  A Host
        whose Hub had ever come up could therefore never be wiped and
        reinstalled: the reset emptied the database and the install handed it a
        capability Hub is right to refuse.

        The decision is made from Host evidence, never from the flags alone.
        The rules are stated once, in
        :func:`eidolon_ops.owner_domain_assets.decide_owner_authority`, because
        an install is not the only operation that reaches this fork: a Mac
        source run reaches it from its own ``reset --wipe-authority-data``.

        ``will_wipe`` is not one of those rules.  It only says the database this
        observation found is about to be removed, so the decision is made
        against the Host as it will be, not as it is.
        """

        if self.app is None:
            return None
        materializer = self.host_layer.materializer()
        try:
            current = materializer.owner_assets()
        except OwnerDomainAssetError as exc:
            raise OperationsError(str(exc)) from exc
        observed = self._observed_authority_lineage()
        # A capability the Host can prove it used, but whose use this
        # controller never got to record, is consumed. Reading it as pending
        # would hand a wiped Host the state id its destroyed database carried.
        if (
            decide_owner_authority(
                current,
                marker=observed["marker"],
                established=observed["established"],
            )
            is AuthorityDecision.RECORD_CONSUMED
        ):
            try:
                mark_authority_bootstrapped(
                    materializer.material_root,
                    owner_domain_id=current.owner_domain_id,
                    owner_domain_generation=current.owner_domain_generation,
                    authority_state_id=current.authority_state_id,
                )
                current = materializer.owner_assets()
            except OwnerDomainAssetError as exc:
                raise OperationsError(str(exc)) from exc
        lineage = authority_lineage(current)
        marker = None if will_wipe else observed["marker"]
        decision = decide_owner_authority(
            current, marker=marker, established=observed["established"]
        )
        if decision is AuthorityDecision.RECOVERY_REQUIRED:
            raise OperationsError(_authority_recovery_required(marker, lineage))
        if decision in {
            AuthorityDecision.CARRY_PENDING,
            AuthorityDecision.KEEP_ESTABLISHED,
        }:
            return {
                "decision": str(decision),
                "owner_domain_id": current.owner_domain_id,
                "owner_domain_generation": current.owner_domain_generation,
                "generation_advanced": False,
                "observed": observed,
                "lineage": lineage,
            }
        if not apply:
            return {
                "decision": "advance_generation",
                "owner_domain_id": current.owner_domain_id,
                "owner_domain_generation": current.owner_domain_generation + 1,
                "previous_generation": current.owner_domain_generation,
                "generation_advanced": False,
                "observed": observed,
                "lineage": None,
                "next": (
                    "this install rebuilds an Authority whose state is gone; "
                    "every existing device Claim and credential becomes void"
                ),
            }
        try:
            # Same CAS primitive as authority-reset: the new lineage evidence is
            # persisted before any descriptor or capability naming it is
            # rendered, let alone shipped.
            advance_owner_authority(
                materializer.material_root,
                expected_owner_domain_id=current.owner_domain_id,
                expected_generation=current.owner_domain_generation,
            )
            advanced = materializer.owner_assets()
        except OwnerDomainAssetError as exc:
            raise OperationsError(str(exc)) from exc
        if not advanced.bootstrap_pending:
            raise OperationsError(
                "Owner Authority rebuild has no pending bootstrap capability"
            )
        return {
            "decision": "advance_generation",
            "owner_domain_id": advanced.owner_domain_id,
            "owner_domain_generation": advanced.owner_domain_generation,
            "previous_generation": current.owner_domain_generation,
            "generation_advanced": True,
            "observed": observed,
            "lineage": authority_lineage(advanced),
        }

    def commit_authority_capability(
        self, capability: dict[str, object] | None, installed: object
    ) -> dict[str, object] | None:
        """Record the one-shot capability as spent, against the Host's proof.

        Nothing else does this.  A first install left the controller saying
        ``bootstrap_pending`` forever, which is why ``authority-backup``
        refused every Host that had only ever been installed, and why the
        decision above could not have been made from the material root alone.
        """

        if capability is None:
            return None
        expected = capability["lineage"]
        reported = installed.get("authority") if isinstance(installed, dict) else None
        if reported != expected:
            raise OperationsError(
                "the installed Host did not establish the Owner Authority lineage this "
                f"install carried: expected {expected}, Host reported {reported}"
            )
        materializer = self.host_layer.materializer()
        try:
            mark_authority_bootstrapped(
                materializer.material_root,
                owner_domain_id=str(expected["owner_domain_id"]),
                owner_domain_generation=int(expected["owner_domain_generation"]),
                authority_state_id=str(expected["state_id"]),
            )
        except OwnerDomainAssetError as exc:
            raise OperationsError(str(exc)) from exc
        return {"status": "authority_bootstrap_consumed", "authority": expected}

    def controller_reset(self, *, apply: bool) -> dict[str, object]:
        """Return a claimed Host to unclaimed so a new phone can manage it.

        The Owner keeps everything else: Host identity, Owner binding, saved
        Wi-Fi and all component data. Only the managing phones lose access.
        """

        self.preflight.validate_ssh_material()
        if not apply:
            return {
                "status": "planned",
                "host": self.config.host.target,
                "revokes": "every Controller Grant; managing phones lose access immediately",
                "preserves": [
                    "Host identity and pinned TLS",
                    "Owner binding, Companions and Persona",
                    "saved Wi-Fi profiles and the current connection",
                    "admitted Devices and their Kernel mounts",
                ],
                "next": "rerun controller-reset --apply, then claim the Host from a new phone",
            }
        result = self.transport.run_agent(
            "controller-reset", self.host_layer.target_payload(), timeout=180
        )
        if result.get("status") != "reset":
            raise OperationsError("Controller reset returned invalid evidence")
        return result

    def authority_reset(self, *, apply: bool) -> dict[str, object]:
        """Advance one Owner Authority generation and replace only Hub state.

        A pending generation is a durable retry journal: once the controller
        has advanced, a transport or Host failure retries the same state id
        instead of minting another generation.  The target independently
        proves the DB marker and external lineage anchor before the one-shot
        bootstrap is marked consumed here.
        """

        self.preflight.validate_ssh_material()
        if self.app is None:
            raise OperationsError("Owner Authority reset requires the Pi Host app contract")
        materializer = self.host_layer.materializer()
        try:
            current = materializer.owner_assets()
        except OwnerDomainAssetError as exc:
            raise OperationsError(str(exc)) from exc
        retry = current.bootstrap_pending and current.owner_domain_generation > 1
        next_generation = (
            current.owner_domain_generation
            if retry
            else current.owner_domain_generation + 1
        )
        if not apply:
            return {
                "status": "planned",
                "owner_domain_id": current.owner_domain_id,
                "previous_generation": next_generation - 1,
                "next_generation": next_generation,
                "retry_pending_generation": retry,
                "removes": "Hub database, SQLite sidecars and Authority lineage anchor only",
                "preserves": [
                    "Host identity, active release and Owner root",
                    "network configuration and all non-Hub authorities",
                    "Controller-side monotonic generation journal",
                ],
                "next": "rerun authority-reset --apply after reviewing the exact target",
            }
        try:
            if not retry:
                advance_owner_authority(
                    materializer.material_root,
                    expected_owner_domain_id=current.owner_domain_id,
                    expected_generation=current.owner_domain_generation,
                )
            pending = materializer.owner_assets()
        except OwnerDomainAssetError as exc:
            raise OperationsError(str(exc)) from exc
        if not pending.bootstrap_pending:
            raise OperationsError("Owner Authority reset has no pending bootstrap capability")
        request = {
            "owner_domain_id": pending.owner_domain_id,
            "previous_generation": pending.owner_domain_generation - 1,
            "next_generation": pending.owner_domain_generation,
            "state_id": pending.authority_state_id,
        }
        release_id = self._active_release("release_id")
        # The expand release is active before this is called. Both interpreter
        # checks therefore validate the same current parser; no N-1 process is
        # restarted against a schema it cannot read.
        refreshed = self.host_layer.refresh(release_id)
        payload = {
            **self.host_layer.target_payload(),
            "authority_reset": request,
        }
        plan = self.transport.run_agent("authority-reset-plan", payload, timeout=180)
        if plan.get("status") not in {"planned", "already_reset"}:
            raise OperationsError("Owner Authority reset plan returned invalid evidence")
        result = self.transport.run_agent("authority-reset", payload, timeout=420)
        authority = result.get("authority")
        if (
            result.get("status") not in {"authority_reset", "already_reset"}
            or not isinstance(authority, dict)
            or authority.get("owner_domain_id") != pending.owner_domain_id
            or authority.get("owner_domain_generation")
            != pending.owner_domain_generation
            or authority.get("state_id") != pending.authority_state_id
        ):
            raise OperationsError("Owner Authority reset returned invalid lineage proof")
        try:
            mark_authority_bootstrapped(
                materializer.material_root,
                owner_domain_id=pending.owner_domain_id,
                owner_domain_generation=pending.owner_domain_generation,
                authority_state_id=pending.authority_state_id,
            )
        except OwnerDomainAssetError as exc:
            raise OperationsError(str(exc)) from exc
        consumed = self.host_layer.refresh(release_id)
        reclaimed = self.bundles.reclaim(release_id, phase="commit")
        self.bundles.require_reclamation(reclaimed, "committed")
        ready = self.app_ready()
        return {
            **result,
            "plan": plan,
            "host_application": refreshed,
            "bootstrap_tombstone": consumed,
            "release_reclaim": reclaimed,
            "app": ready,
        }

    def authority_backup(self, *, output: Path) -> dict[str, object]:
        """Capture the Owner root package and the matching complete Hub state."""

        self.preflight.validate_ssh_material()
        if not output.is_absolute():
            raise OperationsError("Owner Authority backup output must be absolute")
        if (
            output.is_symlink()
            or not output.is_dir()
            or stat.S_IMODE(output.stat().st_mode) != 0o700
            or output.stat().st_uid != os.getuid()
        ):
            raise OperationsError(
                "Owner Authority backup output must be an owned private directory"
            )
        if self.app is None:
            raise OperationsError("Owner Authority backup requires the Pi Host app contract")
        materializer = self.host_layer.materializer()
        try:
            owner = materializer.owner_assets()
        except OwnerDomainAssetError as exc:
            raise OperationsError(str(exc)) from exc
        if owner.bootstrap_pending:
            raise OperationsError(
                "AUTHORITY_RESTORE_INCOMPLETE: pending ResetAuthority state cannot be backed up"
            )
        release_id = self._active_release("release_id")
        request = {
            "owner_domain_id": owner.owner_domain_id,
            "owner_domain_generation": owner.owner_domain_generation,
            "state_id": owner.authority_state_id,
        }
        captured = self.transport.run_agent(
            "authority-backup",
            {
                **self.host_layer.target_payload(),
                "release_id": release_id,
                "authority_restore": request,
            },
            timeout=300,
        )
        if captured.get("status") != "authority_backup_captured":
            raise OperationsError("Owner Authority backup returned invalid evidence")
        if captured.get("authority") != {
            "contract_version": 1,
            **request,
        }:
            raise OperationsError("Owner Authority backup lineage evidence mismatched")
        destination = output / (
            f"{release_id}-owner-authority-generation-{owner.owner_domain_generation}"
        )
        if destination.exists() or destination.is_symlink():
            raise OperationsError(f"Owner Authority backup destination exists: {destination}")
        destination.mkdir(mode=0o700)
        host_state = destination / "host-state"
        self.transport.download(str(captured["directory"]), host_state, recursive=True)
        owner_material = destination / "owner-material"
        shutil.copytree(materializer.material_root, owner_material)
        os.chmod(host_state, 0o700)
        os.chmod(owner_material, 0o700)
        for path in owner_material.iterdir():
            if path.is_symlink() or not path.is_file():
                raise OperationsError("Owner Authority material package is unsafe")
            os.chmod(path, 0o600)
        for path in host_state.iterdir():
            if path.is_symlink() or not path.is_file():
                raise OperationsError("Owner Authority Host state package is unsafe")
            os.chmod(path, 0o600)
        files = captured.get("files")
        if not isinstance(files, dict) or set(files) != {"database", "anchor"}:
            raise OperationsError("Owner Authority backup returned no file evidence")
        for kind, expected_name in {
            "database": "eidolon-hub.sqlite3",
            "anchor": "authority-lineage.json",
        }.items():
            record = files[kind]
            path = host_state / expected_name
            if (
                not isinstance(record, dict)
                or record.get("name") != expected_name
                or record.get("sha256") != file_sha256(path)
                or record.get("bytes") != path.stat().st_size
            ):
                raise OperationsError("Owner Authority backup file evidence mismatched")
        material_files = {
            path.name: {"sha256": file_sha256(path), "bytes": path.stat().st_size}
            for path in sorted(owner_material.iterdir())
        }
        manifest = {
            "contract_version": 1,
            "operation": "owner-authority.restore-package",
            "release_id": release_id,
            "authority": captured.get("authority"),
            "host_files": files,
            "owner_material": material_files,
        }
        write_json(destination / "authority-restore.json", manifest)
        os.chmod(destination / "authority-restore.json", 0o600)
        return {
            "status": "authority_backup_captured",
            "directory": str(destination),
            "authority": captured.get("authority"),
            "host_files": files,
            "owner_material_files": sorted(material_files),
        }

    def authority_restore(self, *, source: Path, apply: bool) -> dict[str, object]:
        """Restore one complete same-generation Owner Authority package."""

        self.preflight.validate_ssh_material()
        if self.app is None:
            raise OperationsError("Owner Authority restore requires the Pi Host app contract")
        manifest_path = source / "authority-restore.json"
        if (
            not source.is_absolute()
            or source.is_symlink()
            or not source.is_dir()
            or stat.S_IMODE(source.stat().st_mode) != 0o700
            or source.stat().st_uid != os.getuid()
            or manifest_path.is_symlink()
            or not manifest_path.is_file()
            or stat.S_IMODE(manifest_path.stat().st_mode) != 0o600
            or manifest_path.stat().st_uid != os.getuid()
        ):
            raise OperationsError("AUTHORITY_RESTORE_INVALID: restore package is unsafe")
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise OperationsError(
                "AUTHORITY_RESTORE_INCOMPLETE: restore manifest is missing or invalid"
            ) from exc
        authority = manifest.get("authority") if isinstance(manifest, dict) else None
        host_files = manifest.get("host_files") if isinstance(manifest, dict) else None
        owner_files = manifest.get("owner_material") if isinstance(manifest, dict) else None
        if (
            not isinstance(manifest, dict)
            or set(manifest)
            != {
                "contract_version",
                "operation",
                "release_id",
                "authority",
                "host_files",
                "owner_material",
            }
            or manifest.get("contract_version") != 1
            or manifest.get("operation") != "owner-authority.restore-package"
            or not isinstance(manifest.get("release_id"), str)
            or not isinstance(authority, dict)
            or set(authority)
            != {"contract_version", "owner_domain_id", "owner_domain_generation", "state_id"}
            or authority.get("contract_version") != 1
            or not isinstance(authority.get("owner_domain_id"), str)
            or not authority["owner_domain_id"].startswith("owner-")
            or type(authority.get("owner_domain_generation")) is not int
            or authority["owner_domain_generation"] < 1
            or not isinstance(authority.get("state_id"), str)
            or not authority["state_id"].startswith("authority-state_")
            or not isinstance(host_files, dict)
            or set(host_files) != {"database", "anchor"}
            or not isinstance(owner_files, dict)
            or set(owner_files) != set(OWNER_DOMAIN_MATERIAL_NAMES)
        ):
            raise OperationsError("AUTHORITY_RESTORE_INVALID: restore manifest shape is invalid")
        try:
            validate_release_id(manifest["release_id"])
        except (TypeError, ValueError) as exc:
            raise OperationsError(
                "AUTHORITY_RESTORE_INVALID: backup release identity is invalid"
            ) from exc
        if {path.name for path in source.iterdir()} != {
            "authority-restore.json",
            "host-state",
            "owner-material",
        }:
            raise OperationsError("AUTHORITY_RESTORE_INVALID: restore package has extra content")
        for section, directory in ((host_files, source / "host-state"), (owner_files, source / "owner-material")):
            if (
                directory.is_symlink()
                or not directory.is_dir()
                or stat.S_IMODE(directory.stat().st_mode) != 0o700
                or directory.stat().st_uid != os.getuid()
            ):
                raise OperationsError("AUTHORITY_RESTORE_INVALID: restore directory is unsafe")
            expected_names = {
                str(value.get("name")) if section is host_files else str(name)
                for name, value in section.items()
                if isinstance(value, dict)
            }
            if {path.name for path in directory.iterdir()} != expected_names:
                raise OperationsError("AUTHORITY_RESTORE_INCOMPLETE: restore package is partial")
            for name, value in section.items():
                if not isinstance(value, dict):
                    raise OperationsError("AUTHORITY_RESTORE_INVALID: restore file record is invalid")
                filename = str(value.get("name")) if section is host_files else str(name)
                expected_filename = (
                    {"database": "eidolon-hub.sqlite3", "anchor": "authority-lineage.json"}[name]
                    if section is host_files
                    else name
                )
                if (
                    filename != expected_filename
                    or not isinstance(value.get("sha256"), str)
                    or len(value["sha256"]) != 64
                    or any(character not in "0123456789abcdef" for character in value["sha256"])
                    or type(value.get("bytes")) is not int
                    or value["bytes"] < 1
                ):
                    raise OperationsError("AUTHORITY_RESTORE_INVALID: restore file record is invalid")
                path = directory / filename
                if (
                    path.is_symlink()
                    or not path.is_file()
                    or stat.S_IMODE(path.stat().st_mode) != 0o600
                    or path.stat().st_uid != os.getuid()
                    or file_sha256(path) != value.get("sha256")
                    or path.stat().st_size != value.get("bytes")
                ):
                    raise OperationsError("AUTHORITY_RESTORE_INVALID: restore file evidence drifted")
        state_path = source / "owner-material/owner-domain-state.json"
        try:
            recovery_state = json.loads(state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise OperationsError("AUTHORITY_RESTORE_INVALID: Owner recovery state is invalid") from exc
        if (
            not isinstance(recovery_state, dict)
            or
            recovery_state.get("owner_domain_id") != authority["owner_domain_id"]
            or recovery_state.get("owner_domain_generation")
            != authority["owner_domain_generation"]
            or recovery_state.get("authority_state_id") != authority["state_id"]
            or recovery_state.get("bootstrap_pending") is not False
        ):
            raise OperationsError(
                "AUTHORITY_RESTORE_MISMATCH: Owner root state and Authority snapshot differ"
            )
        materializer = self.host_layer.materializer()
        # Validate the complete private root, key pairs, certificates,
        # descriptor signature, endpoint binding and recovery state without
        # allowing validation to rewrite the supplied backup package.
        try:
            with tempfile.TemporaryDirectory(prefix="eidolon-authority-restore-") as temporary:
                validation_root = Path(temporary) / "owner-domain"
                shutil.copytree(source / "owner-material", validation_root)
                os.chmod(validation_root, 0o700)
                for path in validation_root.iterdir():
                    os.chmod(path, 0o600)
                validated = ensure_owner_domain_assets(
                    validation_root,
                    materializer.identity(),
                    self.app.hub_https_port,
                )
        except OwnerDomainAssetError as exc:
            raise OperationsError(f"AUTHORITY_RESTORE_INVALID: {exc}") from exc
        if (
            validated.owner_domain_id != authority["owner_domain_id"]
            or validated.owner_domain_generation != authority["owner_domain_generation"]
            or validated.authority_state_id != authority["state_id"]
            or validated.bootstrap_pending
        ):
            raise OperationsError(
                "AUTHORITY_RESTORE_MISMATCH: validated Owner root lineage differs from snapshot"
            )
        material_installed = False
        if materializer.material_root.exists():
            try:
                current = materializer.owner_assets()
            except OwnerDomainAssetError as exc:
                raise OperationsError(str(exc)) from exc
            if (
                current.owner_domain_id != authority["owner_domain_id"]
                or current.owner_domain_generation != authority["owner_domain_generation"]
                or current.authority_state_id != authority["state_id"]
                or current.bootstrap_pending
            ):
                raise OperationsError(
                    "AUTHORITY_RESTORE_MISMATCH: current Owner root lineage differs from backup"
                )
        request = {
            "owner_domain_id": authority["owner_domain_id"],
            "owner_domain_generation": authority["owner_domain_generation"],
            "state_id": authority["state_id"],
            "database_sha256": host_files["database"]["sha256"],
            "anchor_sha256": host_files["anchor"]["sha256"],
        }
        if not apply:
            return {
                "status": "authority_restore_planned",
                "authority": authority,
                "owner_root_import_required": not materializer.material_root.exists(),
                "generation_advanced": False,
                "next": "rerun with --apply to restore this exact same-generation package",
            }
        # Re-render Host endpoint/TLS material from the restored root before Hub
        # sees the restored database. Endpoint relocation changes directory
        # revision, never the Authority generation or database marker.
        release_id = self._active_release("release_id")
        remote = f"/var/tmp/eidolon-authority-restore-{release_id}"
        staged = self.transport.run_agent(
            "authority-restore-stage-reset",
            {"release_id": release_id, **self.host_layer.target_payload()},
            timeout=120,
        )
        if staged.get("status") != "authority_restore_stage_ready":
            raise OperationsError("AUTHORITY_RESTORE_FAILED: restore staging was not prepared")
        try:
            self.transport.upload(source / "host-state", remote, recursive=True)
            payload = {
                **self.host_layer.target_payload(),
                "release_id": release_id,
                "authority_restore": request,
            }
            plan = self.transport.run_agent("authority-restore-plan", payload, timeout=180)
            if (
                plan.get("status") != "authority_restore_planned"
                or plan.get("authority") != authority
            ):
                raise OperationsError(
                    "AUTHORITY_RESTORE_FAILED: target restore plan is invalid"
                )
            if not materializer.material_root.exists():
                materializer.material_root.parent.mkdir(parents=True, exist_ok=True)
                shutil.copytree(source / "owner-material", materializer.material_root)
                material_installed = True
            refreshed = self.host_layer.refresh(release_id)
            result = self.transport.run_agent("authority-restore", payload, timeout=420)
            if (
                result.get("status") != "authority_restored"
                or result.get("authority") != authority
            ):
                raise OperationsError(
                    "AUTHORITY_RESTORE_FAILED: target returned invalid proof"
                )
            reclaimed = self.bundles.reclaim(release_id, phase="commit")
            self.bundles.require_reclamation(reclaimed, "committed")
            ready = _wait_for_restored_authority_readiness(
                self.app_ready,
                timeout_seconds=self.config.host.readiness_timeout_seconds,
            )
            response = {
                **result,
                "plan": plan,
                "host_application": refreshed,
                "owner_root_imported": material_installed,
                "release_reclaim": reclaimed,
                "app": ready,
            }
        except Exception as primary_error:
            try:
                _finalize_authority_restore_stage(
                    self.transport,
                    {"release_id": release_id, **self.host_layer.target_payload()},
                )
            except Exception:
                raise OperationsError(
                    "AUTHORITY_RESTORE_FAILED: restore failed and sensitive staging cleanup failed"
                ) from primary_error
            raise
        finalized = _finalize_authority_restore_stage(
            self.transport,
            {"release_id": release_id, **self.host_layer.target_payload()},
        )
        return {**response, "restore_stage_cleanup": finalized}

    # -- authorities ---------------------------------------------------------

    def backup(self, *, output: Path) -> dict[str, object]:
        """Take a backup on the Host and bring it here.

        Left on the Host it would be lost with the Host, which is most of what
        a backup is for.
        """

        self.preflight.validate_ssh_material()
        release_id = self._active_release("release_id")
        result = self.transport.run_agent(
            "backup",
            {**self.host_layer.target_payload(), "release_id": release_id},
            timeout=900,
        )
        destination = output / f"{release_id}-{result['host_id']}"
        if destination.exists():
            raise OperationsError(f"backup destination already exists: {destination}")
        destination.parent.mkdir(parents=True, exist_ok=True)
        self.transport.download(str(result["directory"]), destination, recursive=True)
        write_json(destination / "backup.json", result)
        return {**result, "local_directory": str(destination)}

    def restore(self, *, source: Path, apply: bool) -> dict[str, object]:
        """Put a backup back, after proving it belongs to this Host."""

        self.preflight.validate_ssh_material()
        manifest_path = source / "backup.json"
        if not manifest_path.is_file():
            raise OperationsError(f"backup manifest is missing: {manifest_path}")
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise OperationsError("backup manifest is not one JSON document") from exc
        if not isinstance(manifest, dict):
            raise OperationsError("backup manifest is not an object")
        release_id = validate_release_id(str(manifest.get("release_id")))
        if not apply:
            return {
                "status": "planned",
                "release_id": release_id,
                "host_id": manifest.get("host_id"),
                "authorities": [
                    entry.get("authority") for entry in manifest.get("authorities", [])
                ],
                "not_restored": [entry["state"] for entry in manifest.get("not_covered", [])],
                "next": "rerun with --apply; the product stops while its authorities are replaced",
            }
        remote = f"/var/tmp/eidolon-backup-{release_id}"
        self.transport.run(
            ("/bin/rm", "-rf", remote),
            sudo=True,
            operation="previous backup staging removal",
        )
        self.transport.upload(source, remote, recursive=True)
        return self.transport.run_agent(
            "restore",
            {
                **self.host_layer.target_payload(),
                "release_id": release_id,
                "manifest": manifest,
            },
            # A restore ends by starting the product again, and only the
            # release's own interpreter can drive its activation.
            python=self.active_release_interpreter(),
            timeout=900,
        )

    def active_release_interpreter(self) -> str:
        """Ask the target which interpreter can drive its active release."""

        return self._active_release("interpreter")

    def _active_release(self, field: str) -> str:
        """Resolve the active release's operator entries on the target itself.

        The deployer must not derive these from a component directory name; the
        target owns its own layout and reports the published, component-neutral
        entries.
        """

        observed = self.transport.run_agent(
            "active-release", self.host_layer.target_payload(), timeout=120
        )
        value = observed.get(field)
        if observed.get("status") != "observed" or not isinstance(value, str):
            raise OperationsError(f"target did not report an active release {field}")
        return value


def write_json(path: Path, document: object) -> None:
    path.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8")
