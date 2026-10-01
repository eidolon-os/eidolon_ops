"""Shell-free local process boundary."""

from __future__ import annotations

import os
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
        user: int | None = None,
        group: int | None = None,
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
        user: int | None = None,
        group: int | None = None,
    ) -> ProcessResult:
        if os.geteuid() == 0 and Path(command[0]).name == "git" and "-C" in command:
            # The privileged Host adapter reads the explicitly selected source
            # repositories. Keep Git's ownership protection elsewhere; neither
            # write root's global config nor allow a wildcard directory.
            repository = Path(command[command.index("-C") + 1]).resolve()
            command = (command[0], "-c", f"safe.directory={repository}", *command[1:])
        try:
            result = subprocess.run(
                tuple(command),
                input=input_bytes,
                cwd=cwd,
                env=env,
                check=False,
                capture_output=True,
                timeout=timeout,
                user=user,
                group=group,
                extra_groups=[] if user is not None else None,
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
