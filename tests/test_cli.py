from __future__ import annotations

import json
from pathlib import Path

import pytest

from eidolon_ops import cli
from eidolon_ops.config import ConfigurationError

pytestmark = pytest.mark.unit


class FakeController:
    calls: list[tuple[str, dict[str, object]]]

    def __init__(self, config, runner) -> None:
        self.calls = []
        FakeController.instance = self

    def _result(self, name: str, values: dict[str, object]):
        self.calls.append((name, values))
        statuses = {
            "doctor": "healthy",
            "provision": "installed",
            "app-ready": "app_ready",
        }
        return {"status": statuses.get(name, name)}

    def status(self):
        return self._result("status", {})

    def app_ready(self):
        return self._result("app-ready", {})

    def doctor(self, **kwargs):
        return self._result("doctor", kwargs)

    def provision(self, **kwargs):
        return self._result("provision", kwargs)

    def install(self, **kwargs):
        return self._result("install", kwargs)

    def reset(self, **kwargs):
        return self._result("reset", kwargs)

    def expand(self, **kwargs):
        return self._result("expand", kwargs)

    def deploy(self, **kwargs):
        return self._result("deploy", kwargs)

    def lifecycle(self, action, **kwargs):
        return self._result("lifecycle", {"action": action, **kwargs})

    def rollback(self, **kwargs):
        return self._result("rollback", kwargs)

    def logs(self, **kwargs):
        return self._result("logs", kwargs)

    def diagnose(self, **kwargs):
        return self._result("diagnose", kwargs)


@pytest.fixture(autouse=True)
def fake_controller(monkeypatch) -> None:
    monkeypatch.setattr(cli, "EidolonPiController", FakeController)


@pytest.mark.parametrize(
    ("arguments", "expected"),
    [
        (["status"], "status"),
        (["app-ready"], "app-ready"),
        (["doctor", "--release-id", "r1"], "doctor"),
        (["provision", "--apply"], "provision"),
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
        (["update", "--release-id", "r1", "--resume"], "deploy"),
        (["start", "--dry-run"], "lifecycle"),
        (["stop"], "lifecycle"),
        (["restart"], "lifecycle"),
        (
            [
                "rollback",
                "--release-id",
                "r1",
                "--snapshot",
                "/var/lib/eidolon/deployments/r1-tx",
                "--apply",
            ],
            "rollback",
        ),
        (["logs", "--unit", "eidolond.service", "--lines", "5", "--since", "today"], "logs"),
        (["diagnose", "--output", "/tmp/diagnostic.tar.gz"], "diagnose"),
    ],
)
def test_cli_routes_every_operation(
    config_path: Path,
    capsys,
    arguments: list[str],
    expected: str,
) -> None:
    argv = ["--config", str(config_path), *arguments]

    assert cli.main(argv) == 0

    output = json.loads(capsys.readouterr().out)
    statuses = {"doctor": "healthy", "provision": "installed", "app-ready": "app_ready"}
    assert output["status"] == statuses.get(expected, expected)
    assert FakeController.instance.calls[-1][0] == expected


def test_cli_applies_revision_override(config_path: Path, capsys) -> None:
    revision = "f" * 40

    assert (
        cli.main(
            [
                "--config",
                str(config_path),
                "--revision",
                f"eidolon_data={revision}",
                "status",
            ]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["status"] == "status"


def test_cli_returns_nonzero_for_degraded_doctor(config_path: Path, monkeypatch, capsys) -> None:
    monkeypatch.setattr(
        FakeController,
        "doctor",
        lambda self, **kwargs: {"status": "degraded"},
    )

    assert cli.main(["--config", str(config_path), "doctor"]) == 1
    assert json.loads(capsys.readouterr().out)["status"] == "degraded"


@pytest.mark.parametrize(
    ("operation", "method"),
    [("provision", "provision"), ("app-ready", "app_ready")],
)
def test_cli_returns_nonzero_for_unready_gates(
    config_path: Path,
    monkeypatch,
    capsys,
    operation: str,
    method: str,
) -> None:
    monkeypatch.setattr(
        FakeController,
        method,
        lambda self, **kwargs: {"status": "degraded"},
    )

    assert cli.main(["--config", str(config_path), operation]) == 1
    assert json.loads(capsys.readouterr().out)["status"] == "degraded"


def test_cli_returns_json_error(config_path: Path, monkeypatch, capsys) -> None:
    def fail(_path):
        raise ConfigurationError("invalid config")

    monkeypatch.setattr(cli, "load_config", fail)

    assert cli.main(["--config", str(config_path), "status"]) == 1
    error = json.loads(capsys.readouterr().err)
    assert error == {"status": "failed", "error": "invalid config"}
