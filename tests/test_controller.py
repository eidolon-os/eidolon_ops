from __future__ import annotations

import hashlib
import json
import tarfile
from ipaddress import IPv4Address
from pathlib import Path

import pytest

from eidolon_ops.config import SOURCE_IDS, ConfigurationError
from eidolon_ops.controller import EidolonPiController, OperationsError
from eidolon_ops.paths import AppAccess
from eidolon_ops.process import ProcessResult
from eidolon_ops.release_matrix import SYSTEMD_ASSET_CONTRACTS

pytestmark = pytest.mark.component


class ControllerRunner:
    def __init__(
        self,
        config,
        *,
        wrong_revision: bool = False,
        invalid_release_matrix: bool = False,
        cli_revision_mismatch: bool = False,
        dirty_cli: bool = False,
        wrong_uv_version: bool = False,
    ) -> None:
        self.config = config
        self.wrong_revision = wrong_revision
        self.invalid_release_matrix = invalid_release_matrix
        self.cli_revision_mismatch = cli_revision_mismatch
        self.dirty_cli = dirty_cli
        self.wrong_uv_version = wrong_uv_version
        self.calls: list[tuple[str, ...]] = []

    def run(self, command, **kwargs):
        command = tuple(command)
        self.calls.append(command)
        if "rev-parse" in command:
            revision = (
                self.config.sources["eidolon_kernel"].revision
                if command[-1] == "HEAD"
                else command[-1].removesuffix("^{commit}")
            )
            if self.cli_revision_mismatch and command[-1] == "HEAD":
                revision = "e" * 40
            if self.wrong_revision and command[-1] != "HEAD":
                revision = "f" * 40
            return ProcessResult(0, revision + "\n", "")
        if "show" in command:
            path = command[-1].partition(":")[2]
            contract = next(item for item in SYSTEMD_ASSET_CONTRACTS if item.path == path)
            if self.invalid_release_matrix:
                return ProcessResult(
                    0,
                    "[Unit]\nDescription=test\n[Service]\n"
                    "ExecStart=/srv/eidolon/current/component/service\n",
                    "",
                )
            executable = (
                f"/opt/eidolon/current/{contract.component_root}/.venv/bin/service"
                if contract.component_root is not None
                else "/usr/local/bin/service"
            )
            return ProcessResult(
                0,
                "[Unit]\nDescription=test\n"
                "[Service]\nEnvironmentFile=/etc/eidolon/host.env\n"
                f"ExecStart={executable}\n",
                "",
            )
        if "status" in command and "--porcelain" in command:
            return ProcessResult(0, " M eidolon_deploy/cli.py\n" if self.dirty_cli else "", "")
        if command[-1:] == ("--version",) and command[0].endswith("/uv"):
            version = "uv 0.11.14" if self.wrong_uv_version else "uv 0.11.15"
            return ProcessResult(0, version + "\n", "")
        if len(command) > 1 and command[1] == "bundle":
            output = Path(command[3])
            output.mkdir(parents=True)
            sources = output / "sources"
            sources.mkdir()
            records = []
            for source_id in SOURCE_IDS:
                archive = sources / f"{source_id}.tar"
                archive.write_bytes(f"archive:{source_id}".encode())
                records.append(
                    {
                        "source_id": source_id,
                        "revision": self.config.sources[source_id].revision,
                        "archive": f"sources/{source_id}.tar",
                        "sha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
                    }
                )
            preparer = output / "prepare_target.py"
            preparer.write_bytes(b"preparer")
            dependencies = output / "python-dependencies.tar.gz"
            dependencies.write_bytes(b"arm64 dependency cache")
            (output / "bundle.json").write_text(
                json.dumps(
                    {
                        "schema_version": 2,
                        "release_id": command[2],
                        "target": {"system": "linux", "machine": "aarch64"},
                        "sources": records,
                        "preparer": {
                            "path": "prepare_target.py",
                            "sha256": hashlib.sha256(preparer.read_bytes()).hexdigest(),
                        },
                        "python_dependencies": {
                            "path": "python-dependencies.tar.gz",
                            "sha256": hashlib.sha256(dependencies.read_bytes()).hexdigest(),
                            "uv_version": "0.11.15",
                            "python_version": "3.13",
                            "platform": "aarch64-manylinux_2_40",
                            "build_requirements": [
                                "setuptools==80.9.0",
                                "wheel==0.45.1",
                                "hatchling==1.27.0",
                            ],
                            "index_url": "https://pypi.org/simple",
                        },
                    }
                ),
                encoding="utf-8",
            )
            return ProcessResult(
                0,
                json.dumps({"status": "bundled", "manifest": str(output / "bundle.json")}),
                "",
            )
        return ProcessResult(0, "{}", "")


