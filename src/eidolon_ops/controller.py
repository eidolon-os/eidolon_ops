"""Workstation orchestration that composes, but does not replace, eidolon-release.

This is the facade a Host adapter calls. The work is in the collaborators it
holds: proving the workstation, sealing and transferring a release, running the
release transaction, deriving the Host layer, and the read-only and boundary
operations the injected agent performs.
"""

from __future__ import annotations

import json
import tarfile
import tempfile
from pathlib import Path

from eidolon_ops.component_contract import read_component_contracts
from eidolon_ops.config import SOURCE_IDS, OperationsConfig, validate_release_id
from eidolon_ops.errors import OperationsError
from eidolon_ops.foundation import (
    FOUNDATION_PROFILE,
    foundation_payload,
    python_bootstrap_script,
    python_probe_script,
)
from eidolon_ops.host_layer import ASSET_ERRORS, HostLayer
from eidolon_ops.hostagent.contract import RESET_AUTHORITY_ROOTS
from eidolon_ops.install_inputs import initialize_install_inputs
from eidolon_ops.owner_domain_assets import (
    OwnerDomainAssetError,
    mark_authority_bootstrapped,
)
from eidolon_ops.owner_domain_assets import (
    reset_owner_authority as advance_owner_authority,
)
from eidolon_ops.paths import AppAccess
from eidolon_ops.process import ProcessRunner
from eidolon_ops.progress import Journal, ProgressSink
from eidolon_ops.readiness import READINESS_TRANSPORT_TIMEOUT_SECONDS
from eidolon_ops.release_bundle import BundleTransfer, parse_json
from eidolon_ops.release_preflight import ReleasePreflight
from eidolon_ops.release_transaction import ReleaseTransaction
from eidolon_ops.transport import SSHTransport

__all__ = ["EidolonPiController", "OperationsError"]

#: Sources whose settings templates the private input set is derived from.
_SETTINGS_SOURCES = ("eidolon_agent", "eidolon_channel", "eidolon_memory")
_LIFECYCLE_ACTIONS = frozenset({"start", "stop", "restart"})


#: What SQLite leaves beside a database it owns. A component that declared the
#: database has declared these; listing them as unclaimed would bury the one
#: entry that genuinely belongs to nobody under seven that obviously do not.
_SQLITE_SIDECARS = ("-shm", "-wal", ".lock", "-journal")


def _same_state(entry: str, declared: str) -> bool:
    """Whether a path on the Host is the state a component declared."""

    if entry == declared:
        return True
    for suffix in _SQLITE_SIDECARS:
        if entry == f"{declared}{suffix}":
            return True
    return Path(entry).is_relative_to(declared) or Path(declared).is_relative_to(entry)


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
    ) -> None:
        self.config = config
        self.runner = runner
        self.transport = transport or SSHTransport(config.host, runner)
        self.git = git
        self.progress = progress
        self.preflight = ReleasePreflight(config, runner, git=git)
        self.bundles = BundleTransfer(config, runner, self.transport)
        self.host_layer = HostLayer(
            config,
            self.transport,
            app,
            read_exact_source_file=self.preflight.read_exact_source_file,
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

    def local_preflight(self, *, require_install_files: bool) -> dict[str, object]:
        return self.preflight.run(require_install_files=require_install_files)

    # -- read-only -----------------------------------------------------------

    def status(self) -> dict[str, object]:
        self.preflight.validate_ssh_material()
        report = self.transport.run_agent("status", self.host_layer.target_payload())
        # Which link this ran over decides whether the next release takes two
        # seconds or three minutes, so the operator gets told rather than
        # having to infer it from how long they waited.
        return {**report, "endpoint": self.transport.describe()}

    def app_ready(self) -> dict[str, object]:
        self.preflight.validate_ssh_material()
        return self.transport.run_agent(
            "app-ready",
            self.host_layer.target_payload(),
            timeout=READINESS_TRANSPORT_TIMEOUT_SECONDS,
        )

    def doctor(self, *, release_id: str | None = None) -> dict[str, object]:
        local = self.preflight.run(require_install_files=False)
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
                    "sources": {
                        source_id: self.config.sources[source_id].revision
                        for source_id in SOURCE_IDS
                    },
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
                self.config,
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

    def commissioning_code(self, *, ttl_seconds: int) -> dict[str, object]:
        """Mint the one-time Setup code a phone types to claim this Host.

        SSH to the Host is what authorises this, the same way it authorises
        controller-reset: both reach a root-owned local socket, and issuing a
        code is the lesser of the two acts.
        """

        self.preflight.validate_ssh_material()
        return self.transport.run_agent(
            "commissioning-code",
            {**self.host_layer.target_payload(), "ttl_seconds": ttl_seconds},
            timeout=180,
        )

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
        ready = self.app_ready()
        return {
            **result,
            "plan": plan,
            "host_application": refreshed,
            "bootstrap_tombstone": consumed,
            "app": ready,
        }

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
