from __future__ import annotations

import hashlib
import json
import tarfile
from ipaddress import IPv4Address
from pathlib import Path

import pytest

# The overlay still rewrites specific literals in three components' settings,
# so refreshing the derived inputs must be exercised against text that actually
# contains them. Shared with the install-inputs suite.
from test_install_inputs import _settings_reader as _product_settings_reader

from eidolon_ops.config import SOURCE_IDS, ConfigurationError
from eidolon_ops.controller import EidolonPiController, OperationsError
from eidolon_ops.embedding_model import PINNED_EMBEDDING_MODEL, embedding_model_digest
from eidolon_ops.endpoints import HostEndpoint
from eidolon_ops.hub_assets import HUB_SETTINGS_TEMPLATE as HUB_SETTINGS_TEMPLATE_CONTRACT
from eidolon_ops.paths import AppAccess
from eidolon_ops.process import ProcessResult
from eidolon_ops.release_matrix import SYSTEMD_ASSET_CONTRACTS

pytestmark = pytest.mark.component


DEPLOY_TREE_LISTING = (
    "100644 blob aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa\teidolon_deploy/bundle.py\n"
    "100644 blob bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb\teidolon_deploy/cli.py\n"
    "100644 blob cccccccccccccccccccccccccccccccccccccccc\teidolon_deploy/contracts/schemas/x.schema.json\n"
)
DEPLOY_PACKAGE_DIGEST = "4a0a4e9c29dbbebd3c4ddbd73fccbee20aba0cdf1cc9360bbe9fafc0277c262a"

# The Hub settings template the Host binding rewrites; only the two literals the
# materializer replaces have to be present for it to be a faithful stand-in. It
# is read out of Hub's own pinned commit, so the stand-in answers to the source
# as well as to the path — Hub is not the only component with a settings.yaml.
HUB_SETTINGS_TEMPLATE_SOURCE, HUB_SETTINGS_TEMPLATE_PATH = HUB_SETTINGS_TEMPLATE_CONTRACT
HUB_SETTINGS_TEMPLATE = (
    "owner_domain_id: owner-local\n"
    "descriptor_uri: https://eidolon-hub.local/api/device-onboarding/v1/descriptor\n"
)


def _read_target(command: tuple[str, ...]) -> tuple[str, str]:
    """Which repository and which path a ``git show`` in this suite is reading."""

    return (
        command[command.index("-C") + 1].rsplit("/", 1)[-1],
        command[-1].partition(":")[2],
    )