class FakeTransport:
    def __init__(self) -> None:
        self.agent_calls: list[tuple[str, dict[str, object], str, bool]] = []
        self.remote_calls: list[tuple[tuple[str, ...], bool]] = []
        self.remote_timeouts: list[float] = []
        self.uploads: list[tuple[Path, str, bool]] = []
        self.resumable_uploads: list[tuple[Path, str]] = []
        self.fail_actions: dict[str, Exception] = {}
        self.fail_remote_match: str | None = None

    def run_agent(self, action, payload, *, python="/usr/bin/python3", sudo=True, timeout=120):
        self.agent_calls.append((action, dict(payload), python, sudo))
        if action in self.fail_actions:
            raise self.fail_actions[action]
        values = {
            "status": {"status": "observed"},
            "foundation-doctor": {"status": "healthy"},
            "foundation-install": {"status": "installed"},
            "app-ready": {"status": "app_ready"},
            "expansion-plan": {
                "status": "eligible",
                "source_release": "core-release",
            },
            "doctor-host": {"status": "healthy", "checks": {}},
            "guard-upload": {"status": "ready_for_upload"},
            "finalize-upload": {"status": "finalized"},
            "cleanup-stage": {"status": "cleaned"},
            "retire-legacy-root": {"status": "retired"},
            "abort-replacement": {"status": "aborted"},
            "reset-plan": {
                "status": "planned",
                "wipe_authority_data": payload.get("wipe_authority_data", False),
                "detected": ["/opt/eidolon"],
            },
            "reset-host": {
                "status": "reset",
                "wipe_authority_data": payload.get("wipe_authority_data", False),
                "removed": ["/opt/eidolon"],
            },
            "install": {"status": "installed"},
            "expand": {"status": "inputs_installed", "source_release": "core-release"},
            "start": {"status": "started"},
            "stop": {"status": "stopped"},
            "restart": {"status": "restarted"},
            "rollback-plan": {
                "status": "rollback_planned",
                "release_id": payload.get("release_id"),
                "snapshot": payload.get("snapshot"),
            },
            "logs": {"status": "collected", "entries": {}},
            "diagnose": {"status": "diagnosed", "redaction": "yes"},
        }
        return values[action]

    def run(self, remote, *, input_bytes=None, sudo=False, timeout=120, operation="remote"):
        remote = tuple(remote)
        self.remote_calls.append((remote, sudo))
        self.remote_timeouts.append(timeout)
        if self.fail_remote_match is not None and self.fail_remote_match in " ".join(remote):
            raise RuntimeError(f"failed: {self.fail_remote_match}")
        if remote == ("/bin/sh", "-s") and input_bytes is not None:
            if b'python3":true' in input_bytes:
                payload = {"python3": True}
            else:
                payload = {"status": "bootstrapped", "python3": True}
        elif "prepare_target.py" in " ".join(remote):
            payload = {"status": "prepared"}
        elif "--dry-run" in remote:
            payload = {"status": "dry_run", "previous_targets": {}}
        elif "doctor" in remote:
            payload = {"status": "healthy"}
        elif "rollback" in remote:
            payload = {"status": "restored"}
        elif "deploy" in remote:
            payload = {"status": "activated", "transaction_id": "a" * 32}
        else:
            payload = {"status": "ok"}
        return ProcessResult(0, json.dumps(payload), "")

    def upload(self, source, destination, *, recursive=False):
        self.uploads.append((Path(source), destination, recursive))

    def upload_directory_resumable(self, source, destination):
        self.resumable_uploads.append((Path(source), destination))


def _stub_input_contract(controller: EidolonPiController) -> None:
    controller._validate_install_inputs = lambda _names: {  # type: ignore[method-assign]
        "status": "compatible"
    }


def _app() -> AppAccess:
    return AppAccess(
        lan_ipv4=IPv4Address("192.168.100.15"),
        hub_https_port=8443,
        livekit_client_url="ws://192.168.100.15:7880",
        allow_insecure_livekit=True,
    )


@pytest.fixture
def setup_controller(config):
    runner = ControllerRunner(config)
    transport = FakeTransport()
    controller = EidolonPiController(config, runner, transport=transport)
    _stub_input_contract(controller)
    return controller, runner, transport


def test_local_preflight_proves_exact_commits(config) -> None:
    controller = EidolonPiController(config, ControllerRunner(config), transport=FakeTransport())
    _stub_input_contract(controller)

    result = controller.local_preflight(require_install_files=True)

    assert result["sources"] == {
        source_id: config.sources[source_id].revision for source_id in SOURCE_IDS
    }
    assert result["install_prerequisites_checked"] is True
    assert result["release_cli_revision"] == config.sources["eidolon_kernel"].revision
    assert result["install_input_contract"] == {"status": "compatible"}
    assert result["python_resolver"] == {
        "index_url": "https://pypi.org/simple",
        "http_timeout_seconds": 120,
        "http_retries": 8,
        "concurrent_downloads": 4,
        "locked": True,
    }


def test_local_preflight_rejects_revision_alias(config) -> None:
    controller = EidolonPiController(
        config,
        ControllerRunner(config, wrong_revision=True),
        transport=FakeTransport(),
    )

    with pytest.raises(OperationsError, match="exact commit"):
        controller.local_preflight(require_install_files=False)


def test_local_preflight_rejects_mismatched_release_authority(config) -> None:
    controller = EidolonPiController(
        config,
        ControllerRunner(config, cli_revision_mismatch=True),
        transport=FakeTransport(),
    )

    with pytest.raises(OperationsError, match="pinned Kernel"):
        controller.local_preflight(require_install_files=False)


def test_local_preflight_rejects_dirty_release_authority(config) -> None:
    controller = EidolonPiController(
        config,
        ControllerRunner(config, dirty_cli=True),
        transport=FakeTransport(),
    )

    with pytest.raises(OperationsError, match="tracked changes"):
        controller.local_preflight(require_install_files=False)


