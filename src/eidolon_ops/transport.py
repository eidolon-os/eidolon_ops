"""Strict BatchMode SSH/SCP transport authenticated by an explicit host-key file."""

from __future__ import annotations

import base64
import json
import re
import shlex
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path

from eidolon_ops.config import HostConfig
from eidolon_ops.endpoints import (
    HostEndpoint,
    LocalInterface,
    first_reachable,
    rank_addresses,
    resolve_endpoints,
)
from eidolon_ops.hostagent_delivery import injected_script
from eidolon_ops.ports import TransportKind
from eidolon_ops.process import ProcessError, ProcessResult, ProcessRunner, checked

_REMOTE_TOKEN = re.compile(r"^[A-Za-z0-9_./:=+@,-]+$")


class TransportError(RuntimeError):
    """SSH output or a requested remote argument violated the transport contract."""


class SSHTransport:
    kind = TransportKind.SSH

    def __init__(
        self,
        host: HostConfig,
        runner: ProcessRunner,
        *,
        ssh: str = "ssh",
        scp: str = "scp",
        rsync: str = "rsync",
        endpoints: Callable[[str, int], Sequence[HostEndpoint]] | None = None,
        probe: Callable[[HostEndpoint, int, float], bool] | None = None,
        interfaces: Callable[[], tuple[LocalInterface, ...]] | None = None,
    ) -> None:
        self.host = host
        self.runner = runner
        self.ssh = ssh
        self.scp = scp
        self.rsync = rsync
        self._endpoints = endpoints or resolve_endpoints
        self._probe = probe
        self._interfaces = interfaces
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

        self._resolve()
        if self._endpoint is None:
            # Nothing answered, or the name resolves to something we do not
            # rank. Hand the name to SSH and let its own error be the report.
            return self.host.target
        return f"{self.host.user}@{self._endpoint.address}"

    def describe(self) -> str:
        """Which link this session settled on.

        It decides whether the next release takes two seconds or three
        minutes, so the operator is told rather than left to infer it from how
        long they waited.
        """

        self._resolve()
        endpoint = self._endpoint
        return endpoint.describe() if endpoint else self.host.hostname

    def _bind_options(self) -> tuple[str, ...]:
        """Force this session out of the interface the endpoint chose.

        Only link-local endpoints ask for this, and only they need it: their
        route is ambiguous on every machine that has two self-assigned links,
        so without it the packets can leave by Wi-Fi while the Host is on the
        wire. That failure does not look like a routing mistake — it looks
        like a Host that is not there.
        """

        self._resolve()
        interface = self._endpoint.bind_interface if self._endpoint else None
        return () if interface is None else ("-o", f"BindInterface={interface}")

    def _resolve(self) -> None:
        if self._resolved:
            return
        self._resolved = True
        self._endpoint = first_reachable(
            self._endpoints(self.host.hostname, self.host.port),
            self.host.port,
            timeout=self.host.connect_timeout_seconds,
            probe=self._probe or self._answers_over_ssh,
        )

    def prefer_wired_link(self) -> None:
        """Ask the Host for its own addresses when the resolver found no wire.

        The resolver can be missing a link entirely rather than ranking it
        badly: a Host on Wi-Fi and a cable resolves to the Wi-Fi record alone
        once the cable's mDNS announcement has aged out, and then the wire is
        not a candidate at all. Refusing the release at that point reports a
        wireless link over a cable that is plugged in and carrying this very
        session.

        So when the best candidate is not the wire, the Host is asked which
        addresses it has — it publishes them already — and those are attributed
        to local links by the same rule. Only a wired one replaces the choice,
        and only if it answers over SSH like any other candidate.

        Wired and not merely better-ranked: "unknown" outranks Wi-Fi in the
        resolver's ordering because an unattributable address might be the
        cable, but here it would trade a link that is known for one that could
        be the VPN this policy exists to keep a release off. Nothing but the
        wire is worth changing a working choice for.

        Best effort on purpose. A Host that cannot answer leaves the resolver's
        choice standing, because this decides how fast an upload is, not
        whether it is allowed: `require_wired_release_upload` is still the gate
        and still refuses on its own.

        Asked for rather than automatic, and only by the release path. The
        round trip can only pay for itself where the link decides how long the
        work takes; on a Host that genuinely has one link, charging every
        `status` and `logs` for the same unhelpful answer is just slower.
        """

        self._resolve()
        endpoint = self._endpoint
        if endpoint is None or endpoint.link == "wired":
            return
        try:
            report = self.run_agent(
                "host-addresses", {}, timeout=self.host.connect_timeout_seconds + 30
            )
        except (ProcessError, TransportError, OSError):
            return
        reported = report.get("addresses")
        if not isinstance(reported, list):
            return
        wired = [
            candidate
            for candidate in rank_addresses(
                (str(value) for value in reported), interfaces=self._interfaces
            )
            if candidate.link == "wired"
        ]
        found = first_reachable(
            wired,
            self.host.port,
            timeout=self.host.connect_timeout_seconds,
            probe=self._probe or self._answers_over_ssh,
        )
        if found is not None:
            self._endpoint = found

    def _answers_over_ssh(self, endpoint: HostEndpoint, port: int, timeout: float) -> bool:
        """Whether the Host answers on this candidate — asked by SSH itself.

        This used to be a socket opened in this process, which asks a subtly
        different question: on macOS a LAN socket also depends on whether this
        interpreter was granted Local Network permission, and an interpreter
        installed by a virtual environment is not. Three deploys were refused
        with "the selected link is 'unresolved'" over a cable that was plugged
        in, while `ssh` to the same address connected every time.

        So the link is probed with the program that will carry the release. An
        SSH handshake also proves more than an accepted connection: the right
        host key, a usable identity, an account that lets us in. A candidate
        that answers this cannot fail the first real command for any of those
        reasons.
        """

        options = ["-o", f"BindInterface={endpoint.bind_interface}"] if endpoint.bind_interface else []
        result = self.runner.run(
            (
                self.ssh,
                *options,
                "-p",
                str(port),
                "-i",
                str(self.host.identity_file),
                "-o",
                "BatchMode=yes",
                "-o",
                "StrictHostKeyChecking=yes",
                "-o",
                f"HostKeyAlias={self.host.hostname}",
                "-o",
                f"UserKnownHostsFile={self.host.known_hosts_file}",
                "-o",
                f"ConnectTimeout={int(timeout)}",
                f"{self.host.user}@{endpoint.address}",
                "true",
            ),
            timeout=timeout + 10,
        )
        return result.returncode == 0

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
        script = injected_script()
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
            *self._bind_options(),
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

    def upload_directory_resumable(
        self, source: Path, destination: str, *, exclude: Sequence[str] = ()
    ) -> None:
        """Resume an immutable release bundle into an already guarded directory."""

        if (
            not source.is_dir()
            or source.is_symlink()
            or _REMOTE_TOKEN.fullmatch(destination) is None
            or any(re.fullmatch(r"[A-Za-z0-9._-]+", item) is None for item in exclude)
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
                *(f"--exclude=/{item}" for item in exclude),
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
            *self._bind_options(),
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
