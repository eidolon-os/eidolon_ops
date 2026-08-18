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

from eidolon_ops.config import SOURCE_IDS, OperationsConfig, validate_release_id
from eidolon_ops.errors import OperationsError
from eidolon_ops.foundation import (
    FOUNDATION_PROFILE,
    foundation_payload,
    python_bootstrap_script,
    python_probe_script,
)
from eidolon_ops.host_layer import ASSET_ERRORS, HostLayer
from eidolon_ops.install_inputs import initialize_install_inputs
from eidolon_ops.paths import AppAccess
from eidolon_ops.process import ProcessRunner
from eidolon_ops.readiness import READINESS_TRANSPORT_TIMEOUT_SECONDS
from eidolon_ops.release_bundle import BundleTransfer, parse_json
from eidolon_ops.release_preflight import ReleasePreflight
from eidolon_ops.release_transaction import ReleaseTransaction
from eidolon_ops.transport import SSHTransport

__all__ = ["EidolonPiController", "OperationsError"]

#: Sources whose settings templates the private input set is derived from.
_SETTINGS_SOURCES = ("eidolon_agent", "eidolon_channel", "eidolon_memory")
_LIFECYCLE_ACTIONS = frozenset({"start", "stop", "restart"})


class EidolonPiController:
    def __init__(
        self,
        config: OperationsConfig,
        runner: ProcessRunner,
        *,
        transport: SSHTransport | None = None,
        git: str = "git",
        app: AppAccess | None = None,
    ) -> None:
        self.config = config
        self.runner = runner
        self.transport = transport or SSHTransport(config.host, runner)
        self.git = git
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
            provision=self.provision,
            reset=self.reset,
            app_ready=self.app_ready,
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

    def provision(self, *, apply: bool) -> dict[str, object]:
        """Detect or install the pinned non-Eidolon Raspberry Pi foundation."""

        self.preflight.validate_ssh_material()
        self.preflight.require_commands(("ssh", "scp"))
        python_available = self._remote_python_available()
        phases: list[dict[str, object]] = [{"phase": "python_probe", "available": python_available}]
        if not python_available:
            if not apply:
                return {
                    "status": "planned_bootstrap",
                    "profile": FOUNDATION_PROFILE,
                    "phases": phases,
                    "next": "rerun provision --apply to bootstrap Python and the pinned foundation",
                }
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
        plan = self.transport.run_agent("reset-plan", payload, timeout=180)
        if plan.get("status") != "planned":
            raise OperationsError("Host reset plan returned invalid evidence")
        if not apply:
            return {**plan, "next": "rerun reset --apply after reviewing the detected paths"}
        result = self.transport.run_agent("reset-host", payload, timeout=600)
        if result.get("status") != "reset":
            raise OperationsError("Host reset returned invalid evidence")
        return result

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