def test_local_preflight_requires_exact_bundle_uv(config) -> None:
    controller = EidolonPiController(
        config,
        ControllerRunner(config, wrong_uv_version=True),
        transport=FakeTransport(),
    )
    with pytest.raises(OperationsError, match=r"must be 0\.11\.15"):
        controller.local_preflight(require_install_files=False)

    config.workspace.release_cli.with_name("uv").unlink()
    with pytest.raises(OperationsError, match="local uv executable is missing"):
        controller.local_preflight(require_install_files=False)


def test_local_preflight_rejects_public_identity(config) -> None:
    config.host.identity_file.chmod(0o644)
    controller = EidolonPiController(config, ControllerRunner(config), transport=FakeTransport())

    with pytest.raises(ConfigurationError, match="group/world"):
        controller.status()


def test_status_is_read_only(setup_controller) -> None:
    controller, _runner, transport = setup_controller

    assert controller.status()["status"] == "observed"
    assert [call[0] for call in transport.agent_calls] == ["status"]
    assert transport.remote_calls == []


def test_app_ready_is_read_only_and_bounded(setup_controller) -> None:
    controller, _runner, transport = setup_controller

    assert controller.app_ready()["status"] == "app_ready"
    assert [call[0] for call in transport.agent_calls] == ["app-ready"]


def test_doctor_combines_local_and_remote(setup_controller) -> None:
    controller, _runner, transport = setup_controller

    result = controller.doctor(release_id="r1")

    assert result["status"] == "healthy"
    assert transport.agent_calls[-1][1]["release_id"] == "r1"
    assert [call[0] for call in transport.agent_calls[:2]] == [
        "foundation-doctor",
        "doctor-host",
    ]


def test_provision_is_read_only_by_default(setup_controller) -> None:
    controller, _runner, transport = setup_controller

    result = controller.provision(apply=False)

    assert result["status"] == "healthy"
    assert [call[0] for call in transport.agent_calls] == ["foundation-doctor"]


def test_provision_installs_degraded_foundation(config) -> None:
    transport = FakeTransport()
    original = transport.run_agent

    def degraded(action, payload, **kwargs):
        if action == "foundation-doctor":
            transport.agent_calls.append(
                (action, dict(payload), kwargs.get("python", "/usr/bin/python3"), True)
            )
            return {"status": "degraded"}
        return original(action, payload, **kwargs)

    transport.run_agent = degraded
    controller = EidolonPiController(config, ControllerRunner(config), transport=transport)

    result = controller.provision(apply=True)

    assert result["status"] == "installed"
    assert [call[0] for call in transport.agent_calls] == [
        "foundation-doctor",
        "foundation-install",
    ]


def test_provision_plans_and_bootstraps_missing_python(config) -> None:
    class MissingPythonTransport(FakeTransport):
        def __init__(self) -> None:
            super().__init__()
            self.shell_calls = 0

        def run(self, remote, **kwargs):
            if tuple(remote) == ("/bin/sh", "-s"):
                self.shell_calls += 1
                payload = (
                    {"python3": False}
                    if self.shell_calls == 1
                    else {"status": "bootstrapped", "python3": True}
                )
                self.remote_calls.append((tuple(remote), bool(kwargs.get("sudo", False))))
                return ProcessResult(0, json.dumps(payload), "")
            return super().run(remote, **kwargs)

    planned_transport = MissingPythonTransport()
    planned = EidolonPiController(
        config,
        ControllerRunner(config),
        transport=planned_transport,
    ).provision(apply=False)
    assert planned["status"] == "planned_bootstrap"
    assert planned_transport.agent_calls == []

    applied_transport = MissingPythonTransport()
    applied = EidolonPiController(
        config,
        ControllerRunner(config),
        transport=applied_transport,
    ).provision(apply=True)
    assert applied["status"] == "healthy"
    assert applied_transport.remote_calls[-1][1] is True
    assert [phase["phase"] for phase in applied["phases"]] == [
        "python_probe",
        "python_bootstrap",
        "doctor",
    ]


def test_provision_reports_degraded_without_apply(config) -> None:
    transport = FakeTransport()

    def degraded(action, payload, **kwargs):
        transport.agent_calls.append(
            (action, dict(payload), kwargs.get("python", "/usr/bin/python3"), True)
        )
        return {"status": "degraded"}

    transport.run_agent = degraded
    controller = EidolonPiController(config, ControllerRunner(config), transport=transport)

    result = controller.provision(apply=False)

    assert result["status"] == "degraded"
    assert "provision --apply" in result["next"]


def test_doctor_reports_degraded(config) -> None:
    transport = FakeTransport()
    original = transport.run_agent

    def degraded(action, payload, **kwargs):
        if action == "doctor-host":
            return {"status": "degraded"}
        return original(action, payload, **kwargs)

    transport.run_agent = degraded
    controller = EidolonPiController(config, ControllerRunner(config), transport=transport)

    assert controller.doctor()["status"] == "degraded"


