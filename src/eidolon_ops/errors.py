"""The failures an orchestration step or a private input transaction raises."""

from __future__ import annotations


class OperationsError(RuntimeError):
    """An orchestration invariant or phase failed."""


class InstallInputError(ValueError):
    """Install inputs cannot be created without ambiguity or overwrite."""
