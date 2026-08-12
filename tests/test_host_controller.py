from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from eidolon_ops import host as host_module
from eidolon_ops.errors import OperationsError
from eidolon_ops.host_controller import HostController
from eidolon_ops.model import Capability, Outcome
from eidolon_ops.paths import HostDriver, HostPaths, HostPlatform, HostProfile
from eidolon_ops.process import ProcessResult


class Runner:
    def __init__(self, result: ProcessResult | None = None) -> None:
        self.result = result or ProcessResult(0, "executor output\n", "")
        self.calls: list[tuple[tuple[str, ...], Path | None, dict[str, str] | None]] = []

    def run(self, command, *, input_bytes=None, cwd=None, env=None, timeout=120):
        self.calls.append((tuple(command), cwd, dict(env) if env else None))
        return self.result


def _profile(tmp_path: Path) -> HostProfile:
    workspace = tmp_path / "workspace"
    script = workspace / "deploy/dev/run_all.sh"
    script.parent.mkdir(parents=True)
    script.write_text("#!/bin/sh\n", encoding="utf-8")
    script.chmod(0o755)
    paths = HostPaths(
        install_root=workspace,
        current_root=workspace,
        config_root=tmp_path / "config",
        state_root=tmp_path / "state",
        runtime_root=tmp_path / "run",
        log_root=tmp_path / "logs",
        cache_root=tmp_path / "cache",
        bootstrap_state_root=tmp_path / "bootstrap",
        bootstrap_runtime_root=tmp_path / "bootstrap-run",
    )
    return HostProfile(
        path=tmp_path / "host.toml",
        host_id="mac-test",
        platform=HostPlatform.MACOS,
        driver=HostDriver.LOCAL_SUPERVISORD,
        paths=paths,
        lifecycle_script=script,
        operations_config=None,
    )


def _pi_profile(tmp_path: Path) -> HostProfile:
    return HostProfile(
        path=tmp_path / "pi-host.toml",
        host_id="pi-test",
        platform=HostPlatform.RASPBERRY_PI,
        driver=HostDriver.SSH_SYSTEMD,
        paths=HostPaths(
            install_root=Path("/opt/eidolon"),
            current_root=Path("/opt/eidolon/current"),
            config_root=Path("/etc/eidolon"),
            state_root=Path("/var/lib/eidolon"),
            runtime_root=Path("/run/eidolon"),
            log_root=Path("/var/log/eidolon"),
            cache_root=Path("/var/cache/eidolon"),
            bootstrap_state_root=Path("/var/lib/eidolon-bootstrap"),
            bootstrap_runtime_root=Path("/run/eidolon-bootstrap"),
        ),
        lifecycle_script=None,
        operations_config=tmp_path / "pi.toml",
    )


def _with_product(controller: HostController, product: object) -> None:
    controller.adapter.supervisor._product = lambda: product


def test_local_lifecycle_uses_canonical_product_source_profile(tmp_path: Path) -> None:
    profile = _profile(tmp_path)
    runner = Runner()
    controller = HostController(profile, runner)
    prepared: list[bool] = []
    product = SimpleNamespace(
        prepare=lambda: prepared.append(True) or {"status": "prepared"},
        health=lambda **_kwargs: {"status": "healthy"},
    )
    _with_product(controller, product)

    result = controller.status()
    assert result.outcome is Outcome.OBSERVED
    assert result.report["status"] == "healthy"
    command, cwd, environment = runner.calls[-1]
    assert command == (str(profile.lifecycle_script), "product-source", "status")
    assert cwd == profile.paths.current_root
    assert environment is not None
    assert environment["EIDOLON_STATE_ROOT"] == str(profile.paths.state_root)

    dry_run = controller.lifecycle("restart", dry_run=True)
    assert dry_run.outcome is Outcome.PLANNED
    assert dry_run.report["command"] == [
        str(profile.lifecycle_script),
        "product-source",
        "restart",
    ]
    assert len(runner.calls) == 1
    assert controller.lifecycle("start").report["status"] == "healthy"
    assert prepared == [True]
    assert runner.calls[-1][0][-2:] == ("product-source", "start")
    with pytest.raises(OperationsError, match="legacy Mac lifecycle flags"):
        controller.lifecycle("start", force_cleanup=True, strict=True, wait_ready=False)
    profile_result = controller.local_profile("product-source", "web-status")
    assert profile_result.report["profile"] == "product-source"
    assert runner.calls[-1][0][-2:] == ("product-source", "web-status")
    with pytest.raises(OperationsError, match="unsupported local profile"):
        controller.local_profile("os-control-plane", "status")