def test_deploy_defaults_to_prepare_and_dry_run(setup_controller) -> None:
    controller, _runner, transport = setup_controller

    result = controller.deploy(release_id="r1", resume=False, activate=False)

    assert result["status"] == "dry_run"
    assert [phase["phase"] for phase in result["phases"]] == [
        "bundle",
        "upload_guard",
        "upload_finalize",
        "prepare",
        "dry_run",
    ]
    assert transport.resumable_uploads[0][1] == "/var/tmp/eidolon-release-r1"
    assert transport.resumable_uploads[0][0].name == "r1"
    prepare_command = next(
        remote
        for remote, _sudo in transport.remote_calls
        if any(token.endswith("/prepare_target.py") for token in remote)
    )
    assert prepare_command[:6] == (
        "/usr/bin/env",
        "UV_DEFAULT_INDEX=https://pypi.org/simple",
        "UV_HTTP_TIMEOUT=120",
        "UV_HTTP_RETRIES=8",
        "UV_CONCURRENT_DOWNLOADS=4",
        "/usr/bin/python3",
    )
    prepare_index = next(
        index
        for index, (remote, _sudo) in enumerate(transport.remote_calls)
        if any(token.endswith("/prepare_target.py") for token in remote)
    )
    assert transport.remote_timeouts[prepare_index] == 3600


def test_deploy_resume_activate_skips_transfer(setup_controller) -> None:
    controller, runner, transport = setup_controller

    result = controller.deploy(release_id="r1", resume=True, activate=True)

    assert result["status"] == "activated"
    assert [phase["phase"] for phase in result["phases"]] == [
        "dry_run",
        "activate",
        "doctor",
        "app_ready",
    ]
    assert transport.uploads == []
    assert not any(len(call) > 1 and call[1] == "bundle" for call in runner.calls)


def test_deploy_resume_revalidates_and_resumes_existing_bundle(setup_controller) -> None:
    controller, runner, transport = setup_controller
    controller.deploy(release_id="r1", resume=False, activate=False)
    runner.calls.clear()
    transport.resumable_uploads.clear()

    result = controller.deploy(release_id="r1", resume=True, activate=False)

    assert result["phases"][0]["result"]["status"] == "reused_validated_bundle"
    assert transport.resumable_uploads == [
        (controller.config.workspace.bundle_root / "r1", "/var/tmp/eidolon-release-r1")
    ]
    assert not any(len(call) > 1 and call[1] == "bundle" for call in runner.calls)


def test_deploy_skips_transfer_for_finalized_remote_bundle(setup_controller) -> None:
    controller, _runner, transport = setup_controller
    original = transport.run_agent

    def finalized_guard(action, payload, **kwargs):
        if action == "guard-upload":
            transport.agent_calls.append(
                (action, dict(payload), kwargs.get("python", "/usr/bin/python3"), True)
            )
            return {"status": "ready_for_prepare"}
        return original(action, payload, **kwargs)

    transport.run_agent = finalized_guard

    result = controller.deploy(release_id="r1", resume=False, activate=False)

    assert result["status"] == "dry_run"
    assert transport.resumable_uploads == []
    assert [call[0] for call in transport.agent_calls].count("finalize-upload") == 1


def test_deploy_resume_rejects_existing_bundle_digest_drift(setup_controller) -> None:
    controller, _runner, _transport = setup_controller
    controller.deploy(release_id="r1", resume=False, activate=False)
    archive = controller.config.workspace.bundle_root / "r1/sources/eidolon_channel.tar"
    archive.write_bytes(b"drift")

    with pytest.raises(OperationsError, match="source digest drifted"):
        controller.deploy(release_id="r1", resume=True, activate=False)


def test_deploy_resume_rejects_unreadable_existing_manifest(setup_controller) -> None:
    controller, _runner, _transport = setup_controller
    controller.deploy(release_id="r1", resume=False, activate=False)
    manifest = controller.config.workspace.bundle_root / "r1/bundle.json"
    manifest.write_text("not-json", encoding="utf-8")

    with pytest.raises(OperationsError, match="manifest is unreadable"):
        controller.deploy(release_id="r1", resume=True, activate=False)


def test_deploy_resume_rejects_existing_bundle_identity_drift(setup_controller) -> None:
    controller, _runner, _transport = setup_controller
    controller.deploy(release_id="r1", resume=False, activate=False)
    manifest = controller.config.workspace.bundle_root / "r1/bundle.json"
    document = json.loads(manifest.read_text(encoding="utf-8"))
    document["release_id"] = "different"
    manifest.write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(OperationsError, match="identity or source set"):
        controller.deploy(release_id="r1", resume=True, activate=False)


def test_deploy_app_gate_failure_restores_exact_activation_snapshot(config) -> None:
    transport = FakeTransport()
    original = transport.run_agent

    def degraded(action, payload, **kwargs):
        if action == "app-ready":
            transport.agent_calls.append(
                (action, dict(payload), kwargs.get("python", "/usr/bin/python3"), True)
            )
            return {"status": "degraded"}
        return original(action, payload, **kwargs)

    transport.run_agent = degraded
    controller = EidolonPiController(config, ControllerRunner(config), transport=transport)

    with pytest.raises(OperationsError, match="snapshot was restored"):
        controller.deploy(release_id="r1", resume=True, activate=True)

    rollback = next(call for call, _sudo in transport.remote_calls if "rollback" in call)
    assert rollback[-1] == "/var/lib/eidolon/deployments/r1-" + "a" * 32


