from __future__ import annotations

from pathlib import Path

import pytest

from eidolon_ops.process import ProcessError, ProcessResult
from eidolon_ops.transport import SSHTransport, TransportError

pytestmark = pytest.mark.unit


class RecordingRunner:
    def __init__(self, results: list[ProcessResult] | None = None) -> None:
        self.results = list(results or [ProcessResult(0, "{}", "")])
        self.calls: list[dict[str, object]] = []

    def run(self, command, **kwargs):
        self.calls.append({"command": tuple(command), **kwargs})
        return self.results.pop(0)


def test_ssh_run_uses_strict_batch_mode(config) -> None:
    runner = RecordingRunner([ProcessResult(0, "ok", "")])
    transport = SSHTransport(config.host, runner)

    result = transport.run(("/usr/bin/true",))

    assert result.stdout == "ok"
    command = runner.calls[0]["command"]
    assert "BatchMode=yes" in command
    assert "StrictHostKeyChecking=yes" in command
    assert f"UserKnownHostsFile={config.host.known_hosts_file}" in command
    assert command[-2:] == (config.host.target, "/usr/bin/true")


def test_sudo_is_non_interactive(config) -> None:
    runner = RecordingRunner([ProcessResult(0, "", "")])
    transport = SSHTransport(config.host, runner)

    transport.run(("/usr/bin/systemctl", "status"), sudo=True)

    command = runner.calls[0]["command"]
    target_index = command.index(config.host.target)
    assert command[target_index + 1 :] == (
        "sudo",
        "--non-interactive",
        "/usr/bin/systemctl",
        "status",
    )


@pytest.mark.parametrize("token", ["has space", "$(bad)", "semi;colon", "quote'bad"])
def test_rejects_shell_metacharacters(config, token: str) -> None:
    transport = SSHTransport(config.host, RecordingRunner())

    with pytest.raises(TransportError, match="unsafe"):
        transport.run(("/bin/echo", token))


def test_run_agent_sends_script_and_base64_payload(config) -> None:
    runner = RecordingRunner([ProcessResult(0, '{"status":"ok"}', "")])
    transport = SSHTransport(config.host, runner)

    result = transport.run_agent("status", {"units": ["one"]})

    assert result == {"status": "ok"}
    call = runner.calls[0]
    assert isinstance(call["input_bytes"], bytes)
    assert b"Standalone target-side operations" in call["input_bytes"]
    command = call["command"]
    assert command[-4:-2] == ("/usr/bin/python3", "-")
    assert command[-2] == "status"


def test_run_agent_rejects_non_json_output(config) -> None:
    transport = SSHTransport(
        config.host,
        RecordingRunner([ProcessResult(0, "not-json", "")]),
    )

    with pytest.raises(TransportError, match="JSON"):
        transport.run_agent("status", {})


def test_run_agent_rejects_non_object_json(config) -> None:
    transport = SSHTransport(
        config.host,
        RecordingRunner([ProcessResult(0, "[]", "")]),
    )

    with pytest.raises(TransportError, match="non-object"):
        transport.run_agent("status", {})


def test_remote_failure_preserves_diagnostic(config) -> None:
    transport = SSHTransport(
        config.host,
        RecordingRunner([ProcessResult(5, "", "remote failure")]),
    )

    with pytest.raises(ProcessError, match="remote failure"):
        transport.run(("/usr/bin/false",), operation="test")


def test_upload_uses_scp_port_and_recursive(config, tmp_path: Path) -> None:
    source = tmp_path / "bundle"
    source.mkdir()
    runner = RecordingRunner([ProcessResult(0, "", "")])
    transport = SSHTransport(config.host, runner)

    transport.upload(source, "/var/tmp/eidolon-release-r1", recursive=True)

    command = runner.calls[0]["command"]
    assert command[:3] == ("scp", "-P", "2222")
    assert "-r" in command
    assert command[-1] == f"{config.host.target}:/var/tmp/eidolon-release-r1"


def test_upload_rejects_missing_source(config, tmp_path: Path) -> None:
    transport = SSHTransport(config.host, RecordingRunner())

    with pytest.raises(TransportError, match="missing"):
        transport.upload(tmp_path / "missing", "/var/tmp/target")


def test_resumable_directory_upload_uses_strict_rsync(config, tmp_path: Path) -> None:
    source = tmp_path / "bundle"
    source.mkdir()
    runner = RecordingRunner([ProcessResult(0, "", "")])
    transport = SSHTransport(config.host, runner)

    transport.upload_directory_resumable(source, "/var/tmp/eidolon-release-r1")

    command = runner.calls[0]["command"]
    assert command[0] == "rsync"
    assert "--partial-dir=.eidolon-partial" in command
    assert "--delay-updates" in command
    remote_shell = command[command.index("-e") + 1]
    assert "BatchMode=yes" in remote_shell
    assert "StrictHostKeyChecking=yes" in remote_shell
    assert command[-2:] == (
        f"{source}/",
        f"{config.host.target}:/var/tmp/eidolon-release-r1/",
    )


def test_resumable_upload_rejects_symlink_source(config, tmp_path: Path) -> None:
    source = tmp_path / "bundle"
    source.mkdir()
    link = tmp_path / "bundle-link"
    link.symlink_to(source)
    transport = SSHTransport(config.host, RecordingRunner())

    with pytest.raises(TransportError, match="unsafe"):
        transport.upload_directory_resumable(link, "/var/tmp/eidolon-release-r1")


def test_agent_payload_does_not_log_json_plaintext(config) -> None:
    runner = RecordingRunner([ProcessResult(0, "{}", "")])
    transport = SSHTransport(config.host, runner)

    transport.run_agent("status", {"marker": "sensitive-placeholder"})

    command = runner.calls[0]["command"]
    assert "sensitive-placeholder" not in " ".join(command)