def test_local_doctor_publishes_capabilities_and_bounded_logs(tmp_path: Path) -> None:
    profile = _profile(tmp_path)
    profile.paths.current_root.mkdir(parents=True, exist_ok=True)
    log = profile.paths.log_root / "agent/main.log"
    log.parent.mkdir(parents=True)
    log.write_text("one\ntwo\nthree\n", encoding="utf-8")
    controller = HostController(profile, Runner())

    doctor = controller.doctor()
    assert doctor.outcome is Outcome.OBSERVED
    adapter = doctor.report["adapter"]
    assert adapter["platform_profile"] == "macos-dev"
    assert adapter["transport"] == "local"
    assert adapter["supervisor"] == "supervisord"
    assert adapter["packages"] == "none"
    assert str(Capability.APP_READY) in adapter["capabilities"]
    assert str(Capability.INSTALL) not in adapter["capabilities"]
    assert str(Capability.LOG_HISTORY) not in adapter["capabilities"]

    result = controller.logs(service="agent", lines=2, since=None)
    assert result.report["logs"] == {"agent/main.log": ["two", "three"]}
    with pytest.raises(OperationsError, match="log-history is not available"):
        controller.logs(service=None, lines=10, since="today")
    with pytest.raises(OperationsError, match="between"):
        controller.logs(service=None, lines=0, since=None)


def test_local_controller_exposes_product_app_ready(tmp_path: Path) -> None:
    controller = HostController(_profile(tmp_path), Runner())
    _with_product(controller, SimpleNamespace(app_ready=lambda: {"status": "app_ready"}))

    evidence = controller.app_ready()
    assert evidence.outcome is Outcome.OBSERVED
    assert evidence.report == {"status": "app_ready"}

    _with_product(controller, SimpleNamespace(app_ready=lambda: {"status": "degraded"}))
    assert controller.app_ready().outcome is Outcome.DEGRADED


def test_local_controller_issues_bounded_commissioning_code(tmp_path: Path) -> None:
    controller = HostController(_profile(tmp_path), Runner())
    _with_product(controller, SimpleNamespace())

    result = controller.commissioning_code(ttl_seconds=300)

    assert result.outcome is Outcome.APPLIED
    assert controller.runner.calls[-1][0][-4:] == (
        "product-source",
        "commissioning-code",
        "--ttl",
        "300",
    )
    with pytest.raises(OperationsError, match="TTL"):
        controller.commissioning_code(ttl_seconds=30)


def test_local_controller_rejects_missing_script_and_unavailable_capability(
    tmp_path: Path,
) -> None:
    profile = _profile(tmp_path)
    assert profile.lifecycle_script is not None
    profile.lifecycle_script.unlink()
    controller = HostController(profile, Runner())
    with pytest.raises(OperationsError, match="missing"):
        controller.status()
    with pytest.raises(OperationsError, match="unsupported"):
        controller.lifecycle("deploy")
    with pytest.raises(OperationsError, match="provision is not available"):
        controller.provision(apply=False)


