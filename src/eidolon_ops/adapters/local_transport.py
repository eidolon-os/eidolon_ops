"""Reach a Host that is this machine."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path

from eidolon_ops.ports import TransportKind
from eidolon_ops.process import ProcessResult, ProcessRunner, checked


class LocalTransport:
    """Run a command here, under the Host profile's own environment."""

    kind = TransportKind.LOCAL

    def __init__(
        self,
        runner: ProcessRunner,
        *,
        cwd: Path,
        env: Mapping[str, str],
    ) -> None:
        self.runner = runner
        self.cwd = cwd
        self.env = dict(env)

    def describe(self) -> str:
        return "local"

    def run(
        self,
        command: Sequence[str],
        *,
        timeout: float = 120,
        operation: str = "local command",
    ) -> ProcessResult:
        return checked(
            operation,
            self.runner.run(tuple(command), cwd=self.cwd, env=self.env, timeout=timeout),
        )
