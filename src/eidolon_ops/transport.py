"""Strict BatchMode SSH/SCP transport authenticated by an explicit host-key file."""

from __future__ import annotations

import base64
import json
import re
import shlex
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path

from eidolon_ops.config import HostConfig
from eidolon_ops.endpoints import HostEndpoint, first_reachable, resolve_endpoints
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
        endpoints: Callable[[str, int], Sequence[HostEndpoint]] | None = None,
        probe: Callable[[str, int, float], bool] | None = None,
    ) -> None:
        self.host = host
        self.runner = runner
        self.ssh = ssh
        self.scp = scp
        self.rsync = rsync
        self._endpoints = endpoints or resolve_endpoints
        self._probe = probe
        self._endpoint: HostEndpoint | None = None
        self._resolved = False

    @property
    def endpoint(self) -> HostEndpoint | None:
        """The address this session settled on, once one has been chosen."""

        return self._endpoint

    @property
    def target(self) -> str:
        """``user@address`` for the best link that answers right now.

        The configured hostname stays the Host's identity — it is what the host
        key is trusted under, via HostKeyAlias — while the address is whichever
        of its links is up. Resolution happens once per run: a release that
        starts over the wire should not silently finish over Wi-Fi.
        """

        if not self._resolved:
            self._resolved = True
            self._endpoint = first_reachable(
                self._endpoints(self.host.hostname, self.host.port),
                self.host.port,
                timeout=self.host.connect_timeout_seconds,
                probe=self._probe,
            )
        if self._endpoint is None:
            # Nothing answered, or the name resolves to something we do not
            # rank. Hand the name to SSH and let its own error be the report.
            return self.host.target
        return f"{self.host.user}@{self._endpoint.address}"

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
            (*self._ssh_prefix(), self.target, *tokens),
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

    def _scp_options(self) -> list[str]:
        return [
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
            # The Host is one machine whichever of its links answers, so its key
            # is trusted under its name and not re-approved per address.
            f"HostKeyAlias={self.host.hostname}",
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

    def upload(self, source: Path, destination: str, *, recursive: bool = False) -> None:
        if not source.exists() or _REMOTE_TOKEN.fullmatch(destination) is None:
            raise TransportError("upload source is missing or destination is unsafe")
        options = [*self._scp_options()]
        if recursive:
            options.append("-r")
        result = self.runner.run(
            (*options, str(source), f"{self.target}:{destination}"),
            timeout=1800,
        )
        checked("SCP upload", result)

    def download(self, source: str, destination: Path, *, recursive: bool = False) -> None:
        """Bring a Host-produced directory back to the operator's machine."""

        if _REMOTE_TOKEN.fullmatch(source) is None:
            raise TransportError("download source is unsafe")
        destination.parent.mkdir(parents=True, exist_ok=True)
        options = [*self._scp_options()]
        if recursive:
            options.append("-r")
        result = self.runner.run(
            (*options, f"{self.target}:{source}", str(destination)),
            timeout=1800,
        )
        checked("SCP download", result)

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
                f"{self.target}:{destination}/",
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
            # The Host is one machine whichever of its links answers, so its key
            # is trusted under its name and not re-approved per address.
            f"HostKeyAlias={self.host.hostname}",
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