def test_deploy_doctor_failure_restores_exact_activation_snapshot(config) -> None:
    transport = FakeTransport()
    original = transport.run

    def degraded(remote, **kwargs):
        if "doctor" in remote:
            return ProcessResult(0, json.dumps({"status": "degraded"}), "")
        return original(remote, **kwargs)

    transport.run = degraded
    controller = EidolonPiController(config, ControllerRunner(config), transport=transport)

    with pytest.raises(OperationsError, match="snapshot was restored"):
        controller.deploy(release_id="r1", resume=True, activate=True)

    assert any("rollback" in call for call, _sudo in transport.remote_calls)
    assert not any(call[0] == "app-ready" for call in transport.agent_calls)


def test_deploy_reports_health_gate_and_rollback_failure(config) -> None:
    transport = FakeTransport()
    original = transport.run_agent

    def degraded(action, payload, **kwargs):
        if action == "app-ready":
            return {"status": "degraded"}
        return original(action, payload, **kwargs)

    transport.run_agent = degraded
    transport.fail_remote_match = "rollback"
    controller = EidolonPiController(config, ControllerRunner(config), transport=transport)

    with pytest.raises(OperationsError, match=r"health gate failed.*rollback failed"):
        controller.deploy(release_id="r1", resume=True, activate=True)


def test_deploy_rejects_invalid_activation_evidence_without_guessing_snapshot(config) -> None:
    transport = FakeTransport()
    original = transport.run

    def invalid(remote, **kwargs):
        if "deploy" in remote and "--dry-run" not in remote:
            return ProcessResult(
                0,
                json.dumps({"status": "activated", "transaction_id": "not-a-transaction"}),
                "",
            )
        return original(remote, **kwargs)

    transport.run = invalid
    controller = EidolonPiController(config, ControllerRunner(config), transport=transport)

    with pytest.raises(OperationsError, match="invalid transaction evidence"):
        controller.deploy(release_id="r1", resume=True, activate=True)

    assert not any("rollback" in call for call, _sudo in transport.remote_calls)


def test_deploy_rejects_invalid_rollback_evidence(config) -> None:
    transport = FakeTransport()
    original_agent = transport.run_agent
    original_remote = transport.run

    def degraded(action, payload, **kwargs):
        if action == "app-ready":
            return {"status": "degraded"}
        return original_agent(action, payload, **kwargs)

    def invalid_recovery(remote, **kwargs):
        if "rollback" in remote:
            return ProcessResult(0, json.dumps({"status": "unknown"}), "")
        return original_remote(remote, **kwargs)

    transport.run_agent = degraded
    transport.run = invalid_recovery
    controller = EidolonPiController(config, ControllerRunner(config), transport=transport)

    with pytest.raises(OperationsError, match="invalid recovery evidence"):
        controller.deploy(release_id="r1", resume=True, activate=True)


def test_deploy_refuses_existing_local_bundle(config) -> None:
    bundle = config.workspace.bundle_root / "r1"
    bundle.mkdir(parents=True)
    controller = EidolonPiController(config, ControllerRunner(config), transport=FakeTransport())

    with pytest.raises(OperationsError, match="already exists"):
        controller.deploy(release_id="r1", resume=False, activate=False)


def test_deploy_stops_after_prepare_failure(setup_controller) -> None:
    controller, _runner, transport = setup_controller
    transport.fail_remote_match = "prepare_target.py"

    with pytest.raises(RuntimeError, match="prepare_target"):
        controller.deploy(release_id="r1", resume=False, activate=True)

    assert not any(" deploy " in f" {' '.join(call[0])} " for call in transport.remote_calls)


def test_install_without_apply_is_read_only(setup_controller) -> None:
    controller, _runner, transport = setup_controller

    result = controller.install(release_id="r1", resume=False, apply=False)

    assert result["status"] == "planned"
    assert transport.uploads == []
    assert [call[0] for call in transport.agent_calls] == ["foundation-doctor"]


def test_input_initialization_is_local_and_exact_revision_pinned(setup_controller) -> None:
    controller, runner, transport = setup_controller

    result = controller.initialize_inputs()

    assert result["status"] == "already_initialized"
    assert transport.agent_calls == []
    settings_checks = [call for call in runner.calls if "rev-parse" in call]
    assert len(settings_checks) == 3


def test_install_apply_stages_exact_files_and_cleans(setup_controller) -> None:
    controller, _runner, transport = setup_controller

    result = controller.install(release_id="r1", resume=False, apply=True)

    assert result["status"] == "installed"
    destinations = [item[1] for item in transport.uploads if "eidolon-secrets" in item[1]]
    assert destinations == [
        "/var/tmp/eidolon-secrets-r1/data.env",
        "/var/tmp/eidolon-secrets-r1/hub.env",
        "/var/tmp/eidolon-secrets-r1/kernel.env",
        "/var/tmp/eidolon-secrets-r1/admin.env",
        "/var/tmp/eidolon-secrets-r1/local-api.env",
        "/var/tmp/eidolon-secrets-r1/bootstrap.env",
        "/var/tmp/eidolon-secrets-r1/host_identity.ed25519",
        "/var/tmp/eidolon-secrets-r1/agent.env",
        "/var/tmp/eidolon-secrets-r1/channel.env",
        "/var/tmp/eidolon-secrets-r1/memory.env",
        "/var/tmp/eidolon-secrets-r1/livekit.env",
        "/var/tmp/eidolon-secrets-r1/agent.yaml",
        "/var/tmp/eidolon-secrets-r1/channel.yaml",
        "/var/tmp/eidolon-secrets-r1/memory.yaml",
    ]
    actions = [call[0] for call in transport.agent_calls]
    assert actions[-2:] == ["install", "cleanup-stage"]
    assert actions[0] == "foundation-doctor"


