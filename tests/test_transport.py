from __future__ import annotations

from pathlib import Path

import pytest

from eidolon_ops.endpoints import HostEndpoint
from eidolon_ops.process import ProcessError, ProcessResult
from eidolon_ops.transport import SSHTransport, TransportError

pytestmark = pytest.mark.unit


def _no_endpoints(_hostname: str, _port: int):
    """Unit transports resolve nothing: they assert on the configured name."""

    return ()


class RecordingRunner:
    def __init__(self, results: list[ProcessResult] | None = None) -> None:
        self.results = list(results or [ProcessResult(0, "{}", "")])
        self.calls: list[dict[str, object]] = []

    def run(self, command, **kwargs):
        self.calls.append({"command": tuple(command), **kwargs})
        return self.results.pop(0)


def test_ssh_run_uses_strict_batch_mode(config) -> None:
    runner = RecordingRunner([ProcessResult(0, "ok", "")])
    transport = SSHTransport(config.host, runner, endpoints=_no_endpoints)

    result = transport.run(("/usr/bin/true",))

    assert result.stdout == "ok"
    command = runner.calls[0]["command"]
    assert "BatchMode=yes" in command
    assert "StrictHostKeyChecking=yes" in command
    assert f"UserKnownHostsFile={config.host.known_hosts_file}" in command
    assert "ServerAliveInterval=15" in command
    assert "ServerAliveCountMax=20" in command
    assert "TCPKeepAlive=yes" in command
    assert command[-2:] == (config.host.target, "/usr/bin/true")


def test_sudo_is_non_interactive(config) -> None:
    runner = RecordingRunner([ProcessResult(0, "", "")])
    transport = SSHTransport(config.host, runner, endpoints=_no_endpoints)

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
    transport = SSHTransport(config.host, RecordingRunner(), endpoints=_no_endpoints)

    with pytest.raises(TransportError, match="unsafe"):
        transport.run(("/bin/echo", token))


def test_run_agent_sends_script_and_base64_payload(config) -> None:
    runner = RecordingRunner([ProcessResult(0, '{"status":"ok"}', "")])
    transport = SSHTransport(config.host, runner, endpoints=_no_endpoints)

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
        config.host, RecordingRunner([ProcessResult(0, "not-json", "")]), endpoints=_no_endpoints
    )

    with pytest.raises(TransportError, match="JSON"):
        transport.run_agent("status", {})


def test_run_agent_rejects_non_object_json(config) -> None:
    transport = SSHTransport(
        config.host,
        RecordingRunner([ProcessResult(0, "[]", "")]),
        endpoints=_no_endpoints,
    )

    with pytest.raises(TransportError, match="non-object"):
        transport.run_agent("status", {})


def test_remote_failure_preserves_diagnostic(config) -> None:
    transport = SSHTransport(
        config.host,
        RecordingRunner([ProcessResult(5, "", "remote failure")]),
        endpoints=_no_endpoints,
    )

    with pytest.raises(ProcessError, match="remote failure"):
        transport.run(("/usr/bin/false",), operation="test")


def test_upload_uses_scp_port_and_recursive(config, tmp_path: Path) -> None:
    source = tmp_path / "bundle"
    source.mkdir()
    runner = RecordingRunner([ProcessResult(0, "", "")])
    transport = SSHTransport(config.host, runner, endpoints=_no_endpoints)

    transport.upload(source, "/var/tmp/eidolon-release-r1", recursive=True)

    command = runner.calls[0]["command"]
    assert command[:3] == ("scp", "-P", "2222")
    assert "-r" in command
    assert "ServerAliveInterval=15" in command
    assert "ServerAliveCountMax=20" in command
    assert command[-1] == f"{config.host.target}:/var/tmp/eidolon-release-r1"


def test_upload_rejects_missing_source(config, tmp_path: Path) -> None:
    transport = SSHTransport(config.host, RecordingRunner(), endpoints=_no_endpoints)

    with pytest.raises(TransportError, match="missing"):
        transport.upload(tmp_path / "missing", "/var/tmp/target")


