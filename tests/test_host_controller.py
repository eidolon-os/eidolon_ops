from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from eidolon_ops import host_controller as host_controller_module
from eidolon_ops.controller import OperationsError
from eidolon_ops.host_controller import HostController
from eidolon_ops.paths import HostPaths, HostProfile
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
        platform="macos",
        driver="local-supervisord",
        paths=paths,
        lifecycle_script=script,
        operations_config=None,
    )


def _pi_profile(tmp_path: Path) -> HostProfile:
    return HostProfile(
        path=tmp_path / "pi-host.toml",
        host_id="pi-test",
        platform="raspberry-pi",
        driver="ssh-systemd",
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


def test_local_lifecycle_uses_canonical_product_source_profile(monkeypatch, tmp_path: Path) -> None:
    profile = _profile(tmp_path)
    runner = Runner()
    controller = HostController(profile, runner)
    prepared: list[bool] = []
    product = SimpleNamespace(
        prepare=lambda: prepared.append(True) or {"status": "prepared"},
        health=lambda **_kwargs: {"status": "healthy"},
    )
    monkeypatch.setattr(controller, "_local_product", lambda: product)

    result = controller.status()
    assert result["status"] == "healthy"
    command, cwd, environment = runner.calls[-1]
    assert command == (str(profile.lifecycle_script), "product-source", "status")
    assert cwd == profile.paths.current_root
    assert environment is not None
    assert environment["EIDOLON_STATE_ROOT"] == str(profile.paths.state_root)

    dry_run = controller.lifecycle("restart", dry_run=True)
    assert dry_run["status"] == "dry_run"
    assert dry_run["command"] == [
        str(profile.lifecycle_script),
        "product-source",
        "restart",
    ]
    assert len(runner.calls) == 1
    assert controller.lifecycle("start")["status"] == "healthy"
    assert prepared == [True]
    assert runner.calls[-1][0][-2:] == ("product-source", "start")
    with pytest.raises(OperationsError, match="legacy Mac lifecycle flags"):
        controller.lifecycle("start", force_cleanup=True, strict=True, wait_ready=False)
    profile_result = controller.local_profile("product-source", "web-status")
    assert profile_result["profile"] == "product-source"
    assert runner.calls[-1][0][-2:] == ("product-source", "web-status")
    with pytest.raises(OperationsError, match="unsupported local profile"):
        controller.local_profile("os-control-plane", "status")


def test_local_doctor_and_bounded_logs(tmp_path: Path) -> None:
    profile = _profile(tmp_path)
    profile.paths.current_root.mkdir(parents=True, exist_ok=True)
    log = profile.paths.log_root / "agent/main.log"
    log.parent.mkdir(parents=True)
    log.write_text("one\ntwo\nthree\n", encoding="utf-8")
    controller = HostController(profile, Runner())

    assert controller.doctor()["status"] == "healthy"
    result = controller.logs(service="agent", lines=2, since=None)
    assert result["logs"] == {"agent/main.log": ["two", "three"]}
    with pytest.raises(OperationsError, match="systemd"):
        controller.logs(service=None, lines=10, since="today")
    with pytest.raises(OperationsError, match="between"):
        controller.logs(service=None, lines=0, since=None)


def test_local_controller_exposes_product_app_ready(monkeypatch, tmp_path: Path) -> None:
    controller = HostController(_profile(tmp_path), Runner())
    product = SimpleNamespace(app_ready=lambda: {"status": "app_ready"})
    monkeypatch.setattr(controller, "_local_product", lambda: product)

    assert controller.app_ready() == {"status": "app_ready"}


def test_local_controller_issues_bounded_commissioning_code(monkeypatch, tmp_path: Path) -> None:
    controller = HostController(_profile(tmp_path), Runner())
    monkeypatch.setattr(
        controller,
        "_local_product",
        lambda: SimpleNamespace(),
    )

    result = controller.commissioning_code(ttl_seconds=300)

    assert result["status"] == "ok"
    assert controller.runner.calls[-1][0][-4:] == (
        "product-source",
        "commissioning-code",
        "--ttl",
        "300",
    )
    with pytest.raises(OperationsError, match="TTL"):
        controller.commissioning_code(ttl_seconds=30)


def test_local_controller_rejects_missing_script_and_unknown_operation(tmp_path: Path) -> None:
    profile = _profile(tmp_path)
    assert profile.lifecycle_script is not None
    profile.lifecycle_script.unlink()
    controller = HostController(profile, Runner())
    with pytest.raises(OperationsError, match="missing"):
        controller._local_lifecycle("status")
    with pytest.raises(OperationsError, match="unsupported"):
        controller.lifecycle("deploy")
    with pytest.raises(OperationsError, match="Pi adapter"):
        controller.provision(apply=False)


def test_pi_adapter_delegates_every_remote_capability(monkeypatch, tmp_path: Path) -> None:
    calls: list[tuple[str, dict[str, object]]] = []

    class Config:
        def with_revision_overrides(self, values):
            calls.append(("revisions", {"values": values}))
            return self

    class Pi:
        def __init__(self, config, runner, *, app=None) -> None:
            assert isinstance(config, Config)
            assert app is None

        def _result(self, name, values=None):
            calls.append((name, values or {}))
            return {"status": "healthy" if name == "doctor" else "ok"}

        def status(self):
            return self._result("status")

        def app_ready(self):
            return self._result("app_ready")

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

        def diagnose(self, **kwargs):
            return self._result("diagnose", kwargs)

        def doctor(self, **kwargs):
            return self._result("doctor", kwargs)

        def lifecycle(self, operation, **kwargs):
            return self._result("lifecycle", {"operation": operation, **kwargs})

        def logs(self, **kwargs):
            return self._result("logs", kwargs)

    monkeypatch.setattr(host_controller_module, "load_config", lambda _path: Config())
    monkeypatch.setattr(host_controller_module, "EidolonPiController", Pi)
    controller = HostController(
        _pi_profile(tmp_path), Runner(), revision_overrides=("eidolon_data=" + "f" * 40,)
    )

    assert controller.status()["status"] == "ok"
    controller.app_ready()
    controller.provision(apply=False)
    controller.initialize_inputs()
    controller.install(release_id="r1", resume=True, apply=True)
    controller.reset(wipe_authority_data=False, apply=False)
    controller.deploy(release_id="r1", resume=True, activate=True)
    controller.rollback(release_id="r1", snapshot=Path("/snapshot"), apply=False)
    controller.diagnose(output=tmp_path / "report.tar.gz")
    assert controller.doctor(release_id="r1")["status"] == "healthy"
    controller.lifecycle("restart", dry_run=True)
    controller.logs(service="agent", lines=5, since="today")
    controller.logs(service="eidolond", lines=5, since=None)

    assert any(name == "revisions" for name, _values in calls)
    log_values = [values for name, values in calls if name == "logs"]
    assert log_values == [
        {"unit": "eidolon-agent.service", "lines": 5, "since": "today"},
        {"unit": "eidolond.service", "lines": 5, "since": None},
    ]
    with pytest.raises(OperationsError, match="Mac lifecycle flags"):
        controller.lifecycle("start", force_cleanup=True)
    with pytest.raises(OperationsError, match="macOS adapter"):
        controller.local_profile("core-contract", "status")


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