def test_unified_pi_stage_renders_host_bound_application_assets(config) -> None:
    identity = config.install_files["host_identity"]
    identity.write_bytes(b"a" * 32)
    identity.chmod(0o600)
    config.install_files["local_api_env"].write_text(
        "EIDOLON_LOCAL_API_ADMIN_BASE_URL=http://127.0.0.1:9000\n"
        "EIDOLON_LOCAL_API_ADMIN_SERVICE_TOKEN=test-token\n",
        encoding="utf-8",
    )
    config.install_files["channel_env"].write_text(
        "EIDOLON_LIVEKIT_CLIENT_URL=ws://127.0.0.1:7880\nPAIRING_JWT_SECRET=test-token\n",
        encoding="utf-8",
    )

    class Runner(ControllerRunner):
        def run(self, command, **kwargs):
            if "show" in command and command[-1].endswith("config/hub.systemd.example.yaml"):
                return ProcessResult(
                    0,
                    "onboarding:\n"
                    "  hub_id: eidolon-hub-local\n"
                    "  public_base_url: https://eidolon-hub.local\n"
                    "discovery:\n  mdns:\n    enabled: true\n"
                    "channel_provider:\n  contract_url: http://127.0.0.1:8767/v1\n"
                    "persistence:\n  path: /var/lib/eidolon/eidolon-hub.sqlite3\n",
                    "",
                )
            return super().run(command, **kwargs)

    class CapturingTransport(FakeTransport):
        def __init__(self) -> None:
            super().__init__()
            self.uploaded_bytes: dict[str, bytes] = {}

        def upload(self, source, destination, *, recursive=False):
            super().upload(source, destination, recursive=recursive)
            self.uploaded_bytes[destination] = Path(source).read_bytes()

    transport = CapturingTransport()
    controller = EidolonPiController(config, Runner(config), transport=transport, app=_app())
    stage = "/var/tmp/eidolon-secrets-host-bound"

    controller._stage_install_files("host-bound", stage)
    payload = controller._target_payload()

    assert len(transport.uploaded_bytes) == len(config.install_files) + 6
    app = payload["app"]
    assert isinstance(app, dict)
    assert app["hub_id"] != "eidolon-hub-local"
    assert str(app["hub_hostname"]).endswith(".local")
    assert str(app["hub_origin"]).endswith(":8443")
    rendered_local = transport.uploaded_bytes[f"{stage}/local-api.env"].decode()
    rendered_hub = transport.uploaded_bytes[f"{stage}/hub.generated.yaml"].decode()
    assert f"EIDOLON_LOCAL_API_HUB_ID={app['hub_id']}" in rendered_local
    assert f"hub_id: {app['hub_id']}" in rendered_hub
    assert f"public_base_url: {app['hub_origin']}" in rendered_hub
    assert b"--listen-port 8443" in transport.uploaded_bytes[f"{stage}/hub-ingress.service"]


def test_install_can_reset_and_wipe_existing_host_before_provision(setup_controller) -> None:
    controller, _runner, transport = setup_controller

    result = controller.install(
        release_id="r1",
        resume=False,
        apply=True,
        reset_existing=True,
        wipe_authority_data=True,
    )

    actions = [call[0] for call in transport.agent_calls]
    assert actions[:3] == [
        "reset-plan",
        "reset-host",
        "foundation-doctor",
    ]
    assert result["phases"][0]["phase"] == "reset_existing"


def test_install_release_matrix_failure_happens_before_destructive_reset(config) -> None:
    transport = FakeTransport()
    controller = EidolonPiController(
        config,
        ControllerRunner(config, invalid_release_matrix=True),
        transport=transport,
    )

    with pytest.raises(OperationsError, match="release systemd matrix is incompatible"):
        controller.install(
            release_id="r1",
            resume=False,
            apply=True,
            reset_existing=True,
            wipe_authority_data=True,
        )

    assert transport.agent_calls == []


@pytest.mark.parametrize(
    ("reset_existing", "wipe_authority_data"),
    [(True, False), (False, True)],
)
def test_install_refuses_ambiguous_destructive_flags(
    setup_controller, reset_existing: bool, wipe_authority_data: bool
) -> None:
    controller, _runner, transport = setup_controller

    with pytest.raises(OperationsError, match="requires"):
        controller.install(
            release_id="r1",
            resume=False,
            apply=False,
            reset_existing=reset_existing,
            wipe_authority_data=wipe_authority_data,
        )
    assert transport.agent_calls == []


def test_reset_defaults_to_read_only_and_requires_apply_for_mutation(setup_controller) -> None:
    controller, _runner, transport = setup_controller

    plan = controller.reset(wipe_authority_data=False, apply=False)

    assert plan["status"] == "planned"
    assert [call[0] for call in transport.agent_calls] == ["reset-plan"]

    result = controller.reset(wipe_authority_data=True, apply=True)
    assert result["status"] == "reset"
    assert [call[0] for call in transport.agent_calls][-2:] == [
        "reset-plan",
        "reset-host",
    ]


