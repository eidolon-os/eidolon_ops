from __future__ import annotations

import json
from pathlib import Path

import pytest

from eidolon_ops import host_cli
from eidolon_ops.paths import HostProfileError


class FakeHostController:
    instance: FakeHostController

    def __init__(self, profile, runner, *, revision_overrides=()) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []
        self.revision_overrides = revision_overrides
        FakeHostController.instance = self

    def _result(self, name: str, **values):
        self.calls.append((name, values))
        return {"status": "healthy" if name == "doctor" else "ok"}

    def status(self):
        return self._result("status")

    def app_ready(self):
        self.calls.append(("app-ready", {}))
        return {"status": "app_ready"}

    def doctor(self, **kwargs):
        return self._result("doctor", **kwargs)

    def provision(self, **kwargs):
        return self._result("provision", **kwargs)

    def install(self, **kwargs):
        return self._result("install", **kwargs)

    def deploy(self, **kwargs):
        return self._result("deploy", **kwargs)

    def expand(self, **kwargs):
        return self._result("expand", **kwargs)

    def rollback(self, **kwargs):
        return self._result("rollback", **kwargs)

    def diagnose(self, **kwargs):
        return self._result("diagnose", **kwargs)

    def migrate_paths(self, **kwargs):
        return self._result("migrate-paths", **kwargs)

    def lifecycle(self, operation, **kwargs):
        return self._result("lifecycle", operation=operation, **kwargs)

    def local_profile(self, profile, operation, **kwargs):
        return self._result("local-profile", profile=profile, operation=operation, **kwargs)

    def logs(self, **kwargs):
        return self._result("logs", **kwargs)


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
        (["install", "--release-id", "r1", "--resume", "--apply"], "install"),
        (["deploy", "--release-id", "r1", "--activate"], "deploy"),
        (["update", "--release-id", "r1"], "deploy"),
        (["expand", "--release-id", "r1", "--apply"], "expand"),
        (
            ["rollback", "--release-id", "r1", "--snapshot", "/var/lib/eidolon/deployments/r1-x"],
            "rollback",
        ),
        (["diagnose", "--output", "/tmp/report.tar.gz"], "diagnose"),
        (["migrate-paths", "--apply"], "migrate-paths"),
        (
            ["start", "--force-cleanup", "--strict", "--no-wait-ready"],
            "lifecycle",
        ),
        (["restart", "--dry-run"], "lifecycle"),
        (["logs", "--service", "agent", "--lines", "10"], "logs"),
        (["core-contract", "status"], "local-profile"),
        (
            ["os-control-plane", "issue-operator-token", "--ttl-seconds", "60"],
            "local-profile",
        ),
    ],
)
def test_unified_cli_routes_host_capabilities(capsys, arguments: list[str], expected: str) -> None:
    result = host_cli.main(["--config", "/tmp/host.toml", *arguments])

    assert result == 0
    assert json.loads(capsys.readouterr().out)["status"] in {"ok", "healthy", "app_ready"}
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
        "error": "invalid host profile",
    }
