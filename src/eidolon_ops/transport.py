"""Strict BatchMode SSH/SCP transport authenticated by an explicit host-key file."""

from __future__ import annotations

import base64
import json
import re
import shlex
from collections.abc import Mapping, Sequence
from pathlib import Path

from eidolon_ops.config import HostConfig
from eidolon_ops.process import ProcessResult, ProcessRunner, checked

_REMOTE_TOKEN = re.compile(r"^[A-Za-z0-9_./:=+@,-]+$")


class TransportError(RuntimeError):
    """SSH output or a requested remote argument violated the transport contract."""


class SSHTransport:
    def __init__(
        self,
        host: HostConfig,
        runner: ProcessRunner,
        *,
        ssh: str = "ssh",
        scp: str = "scp",
        rsync: str = "rsync",
    ) -> None:
        self.host = host
        self.runner = runner
        self.ssh = ssh
        self.scp = scp
        self.rsync = rsync

    def run(
        self,
        remote: Sequence[str],
        *,
        input_bytes: bytes | None = None,
        sudo: bool = False,
        timeout: float = 120,
        operation: str = "remote command",
    ) -> ProcessResult:
        tokens = tuple(remote)
        if not tokens or any(_REMOTE_TOKEN.fullmatch(token) is None for token in tokens):
            raise TransportError("remote command contains an unsafe token")
        if sudo:
            tokens = ("sudo", "--non-interactive", *tokens)
        result = self.runner.run(
            (*self._ssh_prefix(), self.host.target, *tokens),
            input_bytes=input_bytes,
            timeout=timeout,
        )
        return checked(operation, result)

    def run_agent(
        self,
        action: str,
        payload: Mapping[str, object],
        *,
        python: str = "/usr/bin/python3",
        sudo: bool = True,
        timeout: float = 120,
    ) -> dict[str, object]:
        if _REMOTE_TOKEN.fullmatch(action) is None or _REMOTE_TOKEN.fullmatch(python) is None:
            raise TransportError("agent action or Python path is unsafe")
        encoded = base64.urlsafe_b64encode(
            json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
        ).decode("ascii")
        script = Path(__file__).with_name("target_agent.py").read_bytes()
        result = self.run(
            (python, "-", action, encoded),
            input_bytes=script,
            sudo=sudo,
            timeout=timeout,
            operation=f"remote {action}",
        )
        try:
            value = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise TransportError(f"remote {action} did not return one JSON document") from exc
        if not isinstance(value, dict):
            raise TransportError(f"remote {action} returned a non-object JSON document")
        return value

    def upload(self, source: Path, destination: str, *, recursive: bool = False) -> None:
        if not source.exists() or _REMOTE_TOKEN.fullmatch(destination) is None:
            raise TransportError("upload source is missing or destination is unsafe")
        options = [
            self.scp,
            "-P",
            str(self.host.port),
            "-i",
            str(self.host.identity_file),
            "-o",
            "BatchMode=yes",
            "-o",
            "StrictHostKeyChecking=yes",
            "-o",
            f"UserKnownHostsFile={self.host.known_hosts_file}",
            "-o",
            f"ConnectTimeout={self.host.connect_timeout_seconds}",
            "-o",
            "ServerAliveInterval=15",
            "-o",
            "ServerAliveCountMax=20",
            "-o",
            "TCPKeepAlive=yes",
        ]
        if recursive:
            options.append("-r")
        result = self.runner.run(
            (*options, str(source), f"{self.host.target}:{destination}"),
            timeout=1800,
        )
        checked("SCP upload", result)

    def upload_directory_resumable(self, source: Path, destination: str) -> None:
        """Resume an immutable release bundle into an already guarded directory."""

        if (
            not source.is_dir()
            or source.is_symlink()
            or _REMOTE_TOKEN.fullmatch(destination) is None
        ):
            raise TransportError("resumable upload source or destination is unsafe")
        remote_shell = shlex.join(self._ssh_prefix())
        result = self.runner.run(
            (
                self.rsync,
                "-rlpt",
                "--partial",
                "--partial-dir=.eidolon-partial",
                "--delay-updates",
                "--timeout=120",
                "--rsync-path=/usr/bin/rsync",
                "-e",
                remote_shell,
                f"{source}/",
                f"{self.host.target}:{destination}/",
            ),
            timeout=3600,
        )
        checked("resumable release upload", result)

    def _ssh_prefix(self) -> tuple[str, ...]:
        return (
            self.ssh,
            "-p",
            str(self.host.port),
            "-i",
            str(self.host.identity_file),
            "-o",
            "BatchMode=yes",
            "-o",
            "StrictHostKeyChecking=yes",
            "-o",
            f"UserKnownHostsFile={self.host.known_hosts_file}",
            "-o",
            f"ConnectTimeout={self.host.connect_timeout_seconds}",
            "-o",
            "ServerAliveInterval=15",
            "-o",
            "ServerAliveCountMax=20",
            "-o",
            "TCPKeepAlive=yes",
        )