def test_install_failure_still_cleans_secret_stage(setup_controller) -> None:
    controller, _runner, transport = setup_controller
    transport.fail_actions["install"] = RuntimeError("install failure")

    with pytest.raises(RuntimeError, match="install failure"):
        controller.install(release_id="r1", resume=True, apply=True)

    assert [call[0] for call in transport.agent_calls][-1] == "cleanup-stage"


def test_install_and_cleanup_failure_reports_both(setup_controller) -> None:
    controller, _runner, transport = setup_controller
    transport.fail_actions["install"] = RuntimeError("install failure")
    original = transport.run_agent
    cleanup_calls = 0

    def fail_second_cleanup(action, payload, **kwargs):
        nonlocal cleanup_calls
        if action == "cleanup-stage":
            cleanup_calls += 1
            if cleanup_calls == 2:
                raise RuntimeError("cleanup failure")
        return original(action, payload, **kwargs)

    transport.run_agent = fail_second_cleanup

    with pytest.raises(OperationsError, match="cleanup also failed"):
        controller.install(release_id="r1", resume=True, apply=True)


def test_expand_without_apply_is_read_only(setup_controller) -> None:
    controller, _runner, transport = setup_controller

    result = controller.expand(release_id="r1", resume=False, apply=False)

    assert result["status"] == "planned"
    assert result["topology"]["source_release"] == "core-release"
    assert transport.uploads == []
    assert [call[0] for call in transport.agent_calls] == [
        "expansion-plan",
        "foundation-doctor",
    ]


def test_expand_apply_stages_only_new_inputs_then_activates(setup_controller) -> None:
    controller, _runner, transport = setup_controller

    result = controller.expand(release_id="r1", resume=False, apply=True)

    assert result["status"] == "expanded"
    destinations = [item[1] for item in transport.uploads if "eidolon-secrets" in item[1]]
    assert destinations == [
        "/var/tmp/eidolon-secrets-r1/agent.env",
        "/var/tmp/eidolon-secrets-r1/channel.env",
        "/var/tmp/eidolon-secrets-r1/memory.env",
        "/var/tmp/eidolon-secrets-r1/livekit.env",
        "/var/tmp/eidolon-secrets-r1/agent.yaml",
        "/var/tmp/eidolon-secrets-r1/channel.yaml",
        "/var/tmp/eidolon-secrets-r1/memory.yaml",
    ]
    assert [phase["phase"] for phase in result["phases"]] == [
        "bundle",
        "upload_guard",
        "upload_finalize",
        "prepare",
        "expansion_inputs",
        "secret_cleanup",
        "dry_run",
        "activate",
        "doctor",
        "app_ready",
    ]


def test_expand_retires_legacy_root_only_after_all_health_gates(config) -> None:
    transport = FakeTransport()
    original = transport.run_agent

    def legacy(action, payload, **kwargs):
        if action == "expansion-plan":
            transport.agent_calls.append(
                (action, dict(payload), kwargs.get("python", "/usr/bin/python3"), True)
            )
            return {
                "status": "eligible",
                "source_release": "legacy-core",
                "source_topology": "legacy_srv_core",
            }
        return original(action, payload, **kwargs)

    transport.run_agent = legacy
    controller = EidolonPiController(config, ControllerRunner(config), transport=transport)
    _stub_input_contract(controller)

    result = controller.expand(release_id="r1", resume=True, apply=True)

    assert result["phases"][-1]["phase"] == "legacy_root_retirement"
    actions = [call[0] for call in transport.agent_calls]
    assert actions.index("retire-legacy-root") > actions.index("app-ready")


def test_legacy_replacement_failure_aborts_unused_bridge(config) -> None:
    transport = FakeTransport()
    original = transport.run_agent

    def legacy(action, payload, **kwargs):
        if action == "expansion-plan":
            transport.agent_calls.append(
                (action, dict(payload), kwargs.get("python", "/usr/bin/python3"), True)
            )
            return {
                "status": "eligible",
                "source_release": "legacy-core",
                "source_topology": "legacy_srv_core",
            }
        return original(action, payload, **kwargs)

    transport.run_agent = legacy
    transport.fail_remote_match = " deploy "
    controller = EidolonPiController(config, ControllerRunner(config), transport=transport)
    _stub_input_contract(controller)

    with pytest.raises(OperationsError, match="bridge links were removed"):
        controller.expand(release_id="r1", resume=True, apply=True)

    actions = [call[0] for call in transport.agent_calls]
    assert "abort-replacement" in actions
    assert "retire-legacy-root" not in actions


def test_interrupted_legacy_cleanup_rechecks_gates_and_retires(config) -> None:
    transport = FakeTransport()
    original = transport.run_agent

    def cleanup_pending(action, payload, **kwargs):
        if action == "expansion-plan":
            transport.agent_calls.append(
                (action, dict(payload), kwargs.get("python", "/usr/bin/python3"), True)
            )
            return {
                "status": "replacement_cleanup_pending",
                "source_release": "r1",
                "source_topology": "opt",
            }
        return original(action, payload, **kwargs)

    transport.run_agent = cleanup_pending
    controller = EidolonPiController(config, ControllerRunner(config), transport=transport)

    result = controller.expand(release_id="r1", resume=True, apply=True)

    assert result["status"] == "expanded"
    assert [phase["phase"] for phase in result["phases"]] == [
        "doctor",
        "app_ready",
        "legacy_root_retirement",
    ]
    assert transport.uploads == []