def test_resumable_directory_upload_uses_strict_rsync(config, tmp_path: Path) -> None:
    source = tmp_path / "bundle"
    source.mkdir()
    runner = RecordingRunner([ProcessResult(0, "", "")])
    transport = SSHTransport(config.host, runner, endpoints=_no_endpoints)

    transport.upload_directory_resumable(source, "/var/tmp/eidolon-release-r1")

    command = runner.calls[0]["command"]
    assert command[0] == "rsync"
    assert "--partial-dir=.eidolon-partial" in command
    assert "--delay-updates" in command
    remote_shell = command[command.index("-e") + 1]
    assert "BatchMode=yes" in remote_shell
    assert "StrictHostKeyChecking=yes" in remote_shell
    assert "ServerAliveInterval=15" in remote_shell
    assert "ServerAliveCountMax=20" in remote_shell
    assert command[-2:] == (
        f"{source}/",
        f"{config.host.target}:/var/tmp/eidolon-release-r1/",
    )


def test_resumable_upload_rejects_symlink_source(config, tmp_path: Path) -> None:
    source = tmp_path / "bundle"
    source.mkdir()
    link = tmp_path / "bundle-link"
    link.symlink_to(source)
    transport = SSHTransport(config.host, RecordingRunner(), endpoints=_no_endpoints)

    with pytest.raises(TransportError, match="unsafe"):
        transport.upload_directory_resumable(link, "/var/tmp/eidolon-release-r1")


def test_agent_payload_does_not_log_json_plaintext(config) -> None:
    runner = RecordingRunner([ProcessResult(0, "{}", "")])
    transport = SSHTransport(config.host, runner, endpoints=_no_endpoints)

    transport.run_agent("status", {"marker": "sensitive-placeholder"})

    command = runner.calls[0]["command"]
    assert "sensitive-placeholder" not in " ".join(command)


def test_wired_endpoint_is_preferred_and_trust_stays_on_the_name(config) -> None:
    # Both links reach the same Host. Work should take the fast one, and the
    # host key should still be looked up under the Host's name, so changing
    # link is not a trust decision.
    candidates = (
        HostEndpoint(address="169.254.55.2", interface="en7", link="wired"),
        HostEndpoint(address="192.168.1.26", interface="en0", link="wireless"),
    )
    runner = RecordingRunner([ProcessResult(0, "", "")])
    transport = SSHTransport(
        config.host,
        runner,
        endpoints=lambda *_: candidates,
        probe=lambda *_: True,
    )

    transport.run(("/usr/bin/true",))

    command = runner.calls[0]["command"]
    assert f"{config.host.user}@169.254.55.2" in command
    assert f"HostKeyAlias={config.host.hostname}" in command
    assert transport.endpoint == candidates[0]


def test_a_dead_wire_falls_through_to_the_link_that_answers(config) -> None:
    candidates = (
        HostEndpoint(address="169.254.55.2", interface="en7", link="wired"),
        HostEndpoint(address="192.168.1.26", interface="en0", link="wireless"),
    )
    runner = RecordingRunner([ProcessResult(0, "", "")])
    transport = SSHTransport(
        config.host,
        runner,
        endpoints=lambda *_: candidates,
        probe=lambda address, *_: address != "169.254.55.2",
    )

    transport.run(("/usr/bin/true",))

    assert f"{config.host.user}@192.168.1.26" in runner.calls[0]["command"]


def test_unreachable_endpoints_fall_back_to_the_configured_name(config) -> None:
    runner = RecordingRunner([ProcessResult(0, "", "")])
    transport = SSHTransport(config.host, runner, endpoints=_no_endpoints)

    transport.run(("/usr/bin/true",))

    assert config.host.target in runner.calls[0]["command"]
    assert transport.endpoint is None


def test_one_run_settles_on_one_endpoint(config) -> None:
    # A release that starts over the wire must not finish over Wi-Fi, so the
    # choice is made once and every later command inherits it.
    resolutions: list[tuple[str, int]] = []

    def _resolver(hostname: str, port: int):
        resolutions.append((hostname, port))
        return (HostEndpoint(address="169.254.55.2", interface="en7", link="wired"),)

    runner = RecordingRunner([ProcessResult(0, "", ""), ProcessResult(0, "", "")])
    transport = SSHTransport(config.host, runner, endpoints=_resolver, probe=lambda *_: True)

    transport.run(("/usr/bin/true",))
    transport.run(("/usr/bin/true",))

    assert resolutions == [(config.host.hostname, config.host.port)]
    assert {call["command"][-2] for call in runner.calls} == {f"{config.host.user}@169.254.55.2"}