def test_pi_adapter_delegates_every_remote_capability(monkeypatch, tmp_path: Path) -> None:
    calls: list[tuple[str, dict[str, object]]] = []

    class Config:
        def with_revision_overrides(self, values):
            calls.append(("revisions", {"values": values}))
            return self

        host = SimpleNamespace()

    class Pi:
        def __init__(self, config, runner, *, transport=None, app=None) -> None:
            assert isinstance(config, Config)
            assert app is None

        def _result(self, name, values=None):
            calls.append((name, values or {}))
            return {"status": "healthy" if name in {"doctor", "provision"} else "ok"}

        def status(self):
            return self._result("status")

        def app_ready(self):
            return {"status": "app_ready"}

        def provision(self, **kwargs):
            return self._result("provision", kwargs)

        def initialize_inputs(self):
            return self._result("init-inputs")

        def install(self, **kwargs):
            return self._result("install", kwargs)

        def reset(self, **kwargs):
            return self._result("reset", kwargs)

        def deploy(self, **kwargs):
            return self._result("deploy", kwargs)

        def rollback(self, **kwargs):
            return self._result("rollback", kwargs)

        def backup(self, **kwargs):
            return self._result("backup", kwargs)

        def restore(self, **kwargs):
            return self._result("restore", kwargs)

        def controller_reset(self, **kwargs):
            return self._result("controller-reset", kwargs)

        def commissioning_code(self, **kwargs):
            return self._result("commissioning-code", kwargs)

        def diagnose(self, **kwargs):
            return self._result("diagnose", kwargs)

        def doctor(self, **kwargs):
            return self._result("doctor", kwargs)

        def lifecycle(self, operation, **kwargs):
            return self._result("lifecycle", {"operation": operation, **kwargs})

        def logs(self, **kwargs):
            return self._result("logs", kwargs)

    monkeypatch.setattr(host_module, "load_config", lambda _path: Config())
    monkeypatch.setattr(host_module, "EidolonPiController", Pi)
    monkeypatch.setattr(
        host_module, "SSHTransport", lambda host, runner: SimpleNamespace(kind="ssh")
    )
    controller = HostController(
        _pi_profile(tmp_path), Runner(), revision_overrides=("eidolon_data=" + "f" * 40,)
    )

    assert controller.status().outcome is Outcome.OBSERVED
    assert controller.app_ready().outcome is Outcome.OBSERVED
    controller.provision(apply=False)
    controller.initialize_inputs()
    controller.install(release_id="r1", resume=True, apply=True)
    controller.reset(wipe_authority_data=False, apply=False)
    controller.deploy(release_id="r1", resume=True, activate=True)
    controller.rollback(release_id="r1", snapshot=Path("/snapshot"), apply=False)
    controller.backup(output=tmp_path / "backups")
    controller.restore(source=tmp_path / "backups/r1", apply=False)
    controller.controller_reset(apply=True)
    controller.commissioning_code(ttl_seconds=600)
    controller.diagnose(output=tmp_path / "report.tar.gz")
    assert controller.doctor(release_id="r1").report["status"] == "healthy"
    controller.lifecycle("restart", dry_run=True)
    controller.logs(service="agent", lines=5, since="today")
    controller.logs(service="eidolond", lines=5, since=None)

    assert any(name == "revisions" for name, _values in calls)
    log_values = [values for name, values in calls if name == "logs"]
    assert log_values == [
        {"unit": "eidolon-agent.service", "lines": 5, "since": "today"},
        {"unit": "eidolond.service", "lines": 5, "since": None},
    ]
    with pytest.raises(OperationsError, match="legacy Mac lifecycle flags"):
        controller.lifecycle("start", force_cleanup=True)
    with pytest.raises(OperationsError, match="debug is not available"):
        controller.local_profile("core-contract", "status")


def test_pi_plans_declare_what_they_touch(monkeypatch, tmp_path: Path) -> None:
    from eidolon_ops import plans

    plan = plans.install(
        "pi-test", apply=True, reset_existing=True, wipe_authority_data=True
    )
    document = plan.to_json()
    assert document["destructive"] == "irreversible"
    assert "data" in document["touches"]
    assert set(document["requires_flags"]) == {
        "--apply",
        "--reset-existing",
        "--wipe-authority-data",
    }
    assert plans.install("pi-test", apply=False, reset_existing=False, wipe_authority_data=False)


def test_controller_rejects_missing_pi_config_and_unsafe_local_logs(tmp_path: Path) -> None:
    pi = _pi_profile(tmp_path)
    missing = HostProfile(
        path=pi.path,
        host_id=pi.host_id,
        platform=pi.platform,
        driver=pi.driver,
        paths=pi.paths,
        lifecycle_script=None,
        operations_config=None,
    )
    with pytest.raises(OperationsError, match="operations config"):
        HostController(missing, Runner()).status()

    local = HostController(_profile(tmp_path / "local"), Runner())
    with pytest.raises(OperationsError, match="escapes"):
        local.logs(service="../outside", lines=10, since=None)
    with pytest.raises(OperationsError, match="unsupported local profile"):
        local.local_profile("unknown", "status")
