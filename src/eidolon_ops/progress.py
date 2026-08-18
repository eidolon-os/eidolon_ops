"""How far an operation has got, said while it is still going.

Every operation that takes minutes already keeps the list of phases it
completed — the same list ``steps_from_phases`` pairs back onto the plan it was
approved under. What it never did was say anything before that list was
finished, so the only progress report an operator had was their own patience.

``Journal`` is that list, told to announce itself. It changes nothing about
what an operation reports: with no sink it is a plain list of the same entries
in the same order, which is what every existing caller and test sees. With a
sink — a console watching one run — the same entries arrive as they happen,
and a phase also says when it *started*, because the interesting question
during a twenty-minute release is which step is running, not which ones are
done.

The vocabulary is deliberately the plan's: a journal entry's ``phase`` is a
``Step.id`` from ``plans``. A phase a plan never declared is still reported —
it is real work — but it renders as activity rather than as a step, and that
asymmetry is honest: the plan is what was approved.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Protocol, runtime_checkable

__all__ = ["Journal", "ProgressSink"]


@runtime_checkable
class ProgressSink(Protocol):
    """Whoever is watching a run happen."""

    def phase_began(self, phase: str) -> None:
        """A named phase started. It has produced no evidence yet."""

    def phase_recorded(self, entry: Mapping[str, object]) -> None:
        """A named phase finished, with the evidence it produced."""


class Journal(list):
    """The phase list an operation keeps, with a listener attached.

    A ``list`` subclass rather than a wrapper, because the phase list *is* the
    report: it is returned inside the Host report under ``phases``, compared
    by value in tests, and serialized as JSON. Making progress a property of
    that list is what kept this change from becoming a callback threaded
    through nine call sites.
    """

    __slots__ = ("_open", "_sink")

    def __init__(
        self,
        sink: ProgressSink | None = None,
        entries: Iterable[Mapping[str, object]] = (),
    ) -> None:
        super().__init__(entries)
        self._sink = sink
        self._open: str | None = None

    @property
    def open_phase(self) -> str | None:
        """The phase that began and has not recorded evidence yet.

        What failed, when an operation raises: the step an operator should be
        shown as red rather than as never having been reached.
        """

        return self._open

    def begin(self, phase: str) -> None:
        """Say a phase is starting, before the work that proves it."""

        self._open = phase
        if self._sink is not None:
            self._sink.phase_began(phase)

    def append(self, entry: Mapping[str, object]) -> None:  # type: ignore[override]
        super().append(entry)
        self._announce(entry)

    def extend(self, entries: Iterable[Mapping[str, object]]) -> None:  # type: ignore[override]
        for entry in entries:
            self.append(entry)

    def _announce(self, entry: Mapping[str, object]) -> None:
        phase = entry.get("phase")
        if isinstance(phase, str) and phase == self._open:
            self._open = None
        if self._sink is not None:
            self._sink.phase_recorded(entry)
