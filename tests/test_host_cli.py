from __future__ import annotations

import json
from pathlib import Path

import pytest

from eidolon_ops import host_cli
from eidolon_ops.model import Evidence, Outcome, Plan
from eidolon_ops.paths import HostProfileError


class FakeHostController:
    instance: FakeHostController

    def __init__(self, profile, runner, *, revision_overrides=()) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []
        self.revision_overrides = revision_overrides
        FakeHostController.instance = self

    def _result(self, name: str, **values):
        self.calls.append((name, values))
        return _evidence(name, Outcome.OBSERVED if name == "doctor" else Outcome.APPLIED)

    def status(self):
        return self._result("status")

    def app_ready(self):
        self.calls.append(("app-ready", {}))
        return _evidence("app-ready", Outcome.OBSERVED)

    def doctor(self, **kwargs):
        return self._result("doctor", **kwargs)

    def provision(self, **kwargs):
        return self._result("provision", **kwargs)

    def initialize_inputs(self, **kwargs):
        return self._result("init-inputs", **kwargs)

    def install(self, **kwargs):
        return self._result("install", **kwargs)

    def controller_reset(self, **kwargs):
        return self._result("controller-reset", **kwargs)

    def reset(self, **kwargs):
        return self._result("reset", **kwargs)

    def deploy(self, **kwargs):
        return self._result("deploy", **kwargs)

    def rollback(self, **kwargs):
        return self._result("rollback", **kwargs)

    def diagnose(self, **kwargs):
        return self._result("diagnose", **kwargs)

    def lifecycle(self, operation, **kwargs):
        return self._result("lifecycle", operation=operation, **kwargs)

    def local_profile(self, profile, operation, **kwargs):
        return self._result("local-profile", profile=profile, operation=operation, **kwargs)

    def logs(self, **kwargs):
        return self._result("logs", **kwargs)


def _evidence(name: str, outcome: Outcome) -> Evidence:
    return Evidence(
        plan=Plan(operation=name, host_id="host", steps=()),
        outcome=outcome,
        report={"status": "ok"},
    )


@pytest.fixture(autouse=True)
def fake_host(monkeypatch) -> None:
    monkeypatch.setattr(host_cli, "load_host_profile", lambda path: object())
    monkeypatch.setattr(host_cli, "HostController", FakeHostController)


@pytest.mark.parametrize(
    ("arguments", "expected"),
    [
        (["status"], "status"),
        (["app-ready"], "app-ready"),
        (["doctor", "--release-id", "r1"], "doctor"),
        (["provision", "--apply"], "provision"),
        (["init-inputs"], "init-inputs"),
        (
            [
                "install",
                "--release-id",
                "r1",
                "--resume",
                "--reset-existing",
                "--wipe-authority-data",
                "--apply",
            ],
            "install",
        ),
        (["reset", "--wipe-authority-data", "--apply"], "reset"),
        (["deploy", "--release-id", "r1", "--activate"], "deploy"),
        (["update", "--release-id", "r1"], "deploy"),
        (
            ["rollback", "--release-id", "r1", "--snapshot", "/var/lib/eidolon/deployments/r1-x"],
            "rollback",
        ),
        (["diagnose", "--output", "/tmp/report.tar.gz"], "diagnose"),
        (
            ["start", "--force-cleanup", "--strict", "--no-wait-ready"],
            "lifecycle",
        ),
        (["restart", "--dry-run"], "lifecycle"),
        (["logs", "--service", "agent", "--lines", "10"], "logs"),
        (["controller-reset", "--apply"], "controller-reset"),
        (["debug", "status"], "local-profile"),
        (["debug", "prepare"], "local-profile"),
    ],
)
def test_unified_cli_routes_host_capabilities(capsys, arguments: list[str], expected: str) -> None:
    result = host_cli.main(["--config", "/tmp/host.toml", *arguments])

    assert result == 0
    document = json.loads(capsys.readouterr().out)
    assert document["outcome"] in {"observed", "applied"}
    assert document["plan"]["host_id"] == "host"
    assert FakeHostController.instance.calls[-1][0] == expected


def test_unified_cli_forwards_revision_overrides(capsys) -> None:
    revision = "f" * 40

    assert (
        host_cli.main(
            [
                "--config",
                "/tmp/host.toml",
                "--revision",
                f"eidolon_data={revision}",
                "status",
            ]
        )
        == 0
    )
    assert FakeHostController.instance.revision_overrides == (f"eidolon_data={revision}",)
    capsys.readouterr()


def test_unified_cli_reports_profile_errors(monkeypatch, capsys) -> None:
    def fail(_path: Path):
        raise HostProfileError("invalid host profile")

    monkeypatch.setattr(host_cli, "load_host_profile", fail)

    assert host_cli.main(["--config", "/tmp/host.toml", "status"]) == 1
    assert json.loads(capsys.readouterr().err) == {
        "status": "failed",
        "outcome": "failed",
        "error": "invalid host profile",
    }


def test_the_exit_code_is_the_operations_own_verdict(capsys, monkeypatch) -> None:
    """A word nobody added to a whitelist is no longer a failure.

    ``commissioning-code`` answered ``issued`` and ``backup`` answered
    ``captured``; neither was in the set of success words this file used to
    keep, so both succeeded on the Host and exited non-zero here.
    """

    class Controller(FakeHostController):
        def commissioning_code(self, **values):
            self.calls.append(("commissioning-code", values))
            return Evidence(
                plan=Plan(operation="commissioning-code", host_id="host", steps=()),
                outcome=Outcome.APPLIED,
                report={"status": "issued", "setup_code": "123456"},
            )

        def app_ready(self):
            self.calls.append(("app-ready", {}))
            return Evidence(
                plan=Plan(operation="app-ready", host_id="host", steps=()),
                outcome=Outcome.DEGRADED,
                report={"status": "degraded", "checks": {"channel_worker_healthy": False}},
            )

    monkeypatch.setattr(host_cli, "HostController", Controller)

    assert host_cli.main(["--config", "/tmp/host.toml", "commissioning-code"]) == 0
    assert json.loads(capsys.readouterr().out)["outcome"] == "applied"

    # A readiness report that says the Host cannot serve is a failure, and the
    # exit code says so without this file knowing what "degraded" means.
    assert host_cli.main(["--config", "/tmp/host.toml", "app-ready"]) == 1
    document = json.loads(capsys.readouterr().out)
    assert document["outcome"] == "degraded"
    assert document["checks"] == {"channel_worker_healthy": False}
