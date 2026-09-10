from __future__ import annotations

import base64
import contextlib
import hashlib
import json
import os
import sqlite3
import subprocess
import sys
from ipaddress import IPv4Address
from pathlib import Path
from types import MappingProxyType, ModuleType, SimpleNamespace

import pytest

from eidolon_ops.hostagent import __main__ as agent_main
from eidolon_ops.hostagent import (
    app_contract,
    authorities,
    authority_reset,
    contract,
    host_application,
    identities,
    memory_realms,
    primitives,
    staging,
)
from eidolon_ops.hostagent import install as host_install
from eidolon_ops.hostagent import lifecycle as host_lifecycle
from eidolon_ops.hostagent import reset as host_reset
from eidolon_ops.hostagent.install import TargetInstaller
from eidolon_ops.hostagent.primitives import TargetError

pytestmark = pytest.mark.component


class FakeHost:
    def __init__(self, root: Path, release, *, fail: str | None = None) -> None:
        self.root = root
        self.release = release
        self.fail = fail
        self.calls: list[str] = []

    def _call(self, name: str) -> None:
        self.calls.append(name)
        if self.fail == name:
            raise RuntimeError(f"injected {name} failure")

    def install_assets(self, release) -> None:
        self._call("install_assets")
        for asset in release.system_assets:
            destination = self.root / asset.destination.relative_to("/")
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_text("unit", encoding="utf-8")

    def switch_components(self, release) -> None:
        self._call("switch_components")
        for component in release.components:
            link = self.root / component.current_link.relative_to("/")
            link.parent.mkdir(parents=True, exist_ok=True)
            link.symlink_to(self.root / component.release_path.relative_to("/"))

    def reload_systemd(self) -> None:
        self._call("reload_systemd")

    def start_release(self, release) -> None:
        self._call("start_release")

    def wait_ready(self, release) -> None:
        self._call("wait_ready")

    def doctor(self, release) -> dict[str, object]:
        self._call("doctor")
        return {"doctor": "healthy"}

    def quiesce(self, release) -> None:
        self._call("quiesce")


class FakeCommand:
    def __init__(self, *, fail_baseline: bool = False) -> None:
        self.fail_baseline = fail_baseline
        self.calls: list[tuple[str, ...]] = []

    def __call__(self, command, **kwargs):
        command = tuple(command)
        self.calls.append(command)
        if len(command) > 1 and command[1] == "is-active":
            # Real systemd answers "inactive" for a stopped unit, not silence.
            return subprocess.CompletedProcess(command, 3, "inactive\n", "")
        if command[0].endswith("alembic"):
            if self.fail_baseline:
                return subprocess.CompletedProcess(command, 1, "", "baseline failed")
            database = Path(kwargs["env"]["EIDOLON_DATA_SQLITE_PATH"])
            database.parent.mkdir(parents=True, exist_ok=True)
            database.write_bytes(b"SQLite format 3\x00test")
        return subprocess.CompletedProcess(command, 0, "", "")


INSTALL_PROVENANCE = {
    "eidolon_hub": {
        "revision": "a" * 40,
        "head": "a" * 40,
        "branch": "main",
        "pinned": False,
        "dirty": False,
    }
}


@pytest.fixture
def install_fixture(tmp_path: Path):
    release_id = "release-test"
    component_ids = ("eidolon_kernel", "eidolon_data", "eidolon_hub", "eidolon_admin")
    components = tuple(
        SimpleNamespace(
            component_id=component_id,
            release_path=contract.RELEASES / release_id / component_id,
            current_link=contract.CURRENT_LINKS[component_id],
        )
        for component_id in component_ids
    )
    for component in components:
        (tmp_path / component.release_path.relative_to("/")).mkdir(parents=True)
    release = SimpleNamespace(
        release_id=release_id,
        components=components,
        components_by_id=MappingProxyType(
            {component.component_id: component for component in components}
        ),
        system_assets=(SimpleNamespace(destination=Path("/etc/systemd/system/eidolond.service")),),
    )
    stage = tmp_path / "secret-stage"
    stage.mkdir()
    for name in contract.SECRET_INPUTS:
        (stage / name).write_text(f"private-{name}", encoding="utf-8")
    data = dict(contract.FIXED_DATA)
    command = FakeCommand()
    host = FakeHost(tmp_path, release)
    installer = TargetInstaller(
        release=release,
        secret_stage=stage,
        data=data,
        host=host,
        root=tmp_path,
        command=command,
        manage_ownership=False,
        sources=INSTALL_PROVENANCE,
    )
    return installer, host, command, stage, release, data


def test_the_install_journal_records_which_commits_were_installed(install_fixture) -> None:
    """A first install is the only record of what a Host started life as.

    It leaves no cutover document, so without this the founding combination of
    commits is unrecoverable the moment the release directory is reclaimed.
    """

    installer, _host, _command, _stage, _release, _data = install_fixture

    installer.install()

    journal = json.loads(installer.journal_path.read_text(encoding="utf-8"))
    assert journal["sources"] == INSTALL_PROVENANCE


def test_first_install_completes_all_phases(install_fixture) -> None:
    installer, host, command, _stage, release, _data = install_fixture

    result = installer.install()

    assert result["status"] == "installed"
    assert result["phase"] == "completed"
    assert host.calls == [
        "install_assets",
        "switch_components",
        "reload_systemd",
        "start_release",
        "wait_ready",
        "doctor",
    ]
    assert any(call[0].endswith("alembic") for call in command.calls)
    journal = json.loads(installer.journal_path.read_text(encoding="utf-8"))
    assert journal["status"] == "completed"
    assert journal["phase"] == "completed"
    for component in release.components:
        assert (installer.root / component.current_link.relative_to("/")).is_symlink()


def test_completed_install_is_doctor_only_idempotent(install_fixture) -> None:
    installer, host, _command, _stage, _release, _data = install_fixture
    installer.install()
    host.calls.clear()

    result = installer.install()

    assert result["status"] == "already_installed"
    assert host.calls == ["doctor"]


def test_start_failure_stops_services_and_preserves_phase(install_fixture) -> None:
    installer, host, _command, stage, release, data = install_fixture
    host.fail = "wait_ready"

    with pytest.raises(RuntimeError, match="wait_ready"):
        installer.install()

    journal = json.loads(installer.journal_path.read_text(encoding="utf-8"))
    assert journal["status"] == "failed"
    assert journal["phase"] == "assets"
    assert host.calls[-1] == "quiesce"

    resumed_host = FakeHost(installer.root, release)
    resumed = TargetInstaller(
        release=release,
        secret_stage=stage,
        data=data,
        host=resumed_host,
        root=installer.root,
        command=FakeCommand(),
        manage_ownership=False,
    )
    assert resumed.install()["status"] == "installed"
    assert resumed_host.calls == ["start_release", "wait_ready", "doctor"]


def test_install_completes_with_a_degraded_app_probe(install_fixture) -> None:
    """A first install has no phone behind it yet, and still has to finish."""

    installer, host, _command, stage, release, data = install_fixture
    degraded = TargetInstaller(
        release=release,
        secret_stage=stage,
        data=data,
        host=host,
        root=installer.root,
        command=FakeCommand(),
        manage_ownership=False,
        app_check=lambda: {"status": "degraded"},
    )

    result = degraded.install()

    assert result["status"] == "installed"
    assert result["app"] == {"status": "degraded"}
    assert "quiesce" not in host.calls
    journal = json.loads(degraded.journal_path.read_text(encoding="utf-8"))
    assert journal["status"] == "completed"


def test_health_gate_failure_is_not_committed_and_resumes_from_assets(install_fixture) -> None:
    installer, _unused_host, _command, stage, release, data = install_fixture
    host = FakeHost(installer.root, release, fail="doctor")
    failing = TargetInstaller(
        release=release,
        secret_stage=stage,
        data=data,
        host=host,
        root=installer.root,
        command=FakeCommand(),
        manage_ownership=False,
        app_check=lambda: {"status": "app_ready"},
    )

    with pytest.raises(RuntimeError, match="injected doctor failure"):
        failing.install()

    journal = json.loads(failing.journal_path.read_text(encoding="utf-8"))
    assert journal["phase"] == "assets"
    assert journal["status"] == "failed"
    assert host.calls[-1] == "quiesce"

    resumed_host = FakeHost(installer.root, release)
    resumed = TargetInstaller(
        release=release,
        secret_stage=stage,
        data=data,
        host=resumed_host,
        root=installer.root,
        command=FakeCommand(),
        manage_ownership=False,
        app_check=lambda: {"status": "app_ready"},
    )
    result = resumed.install()

    assert result["app"] == {"status": "app_ready"}
    assert resumed_host.calls == ["start_release", "wait_ready", "doctor"]


def test_baseline_failure_does_not_attempt_service_stop(install_fixture) -> None:
    installer, host, _command, stage, release, data = install_fixture
    failing = TargetInstaller(
        release=release,
        secret_stage=stage,
        data=data,
        host=host,
        root=installer.root,
        command=FakeCommand(fail_baseline=True),
        manage_ownership=False,
    )

    with pytest.raises(TargetError, match="baseline failed"):
        failing.install()

    assert "quiesce" not in host.calls
    journal = json.loads(failing.journal_path.read_text(encoding="utf-8"))
    assert journal["phase"] == "prerequisites"


def test_changed_resume_input_fails_closed(install_fixture) -> None:
    installer, host, _command, stage, _release, _data = install_fixture
    host.fail = "wait_ready"
    with pytest.raises(RuntimeError):
        installer.install()
    (stage / "data.env").write_text("different", encoding="utf-8")

    with pytest.raises(TargetError, match="inputs do not match"):
        installer.install()


def test_journal_records_hashes_not_secret_values(install_fixture) -> None:
    installer, _host, _command, _stage, _release, _data = install_fixture

    installer.install()

    text = installer.journal_path.read_text(encoding="utf-8")
    assert "private-data.env" not in text
    document = json.loads(text)
    assert len(document["input_sha256"]["data.env"]) == 64


@pytest.mark.parametrize(
    "conflict",
    [
        Path("/var/lib/eidolon/eidolon-system.sqlite3"),
        Path("/var/lib/eidolon/objects/legacy-object"),
        Path("/var/lib/eidolon-bootstrap/bootstrap.sqlite3"),
        Path("/etc/eidolon/data.env"),
        Path("/etc/systemd/system/eidolond.service"),
        contract.CURRENT_LINKS["eidolon_kernel"],
    ],
)
def test_first_install_refuses_unowned_existing_namespace(install_fixture, conflict: Path) -> None:
    installer, _host, _command, _stage, _release, _data = install_fixture
    path = installer.root / conflict.relative_to("/")
    path.parent.mkdir(parents=True, exist_ok=True)
    if conflict.name == "eidolon_kernel":
        path.symlink_to(installer.root)
    else:
        path.write_text("existing", encoding="utf-8")

    with pytest.raises(TargetError, match="unowned namespace"):
        installer.install()


def test_first_install_does_not_refuse_the_encoder_it_just_carried_in(
    install_fixture,
) -> None:
    """The one thing under /var/lib/eidolon that a first install put there.

    The encoder is carried in a step *before* this guard runs, into a path the
    contract declares, so a Host never has to reach the model hub itself.
    Counting it as a stranger's leftover deadlocked exactly the case the guard
    exists for — a genuinely fresh Host — which is why nobody hit it until one
    was wiped: on every other run the weights were already held and the carry
    step wrote nothing.

    Kept next to the refusal cases above rather than replacing any of them: the
    guard must still refuse a real stranger sitting in the same directory, and
    the assertion below says so in the same breath.
    """

    installer, _host, _command, _stage, _release, _data = install_fixture
    models = installer.root / contract.HOST_EMBEDDING_MODEL_ROOT.relative_to("/")
    (models / "bge-small-zh").mkdir(parents=True)
    (models / "bge-small-zh" / contract.EMBEDDING_DIGEST_RECORD).write_text("d1", encoding="utf-8")

    installer._assert_clean_namespace()

    stranger = installer.root / "var/lib/eidolon/objects/legacy-object"
    stranger.parent.mkdir(parents=True, exist_ok=True)
    stranger.write_text("existing", encoding="utf-8")
    with pytest.raises(TargetError, match="unowned namespace"):
        installer._assert_clean_namespace()


def _reset_payload(*, wipe_authority_data: bool = False) -> dict[str, object]:
    return {
        "units": list(contract.PRODUCT_UNITS),
        "data": {name: str(path) for name, path in contract.FIXED_DATA.items()},
        "wipe_authority_data": wipe_authority_data,
    }


def _materialize_reset_fixture(root: Path) -> None:
    for value in (
        Path("/opt/eidolon/releases/old/code.py"),
        Path("/srv/eidolon/current/old"),
        Path("/etc/systemd/system/eidolond.service"),
        Path("/etc/eidolon/data.env"),
        Path("/var/lib/eidolon/eidolon-system.sqlite3"),
        Path("/var/lib/eidolon-bootstrap/bootstrap.sqlite3"),
        Path("/var/lib/eidolon-admin/old.sqlite3"),
        Path("/var/tmp/eidolon-release-old/archive.tar"),
        Path("/var/tmp/not-eidolon/keep"),
    ):
        path = root / value.relative_to("/")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("old", encoding="utf-8")


