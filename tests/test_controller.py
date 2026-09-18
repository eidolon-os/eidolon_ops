from __future__ import annotations

import dataclasses
import functools
import hashlib
import json
import os
import tarfile
from dataclasses import replace
from ipaddress import IPv4Address
from pathlib import Path

import pytest

# The overlay still rewrites specific literals in three components' settings,
# so refreshing the derived inputs must be exercised against text that actually
# contains them. Shared with the install-inputs suite.
from conftest import SOURCE_HEADS
from test_install_inputs import _settings_reader as _product_settings_reader

from eidolon_ops import controller as controller_module
from eidolon_ops.config import SOURCE_IDS, ConfigurationError
from eidolon_ops.controller import (
    EidolonPiController,
    OperationsError,
    _finalize_authority_restore_stage,
    _wait_for_restored_authority_readiness,
)
from eidolon_ops.endpoints import HostEndpoint
from eidolon_ops.host_application import (
    HOST_APPLICATION_STAGE_NAMES,
)
from eidolon_ops.host_delivery import bind_delivery
from eidolon_ops.host_identity import derive_host_lan_identity
from eidolon_ops.hostagent.hardware import verify_binding
from eidolon_ops.hub_assets import HUB_SETTINGS_TEMPLATE as HUB_SETTINGS_TEMPLATE_CONTRACT
from eidolon_ops.owner_domain_assets import HostAuthority
from eidolon_ops.paths import AppAccess
from eidolon_ops.process import ProcessResult
from eidolon_ops.readiness import HostKind, expected_facts
from eidolon_ops.release_matrix import SYSTEMD_ASSET_CONTRACTS

pytestmark = pytest.mark.component

#: A Host answering every fact in the contract. Derived rather than written out
#: so a fact added to the contract cannot leave this fake silently attesting an
#: older, shorter check set.
ALL_FACTS_TRUE = {fact: True for fact in expected_facts(HostKind.PRODUCT)}


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
    "owner_domain_generation: 1\n"
    "descriptor_uri: https://eidolon-hub.local/api/device-onboarding/v1/descriptor\n"
)


def test_authority_restore_waits_for_app_readiness_without_accepting_degraded(
    monkeypatch,
) -> None:
    reports = iter(
        [
            {"status": "degraded", "checks": {"hub_lan_reachable": False}},
            {"status": "app_ready", "checks": {"hub_lan_reachable": True}},
        ]
    )
    monkeypatch.setattr(controller_module.time, "sleep", lambda _seconds: None)

    result = _wait_for_restored_authority_readiness(lambda: next(reports), timeout_seconds=1)

    assert result["status"] == "app_ready"


def test_authority_restore_readiness_timeout_is_a_stable_failure() -> None:
    with pytest.raises(OperationsError, match="AUTHORITY_RESTORE_FAILED"):
        _wait_for_restored_authority_readiness(
            lambda: {
                "status": "degraded",
                "checks": {"hub_lan_reachable": False},
            },
            timeout_seconds=0,
        )


