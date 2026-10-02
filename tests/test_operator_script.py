from __future__ import annotations

import os
import subprocess
from pathlib import Path

from eidolon_ops.host_cli import OPERATIONS
from eidolon_ops.paths import HostDriver

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
    assert "rk3588" in result.stdout
    assert ".example.toml" not in result.stdout

    help_result = run("--help")
    assert help_result.returncode == 0
    assert "命令概览" in help_result.stdout
    assert "mac" in help_result.stdout and "debug" in help_result.stdout
    assert "pi5" in help_result.stdout and "install" in help_result.stdout
    # The overview used to print an empty row and an error for rk3588: it is
    # built by asking each profile for its commands, and that question was the
    # one that could fail. Nothing surfaced, because the loop asks inside a
    # command substitution, where the failure exits only the subshell.
    assert help_result.stderr == ""
    overview = help_result.stdout.split("命令概览", 1)[1]
    rows = [line for line in overview.splitlines() if line.startswith("  rk3588 ")]
    assert len(rows) == 1
    assert "install" in rows[0]


def test_operator_script_covers_every_cli_command() -> None:
    from unittest.mock import patch

    from eidolon_ops.host_cli import main

    with patch("builtins.print") as output:
        assert main(["--all-commands"]) == 0
    assert set(output.call_args.args[0].split()) == set(OPERATIONS)
    text = SCRIPT.read_text(encoding="utf-8")
    assert "LOCAL_SUPERVISORD_COMMANDS" not in text
    assert "SSH_SYSTEMD_COMMANDS" not in text


def test_help_is_host_specific() -> None:
    mac = run("commands", "mac")
    pi = run("commands", "pi5")
    assert mac.returncode == pi.returncode == 0
    assert "debug" in mac.stdout
    assert "install" not in mac.stdout
    assert "install" in pi.stdout
    assert "debug" not in pi.stdout


def test_a_second_product_board_gets_the_same_commands_as_the_first() -> None:
    """rk3588 is reached exactly as the Pi is, so it is offered the same set.

    The script used to answer by platform, and rk3588 was a platform it had
    never been told about — so this Host could not be driven through the entry
    point at all, and every operation on it had to be typed as the long
    ``uv run eidolon-ops --config ...`` form.
    """

    rk3588 = run("commands", "rk3588")
    pi = run("commands", "pi5")
    assert rk3588.returncode == 0
    assert "未知" not in rk3588.stderr

    def commands(stdout: str) -> set[str]:
        return {line.split()[0] for line in stdout.splitlines() if line.startswith("  ")}

    assert commands(rk3588.stdout) == commands(pi.stdout)
    assert {"install", "bring-up", "trust-host-key"} <= commands(rk3588.stdout)
    assert "debug" not in commands(rk3588.stdout)


def test_every_declared_driver_has_a_command_set() -> None:
    from eidolon_ops.host import build_adapter
    from eidolon_ops.host_cli import operation_capability
    from eidolon_ops.paths import load_host_profile
    from eidolon_ops.process import SubprocessRunner

    represented = set()
    for path in (ROOT / "config/hosts").glob("*.toml"):
        if path.name.endswith(".example.toml"):
            continue
        profile = load_host_profile(path)
        represented.add(profile.driver)
        capabilities = build_adapter(profile, SubprocessRunner()).capabilities
        expected = {name for name in OPERATIONS if operation_capability(name) in capabilities}
        result = run("commands", str(path))
        assert result.returncode == 0
        offered = {line.split()[0] for line in result.stdout.splitlines() if line.startswith("  ")}
        assert offered == expected
    assert represented == set(HostDriver)


def test_every_active_host_profile_can_be_driven() -> None:
    """Every profile an operator can name resolves to a command set.

    The list comes from the directory, not from this file: a fourth board
    dropped into config/hosts is covered the moment it exists.
    """

    profiles = sorted(
        path
        for path in (ROOT / "config/hosts").glob("*.toml")
        if not path.name.endswith(".example.toml")
    )
    assert profiles

    for profile in profiles:
        result = run("commands", profile.stem)
        assert result.returncode == 0, f"{profile.stem}: {result.stderr}"
        assert result.stderr == ""
        assert "status" in result.stdout


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
        "--no-sync",
        "eidolon-ops",
        "--config",
        str(ROOT / "config/hosts/pi5.toml"),
        "--allow-dirty",
        "install",
        "--release-id",
        "r1",
        "--apply",
    ]


def test_dispatch_reaches_a_second_product_board_by_its_short_name(tmp_path: Path) -> None:
    captured = tmp_path / "arguments.txt"
    fake_uv = tmp_path / "uv"
    fake_uv.write_text(
        "#!/bin/sh\nprintf '%s\\n' \"$@\" > \"$EIDOLON_CAPTURE\"\n",
        encoding="utf-8",
    )
    fake_uv.chmod(0o755)

    result = run(
        "rk3588",
        "status",
        env={"EIDOLON_UV_BIN": str(fake_uv), "EIDOLON_CAPTURE": str(captured)},
    )

    assert result.returncode == 0
    assert captured.read_text(encoding="utf-8").splitlines() == [
        "run",
        "--no-sync",
        "eidolon-ops",
        "--config",
        str(ROOT / "config/hosts/rk3588.toml"),
        "status",
        "--human",
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