def test_expand_refuses_topology_conflict_before_foundation_mutation(config) -> None:
    transport = FakeTransport()
    original = transport.run_agent

    def conflict(action, payload, **kwargs):
        if action == "expansion-plan":
            transport.agent_calls.append(
                (action, dict(payload), kwargs.get("python", "/usr/bin/python3"), True)
            )
            return {"status": "conflict", "reason": "partial links"}
        return original(action, payload, **kwargs)

    transport.run_agent = conflict
    controller = EidolonPiController(config, ControllerRunner(config), transport=transport)

    with pytest.raises(OperationsError, match="partial links"):
        controller.expand(release_id="r1", resume=False, apply=True)

    assert [call[0] for call in transport.agent_calls] == ["expansion-plan"]


def test_expand_refuses_an_already_full_host_before_foundation_mutation(config) -> None:
    transport = FakeTransport()
    original = transport.run_agent

    def already_full(action, payload, **kwargs):
        if action == "expansion-plan":
            transport.agent_calls.append(
                (action, dict(payload), kwargs.get("python", "/usr/bin/python3"), True)
            )
            return {"status": "already_full", "source_release": "r0"}
        return original(action, payload, **kwargs)

    transport.run_agent = already_full
    controller = EidolonPiController(config, ControllerRunner(config), transport=transport)

    with pytest.raises(OperationsError, match="already full"):
        controller.expand(release_id="r1", resume=False, apply=True)

    assert [call[0] for call in transport.agent_calls] == ["expansion-plan"]


def test_expand_completed_transaction_rechecks_doctor_and_app(config) -> None:
    transport = FakeTransport()
    original = transport.run_agent

    def completed(action, payload, **kwargs):
        if action == "expand":
            return {"status": "already_expanded", "source_release": "r0"}
        return original(action, payload, **kwargs)

    transport.run_agent = completed
    controller = EidolonPiController(config, ControllerRunner(config), transport=transport)
    _stub_input_contract(controller)

    result = controller.expand(release_id="r1", resume=True, apply=True)

    assert result["status"] == "already_expanded"
    assert [phase["phase"] for phase in result["phases"]] == [
        "expansion_inputs",
        "secret_cleanup",
        "doctor",
        "app_ready",
    ]


@pytest.mark.parametrize("action", ["start", "stop", "restart"])
def test_lifecycle_dry_run_has_no_remote_mutation(setup_controller, action: str) -> None:
    controller, _runner, transport = setup_controller

    result = controller.lifecycle(action, dry_run=True)

    assert result["status"] == "planned"
    assert transport.agent_calls == []


@pytest.mark.parametrize("action", ["start", "stop", "restart"])
def test_lifecycle_uses_active_kernel_runtime(setup_controller, action: str) -> None:
    controller, _runner, transport = setup_controller

    result = controller.lifecycle(action, dry_run=False)

    assert result["status"] == (action + "ed" if action != "stop" else "stopped")
    assert transport.agent_calls[-1][2] == ("/opt/eidolon/current/eidolon_kernel/.venv/bin/python")


def test_rollback_defaults_to_plan(setup_controller) -> None:
    controller, _runner, transport = setup_controller
    snapshot = Path("/var/lib/eidolon/deployments/r1-transaction")

    result = controller.rollback(release_id="r1", snapshot=snapshot, apply=False)

    assert result["status"] == "rollback_planned"
    assert transport.remote_calls == []


def test_rollback_apply_invokes_existing_release_cli(setup_controller) -> None:
    controller, _runner, transport = setup_controller
    snapshot = Path("/var/lib/eidolon/deployments/r1-transaction")

    result = controller.rollback(release_id="r1", snapshot=snapshot, apply=True)

    assert result["status"] == "restored"
    assert "rollback" in transport.remote_calls[-1][0]


def test_rollback_rejects_outside_snapshot(setup_controller) -> None:
    controller, _runner, _transport = setup_controller

    with pytest.raises(OperationsError, match="direct child"):
        controller.rollback(release_id="r1", snapshot=Path("/tmp/bad"), apply=False)


def test_logs_preserve_bounds_for_target_validation(setup_controller) -> None:
    controller, _runner, transport = setup_controller

    result = controller.logs(unit="eidolond.service", lines=50, since="1 hour ago")

    assert result["status"] == "collected"
    assert transport.agent_calls[-1][1]["lines"] == 50


def test_diagnose_creates_redacted_archive(setup_controller, tmp_path: Path) -> None:
    controller, _runner, _transport = setup_controller
    output = tmp_path / "diagnostic.tar.gz"

    result = controller.diagnose(output=output)

    assert result["redacted"] is True
    with tarfile.open(output, "r:gz") as archive:
        assert sorted(archive.getnames()) == ["release-inputs.json", "target.json"]
        inputs = json.load(archive.extractfile("release-inputs.json"))
    assert "install_files" not in inputs
    assert "identity_file" not in inputs


def test_diagnose_refuses_existing_output(setup_controller, tmp_path: Path) -> None:
    controller, _runner, _transport = setup_controller
    output = tmp_path / "diagnostic.tar.gz"
    output.write_bytes(b"existing")

    with pytest.raises(OperationsError, match="already exists"):
        controller.diagnose(output=output)