def test_reset_plan_is_read_only_and_preserves_authority_data_by_default(tmp_path: Path) -> None:
    _materialize_reset_fixture(tmp_path)

    result = host_reset.reset_plan(_reset_payload(), root=tmp_path)

    assert result["status"] == "planned"
    assert result["wipe_authority_data"] is False
    assert "/opt/eidolon" in result["detected"]
    assert "/var/lib/eidolon" not in result["detected"]
    assert "/var/tmp/eidolon-release-old" in result["staging"]
    assert (tmp_path / "opt/eidolon/releases/old/code.py").is_file()


def test_reset_removes_deployment_but_preserves_data_and_is_idempotent(tmp_path: Path) -> None:
    _materialize_reset_fixture(tmp_path)

    result = host_reset.reset_host(_reset_payload(), root=tmp_path, manage_services=False)

    assert result["status"] == "reset"
    assert not (tmp_path / "opt/eidolon").exists()
    assert not (tmp_path / "etc/eidolon").exists()
    assert not (tmp_path / "etc/systemd/system/eidolond.service").exists()
    assert not (tmp_path / "var/tmp/eidolon-release-old").exists()
    assert (tmp_path / "var/tmp/not-eidolon/keep").is_file()
    assert (tmp_path / "var/lib/eidolon/eidolon-system.sqlite3").is_file()
    assert (tmp_path / "var/lib/eidolon-bootstrap/bootstrap.sqlite3").is_file()

    repeated = host_reset.reset_host(_reset_payload(), root=tmp_path, manage_services=False)
    assert repeated["removed"] == []


def test_reset_can_explicitly_wipe_all_authority_data_for_clean_install(tmp_path: Path) -> None:
    _materialize_reset_fixture(tmp_path)

    result = host_reset.reset_host(
        _reset_payload(wipe_authority_data=True),
        root=tmp_path,
        manage_services=False,
    )

    assert result["wipe_authority_data"] is True
    for value in contract.RESET_AUTHORITY_ROOTS:
        assert not (tmp_path / value.relative_to("/")).exists()


def test_reset_clears_the_candidate_marker_it_would_otherwise_strand(
    tmp_path: Path,
) -> None:
    """A driver process that died leaves this marker outside every reset root.

    It names a release directory under a root the reset removes, so afterwards
    there is nothing left for it to refer to — but it refuses the very next
    install by that name, on a Host that no longer holds a byte of it.
    """

    _materialize_reset_fixture(tmp_path)
    marker = tmp_path / contract.RECLAMATION_STATE.relative_to("/")
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(
        json.dumps({"schema_version": 1, "candidate_release_id": "r-dead"}),
        encoding="utf-8",
    )

    plan = host_reset.reset_plan(_reset_payload(wipe_authority_data=True), root=tmp_path)
    result = host_reset.reset_host(
        _reset_payload(wipe_authority_data=True), root=tmp_path, manage_services=False
    )

    assert str(contract.RECLAMATION_STATE) in plan["detected"]
    assert str(contract.RECLAMATION_STATE) in result["removed"]
    assert not marker.exists()


def test_reset_stops_the_manager_before_the_workers_it_would_restore(tmp_path: Path) -> None:
    """systemd orders a transaction by dependencies, not by argument order.

    Naming the manager first inside one long call therefore said nothing: its
    stop job was cancelled, it outlived the sweep, and six seconds later it had
    reconciled the workers back up while the reset was still running. One
    transaction per phase still holds — a unit inside its restart loop
    re-enqueues start jobs for whatever it depends on — but the manager needs
    a phase to itself.
    """

    _materialize_reset_fixture(tmp_path)
    command = FakeCommand()

    host_reset.reset_host(_reset_payload(), root=tmp_path, command=command)

    assert set(contract.RESET_STOP_UNITS) == {
        *contract.PRODUCT_UNITS,
        "eidolon-hub-ingress.service",
    }
    disables = [call for call in command.calls if call[1:3] == ("disable", "--now")]
    assert len(disables) == 2
    assert disables[0][3:] == contract.RESET_RECONCILER_UNITS
    assert disables[1][3:] == tuple(
        unit for unit in contract.RESET_STOP_UNITS if unit not in contract.RESET_RECONCILER_UNITS
    )
    assert not [call for call in command.calls if call[1] == "stop"]
    assert ("/usr/bin/systemctl", "daemon-reload") in command.calls


def test_every_reconciler_is_a_unit_the_reset_stops(tmp_path: Path) -> None:
    """A reconciler outside the stop set would be named into a phase that
    never runs, and would go on restoring workers unopposed."""

    assert set(contract.RESET_RECONCILER_UNITS) <= set(contract.RESET_STOP_UNITS)


def test_reset_refuses_to_delete_while_a_unit_is_still_active(tmp_path: Path) -> None:
    """Deleting the tree under a live unit would leave the Host half-removed."""

    _materialize_reset_fixture(tmp_path)

    def kernel_survives(command, **_kwargs):
        command = tuple(command)
        if command[1] == "is-active" and command[2] == "eidolon-kernel.service":
            return subprocess.CompletedProcess(command, 0, "active", "")
        if command[1] == "is-active":
            return subprocess.CompletedProcess(command, 0, "inactive", "")
        if command[1:3] == ("disable", "--now"):
            return subprocess.CompletedProcess(command, 1, "", "Job canceled")
        return subprocess.CompletedProcess(command, 0, "", "")

    with pytest.raises(TargetError, match="could not stop product unit"):
        host_reset.reset_host(_reset_payload(), root=tmp_path, command=kernel_survives)


def test_reset_treats_missing_units_as_already_clean(tmp_path: Path) -> None:
    _materialize_reset_fixture(tmp_path)
    calls: list[tuple[str, ...]] = []

    def missing(command, **_kwargs):
        command = tuple(command)
        calls.append(command)
        output = "not-found\n" if command[1] == "show" else ""
        return subprocess.CompletedProcess(command, 0, output, "")

    result = host_reset.reset_host(_reset_payload(), root=tmp_path, command=missing)

    assert all(item["state"] == "absent" for item in result["services"])
    assert not any(call[1] in {"stop", "disable"} for call in calls)
    assert not (tmp_path / "opt/eidolon").exists()


def test_reset_refuses_to_delete_when_an_active_unit_cannot_stop(tmp_path: Path) -> None:
    _materialize_reset_fixture(tmp_path)

    def fail_stop(command, **_kwargs):
        command = tuple(command)
        if command[1] == "stop":
            return subprocess.CompletedProcess(command, 1, "", "stop failed")
        if command[1] == "is-active":
            return subprocess.CompletedProcess(command, 0, "active\n", "")
        return subprocess.CompletedProcess(command, 0, "", "")

    with pytest.raises(TargetError, match="could not stop"):
        host_reset.reset_host(_reset_payload(), root=tmp_path, command=fail_stop)
    assert (tmp_path / "opt/eidolon/releases/old/code.py").is_file()


def test_reset_rejects_non_boolean_authority_wipe(tmp_path: Path) -> None:
    payload = _reset_payload()
    payload["wipe_authority_data"] = "yes"

    with pytest.raises(TargetError, match="must be a boolean"):
        host_reset.reset_plan(payload, root=tmp_path)


def test_concurrent_install_lock_fails_fast(install_fixture) -> None:
    installer, _host, _command, _stage, _release, _data = install_fixture

    with (
        primitives.exclusive(installer.lock_path),
        pytest.raises(TargetError, match="another first-install"),
    ):
        installer.install()


def test_secret_stage_requires_exact_file_set(install_fixture) -> None:
    installer, _host, _command, stage, _release, _data = install_fixture
    (stage / "extra").write_text("x", encoding="utf-8")

    with pytest.raises(TargetError, match="file set"):
        installer.install()


def test_secret_stage_rejects_symlink(install_fixture) -> None:
    installer, _host, _command, stage, _release, _data = install_fixture
    (stage / "data.env").unlink()
    (stage / "data.env").symlink_to(stage / "hub.env")

    with pytest.raises(TargetError, match="unsafe"):
        installer.install()


def _install_kernel_stubs(
    monkeypatch,
    release: object,
    *,
    host_type: type | None = None,
) -> None:
    package = ModuleType("eidolon_deploy")
    package.__path__ = []  # type: ignore[attr-defined]
    linux = ModuleType("eidolon_deploy.linux")
    manifest = ModuleType("eidolon_deploy.manifest")
    linux.CommandResult = (  # type: ignore[attr-defined]
        lambda returncode, stdout, stderr: SimpleNamespace(
            returncode=returncode,
            stdout=stdout,
            stderr=stderr,
        )
    )
    if host_type is not None:
        linux.LinuxDeploymentHost = host_type  # type: ignore[attr-defined]
    manifest.load_release_descriptor = lambda _path: release  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "eidolon_deploy", package)
    monkeypatch.setitem(sys.modules, "eidolon_deploy.linux", linux)
    monkeypatch.setitem(sys.modules, "eidolon_deploy.manifest", manifest)


def test_status_parses_systemd_properties(monkeypatch) -> None:
    def fake_run(command, **kwargs):
        return subprocess.CompletedProcess(
            command,
            0,
            "LoadState=loaded\nActiveState=active\nSubState=running\nNRestarts=2\n",
            "",
        )

    monkeypatch.setattr(primitives, "run", fake_run)

    result = host_lifecycle.status({"units": list(contract.PRODUCT_UNITS)})

    assert result["units"]["eidolond.service"]["ActiveState"] == "active"
    assert result["units"]["eidolond.service"]["NRestarts"] == 2
    assert result["network"] == {"lan_ipv4": None, "addresses": []}


def test_status_records_systemctl_failure(monkeypatch) -> None:
    monkeypatch.setattr(
        primitives,
        "run",
        lambda command, **kwargs: subprocess.CompletedProcess(command, 1, "", "not found"),
    )

    result = host_lifecycle.status({"units": list(contract.PRODUCT_UNITS)})

    assert result["units"]["eidolond.service"] == {"error": "not found"}


def test_logs_rejects_unknown_unit() -> None:
    with pytest.raises(TargetError, match="outside"):
        host_lifecycle.logs(
            {
                "units": list(contract.PRODUCT_UNITS),
                "unit": "ssh.service",
                "lines": 10,
            }
        )


@pytest.mark.parametrize("lines", [0, 5001, "100"])
def test_logs_rejects_unbounded_line_count(lines) -> None:
    with pytest.raises(TargetError, match="line count"):
        host_lifecycle.logs({"units": list(contract.PRODUCT_UNITS), "lines": lines})


def test_logs_collects_fixed_units(monkeypatch) -> None:
    monkeypatch.setattr(
        primitives,
        "run",
        lambda command, **kwargs: subprocess.CompletedProcess(command, 0, "entry\n", ""),
    )

    result = host_lifecycle.logs(
        {
            "units": list(contract.PRODUCT_UNITS),
            "unit": "eidolond.service",
            "lines": 2,
            "since": "1 hour ago",
        }
    )

    assert result["entries"] == {"eidolond.service": "entry\n"}


def test_logs_rejects_control_character_in_since() -> None:
    with pytest.raises(TargetError, match="since"):
        host_lifecycle.logs(
            {
                "units": list(contract.PRODUCT_UNITS),
                "lines": 10,
                "since": "today\nnext",
            }
        )


def test_payload_round_trip_and_rejects_non_object() -> None:
    encoded = base64.urlsafe_b64encode(b'{"release_id":"r1"}').decode("ascii")
    assert primitives.decode_payload(encoded) == {"release_id": "r1"}

    encoded_list = base64.urlsafe_b64encode(b"[]").decode("ascii")
    with pytest.raises(TargetError, match="object"):
        primitives.decode_payload(encoded_list)


def test_guard_upload_accepts_absent_path(monkeypatch, tmp_path: Path) -> None:
    release_id = "test-guard-upload-ops"
    monkeypatch.setattr(contract, "VAR_TMP", tmp_path)
    path = tmp_path / f"eidolon-release-{release_id}"

    result = staging.guard_upload({"release_id": release_id, "transfer_id": "a" * 64})
    assert result["status"] == "ready_for_upload"
    assert path.stat().st_mode & 0o777 == 0o700