class ControllerRunner:
    def __init__(
        self,
        config,
        *,
        wrong_revision: bool = False,
        invalid_release_matrix: bool = False,
        wrong_uv_version: bool = False,
        release_contract_overrides: dict | None = None,
    ) -> None:
        self.config = config
        self.wrong_revision = wrong_revision
        self.invalid_release_matrix = invalid_release_matrix
        self.wrong_uv_version = wrong_uv_version
        self.release_contract_overrides = release_contract_overrides or {}
        self.calls: list[tuple[str, ...]] = []

    def run(self, command, **kwargs):
        command = tuple(command)
        self.calls.append(command)
        if "ls-tree" in command:
            # A synthetic eidolon_deploy tree; the digest below must agree.
            return ProcessResult(0, DEPLOY_TREE_LISTING, "")
        if "rev-parse" in command:
            revision = command[-1].removesuffix("^{commit}")
            if self.wrong_revision:
                revision = "f" * 40
            return ProcessResult(0, revision + "\n", "")
        if "show" in command:
            source, path = _read_target(command)
            if (source, path) == (HUB_SETTINGS_TEMPLATE_SOURCE, HUB_SETTINGS_TEMPLATE_PATH):
                return ProcessResult(0, HUB_SETTINGS_TEMPLATE, "")
            if path == "config/settings.yaml":
                # Component settings are read from the pinned commit whenever the
                # derived inputs are refreshed.
                return ProcessResult(0, _product_settings_reader(source, "", path), "")
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
        if command[-1:] == ("contract",) and command[0].endswith("/eidolon-release"):
            document = {
                "tool": "eidolon-release",
                "cli_contract_version": 1,
                "bundle_schema_version": 2,
                "descriptor_schema_version": 2,
                "snapshot_schema_version": 2,
                "activator_relative_path": ".release/bin/eidolon-release",
                "interpreter_relative_path": ".release/bin/python",
                "package_digest": DEPLOY_PACKAGE_DIGEST,
            }
            document.update(self.release_contract_overrides)
            return ProcessResult(0, json.dumps(document), "")
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
    #: No link was chosen: these tests never open one, so status reports the
    #: configured name the way the real transport does when nothing answers.
    endpoint = None

    def describe(self) -> str:
        return self.endpoint.describe() if self.endpoint else "pi.example"

    def __init__(self) -> None:
        self.agent_calls: list[tuple[str, dict[str, object], str, bool]] = []
        self.remote_calls: list[tuple[tuple[str, ...], bool]] = []
        self.remote_timeouts: list[float] = []
        self.uploads: list[tuple[Path, str, bool]] = []
        self.downloads: list[tuple[str, Path, bool]] = []
        self.resumable_uploads: list[tuple[Path, str]] = []
        self.fail_actions: dict[str, Exception] = {}
        self.fail_remote_match: str | None = None
        self.overrides: dict[str, dict] = {}

    def run_agent(self, action, payload, *, python="/usr/bin/python3", sudo=True, timeout=120):
        self.agent_calls.append((action, dict(payload), python, sudo))
        if action in self.overrides:
            return self.overrides[action]
        if action in self.fail_actions:
            raise self.fail_actions[action]
        if action == "reclaim-releases":
            return {
                "status": {
                    "prepare": "ready",
                    "retain": "retained",
                    "commit": "committed",
                    "abort": "aborted",
                }[payload["phase"]],
                "phase": payload["phase"],
                "candidate_release_id": payload["release_id"],
                "capacity": {
                    "free_bytes_after": 20 * 1024**3,
                    "effective_required_bytes": payload["required_bytes"],
                    "reserve_bytes": payload["reserve_bytes"],
                    "sufficient": True,
                },
                "removed": {"releases": [], "uploads": [], "secrets": []},
            }
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
            "ensure-service-identities": {
                "status": "service_identities_ready",
                "uids": {
                    "eidolon": 41000,
                    "eidolon-bootstrap": 41001,
                    "eidolon-local-api": 41002,
                    "eidolon-lifecycle": 41003,
                },
                "socket_group": "eidolon-lifecycle-client",
                "persistent_socket_group_members": [],
            },
            "controller-reset": {
                "status": "reset",
                "controller_reset": {
                    "revoked_controllers": ["ectrl-0123456789abcdef0123"],
                    "after": {"claim_state": "unclaimed", "reset_epoch": 1},
                },
            },
            "active-release": {
                "status": "observed",
                "release_id": "r1",
                "release_root": "/opt/eidolon/releases/r1",
                "activator": "/opt/eidolon/releases/r1/.release/bin/eidolon-release",
                "interpreter": "/opt/eidolon/releases/r1/.release/bin/python",
            },
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
            "backup": {
                "status": "captured",
                "release_id": payload.get("release_id"),
                "host_id": "ehost-0123456789abcdefabcd",
                "directory": "/var/tmp/eidolon-backup-r1",
                "authorities": [{"authority": "system", "file": "system.sqlite3"}],
                "not_covered": [{"state": "memory", "path": "/var/lib/eidolon/memory"}],
            },
            "restore": {"status": "restored", "restored": ["system"]},
            "commissioning-code": {"status": "issued", "setup_code": "123456"},
            "refresh-host-application": {"status": "refreshed", "changed": []},
            # Controller tests stay hermetic; transfer of an absent encoder is
            # covered by the dedicated embedding-model contract suite.
            "embedding-model-state": {
                "status": "held",
                "digest": embedding_model_digest(PINNED_EMBEDDING_MODEL),
            },
            "install-embedding-model": {"status": "installed"},
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

    def download(self, source, destination, *, recursive=False):
        self.downloads.append((source, Path(destination), recursive))
        Path(destination).mkdir(parents=True, exist_ok=True)

    def upload_directory_resumable(self, source, destination):
        self.resumable_uploads.append((Path(source), destination))


