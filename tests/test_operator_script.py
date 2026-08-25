from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

from eidolon_ops.host_cli import OPERATIONS

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "eidolon"


def run(*arguments: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        (str(SCRIPT), *arguments),
        cwd=ROOT,
        env={**os.environ, **(env or {})},
        text=True,
        capture_output=True,
        check=False,
    )


def test_operator_script_is_executable_and_lists_real_hosts() -> None:
    assert os.access(SCRIPT, os.X_OK)
    result = run("hosts")
    assert result.returncode == 0
    assert "mac" in result.stdout
    assert "pi5" in result.stdout
    assert ".example.toml" not in result.stdout

    help_result = run("--help")
    assert help_result.returncode == 0
    assert "命令概览" in help_result.stdout
    assert "mac" in help_result.stdout and "debug" in help_result.stdout
    assert "pi5" in help_result.stdout and "install" in help_result.stdout


def test_operator_script_covers_every_cli_command() -> None:
    text = SCRIPT.read_text(encoding="utf-8")
    match = re.search(r'^ALL_COMMANDS="([^"]+)"$', text, re.MULTILINE)
    assert match is not None
    assert set(match.group(1).split()) == set(OPERATIONS)


def test_help_is_host_specific() -> None:
    mac = run("commands", "mac")
    pi = run("commands", "pi5")
    assert mac.returncode == pi.returncode == 0
    assert "debug" in mac.stdout
    assert "install" not in mac.stdout
    assert "install" in pi.stdout
    assert "debug" not in pi.stdout


def test_unknown_host_and_unsupported_command_are_actionable() -> None:
    unknown = run("missing-host", "status")
    assert unknown.returncode == 2
    assert "未知主机" in unknown.stderr
    assert "mac" in unknown.stderr

    unsupported = run("mac", "install")
    assert unsupported.returncode == 2
    assert "不适用于主机 mac" in unsupported.stderr
    assert "start" in unsupported.stderr


def test_dispatch_adds_the_selected_profile_and_preserves_arguments(tmp_path: Path) -> None:
    captured = tmp_path / "arguments.txt"
    fake_uv = tmp_path / "uv"
    fake_uv.write_text(
        "#!/bin/sh\nprintf '%s\\n' \"$@\" > \"$EIDOLON_CAPTURE\"\n",
        encoding="utf-8",
    )
    fake_uv.chmod(0o755)

    result = run(
        "--allow-dirty",
        "pi5",
        "install",
        "--release-id",
        "r1",
        "--apply",
        env={"EIDOLON_UV_BIN": str(fake_uv), "EIDOLON_CAPTURE": str(captured)},
    )

    assert result.returncode == 0
    assert captured.read_text(encoding="utf-8").splitlines() == [
        "run",
        "eidolon-ops",
        "--config",
        str(ROOT / "config/hosts/pi5.toml"),
        "--allow-dirty",
        "install",
        "--release-id",
        "r1",
        "--apply",
    ]


def test_host_first_help_delegates_to_the_selected_command(tmp_path: Path) -> None:
    captured = tmp_path / "arguments.txt"
    fake_uv = tmp_path / "uv"
    fake_uv.write_text(
        "#!/bin/sh\nprintf '%s\\n' \"$@\" > \"$EIDOLON_CAPTURE\"\n",
        encoding="utf-8",
    )
    fake_uv.chmod(0o755)

    result = run(
        "mac",
        "help",
        "start",
        env={"EIDOLON_UV_BIN": str(fake_uv), "EIDOLON_CAPTURE": str(captured)},
    )

    assert result.returncode == 0
    assert captured.read_text(encoding="utf-8").splitlines()[-2:] == ["start", "--help"]


def test_status_is_human_by_default_and_json_on_request(tmp_path: Path) -> None:
    captured = tmp_path / "arguments.txt"
    fake_uv = tmp_path / "uv"
    fake_uv.write_text(
        "#!/bin/sh\nprintf '%s\\n' \"$@\" > \"$EIDOLON_CAPTURE\"\n",
        encoding="utf-8",
    )
    fake_uv.chmod(0o755)
    environment = {"EIDOLON_UV_BIN": str(fake_uv), "EIDOLON_CAPTURE": str(captured)}

    assert run("mac", "status", env=environment).returncode == 0
    assert captured.read_text(encoding="utf-8").splitlines()[-2:] == ["status", "--human"]

    assert run("mac", "status", "--json", env=environment).returncode == 0
    assert captured.read_text(encoding="utf-8").splitlines()[-2:] == ["status", "--json"]