@pytest.mark.parametrize(
    "result",
    [RuntimeError("transport failed"), {"status": "authority_restore_stage_ready"}],
)
def test_authority_restore_stage_cleanup_fails_closed(result) -> None:
    class CleanupTransport:
        def run_agent(self, action, payload, *, timeout):
            assert action == "authority-restore-stage-finalize"
            assert payload["release_id"] == "r1"
            assert timeout == 120
            if isinstance(result, Exception):
                raise result
            return result

    with pytest.raises(OperationsError, match="AUTHORITY_RESTORE_FAILED"):
        _finalize_authority_restore_stage(CleanupTransport(), {"release_id": "r1"})


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
        dirty: dict[str, str] | None = None,
        heads: dict[str, str] | None = None,
    ) -> None:
        self.config = config
        self.wrong_revision = wrong_revision
        self.invalid_release_matrix = invalid_release_matrix
        self.wrong_uv_version = wrong_uv_version
        self.release_contract_overrides = release_contract_overrides or {}
        #: What each fake checkout's HEAD answers, and what it reports dirty.
        #: A release input is now read out of the repository rather than out of
        #: the profile, so a fake git has to be able to say both.
        self.heads = {**SOURCE_HEADS, **(heads or {})}
        self.dirty = dict(dirty or {})
        self.calls: list[tuple[str, ...]] = []

    def _source(self, command: tuple[str, ...]) -> str:
        return Path(command[command.index("-C") + 1]).name

    def revision(self, source_id: str) -> str:
        return self.heads[source_id]

    def run(self, command, **kwargs):
        command = tuple(command)
        self.calls.append(command)
        if "ls-tree" in command and command[-1] == "ops/component.toml":
            path = self.config.sources[self._source(command)].path / "ops/component.toml"
            return ProcessResult(0, "ops/component.toml\n" if path.is_file() else "", "")
        if "ls-tree" in command:
            # A synthetic eidolon_deploy tree; the digest below must agree.
            return ProcessResult(0, DEPLOY_TREE_LISTING, "")
        if command[-2:] == ("rev-parse", "HEAD"):
            head = self.heads[self._source(command)]
            return ProcessResult(0, head + "\n", "")
        if command[-1] == "--show-toplevel":
            # The activator lives in the Kernel checkout's own virtualenv.
            return ProcessResult(0, str(self.config.sources["eidolon_kernel"].path) + "\n", "")
        if command[-2:] == ("branch", "--show-current"):
            return ProcessResult(0, "main\n", "")
        if "status" in command and "--porcelain" in command:
            source = self._source(command)
            if "--" in command:
                # Scoped to eidolon_deploy: only the Kernel checkout answers.
                return ProcessResult(0, self.dirty.get("eidolon_deploy", ""), "")
            return ProcessResult(0, self.dirty.get(source, ""), "")
        if "rev-list" in command:
            return ProcessResult(0, "0\t0\n", "")
        if "rev-parse" in command:
            revision = command[-1].removesuffix("^{commit}")
            if self.wrong_revision:
                revision = "f" * 40
            return ProcessResult(0, revision + "\n", "")
        if "show" in command:
            source, path = _read_target(command)
            if (source, path) == (HUB_SETTINGS_TEMPLATE_SOURCE, HUB_SETTINGS_TEMPLATE_PATH):
                return ProcessResult(0, HUB_SETTINGS_TEMPLATE, "")
            if path == "ops/component.toml":
                return ProcessResult(0, (self.config.sources[source].path / path).read_text(), "")
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
            if contract.unit.endswith(".socket"):
                # A socket unit declares a listener, not a process; a service
                # body here would only pass a check that no longer exists.
                return ProcessResult(
                    0,
                    "[Unit]\nDescription=test\n[Socket]\nListenStream=/run/test.sock\n",
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
                "bundle_schema_version": 3,
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
            cutover_mode = command[command.index("--cutover-mode") + 1]
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
                        "revision": self.revision(source_id),
                        "archive": f"sources/{source_id}.tar",
                        "sha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
                    }
                )
            preparer = output / "prepare_target.py"
            preparer.write_bytes(b"preparer")
            artifact_root = output / "artifacts/sha256"
            artifact_root.mkdir(parents=True)
            artifact_records = []
            artifact_inputs = [
                ("python-dependencies", "dependency-cache", "", b"arm64 dependency cache"),
                *[
                    (
                        f"channel-model:model-{index}.bin",
                        "channel-model",
                        f"model-{index}.bin",
                        f"model-{index}".encode(),
                    )
                    for index in range(8)
                ],
            ]
            for artifact_id, kind, install_path, content in artifact_inputs:
                digest = hashlib.sha256(content).hexdigest()
                (artifact_root / digest).write_bytes(content)
                artifact_records.append(
                    {
                        "artifact_id": artifact_id,
                        "kind": kind,
                        "sha256": digest,
                        "size": len(content),
                        "bundle_path": f"artifacts/sha256/{digest}",
                        "install_path": install_path,
                    }
                )
            (output / "bundle.json").write_text(
                json.dumps(
                    {
                        "schema_version": 3,
                        "release_id": command[2],
                        "cutover_mode": cutover_mode,
                        "target": {"system": "linux", "machine": "aarch64"},
                        "sources": records,
                        "preparer": {
                            "path": "prepare_target.py",
                            "sha256": hashlib.sha256(preparer.read_bytes()).hexdigest(),
                        },
                        "artifacts": artifact_records,
                        "python_dependencies": {
                            "artifact_id": "python-dependencies",
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


@functools.cache
def _declared_artifact_digests() -> dict[str, str]:
    """Every declared artifact's destination and set digest, read once."""

    from eidolon_ops.capabilities import HOST_CAPABILITIES
    from eidolon_ops.component_artifacts import carried_artifacts, host_artifact_root
    from eidolon_ops.component_contract import read_component_contracts
    from eidolon_ops.config import expected_sources

    root = Path(__file__).resolve().parents[2]
    sources = {
        source_id: root / source_id for source_id in sorted(expected_sources(HOST_CAPABILITIES))
    }
    topology = read_component_contracts(sources, HOST_CAPABILITIES)
    return {
        str(host_artifact_root(artifact)): artifact.digest
        for artifact in carried_artifacts(topology)
    }


class FakeTransport:
    #: No link was chosen: these tests never open one, so status reports the
    #: configured name the way the real transport does when nothing answers.
    endpoint = None

    def describe(self) -> str:
        return self.endpoint.describe() if self.endpoint else "pi.example"

    def prefer_wired_link(self) -> None:
        """A fake Host has exactly the link the test gave it.

        The real transport asks the Host for addresses the resolver may have
        omitted; there is nothing here that could answer differently, so the
        choice stands and a wireless-only Host still refuses.
        """

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
        if action == "component-artifact-state":
            # Answered from the contracts, so this stays hermetic without
            # claiming a digest no declaration has: a canned single answer
            # would match one artifact and send the others down the fetch path.
            # Transfer of an absent artifact has its own suite.
            return {
                "status": "held",
                "destination": payload["destination"],
                "digest": _declared_artifact_digests()[payload["destination"]],
            }
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
            "app-ready": {"status": "app_ready", "checks": dict(ALL_FACTS_TRUE)},
            "expansion-plan": {
                "status": "eligible",
                "source_release": "core-release",
            },
            "doctor-host": {"status": "healthy", "checks": {}},
            "guard-upload": {"status": "ready_for_upload"},
            "finalize-upload": {"status": "finalized"},
            "release-artifact-state": {
                "status": "complete",
                "missing": [],
            },
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
            # A Host whose processes all run the release its links name. The
            # deploy asks after every activation, because a Host that has not
            # got there reports exactly the same health as one that has.
            "converge-running-release": {
                "status": "converged",
                "release_id": "release-1",
                "restarted": [],
                "running_releases": {},
            },
            # An unowned Host: no Hub database, so no Authority lineage. Tests
            # that need a Host which has already established one override it.
            "authority-lineage": {
                "status": "observed",
                "marker": None,
                "anchor": None,
                "established": None,
            },
            # The same Host as an install sees it: nothing established, no
            # directory served, no identity held, one permanent hardware id.
            # `_host_established` replaces this for an installed Host.
            "install-context": {
                "status": "observed",
                "marker": None,
                "anchor": None,
                "established": None,
                "directory": None,
                "hardware": {"kind": "device-tree:raspberrypi,5-model-b", "fingerprint": "sha256:" + "a" * 64},
                "identity_sha256": None,
            },
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
            "authority-restore-stage-reset": {
                "status": "authority_restore_stage_ready",
                "directory": "/var/tmp/eidolon-authority-restore-r1",
            },
            "authority-restore-stage-finalize": {
                "status": "authority_restore_stage_absent",
                "directory": "/var/tmp/eidolon-authority-restore-r1",
                "removed": True,
            },
            "commissioning-code": {"status": "issued", "setup_code": "123456"},
            "refresh-host-application": {"status": "refreshed", "changed": []},
            "refresh-release-configuration": {"status": "refreshed", "changed": []},
            "host-hardware": {"status": "observed", "hardware": {"kind": "device-tree:raspberrypi,5-model-b", "fingerprint": "sha256:" + "a" * 64}},
            "deployment-identity": {
                "status": "observed",
                "host_id": derive_host_lan_identity(b"a" * 32).host_id,
                "authority": {"contract_version": 1, "owner_domain_id": "owner-" + "a" * 20,
                              "owner_domain_generation": 8, "state_id": "authority-state_board"},
                "descriptor_uri": derive_host_lan_identity(b"a" * 32).hub_origin(8443) + "/api/device-onboarding/v1/descriptor",
                "preserved_files": {"host_identity.ed25519": hashlib.sha256(b"a" * 32).hexdigest()},
            },
            "release-cutover-snapshot": {
                "status": "host_cutover_snapshotted",
                "host_snapshot": "/var/lib/eidolon/deployments/r1-host-" + "b" * 32,
            },
            "release-cutover-restore": {"status": "host_cutover_restored"},
            "release-cutover-finalize": {"status": "cutover_recorded"},
            "install-component-artifact": {"status": "installed"},
            # A Host that already holds every declared credential. Deploy asks
            # before it ships anything; a Host that answered otherwise is
            # covered by its own test below.
            "converge-secret-inputs": {
                "status": "already_current",
                "added": {},
                "missing": {},
                "absent": [],
                "applied": False,
            },
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
            payload = {
                "status": "activated",
                "transaction_id": "a" * 32,
                "cutover_mode": "reversible",
                "persistent_state_mutated": False,
            }
        else:
            payload = {"status": "ok"}
        return ProcessResult(0, json.dumps(payload), "")

    def upload(self, source, destination, *, recursive=False):
        self.uploads.append((Path(source), destination, recursive))

    def download(self, source, destination, *, recursive=False):
        self.downloads.append((source, Path(destination), recursive))
        Path(destination).mkdir(parents=True, exist_ok=True)

    def upload_directory_resumable(self, source, destination, *, exclude=()):
        self.resumable_uploads.append((Path(source), destination))


def _stub_input_contract(controller: EidolonPiController) -> None:
    controller.preflight._validate_install_inputs = lambda: {  # type: ignore[method-assign]
        "status": "compatible"
    }


def _app() -> AppAccess:
    return AppAccess(
        lan_ipv4=IPv4Address("192.168.100.15"),
        hub_https_port=8443,
        allow_insecure_livekit=True,
    )


@pytest.fixture
def setup_controller(config):
    config.install_files["host_identity"].write_bytes(b"a" * 32)
    runner = ControllerRunner(config)
    transport = FakeTransport()
    controller = EidolonPiController(config, runner, transport=transport)
    _stub_input_contract(controller)
    return controller, runner, transport


def test_local_preflight_proves_exact_commits(config) -> None:
    controller = EidolonPiController(config, ControllerRunner(config), transport=FakeTransport())
    _stub_input_contract(controller)

    result = controller.local_preflight(require_install_files=True)

    # The evidence is still a full commit object per source; what changed is
    # only where it came from. Nothing downstream — a bundle, a Host record, an
    # audit — may be handed an abbreviation, a tag or a branch name.
    assert result["sources"] == SOURCE_HEADS
    assert set(result["sources"]) == set(SOURCE_IDS)
    assert all(len(value) == 40 for value in result["sources"].values())
    assert result["source_selection"] == {"mode": "repository_head"}
    assert result["install_prerequisites_checked"] is True
    assert result["release_tool_contract"]["tool"] == "eidolon-release"
    assert result["release_tool_contract"]["bundle_schema_version"] == 3
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
    by_action = {call[0]: call[1] for call in transport.agent_calls}
    assert by_action["doctor-host"]["release_id"] == "r1"
    assert [call[0] for call in transport.agent_calls] == [
        "foundation-doctor",
        "doctor-host",
        "app-ready",
    ]


def test_doctor_asks_the_readiness_contract_too(setup_controller) -> None:
    """The two verbs answered different questions and nothing said so.

    On 2026-09-17 the Pi was breaking a claim-window promise it had made,
    `claim_window_honored` was false, and `doctor` answered `healthy` in the
    same minute. Neither was wrong on its own; between them they told an
    operator their Host was fine.
    """

    controller, _runner, _transport = setup_controller

    result = controller.doctor()

    assert result["readiness"]["status"] == "ready"
    assert result["readiness"]["checks"] == ALL_FACTS_TRUE
    assert "failures" not in result["readiness"], "a green report invents no empty list"


def test_doctor_takes_a_snapshot_rather_than_waiting_for_one(setup_controller) -> None:
    """Zero settle. A diagnosis says what is true when it is asked.

    Waiting four minutes for a Channel worker to register is what a release
    cutover should do. Here it would mean the command run *because* something
    looks wrong is the slowest one to answer.
    """

    controller, _runner, transport = setup_controller

    controller.doctor()

    readiness = {call[0]: call[1] for call in transport.agent_calls}["app-ready"]
    assert readiness["readiness"]["channel_worker"]["settle_seconds"] == 0
    # And the verb that is allowed to wait still does.
    controller.app_ready()
    waiting = [call for call in transport.agent_calls if call[0] == "app-ready"][-1][1]
    assert waiting["readiness"]["channel_worker"]["settle_seconds"] > 0


def test_doctor_is_degraded_by_a_broken_promise_and_names_the_verb(
    setup_controller,
) -> None:
    """The whole point: red, and with the command that makes it green."""

    controller, _runner, transport = setup_controller
    transport.overrides["app-ready"] = {
        "status": "degraded",
        "checks": {**ALL_FACTS_TRUE, "claim_window_honored": False},
    }

    result = controller.doctor()

    assert result["status"] == "degraded"
    assert result["readiness"]["status"] == "degraded"
    assert result["readiness"]["failures"] == [
        {
            "fact": "claim_window_honored",
            "means": (
                "this Host holds what its claim-window declaration needs: one "
                "standing a window has the factory Setup code that opens it"
            ),
            "remedy": (
                "run `converge-inputs --apply` to deliver the factory Setup code, "
                "then restart `eidolon-bootstrapd` — the code is read while that "
                "unit initialises, so the Host stands no window until it does"
            ),
        }
    ]


def test_doctor_will_not_call_a_host_healthy_it_could_not_ask(setup_controller) -> None:
    """An unanswerable probe is a finding, not a silence.

    Reported rather than raised, because this is the command someone runs when
    they already suspect something — but it must not come back `healthy`
    either, which would rebuild the green headline this change exists to end.
    """

    controller, _runner, transport = setup_controller
    transport.fail_actions["app-ready"] = RuntimeError("no route to host")

    result = controller.doctor()

    assert result["status"] == "degraded"
    assert result["readiness"]["status"] == "unobserved"
    assert "no route to host" in result["readiness"]["error"]


def test_app_ready_names_what_failed_and_what_to_run(setup_controller) -> None:
    """The same joining, on the verb that always owned the question."""

    controller, _runner, transport = setup_controller
    transport.overrides["app-ready"] = {
        "status": "degraded",
        "checks": {**ALL_FACTS_TRUE, "claim_window_honored": False},
    }

    report = controller.app_ready()

    assert [entry["fact"] for entry in report["failures"]] == ["claim_window_honored"]
    assert "converge-inputs --apply" in report["failures"][0]["remedy"]


def test_a_green_app_ready_carries_no_failures_key(setup_controller) -> None:
    """Absence would otherwise be readable as "not checked"."""

    controller, _runner, _transport = setup_controller

    assert "failures" not in controller.app_ready()


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
    assert result["local"]["product_settings_contract"] == {
        "status": "exact",
        "refreshed": ["agent.yaml", "channel.yaml", "memory.yaml"],
        "sources": {
            "eidolon_agent": SOURCE_HEADS["eidolon_agent"],
            "eidolon_channel": SOURCE_HEADS["eidolon_channel"],
            "eidolon_memory": SOURCE_HEADS["eidolon_memory"],
        },
    }
    assert "thinking: disabled" in controller.config.install_files["agent_settings"].read_text(
        encoding="utf-8"
    )
    channel_settings = controller.config.install_files["channel_settings"].read_text(
        encoding="utf-8"
    )
    assert "dump_wav" not in channel_settings
    assert [phase["phase"] for phase in result["phases"]] == [
        "bundle",
        "release_reclaim_prepare",
        "upload_guard",
        "upload_finalize",
        "release_artifacts",
        # Weights are carried before the release is prepared: a Host that gets
        # the code without them answers memory queries slowly and emptily, and
        # starts a model server that exits for want of a model.
        "component_artifacts",
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
    assert prepare_command[:7] == (
        "/usr/bin/env",
        # Where uv puts an interpreter it has to fetch. Under sudo its default
        # is root's home, which is 0700 and refuses every service user, so the
        # venvs would point at a Python none of them may execute.
        "UV_PYTHON_INSTALL_DIR=/usr/local/lib/eidolon-foundation/python",
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
        payload
        for action, payload, _python, _sudo in transport.agent_calls
        if action == "reclaim-releases"
    )
    assert reclaim["phase"] == "prepare"
    assert reclaim["required_bytes"] > 0
    assert reclaim["reserve_bytes"] == 1024**3
    assert (controller.config.workspace.bundle_root / "r1").is_dir()


def test_deploy_refuses_wireless_before_sealing_or_upload(config) -> None:
    strict = replace(
        config,
        host=replace(config.host, require_wired_release_upload=True),
    )
    transport = FakeTransport()
    transport.endpoint = HostEndpoint(address="192.168.3.40", interface="en0", link="wireless")
    controller = EidolonPiController(
        strict,
        ControllerRunner(strict),
        transport=transport,
    )
    _stub_input_contract(controller)

    with pytest.raises(OperationsError, match="requires a wired endpoint"):
        controller.deploy(release_id="r1", resume=False, activate=False)

    assert not (strict.workspace.bundle_root / "r1").exists()
    assert transport.resumable_uploads == []


def test_deploy_uploads_only_release_artifacts_the_host_reports_missing(config) -> None:
    class ColdArtifactTransport(FakeTransport):
        def run_agent(self, action, payload, **kwargs):
            if action == "release-artifact-state":
                self.agent_calls.append((action, dict(payload), "/usr/bin/python3", True))
                return {
                    "status": "missing",
                    "missing": [payload["artifacts"][0]["sha256"]],
                }
            if action == "guard-release-artifacts":
                self.agent_calls.append((action, dict(payload), "/usr/bin/python3", False))
                return {"status": "ready_for_upload"}
            if action == "finalize-release-artifacts":
                self.agent_calls.append((action, dict(payload), "/usr/bin/python3", True))
                return {"status": "installed"}
            return super().run_agent(action, payload, **kwargs)

    transport = ColdArtifactTransport()
    controller = EidolonPiController(config, ControllerRunner(config), transport=transport)
    _stub_input_contract(controller)

    controller.deploy(release_id="r1", resume=False, activate=False)

    assert len(transport.resumable_uploads) == 2
    assert transport.resumable_uploads[1][1].startswith("/var/tmp/eidolon-artifacts-")
    guard = next(call for call in transport.agent_calls if call[0] == "guard-release-artifacts")
    finalized = next(
        call for call in transport.agent_calls if call[0] == "finalize-release-artifacts"
    )
    assert guard[3] is False
    assert finalized[3] is True
    assert len(guard[1]["artifacts"]) == 1


def test_successful_deploy_removes_current_and_expired_local_bundles(
    setup_controller,
) -> None:
    controller, _runner, _transport = setup_controller
    root = controller.config.workspace.bundle_root
    expired = root / "expired-release"
    expired.mkdir(parents=True)
    expired_manifest = expired / "bundle.json"
    expired_manifest.write_text("{}", encoding="utf-8")
    os.utime(expired_manifest, (1, 1))
    os.utime(expired, (1, 1))
    recent = root / "recent-release"
    recent.mkdir()
    (recent / "bundle.json").write_text("{}", encoding="utf-8")

    result = controller.deploy(release_id="r1", resume=False, activate=True)

    cleanup = result["phases"][-1]
    assert cleanup["phase"] == "local_bundle_cleanup"
    assert cleanup["result"]["status"] == "cleaned"
    assert cleanup["result"]["current"]["status"] == "removed"
    assert [item["path"] for item in cleanup["result"]["expired"]] == [str(expired)]
    assert not (root / "r1").exists()
    assert not expired.exists()
    assert recent.is_dir()


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
        "converge_running_release",
        "doctor",
        "app_ready",
        "release_reclaim_commit",
        "local_bundle_cleanup",
    ]
    assert transport.uploads == []
    assert not any(len(call) > 1 and call[1] == "bundle" for call in runner.calls)
    # The operator's side must never be the first to give up on an activation.
    # A Host can spend its 300s readiness gate and then another 300s waiting for
    # what its rollback restores; at 600 the client gave up first, the remote
    # kept running and kept the flock, and the Host was left holding its own
    # upgrade lock with a candidate marker no later release could clear.
    activate_index = next(
        index
        for index, (remote, _sudo) in enumerate(transport.remote_calls)
        if "deploy" in remote and "--dry-run" not in remote
    )
    assert transport.remote_timeouts[activate_index] >= 1800


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
        "refresh_release",
        lambda release_id, *, cutover_mode="reversible": (
            events.append(f"host application {release_id}") or {"status": "refreshed"}
        ),
    )

    result = controller.deploy(release_id="r1", resume=True, activate=True)

    assert result["status"] == "activated"
    assert events.index("host application r1") < events.index("release activation")
    assert [phase["phase"] for phase in result["phases"]] == [
        "release_reclaim_prepare",
        "dry_run",
        "service_identities",
        "host_cutover_snapshot",
        "host_application",
        "activate",
        "converge_running_release",
        "doctor",
        "app_ready",
        "cutover_receipt",
        "release_reclaim_commit",
        "local_bundle_cleanup",
    ]


def test_forward_only_activation_failure_requires_same_schema_fix_without_abort(
    setup_controller, monkeypatch
) -> None:
    controller, _runner, transport = setup_controller
    controller.host_layer.app = _app()
    monkeypatch.setattr(
        controller.host_layer,
        "refresh_release",
        lambda release_id, *, cutover_mode="reversible": {"status": "refreshed"},
    )
    monkeypatch.setattr(
        controller.releases,
        "_activation_json",
        lambda *args, **kwargs: {
            "status": "forward_fix_required",
            "transaction_id": "a" * 32,
            "cutover_mode": "forward-only",
            "persistent_state_mutated": True,
            "database_migrations": [],
        },
    )

    with pytest.raises(OperationsError, match="same-schema forward fix"):
        controller.deploy(release_id="r1", resume=False, activate=True, cutover_mode="forward-only")

    actions = [action for action, _payload, _python, _sudo in transport.agent_calls]
    assert "release-cutover-finalize" in actions
    assert not any(
        action == "reclaim-releases" and payload.get("phase") == "abort"
        for action, payload, _python, _sudo in transport.agent_calls
    )
    assert "release-cutover-restore" not in actions


def test_forward_only_health_gate_failure_never_restores_old_interpreters(
    setup_controller, monkeypatch
) -> None:
    controller, _runner, transport = setup_controller
    controller.host_layer.app = _app()
    original_run = transport.run

    def degraded(remote, **kwargs):
        if "doctor" in remote:
            return ProcessResult(0, json.dumps({"status": "degraded"}), "")
        return original_run(remote, **kwargs)

    transport.run = degraded
    monkeypatch.setattr(
        controller.host_layer,
        "refresh_release",
        lambda release_id, *, cutover_mode="reversible": {"status": "refreshed"},
    )
    monkeypatch.setattr(
        controller.releases,
        "_activation_json",
        lambda *args, **kwargs: {
            "status": "activated",
            "transaction_id": "a" * 32,
            "cutover_mode": "forward-only",
            "persistent_state_mutated": True,
            "database_migrations": [],
        },
    )

    with pytest.raises(OperationsError, match="same-schema forward fix"):
        controller.deploy(release_id="r1", resume=False, activate=True, cutover_mode="forward-only")

    actions = [action for action, _payload, _python, _sudo in transport.agent_calls]
    assert "release-cutover-finalize" in actions
    assert "release-cutover-restore" not in actions
    assert not any("rollback" in command for command, _sudo in transport.remote_calls)


def test_reversible_activation_failure_restores_host_layer_before_candidate_abort(
    setup_controller, monkeypatch
) -> None:
    controller, _runner, transport = setup_controller
    controller.host_layer.app = _app()
    monkeypatch.setattr(
        controller.host_layer,
        "refresh_release",
        lambda release_id, *, cutover_mode="reversible": {"status": "refreshed"},
    )

    def fail_activation(*args, **kwargs):
        raise RuntimeError("activation failed before persistent mutation")

    monkeypatch.setattr(controller.releases, "_activation_json", fail_activation)

    with pytest.raises(RuntimeError, match="before persistent mutation"):
        controller.deploy(release_id="r1", resume=False, activate=True)

    actions = [action for action, _payload, _python, _sudo in transport.agent_calls]
    restore_index = actions.index("release-cutover-restore")
    abort_index = next(
        index
        for index, (action, payload, _python, _sudo) in enumerate(transport.agent_calls)
        if action == "reclaim-releases" and payload.get("phase") == "abort"
    )
    assert restore_index < abort_index


def test_forward_only_failure_before_barrier_restores_host_layer_and_aborts(
    setup_controller, monkeypatch
) -> None:
    controller, _runner, transport = setup_controller
    controller.host_layer.app = _app()
    monkeypatch.setattr(
        controller.host_layer,
        "refresh_release",
        lambda release_id, *, cutover_mode="reversible": {"status": "refreshed"},
    )

    def fail_before_barrier(*args, **kwargs):
        raise RuntimeError("quiesce failed before candidate start")

    monkeypatch.setattr(controller.releases, "_activation_json", fail_before_barrier)

    with pytest.raises(RuntimeError, match="before candidate start"):
        controller.deploy(release_id="r1", resume=False, activate=True, cutover_mode="forward-only")

    assert any(
        action == "release-cutover-restore"
        for action, _payload, _python, _sudo in transport.agent_calls
    )
    assert any(
        action == "reclaim-releases" and payload.get("phase") == "abort"
        for action, payload, _python, _sudo in transport.agent_calls
    )


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
    _host_established(controller, transport)

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


def test_deploy_records_a_degraded_app_probe_without_refusing(config) -> None:
    """The deadlock, stated as a test.

    The App probe asks whether a phone could finish setting this Host up.
    Finishing needs a Workspace; a Workspace needs somebody to claim the Host
    with a phone; claiming needs the release installed. A factory-fresh machine
    could not pass its own last gate, and on the release path a degraded probe
    rolled a Host back to a release that would not start at all. So it is
    observed and written down, and it refuses nothing.
    """

    transport = FakeTransport()
    original = transport.run_agent

    def degraded(action, payload, **kwargs):
        if action == "app-ready":
            return {"status": "degraded", "checks": [{"name": "workspace", "ok": False}]}
        return original(action, payload, **kwargs)

    transport.run_agent = degraded
    controller = EidolonPiController(config, ControllerRunner(config), transport=transport)

    result = controller.deploy(release_id="r1", resume=True, activate=True)

    assert result["status"] == "activated"
    assert not any("rollback" in call for call, _sudo in transport.remote_calls)
    app_phase = next(phase for phase in result["phases"] if phase["phase"] == "app_ready")
    assert app_phase["result"]["status"] == "degraded"


def test_deploy_records_an_unreachable_app_probe_without_refusing(config) -> None:
    """An unreachable probe is not a refusal wearing a different name."""

    transport = FakeTransport()
    original = transport.run_agent

    def unreachable(action, payload, **kwargs):
        if action == "app-ready":
            raise OperationsError("host agent app-ready failed: connection reset")
        return original(action, payload, **kwargs)

    transport.run_agent = unreachable
    controller = EidolonPiController(config, ControllerRunner(config), transport=transport)

    result = controller.deploy(release_id="r1", resume=True, activate=True)

    assert result["status"] == "activated"
    assert not any("rollback" in call for call, _sudo in transport.remote_calls)
    app_phase = next(phase for phase in result["phases"] if phase["phase"] == "app_ready")
    assert app_phase["result"]["status"] == "unobserved"
    assert "connection reset" in app_phase["result"]["error"]


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

    rollback = next(call for call, _sudo in transport.remote_calls if "rollback" in call)
    assert rollback[-1] == "/var/lib/eidolon/deployments/r1-" + "a" * 32
    actions = [action for action, *_rest in transport.agent_calls]
    # Cleanup first, explanation after: the candidate has to be released before
    # the failure is annotated, never the other way round.
    assert actions[-2:] == ["reclaim-releases", "release-sources"]
    reclaim = next(
        payload
        for action, payload, *_rest in reversed(transport.agent_calls)
        if action == "reclaim-releases"
    )
    assert reclaim["phase"] == "abort"
    # The App probe is observed after the gate, so a failing gate never reaches
    # it — and it can no longer be the thing that rolled a Host back.
    assert not any(call[0] == "app-ready" for call in transport.agent_calls)


def test_deploy_reports_health_gate_and_rollback_failure(config) -> None:
    transport = FakeTransport()
    original = transport.run

    def degraded(remote, **kwargs):
        if "doctor" in remote:
            return ProcessResult(0, json.dumps({"status": "degraded"}), "")
        return original(remote, **kwargs)

    transport.run = degraded
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
    original_remote = transport.run

    def degraded_then_invalid_recovery(remote, **kwargs):
        if "doctor" in remote:
            return ProcessResult(0, json.dumps({"status": "degraded"}), "")
        if "rollback" in remote:
            return ProcessResult(0, json.dumps({"status": "unknown"}), "")
        return original_remote(remote, **kwargs)

    transport.run = degraded_then_invalid_recovery
    controller = EidolonPiController(config, ControllerRunner(config), transport=transport)

    with pytest.raises(OperationsError, match="invalid recovery evidence"):
        controller.deploy(release_id="r1", resume=True, activate=True)


def test_deploy_refuses_existing_local_bundle(config) -> None:
    bundle = config.workspace.bundle_root / "r1"
    bundle.mkdir(parents=True)
    controller = EidolonPiController(config, ControllerRunner(config), transport=FakeTransport())

    with pytest.raises(OperationsError, match="already exists"):
        controller.deploy(release_id="r1", resume=False, activate=False)


def test_deploy_refuses_a_host_short_of_a_declared_credential(
    setup_controller,
) -> None:
    """The gate that was missing, and the two weeks it would have saved.

    Two credentials were added to the product on 2026-08-25. The Host installed
    on 2026-08-10 could not be given them, so every memory and conversation
    feature answered 503 — while releases kept shipping onto it and reporting
    success, because nothing on the deploy path ever asked whether the Host held
    what the code it was receiving required.

    Refused before anything is prepared or transferred, and the message names
    the verb that fixes it: a gate that refuses without saying what to run is a
    gate people learn to route around.
    """

    controller, _runner, transport = setup_controller
    transport.overrides["converge-secret-inputs"] = {
        "status": "planned",
        "added": {},
        "missing": {
            "admin.env": ["EIDOLON_ADMIN_MEMORY_API_SERVICE_TOKEN"],
            "memory.env": ["EIDOLON_MEMORY_API_TOKEN"],
        },
        "absent": [],
        "applied": False,
    }

    with pytest.raises(OperationsError) as refused:
        controller.deploy(release_id="r1", resume=False, activate=True)

    message = str(refused.value)
    assert "EIDOLON_ADMIN_MEMORY_API_SERVICE_TOKEN" in message
    assert "EIDOLON_MEMORY_API_TOKEN" in message
    assert "converge-inputs --apply" in message
    # Nothing was moved. The refusal is the whole operation.
    assert not any(call[0] in {"guard-upload", "finalize-upload"} for call in transport.agent_calls)


def test_deploy_asks_the_host_rather_than_the_workstation(setup_controller) -> None:
    """What decides whether this release works is what is in /etc/eidolon.

    The workstation's input set is the source a Host is converged *from*, and it
    is checked where it is written and where it is repaired. Checking it here
    instead would pass on a machine whose files were fixed and whose Host was
    never given them — which is precisely the state that produced the outage.
    """

    controller, _runner, transport = setup_controller
    controller.deploy(release_id="r1", resume=False, activate=False)

    asked = [call for call in transport.agent_calls if call[0] == "converge-secret-inputs"]
    assert len(asked) == 1
    payload = asked[0][1]
    assert payload["apply"] is False, "a gate must not write"
    assert "admin.env" in payload["declared"]
    assert "EIDOLON_ADMIN_MEMORY_API_SERVICE_TOKEN" in payload["declared"]["admin.env"]


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
    # Every settings template is read out of a commit, and each repository is
    # asked which commit that is exactly once: a second answer during one
    # operation is the failure mode this replaced.
    heads = [call for call in runner.calls if call[-2:] == ("rev-parse", "HEAD")]
    assert len(heads) == len(set(heads)) == len(SOURCE_IDS)
    reads = [call for call in runner.calls if "show" in call]
    assert {call[-1].split(":", 1)[0] for call in reads} <= set(SOURCE_HEADS.values())
    # Derived settings follow the pinned commits; credentials are not reissued.
    assert result["refreshed_settings"] == ["agent.yaml", "channel.yaml", "memory.yaml"]


def test_install_apply_stages_exact_files_and_cleans(setup_controller) -> None:
    controller, _runner, transport = setup_controller

    result = controller.install(release_id="r1", resume=False, apply=True)

    assert result["status"] == "installed"
    assert result["phases"][-1]["phase"] == "local_bundle_cleanup"
    assert result["phases"][-1]["result"]["current"]["status"] == "removed"
    assert not (controller.config.workspace.bundle_root / "r1").exists()
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


def test_local_bundle_cleanup_failure_does_not_change_successful_install(
    setup_controller, monkeypatch
) -> None:
    controller, _runner, _transport = setup_controller
    original = controller.bundles._remove_local_bundle

    def fail_current(release_id: str):
        if release_id == "r1":
            return {
                "status": "cleanup_failed",
                "path": str(controller.config.workspace.bundle_root / release_id),
                "error": "busy",
            }
        return original(release_id)

    monkeypatch.setattr(controller.bundles, "_remove_local_bundle", fail_current)

    result = controller.install(release_id="r1", resume=False, apply=True)

    assert result["status"] == "installed"
    cleanup = result["phases"][-1]["result"]
    assert cleanup["status"] == "cleanup_incomplete"
    assert cleanup["failures"][0]["error"] == "busy"


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
        "PAIRING_JWT_SECRET=test-token\n",
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
                    "  owner_domain_generation: 1\n"
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
    _host_established(controller, transport)

    controller.host_layer.stage_install_files("host-bound", stage)
    payload = controller.host_layer.target_payload()

    assert len(transport.uploaded_bytes) == (
        len(config.install_files) + len(HOST_APPLICATION_STAGE_NAMES)
    )
    # This profile names no Setup code, so nothing was staged for it. That
    # absence is the switch: an unclaimed Host with no code file stands up no
    # claim window, which is what keeps a development fleet sharing one code
    # from being claimable by anyone in Bluetooth range (ADR-0007).
    assert not [
        key for key in transport.uploaded_bytes if key.endswith("/factory_setup_code")
    ]

    # And with one named, it is delivered — rendered from the profile rather
    # than collected as a second copy of a value it already holds.
    coded = EidolonPiController(
        config,
        Runner(config),
        transport=CapturingTransport(),
        app=dataclasses.replace(_app(), setup_code="48213097"),
    )
    _host_established(coded, coded.transport)
    coded.host_layer.stage_install_files("host-bound", stage)
    delivered = {
        key: value
        for key, value in coded.transport.uploaded_bytes.items()
        if key.endswith("/factory_setup_code")
    }
    assert list(delivered.values()) == [b"48213097\n"]
    app = payload["app"]
    assert isinstance(app, dict)
    assert str(app["owner_domain_id"]).startswith("owner-")
    assert str(app["hub_hostname"]).endswith(".local")
    assert str(app["hub_origin"]).endswith(":8443")
    rendered_local = transport.uploaded_bytes[f"{stage}/local-api.env"].decode()
    rendered_hub = transport.uploaded_bytes[f"{stage}/hub.generated.yaml"].decode()
    assert f"EIDOLON_LOCAL_API_OWNER_DOMAIN_ID={app['owner_domain_id']}" in rendered_local
    assert f"owner_domain_id: {app['owner_domain_id']}" in rendered_hub
    assert (
        f"descriptor_uri: {app['hub_origin']}/api/device-onboarding/v1/descriptor" in rendered_hub
    )
    assert f"{stage}/owner-domain-root.key.pem" not in transport.uploaded_bytes
    assert f"{stage}/authority-signing.key.pem" not in transport.uploaded_bytes
    assert b"--listen-port 8443" in transport.uploaded_bytes[f"{stage}/hub-ingress.service"]

    transport.uploaded_bytes.clear()
    controller.host_layer.refresh("host-bound")
    assert {
        f"{stage}/agent.yaml",
        f"{stage}/channel.yaml",
        f"{stage}/memory.yaml",
    } <= set(transport.uploaded_bytes)


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
    assert [phase["phase"] for phase in result["phases"][:2]] == ["replacement_inputs", "reset_existing"]


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

    A behaviour fix on the workstation does nothing unless the commit that ships
    contains it, and without this check the mismatch only surfaces after a full
    install has already run on the target.
    """

    controller = EidolonPiController(
        config,
        ControllerRunner(config, release_contract_overrides={"package_digest": "0" * 64}),
        transport=FakeTransport(),
    )

    with pytest.raises(OperationsError, match="not the one this release will ship"):
        controller.local_preflight(require_install_files=False)


def test_preflight_refuses_an_activator_from_another_checkout(config) -> None:
    """One of the two premises that used to make the digest agree by itself.

    Once the shipped commit is the Kernel checkout's own HEAD, an activator
    taken out of that checkout matches the shipped tree for free — so the check
    has to say that it *is* that checkout rather than assume it.
    """

    class Elsewhere(ControllerRunner):
        def run(self, command, **kwargs):
            if tuple(command)[-1] == "--show-toplevel":
                return ProcessResult(0, "/somewhere/else/eidolon_kernel\n", "")
            return super().run(command, **kwargs)

    controller = EidolonPiController(config, Elsewhere(config), transport=FakeTransport())

    with pytest.raises(OperationsError, match="not inside the eidolon_kernel checkout"):
        controller.local_preflight(require_install_files=False)


def test_preflight_refuses_an_uncommitted_fix_to_the_shipped_activator(config) -> None:
    """The other premise, and the mistake the whole check exists for.

    An edit to eidolon_deploy that was never committed cannot be in the archive
    the release is sealed from, so running it here proves nothing about what
    will run on the board.
    """

    controller = EidolonPiController(
        config,
        ControllerRunner(config, dirty={"eidolon_deploy": " M eidolon_deploy/bundle.py\n"}),
        transport=FakeTransport(),
    )

    with pytest.raises(OperationsError, match="uncommitted changes under eidolon_deploy"):
        controller.local_preflight(require_install_files=False)


def test_preflight_refuses_a_shipped_commit_without_the_deploy_package(config) -> None:
    class EmptyTree(ControllerRunner):
        def run(self, command, **kwargs):
            if "ls-tree" in tuple(command):
                return ProcessResult(0, "", "")
            return super().run(command, **kwargs)

    controller = EidolonPiController(config, EmptyTree(config), transport=FakeTransport())

    with pytest.raises(OperationsError, match="no eidolon_deploy package"):
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

    with pytest.raises(OperationsError, match="tag no longer names the resolved commit"):
        controller.local_preflight(require_install_files=False)


def test_a_tag_that_still_resolves_is_accepted(config) -> None:
    from dataclasses import replace as _replace

    revision = SOURCE_HEADS["eidolon_kernel"]
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


def test_which_setup_code_gets_named(setup_controller) -> None:
    """Explicit beats the profile, the profile beats nothing, nothing means
    the Host draws one.

    Pinning the value is the whole of what a profile code does — the Host still
    opens one ordinary session for it — so what matters here is only which of
    the three values travels.
    """

    controller, _runner, _transport = setup_controller

    controller.app = None
    assert controller._configured_setup_code() is None

    controller.app = replace(_app(), setup_code=None)
    assert controller._configured_setup_code() is None

    controller.app = replace(_app(), setup_code="99999990")
    assert controller._configured_setup_code() == "99999990"


def test_a_setup_code_and_a_boundary_action_reach_the_host(setup_controller) -> None:
    controller, _runner, _transport = setup_controller

    assert controller.commissioning_code()["status"] == "issued"
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


def test_state_a_reset_could_not_reach_stops_the_reset(setup_controller, config) -> None:
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


def test_a_fully_declared_reset_reports_what_each_component_loses(setup_controller, config) -> None:
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


def test_the_report_is_about_what_will_actually_be_removed(setup_controller, config) -> None:
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


def test_a_databases_own_sidecars_belong_to_whoever_declared_it(setup_controller, config) -> None:
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


def _dirty_controller(config, **arguments):
    controller = EidolonPiController(
        config,
        ControllerRunner(config, dirty={"eidolon_sdk": " M eidolon_sdk/session.py\n"}),
        transport=FakeTransport(),
        **arguments,
    )
    _stub_input_contract(controller)
    return controller


def test_a_deploy_refuses_to_seal_a_release_out_of_an_uncommitted_change(config) -> None:
    """The one invariant a single copy of the truth still has.

    A release is sealed with `git archive`, so an uncommitted change is not in
    it: the tree that was tested is not the tree that ships. This is not the
    old HEAD-equals-pin check renamed — that one compared two declarations.
    """

    controller = _dirty_controller(config)

    with pytest.raises(OperationsError, match="sealed from commits") as failure:
        controller.deploy(release_id="r1", resume=False, activate=False)
    assert "eidolon_sdk" in str(failure.value)
    assert "--allow-dirty" in str(failure.value)


def test_a_doctor_reports_a_dirty_worktree_instead_of_refusing(config) -> None:
    """Diagnosis reports; shipping refuses.

    A doctor that will not answer because a sibling repository has an edited
    test file cannot diagnose the thing it was asked about.
    """

    controller = _dirty_controller(config)

    result = controller.doctor()

    assert result["local"]["source_provenance"]["eidolon_sdk"]["dirty"] is True
    assert result["local"]["source_provenance"]["eidolon_hub"]["dirty"] is False


def test_allow_dirty_ships_the_committed_head_and_tells_the_host_it_did(config) -> None:
    """The escape hatch has to leave a trace where the release lives.

    The dirty bytes are not in the bundle either way; what matters afterwards
    is being able to see that the release was sealed beside changes nobody
    committed, and an operator's terminal is not where that survives.
    """

    controller = _dirty_controller(config, allow_dirty=True)
    controller.host_layer.app = _app()
    config.install_files["host_identity"].write_bytes(b"a" * 32)
    controller.host_layer.refresh_release = (
        lambda release_id, *, cutover_mode="reversible": {"status": "refreshed"}
    )
    controller.releases._app_ready = lambda: {"status": "app_ready"}

    controller.deploy(release_id="r1", resume=True, activate=True)

    snapshot = next(
        payload
        for action, payload, _python, _sudo in controller.transport.agent_calls
        if action == "release-cutover-snapshot"
    )
    assert snapshot["sources"]["eidolon_sdk"]["dirty"] is True
    assert snapshot["sources"]["eidolon_sdk"]["revision"] == SOURCE_HEADS["eidolon_sdk"]
    assert snapshot["sources"]["eidolon_hub"]["pinned"] is False


def test_an_install_tells_the_host_which_commits_it_is_installing(setup_controller) -> None:
    controller, _runner, transport = setup_controller

    controller.install(release_id="r1", resume=False, apply=True)

    payload = next(
        payload for action, payload, _p, _s in transport.agent_calls if action == "install"
    )
    assert payload["sources"] == {
        source_id: {
            "revision": revision,
            "head": revision,
            "branch": "main",
            "pinned": False,
            "dirty": False,
        }
        for source_id, revision in SOURCE_HEADS.items()
    }


def test_a_failed_deploy_names_how_far_each_repository_moved(config) -> None:
    """The incident's error was `readiness timeout: hub, kernel, local-api`.

    A combination of commits that is not self-consistent cannot be detected
    before it runs, so what has to improve is the failure: from nothing to
    "these repositories moved since the release that worked".
    """

    class Counting(ControllerRunner):
        def run(self, command, **kwargs):
            command = tuple(command)
            if "rev-list" in command:
                counts = {"eidolon_sdk": "0\t1", "eidolon_admin": "0\t6"}
                return ProcessResult(0, counts.get(self._source(command), "0\t0") + "\n", "")
            return super().run(command, **kwargs)

    transport = FakeTransport()
    transport.overrides["release-sources"] = {
        "status": "observed",
        "releases": [
            {
                "release_id": "last-good",
                "status": "activated",
                "sources": {
                    "eidolon_sdk": {"revision": "d" * 40},
                    "eidolon_admin": {"revision": "e" * 40},
                },
            }
        ],
    }
    transport.fail_remote_match = "--dry-run"
    controller = EidolonPiController(config, Counting(config), transport=transport)
    _stub_input_contract(controller)

    with pytest.raises(OperationsError, match="since release last-good") as failure:
        controller.deploy(release_id="r1", resume=False, activate=False)
    message = str(failure.value)
    assert "eidolon_admin +6" in message
    assert "eidolon_sdk +1" in message
    # The failure it is explaining must still be the failure.
    assert "--dry-run" in message


def test_a_failed_deploy_without_release_history_is_left_alone(config) -> None:
    """A diagnostic that cannot answer must not replace the real error."""

    transport = FakeTransport()
    transport.overrides["release-sources"] = {"status": "observed", "releases": []}
    transport.fail_remote_match = "--dry-run"
    controller = EidolonPiController(config, ControllerRunner(config), transport=transport)
    _stub_input_contract(controller)

    with pytest.raises(RuntimeError, match="failed: --dry-run") as failure:
        controller.deploy(release_id="r1", resume=False, activate=False)
    assert "since release" not in str(failure.value)


def test_resuming_a_bundle_after_a_commit_names_both_ways_out(config) -> None:
    """A release id is one exact combination, so this refusal is correct.

    It is also newly easy to hit: HEAD moves by committing, and the operator's
    instinct on "drifted" is to delete the directory. The message has to name
    the two things that actually continue the work.
    """

    sealed = EidolonPiController(config, ControllerRunner(config), transport=FakeTransport())
    _stub_input_contract(sealed)
    sealed.deploy(release_id="r1", resume=False, activate=False)

    moved = EidolonPiController(
        config,
        ControllerRunner(config, heads={"eidolon_hub": "9" * 40}),
        transport=FakeTransport(),
    )
    _stub_input_contract(moved)

    with pytest.raises(OperationsError, match="sealed from a different commit of eidolon_hub") as f:
        moved.deploy(release_id="r1", resume=True, activate=False)
    assert "--revision eidolon_hub=" + SOURCE_HEADS["eidolon_hub"] in str(f.value)
    assert "--release-id" in str(f.value)


# -- the Owner Authority an install carries -------------------------------------
#
# The Host is the only ledger of what Authority it has established. An install
# used to compare a generation this side kept against the Host's first, and
# consult the hardware delivery binding — the one fact that could say whether
# this is even the same board — last. On 2026-09-10 that let one Owner's
# material commission a second board without a word, and then refused the first
# board with a sentence naming only a backup nobody had and a factory reset.
# Now this side keeps no copy: it asks the Host what it holds, asks its own
# record which board it delivered to, names the situation, and renders for what
# the Host holds.

_BOARD = {"kind": "device-tree:raspberrypi,5-model-b", "fingerprint": "sha256:" + "a" * 64}
_OTHER_BOARD = {"kind": "device-tree:raspberrypi,5-model-b", "fingerprint": "sha256:" + "f" * 64}


def _authority_controller(config, transport=None) -> EidolonPiController:
    identity = config.install_files["host_identity"]
    identity.write_bytes(b"a" * 32)
    identity.chmod(0o600)
    config.install_files["local_api_env"].write_text(
        "EIDOLON_LOCAL_API_ADMIN_BASE_URL=http://127.0.0.1:9000\n", encoding="utf-8"
    )
    config.install_files["channel_env"].write_text(
        "PAIRING_JWT_SECRET=test-token\n", encoding="utf-8"
    )
    controller = EidolonPiController(
        config,
        ControllerRunner(config),
        transport=transport if transport is not None else FakeTransport(),
        app=_app(),
    )
    _stub_input_contract(controller)
    return controller


def _material_root(controller: EidolonPiController) -> Path:
    return controller.host_layer.materializer().material_root


def _material_bytes(controller: EidolonPiController) -> dict[str, bytes]:
    root = _material_root(controller)
    return {p.name: p.read_bytes() for p in root.iterdir()} if root.exists() else {}


def _keys(controller: EidolonPiController) -> dict[str, bytes]:
    root = _material_root(controller)
    return {
        name: (root / name).read_bytes()
        for name in ("owner-domain-root.key.pem", "authority-signing.key.pem", "hub.key", "hub.crt")
    }


def _host_id(controller: EidolonPiController) -> str:
    return controller.host_layer.materializer().identity().host_id


def _identity_sha256(controller: EidolonPiController) -> str:
    return hashlib.sha256(controller.config.install_files["host_identity"].read_bytes()).hexdigest()


def _binding_path(controller: EidolonPiController) -> Path:
    return _material_root(controller).parent / "host_delivery.json"


def _lineage(
    controller: EidolonPiController, *, generation: int = 8, state_id: str = "authority-state_board"
) -> dict[str, object]:
    return {
        "contract_version": 1,
        "owner_domain_id": controller.host_layer.materializer().owner_domain_id(),
        "owner_domain_generation": generation,
        "state_id": state_id,
    }


def _directory_at(controller: EidolonPiController, lineage: dict[str, object]) -> bytes:
    """The signed directory a Host at this lineage serves: this Owner's, at that generation."""

    materializer = controller.host_layer.materializer()
    return controller_module.ensure_owner_domain_assets(
        materializer.material_root,
        materializer.identity(),
        8443,
        HostAuthority.established_from(lineage),
    ).descriptor


def _host_reports(
    transport: FakeTransport,
    *,
    lineage: dict[str, object] | None = None,
    directory: bytes | None = None,
    hardware: dict[str, str] = _BOARD,
    identity_sha256: str | None = None,
    marker=...,
    anchor=...,
) -> None:
    """Say what the Host answers when asked what it holds."""

    marker = lineage if marker is ... else marker
    anchor = lineage if anchor is ... else anchor
    transport.overrides["install-context"] = {
        "status": "observed",
        "marker": marker,
        "anchor": anchor,
        "established": marker if marker is not None and marker == anchor else None,
        "directory": None if directory is None else directory.decode("utf-8"),
        "hardware": hardware,
        "identity_sha256": identity_sha256,
    }


def _bind(controller: EidolonPiController, hardware: dict[str, str]) -> None:
    bind_delivery(_material_root(controller).parent, _host_id(controller), hardware, allow_create=True)


def _host_established(
    controller: EidolonPiController, transport: FakeTransport, *, generation: int = 8, bound: bool = True
) -> dict[str, object]:
    """A Host that stood this Owner's Authority up and serves the directory for it.

    Bound by default: it is the board this profile delivered its identity to.
    Unbound is a Host from before delivery bindings were written.
    """

    lineage = _lineage(controller, generation=generation)
    directory = _directory_at(controller, lineage)
    _host_reports(
        transport, lineage=lineage, directory=directory, identity_sha256=_identity_sha256(controller)
    )
    if bound:
        _bind(controller, _BOARD)
    return lineage


class _StagingTransport(FakeTransport):
    """Answers `install` the way a Host does: with the lineage the shipped capability named."""

    def __init__(self) -> None:
        super().__init__()
        self.staged: dict[str, bytes] = {}

    def run_agent(self, action, payload, **keywords):
        if action == "reset-host":
            # A wiped Host holds nothing again, and the same board answers.
            self.overrides.pop("install-context", None)
        if action == "install":
            self.agent_calls.append((action, dict(payload), "", True))
            staged = json.loads(
                self.staged[f"/var/tmp/eidolon-secrets-{payload['release_id']}/authority-bootstrap.json"]
            )
            return {
                "status": "installed",
                "authority": {key: value for key, value in staged.items() if key != "operation"},
            }
        return super().run_agent(action, payload, **keywords)

    def upload(self, source, destination, *, recursive=False):
        super().upload(source, destination, recursive=recursive)
        if not recursive:
            self.staged[destination] = Path(source).read_bytes()


def test_a_host_holding_nothing_gets_a_fresh_capability_at_generation_one(config) -> None:
    transport = FakeTransport()
    controller = _authority_controller(config, transport)
    root = _material_root(controller)

    plan = controller.authority_capability(will_wipe=False, apply=False)

    assert plan["decision"] == "first_install"
    assert plan["owner_domain_generation"] == 1
    assert plan["state_id_provisional"] is True
    assert plan["directory"] == "issued"
    # A plan decides. It issues no directory and mints nothing that lasts.
    assert not (root / "owner-domain-descriptor.json").exists()

    applied = controller.authority_capability(will_wipe=False, apply=True)

    assert applied["decision"] == "first_install"
    assert applied["state_id_provisional"] is False
    bootstrap = json.loads(controller.host_layer.materializer().owner_assets().authority_bootstrap)
    assert bootstrap["operation"] == "owner-authority.bootstrap"
    assert bootstrap["owner_domain_generation"] == 1
    assert bootstrap["state_id"] == applied["lineage"]["state_id"]
    issued = json.loads((root / "owner-domain-descriptor.json").read_bytes())
    assert issued["owner_domain_generation"] == 1
    assert not (root / "owner-domain-state.json").exists()


def test_the_board_this_identity_went_to_is_continued_at_what_it_established(config) -> None:
    transport = FakeTransport()
    controller = _authority_controller(config, transport)
    established = _host_established(controller, transport, generation=8)
    before = _material_bytes(controller)

    for apply in (False, True):
        capability = controller.authority_capability(will_wipe=False, apply=apply)
        assert capability["decision"] == "continue_established_lineage"
        assert capability["owner_domain_generation"] == 8
        assert capability["lineage"] == established
        assert capability["delivery"] == "verified"
        assert capability["directory"] == "current"
    assert _material_bytes(controller) == before
    assert json.loads(
        controller.host_layer.materializer().owner_assets().authority_bootstrap
    )["operation"] == "owner-authority.bootstrap-consumed"


def test_a_second_board_is_refused_at_the_first_gate_and_no_generation_moves(config) -> None:
    """The 2026-09-10 shape, met the way it should have been.

    This profile delivered its identity to the board on the bench. A blank
    second board answers at the same name. It used to pass the Authority gate
    — nothing on it to disagree with — and be commissioned at a new generation.
    Now the binding is the first question, and the refusal names the board.
    """

    transport = FakeTransport()
    controller = _authority_controller(config, transport)
    _host_established(controller, transport)
    _host_reports(transport, hardware=_OTHER_BOARD)
    before = _material_bytes(controller)

    for apply in (False, True):
        with pytest.raises(OperationsError, match="WRONG_BOARD") as refused:
            controller.authority_capability(will_wipe=False, apply=apply)
        assert "sha256:" + "f" * 64 in str(refused.value)
        assert "trust-host-authority" not in str(refused.value)

    assert _material_bytes(controller) == before
    assert not transport.uploads


def test_the_hosts_own_directory_replaces_a_stale_one_this_side_holds(config) -> None:
    """What that second board left behind, met without a confirmation step.

    This material names generation 9 and holds a directory at it; the board on
    the bench established 8 and serves the directory devices hold. The board's
    directory is signed by this Owner root, so it is adopted as it stands, and
    the record this side used to keep is retired. Nothing to name, nothing to
    confirm: this side no longer holds an opinion to reconcile.
    """

    transport = FakeTransport()
    controller = _authority_controller(config, transport)
    kept = _host_established(controller, transport, generation=8)
    root = _material_root(controller)
    kept_directory = (root / "owner-domain-descriptor.json").read_bytes()
    keys = _keys(controller)
    _directory_at(controller, _lineage(controller, generation=9, state_id="authority-state_the-other-board"))
    legacy = root / "owner-domain-state.json"
    legacy.write_text('{"owner_domain_generation": 9}\n', encoding="utf-8")
    legacy.chmod(0o600)
    assert json.loads((root / "owner-domain-descriptor.json").read_bytes())["owner_domain_generation"] == 9

    plan = controller.authority_capability(will_wipe=False, apply=False)

    assert plan["decision"] == "continue_established_lineage"
    assert plan["lineage"] == kept
    assert plan["directory"] == "adopted from the Host"
    assert plan["legacy_state_removed"] is True
    assert not legacy.exists()
    # A plan writes no directory: the stale one is still there to be replaced.
    assert json.loads((root / "owner-domain-descriptor.json").read_bytes())["owner_domain_generation"] == 9

    applied = controller.authority_capability(will_wipe=False, apply=True)

    assert applied["lineage"] == kept
    assert applied["directory"] == "adopted from the Host"
    assert (root / "owner-domain-descriptor.json").read_bytes() == kept_directory
    assert _keys(controller) == keys
    assert not transport.uploads


def test_an_undelivered_newer_revision_is_kept_over_the_one_the_host_serves(config) -> None:
    transport = FakeTransport()
    controller = _authority_controller(config, transport)
    kept = _host_established(controller, transport, generation=8)
    root = _material_root(controller)
    served = (root / "owner-domain-descriptor.json").read_bytes()
    # The endpoint moved, so this side issued the next revision and has not
    # delivered it yet; the Host still serves the one above.
    materializer = controller.host_layer.materializer()
    issued = controller_module.ensure_owner_domain_assets(
        root, materializer.identity(), 9443, HostAuthority.established_from(kept)
    ).descriptor
    assert json.loads(issued)["directory_revision"] == json.loads(served)["directory_revision"] + 1
    controller.host_layer.app = dataclasses.replace(_app(), hub_https_port=9443)

    applied = controller.authority_capability(will_wipe=False, apply=True)

    assert applied["directory"] == "delivering this material's newer revision"
    assert (root / "owner-domain-descriptor.json").read_bytes() == issued


def test_a_legacy_host_that_proves_it_holds_the_identity_has_its_delivery_recorded_by_install(
    config,
) -> None:
    transport = FakeTransport()
    controller = _authority_controller(config, transport)
    established = _host_established(controller, transport, bound=False)
    transport.overrides["install"] = {"status": "installed", "authority": established}
    assert not _binding_path(controller).exists()

    plan = controller.install(release_id="r1", resume=False, apply=False)
    assert plan["authority"]["decision"] == "adopt_delivery_evidence"
    assert not _binding_path(controller).exists()

    result = controller.install(release_id="r1", resume=False, apply=True)

    assert result["authority_established"] == {
        "status": "authority_established",
        "authority": established,
        "delivery": "recorded",
    }
    verify_binding(_binding_path(controller).read_bytes(), _host_id(controller), _BOARD)


def test_a_first_install_records_the_board_once_the_host_proves_what_it_established(config) -> None:
    transport = _StagingTransport()
    controller = _authority_controller(config, transport)

    result = controller.install(release_id="r1", resume=False, apply=True)

    staged = json.loads(transport.staged["/var/tmp/eidolon-secrets-r1/authority-bootstrap.json"])
    assert staged["operation"] == "owner-authority.bootstrap"
    assert staged["owner_domain_generation"] == 1
    assert result["authority"]["decision"] == "first_install"
    assert result["authority"]["lineage"] == {
        key: value for key, value in staged.items() if key != "operation"
    }
    assert result["authority_established"]["delivery"] == "recorded"
    verify_binding(_binding_path(controller).read_bytes(), _host_id(controller), _BOARD)
    # Nothing on this side remembers the generation or the state id.
    assert not (_material_root(controller) / "owner-domain-state.json").exists()


def test_an_install_refuses_to_take_a_hosts_word_for_a_lineage_it_did_not_ship(config) -> None:
    transport = FakeTransport()
    controller = _authority_controller(config, transport)
    transport.overrides["install"] = {
        "status": "installed",
        "authority": {**_lineage(controller, generation=99), "state_id": "authority-state_x"},
    }

    with pytest.raises(OperationsError, match="did not establish the Owner Authority"):
        controller.install(release_id="r1", resume=False, apply=True)

    # The binding records a delivery that happened; this one did not.
    assert not _binding_path(controller).exists()


def test_the_delivered_board_holding_nothing_is_refused_as_lost_not_reinstalled(config) -> None:
    transport = FakeTransport()
    controller = _authority_controller(config, transport)
    _host_established(controller, transport)
    _host_reports(transport)
    before = _material_bytes(controller)

    for apply in (False, True):
        with pytest.raises(OperationsError, match="AUTHORITY_LOST"):
            controller.authority_capability(will_wipe=False, apply=apply)

    assert _material_bytes(controller) == before


@pytest.mark.parametrize("missing", ["marker", "anchor"])
def test_a_host_whose_two_copies_disagree_is_an_incident(config, missing) -> None:
    transport = FakeTransport()
    controller = _authority_controller(config, transport)
    lineage = _host_established(controller, transport)
    _host_reports(
        transport,
        lineage=lineage,
        directory=(_material_root(controller) / "owner-domain-descriptor.json").read_bytes(),
        identity_sha256=_identity_sha256(controller),
        **{missing: None},
    )
    before = _material_bytes(controller)

    with pytest.raises(OperationsError, match="AUTHORITY_RECOVERY_REQUIRED"):
        controller.authority_capability(will_wipe=False, apply=True)

    assert _material_bytes(controller) == before


def test_a_host_established_by_another_owner_is_refused_not_overwritten(config) -> None:
    transport = FakeTransport()
    controller = _authority_controller(config, transport)
    _host_established(controller, transport)
    stranger = {**_lineage(controller), "owner_domain_id": "owner-" + "b" * 20}
    _host_reports(transport, lineage=stranger, identity_sha256=_identity_sha256(controller))
    before = _material_bytes(controller)

    with pytest.raises(OperationsError, match="FOREIGN_AUTHORITY") as refused:
        controller.authority_capability(will_wipe=False, apply=True)

    assert "owner-" + "b" * 20 in str(refused.value)
    assert _material_bytes(controller) == before


def test_an_observation_the_agent_did_not_send_is_refused(config) -> None:
    transport = FakeTransport()
    controller = _authority_controller(config, transport)
    transport.overrides["install-context"] = {"status": "observed", "marker": None}

    with pytest.raises(OperationsError, match="install context observation is invalid"):
        controller.authority_capability(will_wipe=False, apply=False)


def test_wipe_cannot_reissue_an_existing_owner(config):
    controller = _authority_controller(config, FakeTransport())
    with pytest.raises(OperationsError, match="new Host identity"):
        controller.authority_capability(will_wipe=True, apply=True)


def test_factory_install_plan_names_a_new_identity_without_changing_it(config) -> None:
    transport = FakeTransport()
    controller = _authority_controller(config, transport)
    _host_established(controller, transport)
    before = _material_bytes(controller)

    plan = controller.install(
        release_id="r1",
        resume=False,
        apply=False,
        reset_existing=True,
        wipe_authority_data=True,
    )

    assert plan["authority"]["decision"] == "new_host_identity"
    assert any("new Host/Owner" in mutation for mutation in plan["mutations"])
    assert _material_bytes(controller) == before


def test_a_wiped_host_is_reinstalled_end_to_end_with_a_fresh_capability(config) -> None:
    """The sequence that had no supported path at all.

    A Host whose Hub has started, wiped and reinstalled in one operation: a
    new Owner, a capability at generation 1 an empty Hub accepts, and the board
    recorded only once the Host's own proof arrives.
    """

    from test_install_inputs import _config_for_init

    _config_for_init(config, config.workspace.bundle_root.parent)
    transport = _StagingTransport()
    controller = _authority_controller(config, transport)
    spent = _host_established(controller, transport)

    result = controller.install(
        release_id="r1",
        resume=False,
        apply=True,
        reset_existing=True,
        wipe_authority_data=True,
    )

    assert result["status"] == "installed"
    assert result["authority"]["decision"] == "first_install"
    assert result["authority"]["owner_domain_id"] != spent["owner_domain_id"]
    staged = json.loads(transport.staged["/var/tmp/eidolon-secrets-r1/authority-bootstrap.json"])
    assert staged["operation"] == "owner-authority.bootstrap"
    assert staged["owner_domain_generation"] == 1
    assert result["authority_established"] == {
        "status": "authority_established",
        "authority": {key: value for key, value in staged.items() if key != "operation"},
        "delivery": "recorded",
    }
    verify_binding(_binding_path(controller).read_bytes(), _host_id(controller), _BOARD)
    assert not (_material_root(controller) / "owner-domain-state.json").exists()


def test_reinstall_bundle_failure_precedes_wipe_and_generation_change(setup_controller, monkeypatch):
    controller, _runner, transport = setup_controller
    def failed(*args, **kwargs):
        raise OperationsError("model or bundle preparation failed")
    monkeypatch.setattr(controller.bundles, "_seal", failed)
    monkeypatch.setattr(controller.releases, "_authority_capability", lambda **_: pytest.fail("must not alter Owner state before preparing inputs"))
    with pytest.raises(OperationsError, match="preparation failed"):
        controller.install(release_id="r1", resume=False, apply=True, reset_existing=True, wipe_authority_data=True)
    assert not any(call[0] == "reset-host" for call in transport.agent_calls)


@pytest.mark.parametrize("activate", [False, True])
def test_deploy_preserves_board_authority_without_reading_workstation_issuer(config, activate, monkeypatch):
    transport = FakeTransport()
    controller = _authority_controller(config, transport)
    controller.host_layer.materializer().owner_domain_id()
    before = _material_bytes(controller)
    def no_issuer(*args, **kwargs):
        raise AssertionError("ordinary deploy must not read or issue Owner material")
    monkeypatch.setattr(controller_module, "ensure_owner_domain_assets", no_issuer)
    from eidolon_ops.host_application import HostApplicationMaterializer
    monkeypatch.setattr(HostApplicationMaterializer, "owner_assets", no_issuer)
    controller.config.install_files["host_identity"].unlink()
    result = controller.deploy(release_id="r1", resume=True, activate=activate)
    assert result["status"] == ("activated" if activate else "dry_run")
    assert result["local"]["installed_identity"]["authority"]["owner_domain_generation"] == 8
    assert _material_bytes(controller) == before
    names = {Path(destination).name for _, destination, _ in transport.uploads}
    assert not {"hub.key", "hub.crt", "owner-domain-descriptor.json", "authority-bootstrap.json", "local-api.env", "channel.env", "factory_setup_code"} & names
    if activate:
        assert {"hub.generated.yaml", "agent.yaml", "channel.yaml", "memory.yaml"} <= names


def test_converge_refuses_a_host_that_holds_no_authority_before_staging_anything(config) -> None:
    transport = FakeTransport()
    controller = _authority_controller(config, transport)
    _stub_workstation_half(controller)

    with pytest.raises(OperationsError, match="deliver it with install first"):
        controller.converge_inputs(apply=True)

    assert not transport.uploads


def test_delivery_evidence_is_recorded_for_a_host_that_proves_it_holds_the_identity(
    config,
) -> None:
    transport = FakeTransport()
    controller = _authority_controller(config, transport)
    _host_established(controller, transport, bound=False)

    planned = controller.trust_host_delivery()

    assert planned["status"] == "absent"
    assert planned["decision"] == "adopt_delivery_evidence"
    assert not _binding_path(controller).exists()

    recorded = controller.trust_host_delivery(apply=True)

    assert recorded["status"] == "recorded"
    verify_binding(_binding_path(controller).read_bytes(), _host_id(controller), _BOARD)
    assert controller.trust_host_delivery(apply=True)["status"] == "current"
    assert not transport.uploads


@pytest.mark.parametrize("broken", ["identity", "other_host", "unsigned"])
def test_delivery_refuses_a_host_that_cannot_prove_it_holds_the_identity(config, broken) -> None:
    transport = FakeTransport()
    controller = _authority_controller(config, transport)
    _host_established(controller, transport, bound=False)
    context = transport.overrides["install-context"]
    document = json.loads(context["directory"])
    if broken == "identity":
        context["identity_sha256"] = "0" * 64
    elif broken == "other_host":
        document["descriptor_uri"] = (
            "https://eidolon-hub-" + "d" * 20 + ".local:8443/api/device-onboarding/v1/descriptor"
        )
        context["directory"] = json.dumps(document)
    else:
        document["issued_at"] = "2020-01-01T00:00:00Z"
        context["directory"] = json.dumps(document)

    with pytest.raises(OperationsError, match=r"IDENTITY_UNPROVEN|does not prove"):
        controller.trust_host_delivery(apply=True)

    assert not _binding_path(controller).exists()


def test_delivery_refuses_to_move_an_identity_to_another_board(config) -> None:
    transport = FakeTransport()
    controller = _authority_controller(config, transport)
    lineage = _host_established(controller, transport)
    before = _binding_path(controller).read_bytes()
    _host_reports(
        transport,
        lineage=lineage,
        directory=(_material_root(controller) / "owner-domain-descriptor.json").read_bytes(),
        hardware=_OTHER_BOARD,
        identity_sha256=_identity_sha256(controller),
    )

    with pytest.raises(OperationsError, match="WRONG_BOARD"):
        controller.trust_host_delivery(apply=True)

    assert _binding_path(controller).read_bytes() == before


def test_delivery_has_nothing_to_record_for_a_host_holding_nothing(config) -> None:
    transport = FakeTransport()
    controller = _authority_controller(config, transport)

    with pytest.raises(OperationsError, match="no delivery to record"):
        controller.trust_host_delivery(apply=True)

    assert not _binding_path(controller).exists()


def _stub_workstation_half(controller: EidolonPiController) -> None:
    """Both credential verbs prove this machine's input set before staging.

    That half has its own suite. Stubbed here so each test below is about the
    one thing it names: whether what was staged on the Host is taken back off.
    """

    controller.add_missing_input_credentials = lambda *, apply=False: {  # type: ignore[method-assign]
        "status": "planned", "added": {}, "applied": False,
    }
    controller.preflight.validate_input_contract = lambda: {  # type: ignore[method-assign]
        "status": "compatible"
    }


def _staged_then_cleaned(transport: FakeTransport, release_id: str) -> bool:
    """Was the directory this operation staged credentials into cleared again?

    `stage_install_files` clears the directory on its way in, so a cleanup that
    only happens there leaves the copy behind until something stages the same
    release id again. What this asserts is a cleanup *after* the work.
    """

    actions = [action for action, payload, *_ in transport.agent_calls
               if action == "cleanup-stage" and payload.get("release_id") == release_id]
    return len(actions) >= 2


def test_converge_takes_its_staged_credentials_back_off_the_host(config):
    """The whole input set is staged to deliver one key; it does not stay.

    This directory holds every credential the Host has plus its identity key.
    It used to be cleared only by whoever staged next — which on a Host that
    needs no further convergence is never.
    """

    transport = FakeTransport()
    controller = _authority_controller(config, transport)
    _stub_workstation_half(controller)
    _host_established(controller, transport)
    transport.overrides["converge-secret-inputs"] = {
        "status": "already_current", "added": {}, "missing": {}, "absent": [],
        "applied": False, "relationships": {"status": "agreed", "mismatched": []},
    }

    controller.converge_inputs(apply=True)

    assert _staged_then_cleaned(transport, "credential-convergence")


def test_converge_clears_its_stage_even_when_the_host_refuses(config):
    """A failed run is the one most likely to be walked away from."""

    transport = FakeTransport()
    controller = _authority_controller(config, transport)
    _stub_workstation_half(controller)
    _host_established(controller, transport)
    transport.fail_actions["converge-secret-inputs"] = OperationsError("refused")

    with pytest.raises(OperationsError):
        controller.converge_inputs(apply=True)

    assert _staged_then_cleaned(transport, "credential-convergence")


def test_a_dry_convergence_stages_nothing_to_clean(config):
    """Nothing is put on the Host to report that nothing is needed."""

    transport = FakeTransport()
    controller = _authority_controller(config, transport)
    _stub_workstation_half(controller)
    transport.overrides["converge-secret-inputs"] = {
        "status": "already_current", "added": {}, "missing": {}, "absent": [],
        "applied": False, "relationships": {"status": "agreed", "mismatched": []},
    }

    controller.converge_inputs(apply=False)

    assert not any(action == "cleanup-stage" for action, *_ in transport.agent_calls)


def test_repair_takes_its_staged_credentials_back_off_the_host(config):
    transport = FakeTransport()
    controller = _authority_controller(config, transport)
    _stub_workstation_half(controller)
    _host_established(controller, transport)
    transport.overrides["repair-secret-relationships"] = {
        "status": "consistent", "classes": 13, "divided": [], "repaired": [],
        "unchecked": [], "restart_required": [], "applied": False,
    }

    controller.repair_credentials(apply=True)

    assert _staged_then_cleaned(transport, "credential-repair")


def test_the_host_layer_refresh_takes_its_staged_private_keys_back(config):
    """The widest set any operation puts under /var/tmp.

    The Host identity's Ed25519 private key, the Hub's TLS private key, and both
    Host-bound environment files. The agent has read them by the time it
    answers, and the authority restore that reaches this verb stages its own
    material elsewhere.
    """

    transport = FakeTransport()
    controller = _authority_controller(config, transport)
    _host_established(controller, transport)
    transport.overrides["refresh-host-application"] = {
        "status": "refreshed", "changed": [], "removed": [],
    }

    controller.host_layer.refresh("20260917-release-1")

    assert _staged_then_cleaned(transport, "20260917-release-1")
