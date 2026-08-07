from __future__ import annotations

import base64
import contextlib
import json
import subprocess
from pathlib import Path
from types import MappingProxyType, SimpleNamespace

import pytest

from eidolon_ops import target_agent
from eidolon_ops.target_agent import TargetError, TargetInstaller

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
        if command[0].endswith("alembic"):
            if self.fail_baseline:
                return subprocess.CompletedProcess(command, 1, "", "baseline failed")
            database = Path(kwargs["env"]["EIDOLON_DATA_SQLITE_PATH"])
            database.parent.mkdir(parents=True, exist_ok=True)
            database.write_bytes(b"SQLite format 3\x00test")
        return subprocess.CompletedProcess(command, 0, "", "")


@pytest.fixture
def install_fixture(tmp_path: Path):
    release_id = "release-test"
    component_ids = ("eidolon_kernel", "eidolon_data", "eidolon_hub", "eidolon_admin")
    components = tuple(
        SimpleNamespace(
            component_id=component_id,
            release_path=Path("/srv/eidolon/releases") / release_id / component_id,
            current_link=Path("/srv/eidolon/current") / component_id,
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
    for name in target_agent.SECRET_INPUTS:
        (stage / name).write_text(f"private-{name}", encoding="utf-8")
    data = dict(target_agent.FIXED_DATA)
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
    )
    return installer, host, command, stage, release, data


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
        Path("/srv/eidolon/current/eidolon_kernel"),
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


def test_concurrent_install_lock_fails_fast(install_fixture) -> None:
    installer, _host, _command, _stage, _release, _data = install_fixture

    with (
        target_agent._exclusive(installer.lock_path),
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


def test_status_parses_systemd_properties(monkeypatch) -> None:
    def fake_run(command, **kwargs):
        return subprocess.CompletedProcess(
            command,
            0,
            "LoadState=loaded\nActiveState=active\nSubState=running\nNRestarts=2\n",
            "",
        )

    monkeypatch.setattr(target_agent, "_run", fake_run)

    result = target_agent.status({"units": list(target_agent.PRODUCT_UNITS)})

    assert result["units"]["eidolond.service"]["ActiveState"] == "active"
    assert result["units"]["eidolond.service"]["NRestarts"] == 2


def test_status_records_systemctl_failure(monkeypatch) -> None:
    monkeypatch.setattr(
        target_agent,
        "_run",
        lambda command, **kwargs: subprocess.CompletedProcess(command, 1, "", "not found"),
    )

    result = target_agent.status({"units": list(target_agent.PRODUCT_UNITS)})

    assert result["units"]["eidolond.service"] == {"error": "not found"}


def test_logs_rejects_unknown_unit() -> None:
    with pytest.raises(TargetError, match="outside"):
        target_agent.logs(
            {
                "units": list(target_agent.PRODUCT_UNITS),
                "unit": "ssh.service",
                "lines": 10,
            }
        )


@pytest.mark.parametrize("lines", [0, 5001, "100"])
def test_logs_rejects_unbounded_line_count(lines) -> None:
    with pytest.raises(TargetError, match="line count"):
        target_agent.logs({"units": list(target_agent.PRODUCT_UNITS), "lines": lines})


def test_logs_collects_fixed_units(monkeypatch) -> None:
    monkeypatch.setattr(
        target_agent,
        "_run",
        lambda command, **kwargs: subprocess.CompletedProcess(command, 0, "entry\n", ""),
    )

    result = target_agent.logs(
        {
            "units": list(target_agent.PRODUCT_UNITS),
            "unit": "eidolond.service",
            "lines": 2,
            "since": "1 hour ago",
        }
    )

    assert result["entries"] == {"eidolond.service": "entry\n"}


def test_logs_rejects_control_character_in_since() -> None:
    with pytest.raises(TargetError, match="since"):
        target_agent.logs(
            {
                "units": list(target_agent.PRODUCT_UNITS),
                "lines": 10,
                "since": "today\nnext",
            }
        )


def test_payload_round_trip_and_rejects_non_object() -> None:
    encoded = base64.urlsafe_b64encode(b'{"release_id":"r1"}').decode("ascii")
    assert target_agent._payload(encoded) == {"release_id": "r1"}

    encoded_list = base64.urlsafe_b64encode(b"[]").decode("ascii")
    with pytest.raises(TargetError, match="object"):
        target_agent._payload(encoded_list)


def test_guard_upload_accepts_absent_path() -> None:
    release_id = "test-guard-upload-ops"
    path = Path("/var/tmp") / f"eidolon-release-{release_id}"
    if path.exists():
        pytest.skip("test staging path unexpectedly exists")

    assert target_agent.guard_upload({"release_id": release_id})["status"] == "ready_for_upload"


def test_guard_upload_rejects_existing_path(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(target_agent, "_VAR_TMP", tmp_path)
    path = tmp_path / "eidolon-release-existing"
    path.mkdir()

    with pytest.raises(TargetError, match="already exists"):
        target_agent.guard_upload({"release_id": "existing"})


def test_cleanup_stage_removes_only_exact_secret_path(monkeypatch, tmp_path: Path) -> None:
    release_id = "test-cleanup-stage-ops"
    monkeypatch.setattr(target_agent, "_VAR_TMP", tmp_path)
    path = tmp_path / f"eidolon-secrets-{release_id}"
    path.mkdir(exist_ok=False)
    (path / "data.env").write_text("private", encoding="utf-8")

    result = target_agent.cleanup_stage({"release_id": release_id})

    assert result["status"] == "cleaned"
    assert not path.exists()


def test_main_rejects_unknown_action(capsys) -> None:
    encoded = base64.urlsafe_b64encode(b"{}").decode("ascii")

    assert target_agent.main(("unknown", encoded)) == 1
    assert "unknown target action" in capsys.readouterr().err


def test_main_rejects_missing_arguments(capsys) -> None:
    assert target_agent.main(()) == 2
    assert "expected action" in capsys.readouterr().err


def test_doctor_host_reports_current_platform_without_mutation() -> None:
    result = target_agent.doctor_host(
        {
            "units": list(target_agent.PRODUCT_UNITS),
            "data": {name: str(path) for name, path in target_agent.FIXED_DATA.items()},
            "remote_uv": "/definitely/missing/uv",
        }
    )

    assert result["status"] == "degraded"
    assert result["checks"]["uv"] is False


def test_fixed_payload_contracts_reject_drift() -> None:
    with pytest.raises(TargetError, match="unit set"):
        target_agent._fixed_units({"units": []})
    with pytest.raises(TargetError, match="data path"):
        target_agent._fixed_data({"data": {}})
    with pytest.raises(TargetError, match="release id"):
        target_agent._release_id({"release_id": "../bad"})
    assert target_agent._release_id({}, required=False) is None


def test_atomic_json_and_file_hash(tmp_path: Path) -> None:
    path = tmp_path / "document.json"

    target_agent._atomic_json(path, {"value": 1})

    assert json.loads(path.read_text(encoding="utf-8")) == {"value": 1}
    assert len(target_agent._file_sha256(path)) == 64


def test_checked_command_success_and_failure(monkeypatch) -> None:
    monkeypatch.setattr(
        target_agent,
        "_run",
        lambda command, **kwargs: subprocess.CompletedProcess(command, 0, "ok", ""),
    )
    assert target_agent._checked("probe", ("true",)).stdout == "ok"

    monkeypatch.setattr(
        target_agent,
        "_run",
        lambda command, **kwargs: subprocess.CompletedProcess(command, 1, "", "failed"),
    )
    with pytest.raises(TargetError, match="probe failed"):
        target_agent._checked("probe", ("false",))


def test_diagnose_composes_redacted_status(monkeypatch) -> None:
    monkeypatch.setattr(target_agent, "status", lambda payload: {"status": "observed"})
    monkeypatch.setattr(
        target_agent,
        "logs",
        lambda payload: {"status": "collected", "entries": {"eidolond": "line"}},
    )

    result = target_agent.diagnose({})

    assert result["status"] == "diagnosed"
    assert "private key" in result["redaction"]


def test_target_main_dispatches_status(monkeypatch, capsys) -> None:
    monkeypatch.setattr(target_agent, "status", lambda payload: {"status": "observed"})
    encoded = base64.urlsafe_b64encode(b"{}").decode("ascii")

    assert target_agent.main(("status", encoded)) == 0
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


def test_real_run_and_host_path_guards(tmp_path: Path) -> None:
    assert target_agent._run(("/usr/bin/true",)).returncode == 0
    assert target_agent._host_path(tmp_path, Path("/etc/test")) == tmp_path / "etc/test"
    with pytest.raises(TargetError, match="absolute"):
        target_agent._host_path(tmp_path, Path("relative"))


def test_run_wraps_subprocess_error(monkeypatch) -> None:
    def fail(*args, **kwargs):
        raise OSError("cannot exec")

    monkeypatch.setattr(subprocess, "run", fail)

    with pytest.raises(TargetError, match="cannot exec"):
        target_agent._run(("missing",))


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
    monkeypatch.setitem(target_agent.FIXED_DATA, "deployment_evidence", evidence)
    monkeypatch.setattr(
        target_agent,
        "_run",
        lambda command, **kwargs: subprocess.CompletedProcess(command, 0, "", ""),
    )

    result = target_agent.status({"units": list(target_agent.PRODUCT_UNITS)})

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
    monkeypatch.setattr(target_agent, "_RELEASES", tmp_path)
    monkeypatch.setattr(
        target_agent,
        "_run",
        lambda command, **kwargs: subprocess.CompletedProcess(
            command, returncode, stdout, "failed"
        ),
    )

    result = target_agent.doctor_host(
        {
            "units": list(target_agent.PRODUCT_UNITS),
            "data": {name: str(path) for name, path in target_agent.FIXED_DATA.items()},
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
        target_agent,
        "_run",
        lambda command, **kwargs: subprocess.CompletedProcess(command, 3, "out", "err"),
    )

    result = target_agent._DeploymentRunner(Result).run("command")

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

    import eidolon_deploy.linux
    import eidolon_deploy.manifest

    monkeypatch.setattr(eidolon_deploy.linux, "LinuxDeploymentHost", Host)
    monkeypatch.setattr(eidolon_deploy.manifest, "load_release_descriptor", lambda path: release)
    active = tmp_path / "release/eidolon_kernel"
    active.mkdir(parents=True)
    monkeypatch.setattr(target_agent, "_CURRENT_KERNEL", active)

    result = target_agent.lifecycle("restart", {"units": list(target_agent.PRODUCT_UNITS)})

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
    monkeypatch.setitem(target_agent.FIXED_DATA, "deployment_evidence", evidence)
    monkeypatch.setattr(target_agent, "_RELEASES", releases)

    result = target_agent.rollback_plan({"release_id": "r1", "snapshot": str(snapshot)})

    assert result["status"] == "rollback_planned"
    with pytest.raises(TargetError, match="snapshot path"):
        target_agent.rollback_plan({"release_id": "r1", "snapshot": 1})


def test_install_wrapper_reuses_kernel_descriptor(monkeypatch, tmp_path: Path) -> None:
    release = SimpleNamespace(release_id="r1")

    class Host:
        def __init__(self, **kwargs) -> None:
            pass

    class Installer:
        def __init__(self, **kwargs) -> None:
            assert kwargs["release"] is release

        def install(self):
            return {"status": "installed"}

    import eidolon_deploy.linux
    import eidolon_deploy.manifest

    monkeypatch.setattr(eidolon_deploy.linux, "LinuxDeploymentHost", Host)
    monkeypatch.setattr(eidolon_deploy.manifest, "load_release_descriptor", lambda path: release)
    monkeypatch.setattr(target_agent, "TargetInstaller", Installer)
    monkeypatch.setattr(target_agent, "_RELEASES", tmp_path / "releases")
    monkeypatch.setattr(target_agent, "_VAR_TMP", tmp_path)

    result = target_agent.install(
        {
            "release_id": "r1",
            "data": {name: str(path) for name, path in target_agent.FIXED_DATA.items()},
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
