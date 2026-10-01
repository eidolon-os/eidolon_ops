"""Shell-free local process boundary."""

from __future__ import annotations

import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


class ProcessError(RuntimeError):
    """A fixed local or remote command failed."""

    def __init__(self, operation: str, result: ProcessResult) -> None:
        detail = result.stderr.strip() or result.stdout.strip() or "no diagnostic output"
        super().__init__(f"{operation} failed with exit {result.returncode}: {detail}")
        self.operation = operation
        self.result = result


@dataclass(frozen=True, slots=True)
class ProcessResult:
    returncode: int
    stdout: str
    stderr: str


class ProcessRunner(Protocol):
    def run(
        self,
        command: Sequence[str],
        *,
        input_bytes: bytes | None = None,
        cwd: Path | None = None,
        env: Mapping[str, str] | None = None,
        timeout: float = 120,
    ) -> ProcessResult: ...


class SubprocessRunner:
    def run(
        self,
        command: Sequence[str],
        *,
        input_bytes: bytes | None = None,
        cwd: Path | None = None,
        env: Mapping[str, str] | None = None,
        timeout: float = 120,
    ) -> ProcessResult:
        try:
            result = subprocess.run(
                tuple(command),
                input=input_bytes,
                cwd=cwd,
                env=env,
                check=False,
                capture_output=True,
                timeout=timeout,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise ProcessError(
                "command execution",
                ProcessResult(127, "", str(exc)),
            ) from exc
        return ProcessResult(
            result.returncode,
            result.stdout.decode("utf-8", errors="replace"),
            result.stderr.decode("utf-8", errors="replace"),
        )


def checked(operation: str, result: ProcessResult) -> ProcessResult:
    if result.returncode != 0:
        raise ProcessError(operation, result)
    return result