def test_guard_upload_rejects_existing_path(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(contract, "VAR_TMP", tmp_path)
    path = tmp_path / "eidolon-release-existing"
    path.mkdir()

    with pytest.raises(TargetError, match="not resumable"):
        staging.guard_upload({"release_id": "existing", "transfer_id": "a" * 64})


def test_guard_upload_resumes_only_matching_owned_transfer(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(contract, "VAR_TMP", tmp_path)
    payload = {"release_id": "resume", "transfer_id": "b" * 64}

    first = staging.guard_upload(payload)
    second = staging.guard_upload(payload)

    assert first["status"] == "ready_for_upload"
    assert second["status"] == "resume_upload"
    with pytest.raises(TargetError, match="drifted"):
        staging.guard_upload({"release_id": "resume", "transfer_id": "c" * 64})


def test_upload_finalization_hands_one_closed_bundle_to_kernel(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(contract, "VAR_TMP", tmp_path)
    release_id = "closed-bundle"
    manifest_bytes = b'{"schema_version":3}\n'
    transfer_id = hashlib.sha256(manifest_bytes).hexdigest()
    payload = {"release_id": release_id, "transfer_id": transfer_id}
    staging.guard_upload(payload)
    bundle = tmp_path / f"eidolon-release-{release_id}"
    (bundle / "bundle.json").write_bytes(manifest_bytes)
    (bundle / "prepare_target.py").write_text("preparer", encoding="utf-8")
    (bundle / "sources").mkdir()

    result = staging.finalize_upload(payload)

    assert result["status"] == "finalized"
    assert not (bundle / ".eidolon-upload.json").exists()
    assert staging.finalize_upload(payload)["status"] == "already_finalized"
    assert staging.guard_upload(payload)["status"] == "ready_for_prepare"


def test_upload_finalization_rejects_missing_or_unowned_staging(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(contract, "VAR_TMP", tmp_path)
    with pytest.raises(TargetError, match="transfer identity"):
        staging.finalize_upload({"release_id": "missing", "transfer_id": "short"})
    payload = {"release_id": "missing", "transfer_id": "a" * 64}
    with pytest.raises(TargetError, match="directory is missing"):
        staging.finalize_upload(payload)

    uploaded = tmp_path / "eidolon-release-missing"
    uploaded.mkdir(mode=0o755)
    with pytest.raises(TargetError, match="ownership drifted"):
        staging.finalize_upload(payload)


def test_upload_finalization_rejects_marker_shape_and_digest_drift(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(contract, "VAR_TMP", tmp_path)
    payload = {"release_id": "drift", "transfer_id": "b" * 64}
    staging.guard_upload(payload)
    bundle = tmp_path / "eidolon-release-drift"
    marker = bundle / ".eidolon-upload.json"
    marker.write_text("not-json", encoding="utf-8")
    with pytest.raises(TargetError, match="marker is unreadable"):
        staging.finalize_upload(payload)

    marker.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "release_id": "drift",
                "transfer_id": "b" * 64,
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(TargetError, match="shape or identity drifted"):
        staging.finalize_upload(payload)

    marker.unlink()
    with pytest.raises(TargetError, match="shape or identity drifted"):
        staging.finalize_upload(payload)

    (bundle / "bundle.json").write_text("wrong", encoding="utf-8")
    (bundle / "prepare_target.py").write_text("preparer", encoding="utf-8")
    (bundle / "sources").mkdir()
    with pytest.raises(TargetError, match="manifest digest drifted"):
        staging.finalize_upload(payload)


def test_guard_upload_rejects_invalid_transfer_identity(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(contract, "VAR_TMP", tmp_path)

    with pytest.raises(TargetError, match="transfer identity"):
        staging.guard_upload({"release_id": "resume", "transfer_id": "short"})


def test_release_artifacts_are_missing_then_atomically_installed(
    monkeypatch, tmp_path: Path
) -> None:
    carried = b"content-addressed-object"
    digest = hashlib.sha256(carried).hexdigest()
    payload = {"artifacts": [{"sha256": digest, "size": len(carried)}]}
    store = tmp_path / "store/sha256"
    monkeypatch.setattr(contract, "VAR_TMP", tmp_path)
    monkeypatch.setattr(contract, "RELEASE_ARTIFACT_STORE_ROOT", store)
    monkeypatch.setattr(staging.os, "chown", lambda *_args: None)

    assert staging.release_artifact_state(payload) == {
        "status": "missing",
        "missing": [digest],
        "corrupt": [],
        "objects": 1,
    }
    transfer = {"transfer_id": "a" * 64, **payload}
    assert staging.guard_release_artifacts(transfer)["status"] == "ready_for_upload"
    assert staging.guard_release_artifacts(transfer)["status"] == "resume_upload"
    stage = tmp_path / f"eidolon-artifacts-{'a' * 16}/sha256"
    stage.mkdir()
    (stage / digest).write_bytes(carried)

    finalized = staging.finalize_release_artifacts(transfer)

    assert finalized == {"status": "installed", "objects": 1, "installed": 1}
    assert (store / digest).read_bytes() == carried
    assert staging.release_artifact_state(payload)["status"] == "complete"
    assert not stage.parent.exists()


def test_release_artifact_finalization_rejects_corruption_before_store_visibility(
    monkeypatch, tmp_path: Path
) -> None:
    carried = b"expected"
    digest = hashlib.sha256(carried).hexdigest()
    payload = {
        "transfer_id": "b" * 64,
        "artifacts": [{"sha256": digest, "size": len(carried)}],
    }
    store = tmp_path / "store/sha256"
    monkeypatch.setattr(contract, "VAR_TMP", tmp_path)
    monkeypatch.setattr(contract, "RELEASE_ARTIFACT_STORE_ROOT", store)
    staging.guard_release_artifacts(payload)
    stage = tmp_path / f"eidolon-artifacts-{'b' * 16}/sha256"
    stage.mkdir()
    (stage / digest).write_bytes(b"corrupt!")

    with pytest.raises(TargetError, match="checksum drifted"):
        staging.finalize_release_artifacts(payload)

    assert not (store / digest).exists()


def test_corrupt_regular_artifact_is_reported_missing_and_repaired(
    monkeypatch, tmp_path: Path
) -> None:
    carried = b"repaired-object"
    digest = hashlib.sha256(carried).hexdigest()
    store = tmp_path / "store/sha256"
    store.mkdir(parents=True)
    (store / digest).write_bytes(b"corrupt-object")
    payload = {"artifacts": [{"sha256": digest, "size": len(carried)}]}
    monkeypatch.setattr(contract, "VAR_TMP", tmp_path)
    monkeypatch.setattr(contract, "RELEASE_ARTIFACT_STORE_ROOT", store)
    monkeypatch.setattr(staging.os, "chown", lambda *_args: None)

    state = staging.release_artifact_state(payload)
    assert state["missing"] == [digest]
    assert state["corrupt"] == [digest]
    transfer = {"transfer_id": "c" * 64, **payload}
    staging.guard_release_artifacts(transfer)
    stage = tmp_path / f"eidolon-artifacts-{'c' * 16}/sha256"
    stage.mkdir()
    (stage / digest).write_bytes(carried)

    staging.finalize_release_artifacts(transfer)

    assert (store / digest).read_bytes() == carried


def test_guard_upload_reports_exact_prepared_release(monkeypatch, tmp_path: Path) -> None:
    releases = tmp_path / "releases"
    prepared = releases / "prepared"
    prepared.mkdir(parents=True)
    (prepared / "release.json").write_text("{}", encoding="utf-8")
    (prepared / "release.json.sha256").write_text("digest", encoding="utf-8")
    monkeypatch.setattr(contract, "RELEASES", releases)
    monkeypatch.setattr(contract, "VAR_TMP", tmp_path / "staging")

    result = staging.guard_upload({"release_id": "prepared", "transfer_id": "d" * 64})

    assert result == {
        "status": "already_prepared",
        "path": str(prepared),
        "transfer_id": "d" * 64,
    }


def test_guard_upload_rejects_mode_drift(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(contract, "VAR_TMP", tmp_path)
    payload = {"release_id": "resume", "transfer_id": "e" * 64}
    staging.guard_upload(payload)
    (tmp_path / "eidolon-release-resume").chmod(0o755)

    with pytest.raises(TargetError, match="ownership drifted"):
        staging.guard_upload(payload)


def test_cleanup_stage_removes_only_exact_secret_path(monkeypatch, tmp_path: Path) -> None:
    release_id = "test-cleanup-stage-ops"
    monkeypatch.setattr(contract, "VAR_TMP", tmp_path)
    path = tmp_path / f"eidolon-secrets-{release_id}"
    path.mkdir(exist_ok=False)
    (path / "data.env").write_text("private", encoding="utf-8")

    result = staging.cleanup_stage({"release_id": release_id})

    assert result["status"] == "cleaned"
    assert not path.exists()


def test_main_rejects_unknown_action(capsys) -> None:
    encoded = base64.urlsafe_b64encode(b"{}").decode("ascii")

    assert agent_main.main(("unknown", encoded)) == 1
    assert "unknown target action" in capsys.readouterr().err


def test_main_rejects_missing_arguments(capsys) -> None:
    assert agent_main.main(()) == 2
    assert "expected action" in capsys.readouterr().err


def test_doctor_host_reports_current_platform_without_mutation() -> None:
    result = host_lifecycle.doctor_host(
        {
            "units": list(contract.PRODUCT_UNITS),
            "data": {name: str(path) for name, path in contract.FIXED_DATA.items()},
            "remote_uv": "/definitely/missing/uv",
        }
    )

    assert result["status"] == "degraded"
    assert result["checks"]["uv"] is False


def test_fixed_payload_contracts_reject_drift() -> None:
    with pytest.raises(TargetError, match="unit set"):
        contract.fixed_units({"units": []})
    with pytest.raises(TargetError, match="data path"):
        contract.fixed_data({"data": {}})
    with pytest.raises(TargetError, match="release id"):
        contract.fixed_release_id({"release_id": "../bad"})
    assert contract.fixed_release_id({}, required=False) is None


def test_atomic_json_and_file_hash(tmp_path: Path) -> None:
    path = tmp_path / "document.json"

    primitives.atomic_json(path, {"value": 1})

    assert json.loads(path.read_text(encoding="utf-8")) == {"value": 1}
    assert len(primitives.file_sha256(path)) == 64


def test_checked_command_success_and_failure(monkeypatch) -> None:
    monkeypatch.setattr(
        primitives,
        "run",
        lambda command, **kwargs: subprocess.CompletedProcess(command, 0, "ok", ""),
    )
    assert primitives.checked("probe", ("true",)).stdout == "ok"

    monkeypatch.setattr(
        primitives,
        "run",
        lambda command, **kwargs: subprocess.CompletedProcess(command, 1, "", "failed"),
    )
    with pytest.raises(TargetError, match="probe failed"):
        primitives.checked("probe", ("false",))


def test_diagnose_composes_redacted_status(monkeypatch) -> None:
    monkeypatch.setattr(host_lifecycle, "status", lambda payload: {"status": "observed"})
    monkeypatch.setattr(
        host_lifecycle,
        "logs",
        lambda payload: {"status": "collected", "entries": {"eidolond": "line"}},
    )

    result = host_lifecycle.diagnose({})

    assert result["status"] == "diagnosed"
    assert "private key" in result["redaction"]


def test_target_main_dispatches_status(monkeypatch, capsys) -> None:
    monkeypatch.setattr(host_lifecycle, "status", lambda payload: {"status": "observed"})
    encoded = base64.urlsafe_b64encode(b"{}").decode("ascii")

    assert agent_main.main(("status", encoded)) == 0
    assert json.loads(capsys.readouterr().out) == {"status": "observed"}


def test_service_identity_creation_commands(tmp_path: Path, install_fixture) -> None:
    installer, _host, _command, _stage, _release, _data = install_fixture
    calls: list[tuple[str, ...]] = []

    def command(value, **kwargs):
        value = tuple(value)
        calls.append(value)
        if value[:2] in {("/usr/bin/getent", "group"), ("/usr/bin/id", "-u")}:
            return subprocess.CompletedProcess(value, 1, "", "missing")
        if value[:2] == ("/usr/bin/id", "-gn"):
            return subprocess.CompletedProcess(value, 0, value[-1] + "\n", "")
        return subprocess.CompletedProcess(value, 0, "", "")

    installer.command = command
    installer._ensure_service_identity("eidolon")

    assert any(call[0] == "/usr/sbin/groupadd" for call in calls)
    assert any(call[0] == "/usr/sbin/useradd" for call in calls)


def test_service_identity_rejects_wrong_primary_group(install_fixture) -> None:
    installer, _host, _command, _stage, _release, _data = install_fixture

    def command(value, **kwargs):
        value = tuple(value)
        stdout = "wrong\n" if value[:2] == ("/usr/bin/id", "-gn") else "exists\n"
        return subprocess.CompletedProcess(value, 0, stdout, "")

    installer.command = command
    with pytest.raises(TargetError, match="primary group"):
        installer._ensure_service_identity("eidolon")


def test_upgrade_identity_cutover_proves_distinct_non_root_uids(monkeypatch) -> None:
    uids = {
        "eidolon": 41000,
        "eidolon-bootstrap": 41001,
        "eidolon-local-api": 41002,
        "eidolon-lifecycle": 41003,
    }
    monkeypatch.setattr(identities.os, "geteuid", lambda: 0)

    def run(command, **_kwargs):
        return subprocess.CompletedProcess(command, 0, "exists\n", "")

    def checked(_operation, command, **_kwargs):
        if command[:2] == ("/usr/bin/id", "-u"):
            stdout = f"{uids[command[-1]]}\n"
        elif command[:2] == ("/usr/bin/id", "-gn"):
            stdout = f"{command[-1]}\n"
        elif command[:2] == ("/usr/bin/id", "-nG"):
            stdout = f"{command[-1]} eidolon-owner-trust-readers\n"
        elif command[:2] == ("/usr/bin/getent", "group"):
            stdout = "eidolon-lifecycle-client:x:41999:\n"
        else:
            stdout = ""
        return subprocess.CompletedProcess(command, 0, stdout, "")

    monkeypatch.setattr(primitives, "run", run)
    monkeypatch.setattr(primitives, "checked", checked)

    result = identities.ensure_service_identities({"units": list(contract.PRODUCT_UNITS)})

    assert result["status"] == "service_identities_ready"
    assert len(set(result["uids"].values())) == 4
    assert result["persistent_socket_group_members"] == []
    assert result["owner_trust_group"] == "eidolon-owner-trust-readers"
    assert result["owner_trust_readers"] == [
        "eidolon",
        "eidolon-local-api",
        "eidolon-lifecycle",
    ]


def test_upgrade_identity_cutover_rejects_duplicate_uids(monkeypatch) -> None:
    monkeypatch.setattr(identities.os, "geteuid", lambda: 0)
    monkeypatch.setattr(
        primitives,
        "run",
        lambda command, **_kwargs: subprocess.CompletedProcess(command, 0, "exists\n", ""),
    )

    def checked(_operation, command, **_kwargs):
        if command[:2] == ("/usr/bin/id", "-u"):
            stdout = "41000\n"
        elif command[:2] == ("/usr/bin/id", "-gn"):
            stdout = f"{command[-1]}\n"
        elif command[:2] == ("/usr/bin/id", "-nG"):
            stdout = f"{command[-1]} eidolon-owner-trust-readers\n"
        else:
            stdout = "eidolon-lifecycle-client:x:41999:\n"
        return subprocess.CompletedProcess(command, 0, stdout, "")

    monkeypatch.setattr(primitives, "checked", checked)

    with pytest.raises(TargetError, match="distinct UIDs"):
        identities.ensure_service_identities({"units": list(contract.PRODUCT_UNITS)})


def test_real_run_and_host_path_guards(tmp_path: Path) -> None:
    assert primitives.run(("/usr/bin/true",)).returncode == 0
    assert primitives.host_path(tmp_path, Path("/etc/test")) == tmp_path / "etc/test"
    with pytest.raises(TargetError, match="absolute"):
        primitives.host_path(tmp_path, Path("relative"))


def test_run_wraps_subprocess_error(monkeypatch) -> None:
    def fail(*args, **kwargs):
        raise OSError("cannot exec")

    monkeypatch.setattr(subprocess, "run", fail)

    with pytest.raises(TargetError, match="cannot exec"):
        primitives.run(("missing",))


def test_status_reads_recent_receipt(monkeypatch, tmp_path: Path) -> None:
    evidence = tmp_path / "evidence"
    receipt = evidence / "r1-tx" / "receipt.json"
    receipt.parent.mkdir(parents=True)
    receipt.write_text(
        json.dumps(
            {
                "release_id": "r1",
                "status": "activated",
                "transaction_id": "tx",
                "error": "must not be copied",
            }
        ),
        encoding="utf-8",
    )
    install_journal = evidence / "install-r1" / "install.json"
    install_journal.parent.mkdir(parents=True)
    install_journal.write_text(
        json.dumps({"release_id": "r1", "status": "completed", "phase": "completed"}),
        encoding="utf-8",
    )
    monkeypatch.setitem(contract.FIXED_DATA, "deployment_evidence", evidence)
    monkeypatch.setattr(
        primitives,
        "run",
        lambda command, **kwargs: subprocess.CompletedProcess(command, 0, "", ""),
    )

    result = host_lifecycle.status({"units": list(contract.PRODUCT_UNITS)})

    assert result["recent_receipts"] == [
        {
            "path": str(receipt),
            "release_id": "r1",
            "status": "activated",
            "transaction_id": "tx",
        }
    ]
    assert result["installations"] == [
        {
            "path": str(install_journal),
            "release_id": "r1",
            "status": "completed",
            "phase": "completed",
        }
    ]
    assert result["release_sources"] == []


def test_status_reports_which_commits_each_release_was_built_from(
    monkeypatch, tmp_path: Path
) -> None:
    """The record is useless if no command prints it.

    A Host held the commits of the release it was running in exactly one place
    and no verb read it, so the question was answered by guessing at sibling
    checkouts. Status answers it.
    """

    evidence = tmp_path / "evidence"
    provenance = {
        "eidolon_hub": {
            "revision": "a" * 40,
            "head": "a" * 40,
            "branch": "main",
            "pinned": False,
            "dirty": False,
        }
    }
    for release_id, status, sources in (
        ("r1", "activated", provenance),
        ("r2", "snapshotted", None),
    ):
        document = evidence / f"{release_id}-host-{'b' * 32}" / "cutover.json"
        document.parent.mkdir(parents=True)
        document.write_text(
            json.dumps(
                {
                    "release_id": release_id,
                    "status": status,
                    "cutover_mode": "reversible",
                    "host_transaction_id": "b" * 32,
                    "sources": sources,
                    "host_files_before": {"must": "not be copied"},
                }
            ),
            encoding="utf-8",
        )
    monkeypatch.setitem(contract.FIXED_DATA, "deployment_evidence", evidence)
    monkeypatch.setattr(
        primitives,
        "run",
        lambda command, **kwargs: subprocess.CompletedProcess(command, 0, "", ""),
    )

    result = host_lifecycle.status({"units": list(contract.PRODUCT_UNITS)})

    recorded = {item["release_id"]: item for item in result["release_sources"]}
    assert recorded["r1"]["sources"] == provenance
    assert recorded["r1"]["status"] == "activated"
    assert recorded["r2"]["sources"] is None
    assert "host_files_before" not in recorded["r1"]


def test_release_sources_reports_newest_activated_release_first(
    monkeypatch, tmp_path: Path
) -> None:
    """What a failed deploy is compared against.

    A combination of commits that is not self-consistent cannot be caught
    before it runs. What can be fixed is the failure naming the repositories
    that moved since the last release that worked.
    """

    evidence = tmp_path / "evidence"
    for index, (release_id, status) in enumerate(
        (("old", "activated"), ("broken", "snapshotted"), ("last-good", "activated"))
    ):
        document = evidence / f"{release_id}-host-{'c' * 32}" / "cutover.json"
        document.parent.mkdir(parents=True)
        document.write_text(
            json.dumps(
                {
                    "release_id": release_id,
                    "status": status,
                    "sources": {
                        "eidolon_hub": {
                            "revision": f"{index:040x}",
                            "head": f"{index:040x}",
                            "branch": "main",
                            "pinned": False,
                            "dirty": False,
                        }
                    },
                }
            ),
            encoding="utf-8",
        )
        os.utime(document, (1000 + index, 1000 + index))
    monkeypatch.setitem(contract.FIXED_DATA, "deployment_evidence", evidence)

    result = host_lifecycle.release_sources({})

    assert result["status"] == "observed"
    assert [item["release_id"] for item in result["releases"]] == ["last-good", "old"]
    assert result["releases"][0]["sources"]["eidolon_hub"]["revision"] == f"{2:040x}"


@pytest.mark.parametrize(
    ("returncode", "stdout", "healthy"),
    [(0, '{"status":"healthy"}', True), (0, "not-json", False), (1, "", False)],
)
def test_doctor_host_checks_release_result(
    monkeypatch,
    tmp_path: Path,
    returncode: int,
    stdout: str,
    healthy: bool,
) -> None:
    monkeypatch.setattr(contract, "RELEASES", tmp_path)
    monkeypatch.setattr(
        primitives,
        "run",
        lambda command, **kwargs: subprocess.CompletedProcess(
            command, returncode, stdout, "failed"
        ),
    )

    result = host_lifecycle.doctor_host(
        {
            "units": list(contract.PRODUCT_UNITS),
            "data": {name: str(path) for name, path in contract.FIXED_DATA.items()},
            "remote_uv": "/missing",
            "release_id": "r1",
        }
    )

    assert result["release"]["healthy"] is healthy


def test_deployment_runner_maps_command_result(monkeypatch) -> None:
    class Result:
        def __init__(self, returncode, stdout, stderr) -> None:
            self.returncode = returncode
            self.stdout = stdout
            self.stderr = stderr

    monkeypatch.setattr(
        primitives,
        "run",
        lambda command, **kwargs: subprocess.CompletedProcess(command, 3, "out", "err"),
    )

    result = host_install.DeploymentRunner(Result).run("command")

    assert result.returncode == 3
    assert result.stdout == "out"


def test_lifecycle_uses_kernel_host_adapter(monkeypatch, tmp_path: Path) -> None:
    release = SimpleNamespace(release_id="active")

    class Host:
        def __init__(self, **kwargs) -> None:
            self.calls = []
            Host.instance = self

        def exclusive_activation(self):
            return contextlib.nullcontext()

        def preflight(self, value):
            self.calls.append("preflight")

        def quiesce(self, value):
            self.calls.append("quiesce")

        def start_release(self, value):
            self.calls.append("start")

        def wait_ready(self, value):
            self.calls.append("ready")

    _install_kernel_stubs(monkeypatch, release, host_type=Host)
    active = tmp_path / "release/eidolon_kernel"
    active.mkdir(parents=True)
    monkeypatch.setattr(contract, "CURRENT_KERNEL", active)

    result = host_lifecycle.lifecycle("restart", {"units": list(contract.PRODUCT_UNITS)})

    assert result == {"status": "restarted", "release_id": "active"}
    assert Host.instance.calls == ["preflight", "quiesce", "start", "ready"]


def test_rollback_plan_validates_snapshot(monkeypatch, tmp_path: Path) -> None:
    evidence = tmp_path / "deployments"
    snapshot = evidence / "r1-tx"
    snapshot.mkdir(parents=True)
    releases = tmp_path / "releases"
    descriptor = releases / "r1/release.json"
    descriptor.parent.mkdir(parents=True)
    descriptor.write_text("{}", encoding="utf-8")
    monkeypatch.setitem(contract.FIXED_DATA, "deployment_evidence", evidence)
    monkeypatch.setattr(contract, "RELEASES", releases)

    result = host_lifecycle.rollback_plan({"release_id": "r1", "snapshot": str(snapshot)})

    assert result["status"] == "rollback_planned"
    with pytest.raises(TargetError, match="snapshot path"):
        host_lifecycle.rollback_plan({"release_id": "r1", "snapshot": 1})


def test_install_wrapper_reuses_kernel_descriptor(monkeypatch, tmp_path: Path) -> None:
    release = SimpleNamespace(release_id="r1")

    class Host:
        def __init__(self, **kwargs) -> None:
            pass

    class Installer:
        def __init__(self, **kwargs) -> None:
            assert kwargs["release"] is release
            # The registry travels with the operation; the installer never
            # reaches for one of its own.
            assert kwargs["port_registry"] == "admin:\n  api:\n    port: 9000\n"

        def install(self):
            return {"status": "installed"}

    _install_kernel_stubs(monkeypatch, release, host_type=Host)
    monkeypatch.setattr(host_install, "TargetInstaller", Installer)
    monkeypatch.setattr(contract, "RELEASES", tmp_path / "releases")
    monkeypatch.setattr(contract, "VAR_TMP", tmp_path)

    result = host_install.install(
        {
            "release_id": "r1",
            "data": {name: str(path) for name, path in contract.FIXED_DATA.items()},
            "port_registry": "admin:\n  api:\n    port: 9000\n",
        }
    )

    assert result == {"status": "installed"}


def test_prerequisite_resume_detects_mode_drift(install_fixture) -> None:
    installer, host, _command, _stage, _release, _data = install_fixture
    host.fail = "wait_ready"
    with pytest.raises(RuntimeError):
        installer.install()
    destination = installer.root / "etc/eidolon/data.env"
    destination.chmod(0o644)

    with pytest.raises(TargetError, match="differs during resume"):
        installer.install()


def test_baseline_must_create_database(install_fixture) -> None:
    installer, _host, _command, _stage, _release, _data = install_fixture
    installer.command = lambda command, **kwargs: subprocess.CompletedProcess(command, 0, "", "")

    with pytest.raises(TargetError, match="did not create"):
        installer._create_data_v2_baseline()


def test_command_checked_reports_failure(install_fixture) -> None:
    installer, _host, _command, _stage, _release, _data = install_fixture
    installer.command = lambda command, **kwargs: subprocess.CompletedProcess(
        command, 1, "", "denied"
    )

    with pytest.raises(TargetError, match="denied"):
        installer._command_checked("operation", ("false",))


def test_active_release_reports_component_neutral_operator_entries(tmp_path, monkeypatch) -> None:
    """The target resolves its own layout so the deployer never spells a component."""

    releases = tmp_path / "opt/eidolon/releases"
    release_root = releases / "r7"
    for relative in (contract.RELEASE_ACTIVATOR, contract.RELEASE_INTERPRETER):
        entry = release_root / relative
        entry.parent.mkdir(parents=True, exist_ok=True)
        entry.write_text("#!/bin/sh\n", encoding="utf-8")
        entry.chmod(0o755)
    current = tmp_path / "opt/eidolon/current/eidolon_kernel"
    current.parent.mkdir(parents=True, exist_ok=True)
    current.symlink_to(release_root / "eidolon_kernel")
    (release_root / "eidolon_kernel").mkdir()
    monkeypatch.setattr(contract, "RELEASES", releases)
    monkeypatch.setattr(contract, "CURRENT_KERNEL", current)

    result = host_lifecycle.active_release({"units": list(contract.PRODUCT_UNITS)})

    assert result["release_id"] == "r7"
    assert result["interpreter"] == str(release_root / contract.RELEASE_INTERPRETER)
    assert result["activator"] == str(release_root / contract.RELEASE_ACTIVATOR)


def test_active_release_fails_closed_without_an_activated_release(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(contract, "CURRENT_KERNEL", tmp_path / "missing")

    with pytest.raises(TargetError, match="currently active"):
        host_lifecycle.active_release({"units": list(contract.PRODUCT_UNITS)})


def test_active_release_rejects_a_release_missing_its_operator_entries(
    tmp_path, monkeypatch
) -> None:
    releases = tmp_path / "opt/eidolon/releases"
    release_root = releases / "r8"
    (release_root / "eidolon_kernel").mkdir(parents=True)
    current = tmp_path / "opt/eidolon/current/eidolon_kernel"
    current.parent.mkdir(parents=True, exist_ok=True)
    current.symlink_to(release_root / "eidolon_kernel")
    monkeypatch.setattr(contract, "RELEASES", releases)
    monkeypatch.setattr(contract, "CURRENT_KERNEL", current)

    with pytest.raises(TargetError, match="does not publish its"):
        host_lifecycle.active_release({"units": list(contract.PRODUCT_UNITS)})


def test_controller_reset_reports_the_bootstrap_evidence(tmp_path, monkeypatch) -> None:
    """Recovery is delegated to Bootstrap; the agent only carries its evidence."""

    ctl = tmp_path / "eidolon-bootstrapctl"
    ctl.write_text("#!/bin/sh\n", encoding="utf-8")
    ctl.chmod(0o755)
    monkeypatch.setattr(host_lifecycle, "BOOTSTRAP_CTL", ctl)
    document = {
        "revoked_controllers": ["ectrl-0123456789abcdef0123"],
        "preserved": ["owner_binding"],
    }
    monkeypatch.setattr(
        primitives,
        "run",
        lambda command, **kwargs: subprocess.CompletedProcess(command, 0, json.dumps(document), ""),
    )

    result = host_lifecycle.controller_reset({"units": list(contract.PRODUCT_UNITS)})

    assert result["status"] == "reset"
    assert result["controller_reset"] == document


def test_controller_reset_requires_the_bootstrap_control_cli(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(host_lifecycle, "BOOTSTRAP_CTL", tmp_path / "missing")

    with pytest.raises(TargetError, match="bootstrap control CLI"):
        host_lifecycle.controller_reset({"units": list(contract.PRODUCT_UNITS)})


def test_controller_reset_rejects_output_that_is_not_bootstrap_evidence(
    tmp_path, monkeypatch
) -> None:
    ctl = tmp_path / "eidolon-bootstrapctl"
    ctl.write_text("#!/bin/sh\n", encoding="utf-8")
    ctl.chmod(0o755)
    monkeypatch.setattr(host_lifecycle, "BOOTSTRAP_CTL", ctl)
    monkeypatch.setattr(
        primitives,
        "run",
        lambda command, **kwargs: subprocess.CompletedProcess(command, 0, "{}", ""),
    )

    with pytest.raises(TargetError, match="invalid evidence"):
        host_lifecycle.controller_reset({"units": list(contract.PRODUCT_UNITS)})


def test_a_host_without_a_declared_address_reports_the_one_it_has(monkeypatch) -> None:
    """An address a Host once had says nothing about reaching it now."""

    monkeypatch.setattr(
        primitives,
        "run",
        lambda command, **kwargs: subprocess.CompletedProcess(
            command, 0, "1.1.1.1 via 192.168.1.1 dev eth0 src 192.168.1.26 uid 0\n", ""
        ),
    )

    assert str(app_contract.observed_lan_address()) == "192.168.1.26"


def test_a_host_with_no_routable_address_fails_closed(monkeypatch) -> None:
    monkeypatch.setattr(
        primitives,
        "run",
        lambda command, **kwargs: subprocess.CompletedProcess(command, 1, "", "unreachable"),
    )

    with pytest.raises(TargetError, match="no routable IPv4"):
        app_contract.observed_lan_address()


def test_a_loopback_default_route_is_refused(monkeypatch) -> None:
    monkeypatch.setattr(
        primitives,
        "run",
        lambda command, **kwargs: subprocess.CompletedProcess(
            command, 0, "1.1.1.1 dev lo src 127.0.0.1 uid 0\n", ""
        ),
    )

    with pytest.raises(TargetError, match="must be private IPv4"):
        app_contract.observed_lan_address()


def test_host_env_carries_configuration_that_is_not_a_secret() -> None:
    """A product Host runs a smaller encoder than a laptop, and that is not a
    credential — putting it in the private input set would mean reissuing every
    token to change it, since those inputs are never overwritten."""

    assert "EIDOLON_MEMORY_EMBEDDING_MODEL=bge-small-zh" in contract.HOST_ENV_VALUE
    assert "TOKEN" not in contract.HOST_ENV_VALUE
    assert "KEY" not in contract.HOST_ENV_VALUE


def test_the_host_states_where_the_port_registry_is() -> None:
    """Admin defaults to an ``eidolon_ops`` checkout beside its own source.

    That is a workstation shape. On a Host there is no checkout, so Admin got
    an empty registry and raised ``KeyError: 'admin'`` before serving anything
    — the whole Pi install then failed its readiness gate on Admin.
    """

    assert f"EIDOLON_PORTS_FILE={contract.HOST_PORTS_PATH}" in contract.HOST_ENV_VALUE


def test_the_port_registry_is_carried_not_restated(tmp_path: Path) -> None:
    """The registry has one author, on the operator side.

    A copy of it inside the target agent was a second, and the copy a Host
    wrote for itself is precisely the one nobody would think to update.
    """

    assert not hasattr(contract, "HOST_PORTS_VALUE")

    registry = "admin:\n  api:\n    port: 9000\n"
    contract.ensure_host_path_contract(tmp_path, lambda *_a: None, registry)

    written = tmp_path / contract.HOST_PORTS_PATH.relative_to("/")
    assert written.read_text(encoding="utf-8") == registry
    assert oct(written.stat().st_mode)[-3:] == "640"


def test_an_operation_without_a_port_registry_is_refused() -> None:
    """Inventing one would put a Host's own guess where Admin looks."""

    with pytest.raises(TargetError, match="port registry is missing"):
        contract.fixed_port_registry({"units": []})

    with pytest.raises(TargetError, match="port registry is missing"):
        contract.fixed_port_registry({"port_registry": "   "})


def _app_contract(**overrides: object) -> dict[str, object]:
    host_id = "ehost-0123456789abcdefabcd"
    suffix = host_id.removeprefix("ehost-")
    return {
        "host_id": host_id,
        "owner_domain_id": "owner-0123456789abcdefabcd",
        "hub_hostname": f"eidolon-hub-{suffix}.local",
        "hub_https_port": 8443,
        "hub_origin": f"https://eidolon-hub-{suffix}.local:8443",
        "livekit_client_url": "wss://placeholder.invalid:7880",
        "allow_insecure_livekit": True,
        **overrides,
    }


def test_the_resolved_lan_address_travels_with_the_app_contract(monkeypatch) -> None:
    """Discovery happens during validation, on the Host that owns the answer.

    Throwing the result away left the readiness reporter to read a declared
    ``lan_ipv4`` that no longer has to exist, and the install died on
    ``KeyError: 'lan_ipv4'`` after the release was already activated.
    """

    monkeypatch.setattr(app_contract, "observed_lan_address", lambda: IPv4Address("192.168.1.26"))

    result = app_contract.fixed_app({"app": _app_contract()})

    assert result["lan_ipv4"] == "192.168.1.26"


def test_a_declared_lan_address_is_still_honoured() -> None:
    result = app_contract.fixed_app({"app": _app_contract(lan_ipv4="10.0.0.4")})

    assert result["lan_ipv4"] == "10.0.0.4"


def test_the_readiness_reporter_finds_the_address_the_contract_carries(monkeypatch) -> None:
    """The exact seam that broke: every key the reporter indexes must be there."""

    monkeypatch.setattr(app_contract, "observed_lan_address", lambda: IPv4Address("192.168.1.26"))
    app = app_contract.fixed_app({"app": _app_contract()})

    for key in (
        "hub_hostname",
        "owner_domain_id",
        "lan_ipv4",
        "hub_https_port",
        "hub_origin",
        "host_id",
    ):
        assert key in app


def _ingress_installed(root: Path) -> Path:
    unit = root / contract.HOST_APPLICATION_INPUTS["hub-ingress.service"][0].relative_to("/")
    unit.parent.mkdir(parents=True, exist_ok=True)
    unit.write_text("[Unit]\n", encoding="utf-8")
    return unit


def test_the_host_layer_is_waited_for_never_started(tmp_path: Path) -> None:
    """Two call sites used to each start the ingress by hand.

    The Hub now declares Wants= on it, so start belongs to systemd. What Ops
    still owes is the barrier: a release's readiness set covers release
    components only, and the App gate downstream reads the Hub port once.
    """

    _ingress_installed(tmp_path)
    calls: list[tuple[str, ...]] = []

    def run(command, **_kwargs):
        calls.append(tuple(command))
        return subprocess.CompletedProcess(command, 0, "active\n", "")

    host_application.await_host_application(run, tmp_path)

    assert calls == [("/usr/bin/systemctl", "is-active", host_application.HOST_APPLICATION_UNIT)]
    assert not [call for call in calls if "start" in call]


def test_a_host_without_the_layer_is_not_waited_for(tmp_path: Path) -> None:
    """A Host profile with no [app] never installs the unit; asking systemd
    about a unit that was never written would fail forever."""

    calls: list[tuple[str, ...]] = []

    def run(command, **_kwargs):
        calls.append(tuple(command))
        return subprocess.CompletedProcess(command, 3, "inactive\n", "")

    host_application.await_host_application(run, tmp_path)

    assert calls == []


def test_a_host_layer_that_never_opens_fails_the_install(tmp_path: Path, monkeypatch) -> None:
    _ingress_installed(tmp_path)
    monkeypatch.setattr(host_application, "HOST_APPLICATION_READY_SECONDS", 0.0)
    monkeypatch.setattr(host_application.time, "sleep", lambda _seconds: None)

    def run(command, **_kwargs):
        return subprocess.CompletedProcess(command, 3, "activating\n", "")

    with pytest.raises(TargetError, match="ingress is not active: activating"):
        host_application.await_host_application(run, tmp_path)


def test_a_host_states_how_long_its_own_services_need() -> None:
    """A board is not a laptop, and a deadline compiled in cannot say so.

    The Channel worker spends its stop timeout shutting down and then loads an
    ONNX model coming up; at 90s a rollback reported a readiness timeout for
    services that were healthy moments later, and undid a good release for it.
    """

    assert contract.release_readiness_seconds({"readiness_timeout_seconds": 600}) == 600
    assert contract.release_readiness_seconds({}) == contract.DEFAULT_RELEASE_READINESS_SECONDS


def test_a_readiness_deadline_outside_reason_is_refused() -> None:
    for value in (0, 29, 1801, "240", True, None):
        with pytest.raises(TargetError, match="readiness timeout is invalid"):
            contract.release_readiness_seconds({"readiness_timeout_seconds": value})


def test_a_setup_code_is_issued_through_the_hosts_own_control_socket(monkeypatch, tmp_path) -> None:
    """Authority is reaching the socket, not the build being a development one.

    Issuance used to be refused unless the process ran in development mode, and
    nothing else could create a commissioning session — so a shipped Host could
    not be claimed by any phone, ever.
    """

    monkeypatch.setattr(host_lifecycle, "BOOTSTRAP_CTL", tmp_path / "eidolon-bootstrapctl")
    host_lifecycle.BOOTSTRAP_CTL.write_text("#!/bin/sh\n", encoding="utf-8")
    host_lifecycle.BOOTSTRAP_CTL.chmod(0o755)
    calls: list[tuple[str, ...]] = []

    def run(command, **_kwargs):
        calls.append(tuple(command))
        return subprocess.CompletedProcess(
            command,
            0,
            "Setup code: 48273916\n"
            "Host: ehost-0123456789abcdefabcd\n"
            "Commissioning: 123e4567-e89b-42d3-a456-426614174000\n"
            "Expires: 2026-08-12T01:00:00Z\n",
            "",
        )

    monkeypatch.setattr(primitives, "run", run)

    result = host_lifecycle.commissioning_code(
        {"units": list(contract.PRODUCT_UNITS), "ttl_seconds": 600}
    )

    assert calls[0][1:] == ("commissioning-code", "--ttl", "600")
    assert result["setup_code"] == "48273916"
    # The phone needs the session as well as the code; the code alone has
    # nowhere to be spent.
    assert result["commissioning_id"] == "123e4567-e89b-42d3-a456-426614174000"
    assert result["expires_at"] == "2026-08-12T01:00:00Z"


def test_a_named_setup_code_reaches_the_host_unexamined(monkeypatch, tmp_path) -> None:
    """This hop forwards the value and does not judge it.

    The Host owns the rule about what a usable code is, so a second opinion
    here could only ever disagree with it — and the disagreement would surface
    three hops from where the value was written.
    """

    monkeypatch.setattr(host_lifecycle, "BOOTSTRAP_CTL", tmp_path / "eidolon-bootstrapctl")
    host_lifecycle.BOOTSTRAP_CTL.write_text("#!/bin/sh\n", encoding="utf-8")
    host_lifecycle.BOOTSTRAP_CTL.chmod(0o755)
    calls: list[tuple[str, ...]] = []

    def run(command, **_kwargs):
        calls.append(tuple(command))
        return SimpleNamespace(
            returncode=0,
            stdout=(
                "Setup code: 99999990\n"
                "Host: ehost-0123456789abcdefabcd\n"
                "Commissioning: 123e4567-e89b-42d3-a456-426614174000\n"
                "Expires: 2026-08-12T01:00:00Z\n"
            ),
            stderr="",
        )

    monkeypatch.setattr(primitives, "run", run)

    result = host_lifecycle.commissioning_code(
        {
            "units": list(contract.PRODUCT_UNITS),
            "ttl_seconds": 600,
            "setup_code": "99999990",
        }
    )

    assert calls[0][1:] == ("commissioning-code", "--ttl", "600", "--code", "99999990")
    assert result["setup_code"] == "99999990"

    with pytest.raises(TargetError, match="setup_code must be a string"):
        host_lifecycle.commissioning_code(
            {
                "units": list(contract.PRODUCT_UNITS),
                "ttl_seconds": 600,
                "setup_code": 99999990,
            }
        )


def test_a_window_with_no_deadline_is_reported_as_nothing_not_as_a_word(
    monkeypatch, tmp_path
) -> None:
    """The Evidence may not put a timestamp-shaped word where no time exists.

    A commissioning window stopped having a clock on it (``eidolon_admin``
    ADR-0007): it closes by being consumed or superseded, and every session
    minted now carries no deadline at all. The Host prints that field through
    an f-string, so the absence reaches this parser as the four characters
    ``None`` — and reading it verbatim published ``"expires_at": "None"``, an
    answer no consumer can parse and one that reads as a deadline rather than
    as the deadline nobody set.
    """

    monkeypatch.setattr(host_lifecycle, "BOOTSTRAP_CTL", tmp_path / "eidolon-bootstrapctl")
    host_lifecycle.BOOTSTRAP_CTL.write_text("#!/bin/sh\n", encoding="utf-8")
    host_lifecycle.BOOTSTRAP_CTL.chmod(0o755)
    stdout = (
        "Setup code: 99999990\n"
        "Host: ehost-0123456789abcdefabcd\n"
        "Commissioning: e2346cb6-edd8-4a6b-874a-71001228cce7\n"
        "Expires: None\n"
    )

    def run(command, **_kwargs):
        return subprocess.CompletedProcess(command, 0, stdout, "")

    monkeypatch.setattr(primitives, "run", run)
    payload = {"units": list(contract.PRODUCT_UNITS), "ttl_seconds": 600}

    result = host_lifecycle.commissioning_code(payload)

    assert result["expires_at"] is None
    assert json.dumps(result, sort_keys=True).count('"expires_at": null') == 1
    # The session is still named, so the code the operator reads has somewhere
    # to be spent.
    assert result["commissioning_id"] == "e2346cb6-edd8-4a6b-874a-71001228cce7"

    # A Host that says nothing at all about a deadline is reporting the same
    # fact, and the empty string may not stand in for a timestamp either.
    stdout = stdout.replace("Expires: None\n", "")

    assert host_lifecycle.commissioning_code(payload)["expires_at"] is None


def test_a_setup_code_request_without_a_sane_lifetime_is_refused(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(host_lifecycle, "BOOTSTRAP_CTL", tmp_path / "eidolon-bootstrapctl")
    for ttl in (0, 59, 86401, "600", True, None):
        with pytest.raises(TargetError, match="TTL must be between"):
            host_lifecycle.commissioning_code(
                {"units": list(contract.PRODUCT_UNITS), "ttl_seconds": ttl}
            )


def test_the_derived_host_layer_is_delivered_without_a_reinstall(tmp_path, monkeypatch) -> None:
    """An activation replaces components and leaves this layer as it found it.

    A fix to the ingress unit could therefore reach a Host no way but by
    installing it again, which is exactly what happened: updates kept
    succeeding while the fix sat undelivered.
    """

    monkeypatch.setattr(contract, "VAR_TMP", tmp_path / "var-tmp")
    stage = tmp_path / "var-tmp" / "eidolon-secrets-r1"
    stage.mkdir(parents=True)
    for name in contract.REFRESHABLE_HOST_LAYER_INPUTS:
        (stage / name).write_text(f"new-{name}", encoding="utf-8")
    monkeypatch.setattr(primitives, "chown_path", lambda *_a: None)
    monkeypatch.setattr(host_application, "_expected_ids", lambda *_a: (os.getuid(), os.getgid()))
    monkeypatch.setattr(
        primitives, "checked", lambda *_a, **_k: subprocess.CompletedProcess((), 0, "", "")
    )
    reconciled: list[tuple[Path, str, frozenset[str]]] = []
    monkeypatch.setattr(
        contract,
        "ensure_host_path_contract",
        lambda root, _chown, registry, capabilities=frozenset(): reconciled.append(
            (root, registry, capabilities)
        ),
    )
    placed: dict[str, Path] = {}
    for name in contract.REFRESHABLE_HOST_LAYER_INPUTS:
        destination = tmp_path / "host" / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        placed[name] = destination
    monkeypatch.setattr(
        contract,
        "INSTALL_INPUTS",
        {
            **{
                name: (placed[name], "root", "root", 0o644)
                for name in contract.REFRESHABLE_HOST_LAYER_INPUTS
            }
        },
    )

    result = host_application.refresh_host_application(
        {
            "units": list(contract.PRODUCT_UNITS),
            "release_id": "r1",
            "port_registry": "admin:\n  api:\n    port: 9000\n",
        }
    )

    assert result["status"] == "refreshed"
    assert len(result["changed"]) == len(contract.REFRESHABLE_HOST_LAYER_INPUTS)
    for name, destination in placed.items():
        assert destination.read_text(encoding="utf-8") == f"new-{name}"
    # The refresh reconciles the path contract with the same capability
    # declaration the payload's unit topology was derived from — the sealed
    # Host profile is where eidolond and the applier read it.
    assert reconciled == [(Path("/"), "admin:\n  api:\n    port: 9000\n", frozenset())]

    # Second run has nothing to deliver, so systemd is left alone.
    assert (
        host_application.refresh_host_application(
            {
                "units": list(contract.PRODUCT_UNITS),
                "release_id": "r1",
                "port_registry": "admin:\n  api:\n    port: 9000\n",
            }
        )["changed"]
        == []
    )
    assert reconciled == [
        (Path("/"), "admin:\n  api:\n    port: 9000\n", frozenset()),
        (Path("/"), "admin:\n  api:\n    port: 9000\n", frozenset()),
    ]

    placed["owner-domain-root-ca.pem"].chmod(0o666)
    with pytest.raises(TargetError, match="ownership or mode drifted"):
        host_application.refresh_host_application(
            {
                "units": list(contract.PRODUCT_UNITS),
                "release_id": "r1",
                "port_registry": "admin:\n  api:\n    port: 9000\n",
            }
        )


def test_host_layer_refuses_settings_that_previous_release_cannot_parse(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr(contract, "VAR_TMP", tmp_path / "var-tmp")
    stage = tmp_path / "var-tmp" / "eidolon-secrets-r1"
    stage.mkdir(parents=True)
    for name in contract.REFRESHABLE_HOST_LAYER_INPUTS:
        (stage / name).write_text(f"new-{name}", encoding="utf-8")
    (stage / "commissioning-secrets.json").write_text("private", encoding="utf-8")
    destination = tmp_path / "host/hub.yaml"
    destination.parent.mkdir(parents=True)
    destination.write_text("old-compatible", encoding="utf-8")
    registry_destination = tmp_path / "host/commissioning-secrets.json"
    registry_destination.write_text("old-private", encoding="utf-8")
    registry_destination.chmod(0o640)
    monkeypatch.setattr(
        contract,
        "INSTALL_INPUTS",
        {
            name: (
                destination if name == "hub.generated.yaml" else tmp_path / "host" / name,
                "root",
                "root",
                0o644,
            )
            for name in contract.REFRESHABLE_HOST_LAYER_INPUTS
        },
    )

    def reject_previous(label, command, **_kwargs):
        assert label == "cross-release Hub settings validation"
        if "/opt/eidolon/current/" in command[2]:
            raise TargetError("previous Hub rejected candidate settings")
        return subprocess.CompletedProcess((), 0, "", "")

    monkeypatch.setattr(primitives, "checked", reject_previous)

    with pytest.raises(TargetError, match="previous Hub rejected"):
        host_application.refresh_host_application(
            {
                "units": list(contract.PRODUCT_UNITS),
                "release_id": "r1",
                "port_registry": "admin:\n  api:\n    port: 9000\n",
            }
        )

    assert destination.read_text(encoding="utf-8") == "old-compatible"
    assert registry_destination.read_text(encoding="utf-8") == "old-private"


def test_a_refresh_takes_away_what_a_release_no_longer_installs(tmp_path, monkeypatch) -> None:
    """Dropping an asset from a release stops it being written, not being there.

    The Hub settings copy at /etc/eidolon/hub.yaml is the case this exists for:
    it is read by nothing, it carries a retired template placeholder, and a
    Host installed before it was dropped keeps it until someone removes it.
    """

    legacy = tmp_path / "etc/eidolon/hub.yaml"
    legacy.parent.mkdir(parents=True)
    legacy.write_text("onboarding:\n  owner_domain_id: owner-local\n", encoding="utf-8")
    monkeypatch.setattr(contract, "LEGACY_SYSTEM_ASSETS", (Path("/etc/eidolon/hub.yaml"),))

    assert host_application.remove_legacy_system_assets(tmp_path) == ["/etc/eidolon/hub.yaml"]
    assert not legacy.exists()
    # Nothing left to take away, and a Host that never had it is not an error.
    assert host_application.remove_legacy_system_assets(tmp_path) == []


def test_refresh_rewrites_host_tls_but_never_owner_signing_authority() -> None:
    """Host identity is repairable; Owner signing authority never leaves Ops."""

    assert "hub.crt" in contract.REFRESHABLE_HOST_APPLICATION_INPUTS
    assert "hub.key" in contract.REFRESHABLE_HOST_APPLICATION_INPUTS
    assert "owner-domain-root-ca.pem" in contract.REFRESHABLE_HOST_APPLICATION_INPUTS
    assert "authority-signing-certificate.pem" in contract.REFRESHABLE_HOST_APPLICATION_INPUTS
    assert set(contract.REFRESHABLE_HOST_APPLICATION_INPUTS) <= set(contract.INSTALL_INPUTS)
    assert set(contract.REFRESHABLE_HOST_BOUND_INPUTS) == {"local-api.env", "channel.env"}
    assert set(contract.REFRESHABLE_PRODUCT_SETTINGS) == {
        "agent.yaml",
        "channel.yaml",
        "memory.yaml",
    }
    assert set(contract.REFRESHABLE_HOST_LAYER_INPUTS) <= set(contract.INSTALL_INPUTS)
    # There is no optional Host application input any more. The one that
    # existed was a per-device commissioning registry belonging to no sealed
    # release, which is how a rollback to code reading an older format of it
    # left the Hub restarting 110 times.
    assert not hasattr(contract, "OPTIONAL_HOST_APPLICATION_INPUTS")
    assert "commissioning-secrets.json" not in contract.INSTALL_INPUTS
    assert contract.INSTALL_INPUTS == contract.BASE_INSTALL_INPUTS


def test_public_owner_trust_does_not_grant_access_to_private_host_inputs() -> None:
    """Verification material is shared by services; credentials are not.

    Local API is deliberately not in the broad ``eidolon`` group.  It reaches
    only known paths through the non-enumerable config root, and the three
    public files are root-owned.  Widening Hub's key, settings, or environment
    files along with them would erase that boundary.
    """

    directories = {
        path: (mode, owner, group) for path, mode, owner, group in contract.HOST_DIRECTORIES
    }
    assert directories[Path("/etc/eidolon")] == (0o751, "root", "eidolon")
    assert directories[Path("/etc/eidolon/owner-domain")] == (
        0o750,
        "root",
        "eidolon-owner-trust-readers",
    )

    public_names = {
        "owner-domain-descriptor.json",
        "owner-domain-root-ca.pem",
        "authority-signing-certificate.pem",
    }
    assert {name: contract.HOST_APPLICATION_INPUTS[name][1:] for name in public_names} == {
        name: ("root", "eidolon-owner-trust-readers", 0o640) for name in public_names
    }

    assert contract.HOST_APPLICATION_INPUTS["hub.key"][1:] == ("root", "eidolon", 0o640)
    assert contract.HOST_APPLICATION_INPUTS["hub.generated.yaml"][1:] == (
        "root",
        "eidolon",
        0o640,
    )
    assert contract.SECRET_INPUTS["local-api.env"][1:] == ("root", "root", 0o600)


def _authority_reset_payload() -> dict[str, object]:
    return {
        "units": list(contract.PRODUCT_UNITS),
        "authority_reset": {
            "owner_domain_id": "owner-0123456789abcdefabcd",
            "previous_generation": 1,
            "next_generation": 2,
            "state_id": "authority-state_0123456789abcdef",
        },
    }


def _stage_authority_reset_inputs(root: Path) -> dict[str, object]:
    request = _authority_reset_payload()["authority_reset"]
    assert isinstance(request, dict)
    expected = {
        "contract_version": 1,
        "owner_domain_id": request["owner_domain_id"],
        "owner_domain_generation": request["next_generation"],
        "state_id": request["state_id"],
    }
    descriptor = root / authority_reset.OWNER_DESCRIPTOR.relative_to("/")
    descriptor.parent.mkdir(parents=True)
    descriptor.write_text(
        json.dumps(
            {
                "owner_domain_id": expected["owner_domain_id"],
                "owner_domain_generation": expected["owner_domain_generation"],
            }
        ),
        encoding="utf-8",
    )
    bootstrap = root / authority_reset.AUTHORITY_BOOTSTRAP.relative_to("/")
    bootstrap.parent.mkdir(parents=True)
    bootstrap.write_text(
        json.dumps({"operation": "owner-authority.bootstrap", **expected}),
        encoding="utf-8",
    )
    database = root / authority_reset.HUB_DATABASE.relative_to("/")
    connection = sqlite3.connect(database)
    connection.execute("CREATE TABLE legacy_devices (device_id TEXT PRIMARY KEY)")
    connection.commit()
    connection.close()
    return expected


def test_owner_authority_reset_is_targeted_monotonic_and_proven(tmp_path: Path) -> None:
    expected = _stage_authority_reset_inputs(tmp_path)
    database = tmp_path / authority_reset.HUB_DATABASE.relative_to("/")
    unrelated = database.parent / "preserved.db"
    unrelated.write_text("keep", encoding="utf-8")
    calls: list[tuple[str, ...]] = []

    def command(value, **_kwargs):
        value = tuple(value)
        calls.append(value)
        if value[:3] == ("/usr/bin/systemctl", "start", authority_reset.HUB_UNIT):
            connection = sqlite3.connect(database)
            connection.execute(
                "CREATE TABLE hub_authority_state ("
                "singleton_id INTEGER PRIMARY KEY, owner_domain_id TEXT, "
                "owner_domain_generation INTEGER, state_id TEXT)"
            )
            connection.execute(
                "INSERT INTO hub_authority_state VALUES (1, ?, ?, ?)",
                (
                    expected["owner_domain_id"],
                    expected["owner_domain_generation"],
                    expected["state_id"],
                ),
            )
            connection.commit()
            connection.close()
            anchor = tmp_path / authority_reset.AUTHORITY_ANCHOR.relative_to("/")
            anchor.write_text(json.dumps(expected), encoding="utf-8")
            (tmp_path / authority_reset.AUTHORITY_BOOTSTRAP.relative_to("/")).unlink()
        if value[:2] == ("/usr/bin/systemctl", "is-active"):
            return subprocess.CompletedProcess(value, 0, "active\n", "")
        return subprocess.CompletedProcess(value, 0, "", "")

    plan = authority_reset.authority_reset_plan(_authority_reset_payload(), root=tmp_path)
    assert plan["status"] == "planned"
    assert plan["destructive_work_required"] is True

    result = authority_reset.reset_owner_authority(
        _authority_reset_payload(), root=tmp_path, command=command
    )

    assert result["status"] == "authority_reset"
    assert result["authority"] == expected
    assert unrelated.read_text(encoding="utf-8") == "keep"
    assert calls[0] == (
        "/usr/bin/systemctl",
        "stop",
        authority_reset.HUB_INGRESS_UNIT,
        authority_reset.HUB_UNIT,
    )
    assert (
        authority_reset.authority_reset_plan(_authority_reset_payload(), root=tmp_path)["status"]
        == "already_reset"
    )


def test_owner_authority_reset_refuses_partial_lineage(tmp_path: Path) -> None:
    expected = _stage_authority_reset_inputs(tmp_path)
    anchor = tmp_path / authority_reset.AUTHORITY_ANCHOR.relative_to("/")
    anchor.write_text(json.dumps(expected), encoding="utf-8")

    with pytest.raises(TargetError, match="partially committed"):
        authority_reset.authority_reset_plan(_authority_reset_payload(), root=tmp_path)


def _authority_fixture(tmp_path: Path, monkeypatch):
    """Give the Host a set of real SQLite authorities to snapshot."""

    monkeypatch.setattr(contract, "VAR_TMP", tmp_path / "var-tmp")
    (tmp_path / "var-tmp").mkdir(parents=True)
    table = {}
    for name in contract.BACKED_UP_AUTHORITIES:
        database = tmp_path / "authorities" / f"{name}.sqlite3"
        database.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(database)
        connection.execute("CREATE TABLE truth (value TEXT)")
        connection.execute("INSERT INTO truth VALUES (?)", (f"{name}-before",))
        connection.commit()
        connection.close()
        table[name] = (database, "root", "root")
    monkeypatch.setattr(contract, "BACKED_UP_AUTHORITIES", table)
    monkeypatch.setattr(primitives, "chown_path", lambda *_a: None)
    monkeypatch.setattr(
        primitives, "checked", lambda *_a, **_k: subprocess.CompletedProcess((), 0, "", "")
    )
    monkeypatch.setattr(authorities, "host_id_or_none", lambda: "ehost-0123456789abcdefabcd")
    return table


#: The payload every authority operation carries. Memory's address is in it
#: because a backup asks memory for a copy of each space rather than copying a
#: palace this agent does not understand.
BACKUP_PAYLOAD = {
    "units": list(contract.PRODUCT_UNITS),
    "release_id": "r1",
    "memory_admin_url": "http://127.0.0.1:8019",
}


class _FakeMemory:
    """A supervisor that answers the two actions memory declares.

    A stub rather than a mock of ``capture``: what is worth testing on this side
    is the operator half — that the manifest is checked against what landed, and
    that a space that cannot be copied is reported rather than dropped.
    """

    def __init__(self, *, realms: tuple[str, ...] = ("r_owner_one",)) -> None:
        self.realms = realms
        self.snapshotted: list[str] = []
        self.restored: list[tuple[str, str]] = []
        self.refuse: str | None = None
        self.corrupt_after_snapshot = False

    def __call__(self, base_url, path, *, method, body, timeout):
        if self.refuse is not None:
            raise TargetError(self.refuse)
        if path == "/api/admin/realms":
            return [{"spec": {"memory_realm_id": realm}} for realm in self.realms]
        parts = path.strip("/").split("/")
        realm = parts[3]
        if parts[-1] == "snapshot":
            return {"manifest": self._write(realm, Path(body["destination"]))}
        if parts[-1] == "restore":
            self.restored.append((realm, str(body["source"])))
            return {"worker_running": True, "restored": {"taken_at": "2026-08-24T00:00:00Z"}}
        raise AssertionError(f"unexpected call: {method} {path}")

    def _write(self, realm: str, destination: Path) -> dict:
        self.snapshotted.append(realm)
        entries = []
        for relative in ("palace/chroma.sqlite3", "ledgers/knowledge_graph.sqlite3"):
            path = destination / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(f"{realm}:{relative}", encoding="utf-8")
            entries.append(
                {
                    "path": relative,
                    "method": "sqlite-vacuum-into",
                    "sha256": primitives.file_sha256(path),
                    "bytes": path.stat().st_size,
                }
            )
        if self.corrupt_after_snapshot:
            (destination / "palace/chroma.sqlite3").write_text("not what was copied")
        manifest = {
            "contract_version": "1",
            "memory_space_id": realm,
            "taken_at": "2026-08-24T00:00:00Z",
            "embedder_identity": "bge_base_zh_v15",
            "entries": entries,
        }
        (destination / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        return manifest


def _memory_fixture(monkeypatch, **kwargs) -> _FakeMemory:
    fake = _FakeMemory(**kwargs)
    monkeypatch.setattr(memory_realms, "_request", fake)
    # Ownership is the one thing a test cannot exercise: the accounts do not
    # exist here, and the agent runs as root on a Host.
    monkeypatch.setattr(memory_realms.primitives, "chown_path", lambda *_a: None)
    return fake


def test_a_backup_covers_every_authority_and_names_what_it_cannot(tmp_path, monkeypatch) -> None:
    """A backup believed to be complete is worse than one known to be partial."""

    table = _authority_fixture(tmp_path, monkeypatch)
    _memory_fixture(monkeypatch)

    result = authorities.backup(BACKUP_PAYLOAD)

    assert result["status"] == "captured"
    assert {entry["authority"] for entry in result["authorities"]} == set(table)
    assert {entry["state"] for entry in result["not_covered"]} == set(contract.UNCOVERED_STATE)
    for entry in result["not_covered"]:
        assert entry["reason"]
    assert "not a point-in-time image" in result["consistency"]
    for entry in result["authorities"]:
        copy = Path(result["directory"]) / entry["file"]
        assert copy.is_file()
        assert primitives.file_sha256(copy) == entry["sha256"]


def test_a_backup_round_trips_through_a_restore(tmp_path, monkeypatch) -> None:
    """A backup nobody has restored is not known to work."""

    table = _authority_fixture(tmp_path, monkeypatch)
    _memory_fixture(monkeypatch)
    monkeypatch.setattr(host_reset, "command_stop_units", lambda units: list(units))
    monkeypatch.setattr(host_lifecycle, "lifecycle", lambda *_a: {"status": "started"})
    payload = BACKUP_PAYLOAD
    manifest = authorities.backup(payload)

    for name, (database, _user, _group) in table.items():
        connection = sqlite3.connect(database)
        connection.execute("UPDATE truth SET value = ?", (f"{name}-after",))
        connection.commit()
        connection.close()

    result = authorities.restore({**payload, "manifest": manifest})

    assert result["status"] == "restored"
    # The product is running again; an operator should not have to know which
    # units to start and in what order.
    assert result["started"] == "started"
    assert sorted(result["restored"]) == sorted(table)
    for name, (database, _user, _group) in table.items():
        connection = sqlite3.connect(database)
        try:
            assert connection.execute("SELECT value FROM truth").fetchone()[0] == f"{name}-before"
        finally:
            connection.close()


def test_a_backup_carries_every_memory_space_and_says_where(tmp_path, monkeypatch) -> None:
    """Memory is the one authority this agent does not copy itself.

    A space is a palace directory whose layout belongs to MemPalace plus the
    embedder identity its vectors were produced under, so the component makes
    the copy and this collects it. That declaration is what took memory off the
    uncovered list, and the backup should now be able to show it.
    """

    _authority_fixture(tmp_path, monkeypatch)
    memory = _memory_fixture(monkeypatch, realms=("r_owner_one", "r_owner_two"))

    result = authorities.backup(BACKUP_PAYLOAD)

    assert memory.snapshotted == ["r_owner_one", "r_owner_two"]
    assert "memory" not in {entry["state"] for entry in result["not_covered"]}
    spaces = {entry["memory_space_id"]: entry for entry in result["memory_spaces"]}
    assert set(spaces) == {"r_owner_one", "r_owner_two"}
    for realm, entry in spaces.items():
        # Relative to the backup, because the directory travels to a workstation
        # and back and an absolute path from this Host would not survive it.
        copy = Path(result["directory"]) / str(entry["directory"])
        assert copy.is_dir() and copy.name == realm
        assert entry["embedder_identity"] == "bge_base_zh_v15"
        assert entry["file_count"] == 2


def test_a_memory_copy_that_does_not_match_its_manifest_is_not_reported_as_carried(
    tmp_path, monkeypatch
) -> None:
    """The operator half of the declaration is checking it.

    Memory decides what a whole space is; this decides whether what landed on
    disk is that. A manifest nobody checks describes a backup rather than being
    one — and a copy that fails the check is named as not carried rather than
    listed among the spaces, because the whole point of the list is that an
    operator can restore from it.
    """

    _authority_fixture(tmp_path, monkeypatch)
    _memory_fixture(monkeypatch).corrupt_after_snapshot = True

    result = authorities.backup(BACKUP_PAYLOAD)

    assert result["memory_spaces"] == []
    reason = {entry["state"]: entry["reason"] for entry in result["not_covered"]}["memory"]
    assert "does not match its digest" in reason
    # And the copy that failed the check is gone: a directory nobody should
    # restore from should not be sitting in the backup looking restorable.
    assert not (Path(result["directory"]) / memory_realms.SPACES_DIRECTORY).exists()


def test_a_host_whose_memory_cannot_answer_still_gets_a_backup_that_says_so(
    tmp_path, monkeypatch
) -> None:
    """Its authorities are still worth copying.

    A backup that refused to exist would leave the operator with nothing on the
    day they most need something. What it must not do is stay silent — so the
    same table that named memory while it had no declared snapshot names it
    again, with the reason it could not be reached this time.
    """

    table = _authority_fixture(tmp_path, monkeypatch)
    _memory_fixture(monkeypatch).refuse = "memory is not answering on http://127.0.0.1:8019"

    result = authorities.backup(BACKUP_PAYLOAD)

    assert {entry["authority"] for entry in result["authorities"]} == set(table)
    assert result["memory_spaces"] == []
    uncovered = {entry["state"]: entry for entry in result["not_covered"]}
    assert "not answering" in uncovered["memory"]["reason"]
    assert uncovered["memory"]["path"] == str(contract.MEMORY_STATE_ROOT)


def test_a_backup_that_was_not_told_where_memory_is_refuses(tmp_path, monkeypatch) -> None:
    """A missing address is this deployer's bug, not a fact about the Host.

    Degrading to "memory not covered" would turn a mistake in the payload into
    backups that quietly stop carrying anything an Eidolon remembers.
    """

    _authority_fixture(tmp_path, monkeypatch)
    _memory_fixture(monkeypatch)

    with pytest.raises(TargetError, match="memory admin URL"):
        authorities.backup({"units": list(contract.PRODUCT_UNITS), "release_id": "r1"})


def test_memory_spaces_go_back_only_once_the_product_is_running(tmp_path, monkeypatch) -> None:
    """Ordering is the whole point: only a live supervisor can take a realm's
    runner off its palace, and one process holds a palace."""

    _authority_fixture(tmp_path, monkeypatch)
    memory = _memory_fixture(monkeypatch)
    order: list[str] = []
    monkeypatch.setattr(host_reset, "command_stop_units", lambda units: list(units))

    def _started(*_args):
        order.append("started")
        return {"status": "started"}

    monkeypatch.setattr(host_lifecycle, "lifecycle", _started)
    manifest = authorities.backup(BACKUP_PAYLOAD)
    original = memory_realms.put_back

    def _watched(*args, **kwargs):
        order.append("memory")
        return original(*args, **kwargs)

    monkeypatch.setattr(memory_realms, "put_back", _watched)

    result = authorities.restore({**BACKUP_PAYLOAD, "manifest": manifest})

    assert order == ["started", "memory"]
    assert [realm for realm, _source in memory.restored] == ["r_owner_one"]
    assert result["memory_spaces"] == [
        {
            "memory_space_id": "r_owner_one",
            "worker_running": True,
            "taken_at": "2026-08-24T00:00:00Z",
        }
    ]


def test_a_backup_taken_before_memory_declared_a_snapshot_is_still_restorable(
    tmp_path, monkeypatch
) -> None:
    """And says which state it did not carry, rather than failing or lying."""

    _authority_fixture(tmp_path, monkeypatch)
    _memory_fixture(monkeypatch)
    monkeypatch.setattr(host_reset, "command_stop_units", lambda units: list(units))
    monkeypatch.setattr(host_lifecycle, "lifecycle", lambda *_a: {"status": "started"})
    manifest = authorities.backup(BACKUP_PAYLOAD)
    del manifest["memory_spaces"]

    result = authorities.restore({**BACKUP_PAYLOAD, "manifest": manifest})

    assert result["status"] == "restored"
    assert "no memory spaces" in str(result["memory_spaces"])


def test_a_backup_from_another_host_is_refused(tmp_path, monkeypatch) -> None:
    """Restoring it would produce a machine whose Controller grants, Hub
    identity and TLS names all describe somewhere else."""

    _authority_fixture(tmp_path, monkeypatch)
    _memory_fixture(monkeypatch)
    payload = BACKUP_PAYLOAD
    manifest = authorities.backup(payload)
    manifest["host_id"] = "ehost-ffffffffffffffffffff"

    with pytest.raises(TargetError, match="different Host"):
        authorities.restore({**payload, "manifest": manifest})


def test_a_backup_that_no_longer_matches_its_digest_is_refused(tmp_path, monkeypatch) -> None:
    _authority_fixture(tmp_path, monkeypatch)
    _memory_fixture(monkeypatch)
    payload = BACKUP_PAYLOAD
    manifest = authorities.backup(payload)
    tampered = Path(manifest["directory"]) / manifest["authorities"][0]["file"]
    tampered.write_bytes(tampered.read_bytes() + b"\x00")

    with pytest.raises(TargetError, match="does not match its digest"):
        authorities.restore({**payload, "manifest": manifest})


def test_a_backup_missing_an_authority_is_refused(tmp_path, monkeypatch) -> None:
    """Restoring some of the authorities leaves the Host describing two
    different pasts at once."""

    _authority_fixture(tmp_path, monkeypatch)
    _memory_fixture(monkeypatch)
    payload = BACKUP_PAYLOAD
    manifest = authorities.backup(payload)
    manifest["authorities"] = manifest["authorities"][:-1]

    with pytest.raises(TargetError, match="every authority"):
        authorities.restore({**payload, "manifest": manifest})


def test_a_plaintext_livekit_origin_may_name_the_host_it_belongs_to(monkeypatch) -> None:
    """The operator side forbids a literal address once the address is
    discovered, and this side used to demand one. No value satisfied both, and
    what shipped to devices instead was wss://placeholder.invalid — which
    passed only because the check ignored every scheme but ws.
    """

    monkeypatch.setattr(app_contract, "observed_lan_address", lambda: IPv4Address("192.168.1.26"))
    host_id = "ehost-0123456789abcdefabcd"
    hub_hostname = f"eidolon-hub-{host_id.removeprefix('ehost-')}.local"

    for origin in (f"ws://{hub_hostname}:7880", "ws://192.168.1.26:7880"):
        result = app_contract.fixed_app({"app": _app_contract(livekit_client_url=origin)})
        assert result["livekit_client_url"] == origin


def test_a_livekit_origin_may_name_no_host_at_all(monkeypatch) -> None:
    """The third legal form, and the one Ops writes when nothing is declared.

    Both of the old values were wrong. A deploy-time address froze into an
    environment file and a Host that changed networks went on handing it out;
    the `.local` name it fell back to cannot be resolved by Android at all,
    because getaddrinfo does not do mDNS — the board shipped
    `ws://eidolon-hub-f89c0ecca5d0070a7989.local:7880`, which no phone could
    turn into an address.

    `ws://:7880` says the host is decided when a binding is minted. Naming
    nothing is not a weaker claim than naming this Host; it is no claim,
    resolved later on this Host, which is the only place the answer exists.
    """

    monkeypatch.setattr(app_contract, "observed_lan_address", lambda: IPv4Address("192.168.1.26"))

    result = app_contract.fixed_app({"app": _app_contract(livekit_client_url="ws://:7880")})

    assert result["livekit_client_url"] == "ws://:7880"


def test_a_livekit_origin_with_neither_host_nor_port_is_refused(monkeypatch) -> None:
    """Deferring the host is not licence to omit the port: the port is this
    Host's own listener and is known whenever the contract is written."""

    monkeypatch.setattr(app_contract, "observed_lan_address", lambda: IPv4Address("192.168.1.26"))

    with pytest.raises(TargetError, match="LiveKit origin is invalid"):
        app_contract.fixed_app({"app": _app_contract(livekit_client_url="ws://")})


def test_a_plaintext_livekit_origin_naming_somewhere_else_is_refused(monkeypatch) -> None:
    """A device sent this would open its microphone to another machine."""

    monkeypatch.setattr(app_contract, "observed_lan_address", lambda: IPv4Address("192.168.1.26"))

    for origin in (
        "ws://192.168.1.99:7880",
        "ws://eidolon-hub-ffffffffffffffffffff.local:7880",
        "ws://placeholder.invalid:7880",
    ):
        with pytest.raises(TargetError, match="LiveKit origin is invalid"):
            app_contract.fixed_app({"app": _app_contract(livekit_client_url=origin)})


def _hosts_file(root: Path) -> Path:
    return root / "etc/avahi/hosts"


def _no_reload_expected(monkeypatch) -> list[tuple[object, ...]]:
    reloads: list[tuple[object, ...]] = []
    monkeypatch.setattr(primitives, "chown_path", lambda *_a: None)
    monkeypatch.setattr(
        primitives,
        "checked",
        lambda _label, *command, **_k: (
            reloads.append(command) or subprocess.CompletedProcess((), 0, "", "")
        ),
    )
    return reloads


def test_a_host_stops_claiming_the_name_the_hub_answers_for(monkeypatch, tmp_path: Path) -> None:
    """Two responders, one name, and mDNS settles it by withdrawal.

    Ops registered `eidolon-hub-<host>.local` with avahi-daemon so an address
    answer would exist before the Hub started. The Hub publishes that same name
    itself through python-zeroconf, with every interface address. avahi saw the
    second claim and gave up the name outright — `Host name conflict for
    "eidolon-hub-...local", not established` — so the guarantee cost the name
    resolving at all, which is the failure it was added to prevent.
    """

    reloads = _no_reload_expected(monkeypatch)
    path = _hosts_file(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text("192.168.3.206 eidolon-hub-0123456789abcdefabcd.local\n", encoding="utf-8")

    changed = host_application.withdraw_hub_hostname(root=tmp_path)

    assert changed == ["/etc/avahi/hosts"]
    assert path.read_text(encoding="utf-8") == ""
    assert reloads == [(("/usr/bin/systemctl", "reload", "avahi-daemon.service"),)]


def test_a_stale_claim_at_an_address_the_host_no_longer_owns_is_withdrawn_too(
    monkeypatch, tmp_path: Path
) -> None:
    """The line found on the board: an address from a network it had left.

    The entry is matched by the name, not the address, because the address in a
    stale line is exactly what makes it wrong. Matching on the observed address
    would leave the harmful line and add a second one.
    """

    _no_reload_expected(monkeypatch)
    path = _hosts_file(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text("192.168.100.19 eidolon-hub-0123456789abcdefabcd.local\n", encoding="utf-8")

    assert host_application.withdraw_hub_hostname(root=tmp_path) == ["/etc/avahi/hosts"]
    assert path.read_text(encoding="utf-8") == ""


def test_registrations_this_host_does_not_own_are_kept(monkeypatch, tmp_path: Path) -> None:
    _no_reload_expected(monkeypatch)
    path = _hosts_file(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text(
        "10.0.0.9 something-else.local\n"
        "192.168.3.206 eidolon-hub-0123456789abcdefabcd.local\n",
        encoding="utf-8",
    )

    host_application.withdraw_hub_hostname(root=tmp_path)

    assert path.read_text(encoding="utf-8").splitlines() == ["10.0.0.9 something-else.local"]


def test_a_host_that_never_claimed_the_name_is_left_alone(monkeypatch, tmp_path: Path) -> None:
    """No file and nothing to withdraw means no responder reload."""

    monkeypatch.setattr(primitives, "checked", lambda *_a, **_k: pytest.fail("nothing to reload"))

    assert host_application.withdraw_hub_hostname(root=tmp_path) == []
    assert not _hosts_file(tmp_path).exists()

    path = _hosts_file(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text("10.0.0.9 something-else.local\n", encoding="utf-8")

    assert host_application.withdraw_hub_hostname(root=tmp_path) == []
    assert path.read_text(encoding="utf-8") == "10.0.0.9 something-else.local\n"


def test_delivering_the_host_layer_withdraws_the_hub_name(monkeypatch, tmp_path: Path) -> None:
    """Withdrawing correctly when called directly is not the same fact as being
    called. Hosts installed before this still carry the line, so the deploy
    path is what has to take it away."""

    hosts = tmp_path / "avahi-hosts"
    hosts.write_text("192.168.3.206 eidolon-hub-0123456789abcdefabcd.local\n", encoding="utf-8")
    monkeypatch.setattr(host_application, "_AVAHI_HOSTS", hosts)
    monkeypatch.setattr(primitives, "chown_path", lambda *_a: None)
    monkeypatch.setattr(
        primitives, "checked", lambda *_a, **_k: subprocess.CompletedProcess((), 0, "", "")
    )
    monkeypatch.setattr(contract, "ensure_host_path_contract", lambda *_a: None)
    monkeypatch.setattr(host_application, "remove_legacy_system_assets", lambda *_a: [])
    monkeypatch.setattr(host_application, "_validate_hub_settings_compatibility", lambda *_a: None)
    monkeypatch.setattr(
        host_application, "_validate_product_settings_compatibility", lambda *_a: None
    )
    monkeypatch.setattr(contract, "REFRESHABLE_HOST_LAYER_INPUTS", ())
    monkeypatch.setattr(contract, "INSTALL_INPUTS", {})
    stage = contract.VAR_TMP / "eidolon-secrets-r1"
    stage.mkdir(parents=True, exist_ok=True)

    result = host_application.refresh_host_application(
        {
            "units": list(contract.PRODUCT_UNITS),
            "release_id": "r1",
            "port_registry": "admin:\n  api:\n    port: 9000\n",
            "app": _app_contract(lan_ipv4="192.168.3.206"),
        }
    )

    assert str(hosts) in result["changed"]
    assert hosts.read_text(encoding="utf-8") == ""


def _establish_lineage(root: Path, *, anchor: bool = True) -> dict[str, object]:
    lineage = {
        "contract_version": 1,
        "owner_domain_id": "owner-0123456789abcdefabcd",
        "owner_domain_generation": 4,
        "state_id": "authority-state_0123456789abcdef",
    }
    database = root / authority_reset.HUB_DATABASE.relative_to("/")
    database.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(database)
    connection.execute(
        "CREATE TABLE hub_authority_state ("
        "singleton_id INTEGER PRIMARY KEY, owner_domain_id TEXT, "
        "owner_domain_generation INTEGER, state_id TEXT)"
    )
    connection.execute(
        "INSERT INTO hub_authority_state VALUES (1, ?, ?, ?)",
        (
            lineage["owner_domain_id"],
            lineage["owner_domain_generation"],
            lineage["state_id"],
        ),
    )
    connection.commit()
    connection.close()
    if anchor:
        (root / authority_reset.AUTHORITY_ANCHOR.relative_to("/")).write_text(
            json.dumps(lineage), encoding="utf-8"
        )
    return lineage


def test_authority_lineage_reports_an_unowned_host_as_holding_nothing(
    tmp_path: Path,
) -> None:
    observed = authority_reset.authority_lineage(
        {"units": list(contract.PRODUCT_UNITS)}, root=tmp_path
    )

    assert observed == {
        "status": "observed",
        "marker": None,
        "anchor": None,
        "established": None,
    }


def test_authority_lineage_reports_what_a_started_hub_established(tmp_path: Path) -> None:
    lineage = _establish_lineage(tmp_path)

    observed = authority_reset.authority_lineage(
        {"units": list(contract.PRODUCT_UNITS)}, root=tmp_path
    )

    assert observed == {
        "status": "observed",
        "marker": lineage,
        "anchor": lineage,
        "established": lineage,
    }


def test_a_database_without_its_anchor_is_reported_but_not_called_established(
    tmp_path: Path,
) -> None:
    """The controller must not mistake a recovery case for an empty Host.

    A Hub database whose external lineage anchor is gone is recoverable in
    place. A caller that only looked at ``established`` would read it as "this
    Host holds nothing" and mint a generation over a database still sitting
    there.
    """

    lineage = _establish_lineage(tmp_path, anchor=False)

    observed = authority_reset.authority_lineage(
        {"units": list(contract.PRODUCT_UNITS)}, root=tmp_path
    )

    assert observed["marker"] == lineage
    assert observed["anchor"] is None
    assert observed["established"] is None