def _stub_input_contract(controller: EidolonPiController) -> None:
    controller.preflight._validate_install_inputs = lambda: {  # type: ignore[method-assign]
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
    assert result["release_tool_contract"]["tool"] == "eidolon-release"
    assert result["release_tool_contract"]["bundle_schema_version"] == 2
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


def test_local_preflight_rejects_an_activator_speaking_another_contract(config) -> None:
    """A stale activator is rejected by its own report, not by repository layout."""

    controller = EidolonPiController(
        config,
        ControllerRunner(config, release_contract_overrides={"bundle_schema_version": 1}),
        transport=FakeTransport(),
    )

    with pytest.raises(OperationsError, match="bundle_schema_version"):
        controller.local_preflight(require_install_files=False)


def test_local_preflight_rejects_unexpected_published_operator_entries(config) -> None:
    controller = EidolonPiController(
        config,
        ControllerRunner(
            config,
            release_contract_overrides={
                "interpreter_relative_path": "eidolon_kernel/.venv/bin/python"
            },
        ),
        transport=FakeTransport(),
    )

    with pytest.raises(OperationsError, match="operator entries"):
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
        "release_reclaim_prepare",
        "upload_guard",
        "upload_finalize",
        # The encoder is carried before the release is prepared: a Host that
        # gets the code without the weights answers memory queries slowly and
        # emptily, which reads like an Eidolon that remembers nothing.
        "embedding_model",
        "prepare",
        "dry_run",
        "release_reclaim_retain",
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
    reclaim = next(
        payload for action, payload, _python, _sudo in transport.agent_calls
        if action == "reclaim-releases"
    )
    assert reclaim["phase"] == "prepare"
    assert reclaim["required_bytes"] > 0
    assert reclaim["reserve_bytes"] == 1024**3


def test_deploy_capacity_gate_fails_before_upload(setup_controller) -> None:
    controller, _runner, transport = setup_controller
    transport.overrides["reclaim-releases"] = {
        "status": "insufficient_capacity",
        "capacity": {
            "free_bytes_after": 100,
            "effective_required_bytes": 200,
            "reserve_bytes": 300,
            "sufficient": False,
        },
    }

    with pytest.raises(OperationsError, match="insufficient release capacity"):
        controller.deploy(release_id="r1", resume=False, activate=False)

    assert transport.resumable_uploads == []
    assert not any(call[0] == "guard-upload" for call in transport.agent_calls)


def test_deploy_resume_activate_skips_transfer(setup_controller) -> None:
    controller, runner, transport = setup_controller

    result = controller.deploy(release_id="r1", resume=True, activate=True)

    assert result["status"] == "activated"
    assert [phase["phase"] for phase in result["phases"]] == [
        "release_reclaim_prepare",
        "dry_run",
        "service_identities",
        "activate",
        "doctor",
        "app_ready",
        "release_reclaim_commit",
    ]
    assert transport.uploads == []
    assert not any(len(call) > 1 and call[1] == "bundle" for call in runner.calls)


def test_deploy_prestages_host_application_before_component_activation(
    setup_controller, monkeypatch
) -> None:
    controller, _runner, _transport = setup_controller
    controller.host_layer.app = _app()
    monkeypatch.setattr(controller.releases, "_app_ready", lambda: {"status": "app_ready"})
    events: list[str] = []
    original_remote_json = controller.releases._remote_json

    def observed_remote_json(label, command, *, timeout):
        events.append(label)
        return original_remote_json(label, command, timeout=timeout)

    monkeypatch.setattr(controller.releases, "_remote_json", observed_remote_json)
    monkeypatch.setattr(
        controller.host_layer,
        "refresh",
        lambda release_id: events.append(f"host application {release_id}")
        or {"status": "refreshed"},
    )

    result = controller.deploy(release_id="r1", resume=True, activate=True)

    assert result["status"] == "activated"
    assert events.index("host application r1") < events.index("release activation")
    assert [phase["phase"] for phase in result["phases"]] == [
        "release_reclaim_prepare",
        "dry_run",
        "service_identities",
        "host_application",
        "activate",
        "doctor",
        "app_ready",
        "release_reclaim_commit",
    ]


def test_host_application_refresh_carries_host_identity_and_bound_environments(
    setup_controller,
) -> None:
    controller, _runner, transport = setup_controller
    controller.host_layer.app = _app()
    identity = controller.config.install_files["host_identity"]
    identity.write_bytes(b"i" * 32)
    identity.chmod(0o600)
    for name in ("local_api_env", "channel_env"):
        controller.config.install_files[name].write_text("TOKEN=test\n", encoding="utf-8")

    controller.host_layer.refresh("r1")

    destinations = {destination for _source, destination, _recursive in transport.uploads}
    stage = "/var/tmp/eidolon-secrets-r1"
    assert {
        f"{stage}/host_identity.ed25519",
        f"{stage}/local-api.env",
        f"{stage}/channel.env",
        f"{stage}/hub.crt",
        f"{stage}/hub.key",
        f"{stage}/owner-domain-root-ca.pem",
        f"{stage}/authority-signing-certificate.pem",
    } <= destinations
    assert f"{stage}/owner-domain-root.key.pem" not in destinations
    assert f"{stage}/authority-signing.key.pem" not in destinations


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
    assert transport.agent_calls[-1][0] == "reclaim-releases"
    assert transport.agent_calls[-1][1]["phase"] == "abort"


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
    assert transport.agent_calls[-1][0] == "reclaim-releases"
    assert transport.agent_calls[-1][1]["phase"] == "abort"


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
    # Derived settings follow the pinned commits; credentials are not reissued.
    assert result["refreshed_settings"] == ["agent.yaml", "channel.yaml", "memory.yaml"]


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
    assert actions[-3:] == ["install", "cleanup-stage", "reclaim-releases"]
    assert transport.agent_calls[-1][1]["phase"] == "commit"
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
            command = tuple(command)
            if "show" in command and _read_target(command) == (
                HUB_SETTINGS_TEMPLATE_SOURCE,
                HUB_SETTINGS_TEMPLATE_PATH,
            ):
                return ProcessResult(
                    0,
                    "onboarding:\n"
                    "  owner_domain_id: owner-local\n"
                    "  descriptor_uri: https://eidolon-hub.local/api/device-onboarding/v1/descriptor\n"
                    "discovery:\n  mdns:\n    enabled: true\n"
                    "channel_provider:\n  contract_url: http://127.0.0.1:8767/v1\n"
                    "persistence:\n  path: $EIDOLON_STATE_ROOT/hub/eidolon-hub.sqlite3\n",
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

    controller.host_layer.stage_install_files("host-bound", stage)
    payload = controller.host_layer.target_payload()

    assert len(transport.uploaded_bytes) == len(config.install_files) + 9
    app = payload["app"]
    assert isinstance(app, dict)
    assert str(app["owner_domain_id"]).startswith("owner-")
    assert str(app["hub_hostname"]).endswith(".local")
    assert str(app["hub_origin"]).endswith(":8443")
    rendered_local = transport.uploaded_bytes[f"{stage}/local-api.env"].decode()
    rendered_hub = transport.uploaded_bytes[f"{stage}/hub.generated.yaml"].decode()
    assert f"EIDOLON_LOCAL_API_OWNER_DOMAIN_ID={app['owner_domain_id']}" in rendered_local
    assert f"owner_domain_id: {app['owner_domain_id']}" in rendered_hub
    assert f"descriptor_uri: {app['hub_origin']}/api/device-onboarding/v1/descriptor" in rendered_hub
    assert f"{stage}/owner-domain-root.key.pem" not in transport.uploaded_bytes
    assert f"{stage}/authority-signing.key.pem" not in transport.uploaded_bytes
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

    assert [call[0] for call in transport.agent_calls][-2:] == [
        "cleanup-stage",
        "reclaim-releases",
    ]
    assert transport.agent_calls[-1][1]["phase"] == "abort"


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


@pytest.mark.parametrize("action", ["start", "stop", "restart"])
def test_lifecycle_dry_run_has_no_remote_mutation(setup_controller, action: str) -> None:
    controller, _runner, transport = setup_controller

    result = controller.lifecycle(action, dry_run=True)

    assert result["status"] == "planned"
    assert transport.agent_calls == []


@pytest.mark.parametrize("action", ["start", "stop", "restart"])
def test_lifecycle_runs_under_the_active_release_interpreter(setup_controller, action: str) -> None:
    controller, _runner, transport = setup_controller

    result = controller.lifecycle(action, dry_run=False)

    assert result["status"] == (action + "ed" if action != "stop" else "stopped")
    assert transport.agent_calls[-2][0] == "active-release"
    assert transport.agent_calls[-1][2] == "/opt/eidolon/releases/r1/.release/bin/python"


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


def test_controller_reset_defaults_to_a_plan_that_names_what_survives(
    setup_controller,
) -> None:
    controller, _runner, transport = setup_controller

    result = controller.controller_reset(apply=False)

    assert result["status"] == "planned"
    assert transport.agent_calls == []
    assert any("Owner binding" in item for item in result["preserves"])


def test_controller_reset_apply_returns_the_revoked_grants(setup_controller) -> None:
    controller, _runner, transport = setup_controller

    result = controller.controller_reset(apply=True)

    assert result["status"] == "reset"
    assert transport.agent_calls[-1][0] == "controller-reset"
    assert result["controller_reset"]["after"]["claim_state"] == "unclaimed"


def test_controller_reset_rejects_invalid_target_evidence(setup_controller) -> None:
    controller, _runner, transport = setup_controller
    transport.overrides["controller-reset"] = {"status": "observed"}

    with pytest.raises(OperationsError, match="invalid evidence"):
        controller.controller_reset(apply=True)


def test_preflight_refuses_an_activator_the_release_will_not_ship(config) -> None:
    """Sealing runs from the release's own activator.

    A behaviour fix on the workstation does nothing unless the Kernel pin moves
    with it, and without this check the mismatch only surfaces after a full
    install has already run on the target.
    """

    controller = EidolonPiController(
        config,
        ControllerRunner(config, release_contract_overrides={"package_digest": "0" * 64}),
        transport=FakeTransport(),
    )

    with pytest.raises(OperationsError, match="move the eidolon_kernel pin"):
        controller.local_preflight(require_install_files=False)


def test_preflight_refuses_a_pinned_commit_without_the_deploy_package(config) -> None:
    class EmptyTree(ControllerRunner):
        def run(self, command, **kwargs):
            if "ls-tree" in tuple(command):
                return ProcessResult(0, "", "")
            return super().run(command, **kwargs)

    controller = EidolonPiController(config, EmptyTree(config), transport=FakeTransport())

    with pytest.raises(OperationsError, match="ships no eidolon_deploy"):
        controller.local_preflight(require_install_files=False)


def test_an_annotated_tag_must_still_name_the_pinned_commit(config) -> None:
    """A tag makes a release reviewable; it must never redefine one.

    Tags are movable references, so following one silently would make the
    release irreproducible. Ops reports the move instead.
    """

    from dataclasses import replace as _replace

    tagged = _replace(
        config,
        sources={
            **config.sources,
            "eidolon_kernel": _replace(config.sources["eidolon_kernel"], tag="kernel/v9.9.9"),
        },
    )

    class MovedTag(ControllerRunner):
        def run(self, command, **kwargs):
            command = tuple(command)
            if "rev-parse" in command and command[-1].startswith("kernel/v9.9.9"):
                return ProcessResult(0, "e" * 40 + "\n", "")
            return super().run(command, **kwargs)

    controller = EidolonPiController(tagged, MovedTag(tagged), transport=FakeTransport())

    with pytest.raises(OperationsError, match="tag no longer names the pinned commit"):
        controller.local_preflight(require_install_files=False)


def test_a_tag_that_still_resolves_is_accepted(config) -> None:
    from dataclasses import replace as _replace

    revision = config.sources["eidolon_kernel"].revision
    tagged = _replace(
        config,
        sources={
            **config.sources,
            "eidolon_kernel": _replace(config.sources["eidolon_kernel"], tag="kernel/v1.0.0"),
        },
    )

    class ResolvingTag(ControllerRunner):
        def run(self, command, **kwargs):
            command = tuple(command)
            if "rev-parse" in command and command[-1].startswith("kernel/v1.0.0"):
                return ProcessResult(0, revision + "\n", "")
            return super().run(command, **kwargs)

    controller = EidolonPiController(tagged, ResolvingTag(tagged), transport=FakeTransport())

    assert controller.local_preflight(require_install_files=False)["sources"]


def test_input_initialization_reports_the_host_binding_it_established(
    setup_controller,
) -> None:
    """The contract describes the Host binding; the assets are private material.

    Asking the produced assets for a contract raised AttributeError, so
    init-inputs failed outright on any Host that declares an app contract.
    """

    controller, _runner, _transport = setup_controller
    identity = controller.config.install_files["host_identity"]
    identity.parent.mkdir(parents=True, exist_ok=True)
    identity.write_bytes(b"i" * 32)
    identity.chmod(0o600)
    controller.app = AppAccess(
        lan_ipv4=None,
        hub_https_port=8443,
        livekit_client_url="wss://eidolon-hub.local:7880",
        allow_insecure_livekit=False,
    )

    result = controller.initialize_inputs()

    binding = result["host_application"]
    assert binding["host_id"].startswith("ehost-")
    assert binding["hub_hostname"].endswith(".local")
    # Private material must not ride along in the reported contract.
    assert not [key for key in binding if "key" in key.lower() or "token" in key.lower()]


def test_a_degraded_app_gate_names_what_it_found() -> None:
    """A gate failure rolls the release back and the phases go with it, so
    "degraded" was the whole report an operator got for an undone install."""

    from eidolon_ops.readiness import describe_failures as _degraded_detail

    detail = _degraded_detail(
        {
            "status": "degraded",
            "local_api": {"healthy": True},
            "mdns": {"healthy": False},
            "preflight": {"ok": True},
            "host_application": {
                "healthy": False,
                "checks": {"certificate": True, "hub_lan_health": False},
                "hub_health": {"error": "Hub LAN ingress self-check failed: refused"},
            },
        }
    )

    assert "mdns" in detail
    assert "host_application.checks.hub_lan_health" in detail
    assert "refused" in detail
    assert "certificate" not in detail
    assert "local_api" not in detail


def test_a_section_nobody_anticipated_is_still_reported() -> None:
    """Naming the expected sections meant a later one would go unmentioned in
    exactly the report someone reads when they cannot see the Host."""

    from eidolon_ops.readiness import describe_failures as _degraded_detail

    detail = _degraded_detail({"status": "degraded", "some_future_subsystem": {"healthy": False}})

    assert "some_future_subsystem" in detail


def test_an_unhealthy_section_that_says_why_reports_the_reason_not_itself() -> None:
    from eidolon_ops.readiness import describe_failures as _degraded_detail

    detail = _degraded_detail({"status": "degraded", "mdns": {"healthy": False}})

    assert detail == "mdns"


def test_a_gate_that_fails_for_no_stated_reason_still_says_something() -> None:
    from eidolon_ops.readiness import describe_failures as _degraded_detail

    assert "degraded" in _degraded_detail({"status": "degraded"})


def test_status_reports_which_link_it_ran_over(setup_controller) -> None:
    controller, _runner, transport = setup_controller
    transport.endpoint = HostEndpoint(address="169.254.19.7", interface="en7", link="wired")

    assert controller.status()["endpoint"] == "169.254.19.7 (wired via en7)"


def test_status_names_the_host_when_no_link_was_chosen(setup_controller) -> None:
    controller, _runner, _transport = setup_controller

    assert controller.status()["endpoint"] == controller.config.host.hostname


def test_a_backup_is_taken_on_the_host_and_brought_here(setup_controller, tmp_path: Path) -> None:
    """Left on the Host it would be lost with the Host."""

    controller, _runner, transport = setup_controller
    output = tmp_path / "backups"

    result = controller.backup(output=output)

    assert result["status"] == "captured"
    directory = output / "r1-ehost-0123456789abcdefabcd"
    assert result["local_directory"] == str(directory)
    assert transport.downloads == [("/var/tmp/eidolon-backup-r1", directory, True)]
    manifest = json.loads((directory / "backup.json").read_text(encoding="utf-8"))
    assert manifest["release_id"] == "r1"

    with pytest.raises(OperationsError, match="already exists"):
        controller.backup(output=output)


def test_a_restore_names_what_it_will_replace_before_it_replaces_it(
    setup_controller, tmp_path: Path
) -> None:
    controller, _runner, transport = setup_controller
    source = tmp_path / "backup"
    source.mkdir()
    with pytest.raises(OperationsError, match="manifest is missing"):
        controller.restore(source=source, apply=False)
    (source / "backup.json").write_text("not-json", encoding="utf-8")
    with pytest.raises(OperationsError, match="one JSON document"):
        controller.restore(source=source, apply=False)
    (source / "backup.json").write_text(
        json.dumps(
            {
                "release_id": "r1",
                "host_id": "ehost-0123456789abcdefabcd",
                "authorities": [{"authority": "system"}],
                "not_covered": [{"state": "memory"}],
            }
        ),
        encoding="utf-8",
    )

    planned = controller.restore(source=source, apply=False)
    assert planned["status"] == "planned"
    assert planned["authorities"] == ["system"]
    assert planned["not_restored"] == ["memory"]

    applied = controller.restore(source=source, apply=True)
    assert applied["status"] == "restored"
    assert (source, "/var/tmp/eidolon-backup-r1", True) in transport.uploads
    assert any("eidolon-backup-r1" in " ".join(call[0]) for call in transport.remote_calls)


def test_a_setup_code_and_a_boundary_action_reach_the_host(setup_controller) -> None:
    controller, _runner, _transport = setup_controller

    assert controller.commissioning_code(ttl_seconds=600)["status"] == "issued"
    assert controller.lifecycle("restart", dry_run=True)["status"] == "planned"
    assert controller.lifecycle("start", dry_run=False)["status"] == "started"
    with pytest.raises(OperationsError, match="unknown lifecycle action"):
        controller.lifecycle("reboot", dry_run=False)


def _publish_contract(config, component_id: str, factory: str) -> None:
    """Give one source checkout an operations contract."""

    root = config.sources[component_id].path / "ops"
    root.mkdir(parents=True, exist_ok=True)
    (root / "component.toml").write_text(
        f"""
schema_version = 1
component_id = "{component_id}"
contract_version = "1"

[reset]
factory = ["{factory}"]
""".lstrip(),
        encoding="utf-8",
    )


def test_a_release_from_before_the_contracts_can_still_be_reset(
    setup_controller,
) -> None:
    """Silence from every component is a pre-contract release, not a fault.

    A release pins exact commits, so Ops has to be able to wipe a Host running
    one that predates any of this. Refusing would take the tool away exactly
    when it is needed.
    """

    controller, _runner, transport = setup_controller

    plan = controller.reset(wipe_authority_data=True, apply=False)

    assert plan["authority"]["contracts"] == "absent"
    # And it says the question went unasked rather than implying an answer.
    assert "not checked" in plan["authority"]["note"]
    assert [call[0] for call in transport.agent_calls] == ["reset-plan"]


def test_a_half_declared_release_refuses_to_wipe(setup_controller, config) -> None:
    """The mixture is the dangerous one, so it is the one that fails closed.

    Once the set has started declaring, a component nobody asked is exactly
    where moved state would be — and a wipe of the old roots would report
    success while leaving it behind.
    """

    controller, _runner, transport = setup_controller
    _publish_contract(config, "eidolon_hub", "/var/lib/eidolon/hub")

    with pytest.raises(OperationsError) as error:
        controller.reset(wipe_authority_data=True, apply=True)

    message = str(error.value)
    assert "eidolon_data" in message and "eidolon_memory" in message
    # Refused before anything was asked of the Host.
    assert transport.agent_calls == []


def test_state_a_reset_could_not_reach_stops_the_reset(
    setup_controller, config
) -> None:
    controller, _runner, transport = setup_controller
    for source_id in SOURCE_IDS:
        _publish_contract(config, source_id, "/var/lib/eidolon/example")
    _publish_contract(config, "eidolon_memory", "/srv/eidolon-memory")

    with pytest.raises(OperationsError) as error:
        controller.reset(wipe_authority_data=True, apply=True)

    # The Host clears three roots and nothing else. A component that moved its
    # state outside them would hand the next owner the last one's data, and a
    # wipe of /var/lib/eidolon would have reported success.
    message = str(error.value)
    assert "/srv/eidolon-memory" in message
    assert "would leave data behind" in message
    assert transport.agent_calls == []


def test_a_fully_declared_reset_reports_what_each_component_loses(
    setup_controller, config
) -> None:
    controller, _runner, transport = setup_controller
    for source_id in SOURCE_IDS:
        _publish_contract(config, source_id, f"/var/lib/eidolon/{source_id}")

    plan = controller.reset(wipe_authority_data=True, apply=False)

    authority = plan["authority"]
    assert authority["contracts"] == "complete"
    assert authority["declared_by"]["eidolon_hub"] == ["/var/lib/eidolon/eidolon_hub"]
    # The platform answers here too, out of Ops's own contract, so NATS and
    # LiveKit are named by something rather than turning up unclaimed.
    assert "eidolon_platform" in authority["declared_by"]
    # These fixtures declare no authority, so what is listed is the platform's
    # own: the two stores it says a backup does not carry.
    assert authority["not_in_any_backup"] == [
        "eidolon_platform:/var/lib/eidolon/livekit",
        "eidolon_platform:/var/lib/eidolon/nats/jetstream",
    ]
    assert [call[0] for call in transport.agent_calls] == ["reset-plan"]


def test_a_reset_that_keeps_authority_does_not_ask_the_components(
    setup_controller,
) -> None:
    controller, _runner, transport = setup_controller

    plan = controller.reset(wipe_authority_data=False, apply=False)

    # Nothing is lost, so there is nothing for a component to be consulted
    # about, and a missing contract must not block a deployment-only reset.
    assert "authority" not in plan
    assert [call[0] for call in transport.agent_calls] == ["reset-plan"]


def _plan_with_contents(transport, contents: list[str]) -> None:
    """Make the Host's reset plan report what its roots actually hold."""

    original = transport.run_agent

    def run_agent(action, payload, **kwargs):
        result = original(action, payload, **kwargs)
        if action == "reset-plan":
            return {**result, "authority_contents": contents}
        return result

    transport.run_agent = run_agent


def test_the_report_is_about_what_will_actually_be_removed(
    setup_controller, config
) -> None:
    """The roots are the unit of removal, so they are the unit of the report.

    Narrowing the deletion to the declared paths would be the wrong fix: the
    NATS JetStream store lives under these roots and no component declares it,
    because NATS is a platform server with no repository of ours. Deleting only
    what was claimed would hand the next owner the last one's message history.
    """

    controller, _runner, transport = setup_controller
    for source_id in SOURCE_IDS:
        _publish_contract(config, source_id, f"/var/lib/eidolon/{source_id}")
    _plan_with_contents(
        transport,
        [
            "/var/lib/eidolon/eidolon_hub",
            # The platform's, out of Ops's own contract rather than a source
            # checkout. Deleting it is right — a Host handed on while it still
            # holds the last owner's message history is the failure — and now
            # it reads as a decision instead of as a gap.
            "/var/lib/eidolon/nats",
            # Nobody's: what a component that has since left the product would
            # leave behind. This is the case the report exists for.
            "/var/lib/eidolon/mementos",
        ],
    )

    plan = controller.reset(wipe_authority_data=True, apply=False)
    authority = plan["authority"]

    assert authority["will_be_removed"] == {
        "/var/lib/eidolon/eidolon_hub": "eidolon_hub",
        "/var/lib/eidolon/nats": "eidolon_platform",
    }
    assert authority["removed_but_unclaimed"] == ["/var/lib/eidolon/mementos"]


def test_a_databases_own_sidecars_belong_to_whoever_declared_it(
    setup_controller, config
) -> None:
    controller, _runner, transport = setup_controller
    for source_id in SOURCE_IDS:
        _publish_contract(config, source_id, f"/var/lib/eidolon/{source_id}.sqlite3")
    _plan_with_contents(
        transport,
        [
            "/var/lib/eidolon/eidolon_hub.sqlite3",
            "/var/lib/eidolon/eidolon_hub.sqlite3-wal",
            "/var/lib/eidolon/eidolon_hub.sqlite3-shm",
            "/var/lib/eidolon/eidolon_hub.sqlite3.lock",
        ],
    )

    authority = controller.reset(wipe_authority_data=True, apply=False)["authority"]

    # Seven obvious entries burying the one that matters is how a report stops
    # being read. A component that declared the database declared these.
    assert authority["removed_but_unclaimed"] == []
    assert set(authority["will_be_removed"].values()) == {"eidolon_hub"}
