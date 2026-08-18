"""One run of one operation, watched while it happens.

A run is what the console adds to the CLI. The CLI's unit of work is a process
that prints a report when it is over; here the same work happens on a worker
thread, announces each phase as the Host reaches it, and can be read from a
browser that connected halfway through.

Nothing is written to disk. A run lives in this process and dies with it,
because the authority for what happened to a Host is the Host's own receipts and
the release transaction's evidence — a console that kept its own copy would be a
second answer to the same question, and the wrong one as soon as it was stale.

A run cannot be cancelled. Ops's boundary actions are resumable, not
interruptible: killing an SSH-driven release transaction halfway is how a Host
ends up in a state no plan describes. What the console offers instead is the
thing that makes cancellation tempting — knowing which step is running.
"""

from __future__ import annotations

import queue
import threading
import time
import uuid
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field
from enum import StrEnum

from eidolon_ops.console.errors import ConsoleError
from eidolon_ops.console.redaction import redacted
from eidolon_ops.model import Evidence
from eidolon_ops.progress import ProgressSink

__all__ = ["Event", "Request", "Run", "RunStatus", "RunStore"]

#: How long a stream waits before emitting a keepalive rather than blocking.
_IDLE_SECONDS = 15.0


class RunStatus(StrEnum):
    """Whether the run happened — not what it found.

    Kept apart from ``Outcome`` deliberately. A ``status`` run against a
    degraded Host did everything it was asked and returned the answer; calling
    that a failed run would make the console's red mean two different things,
    and the one an operator needs to act on is the operation that never
    completed. The verdict stays where it belongs, in ``outcome``.
    """

    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


class PhaseStatus(StrEnum):
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"


class EventKind(StrEnum):
    STARTED = "run.started"
    PHASE_BEGAN = "phase.began"
    PHASE_RECORDED = "phase.recorded"
    FINISHED = "run.finished"


_TERMINAL = frozenset({EventKind.FINISHED})


@dataclass(frozen=True, slots=True)
class Event:
    seq: int
    at: float
    kind: EventKind
    payload: Mapping[str, object]

    def to_json(self) -> dict[str, object]:
        return {
            "seq": self.seq,
            "at": self.at,
            "kind": str(self.kind),
            **dict(self.payload),
        }


@dataclass(slots=True)
class Phase:
    """One named phase of a run, as far as it has got."""

    name: str
    status: PhaseStatus
    began_at: float
    ended_at: float | None = None
    detail: object = None

    def to_json(self) -> dict[str, object]:
        return {
            "name": self.name,
            "status": str(self.status),
            "began_at": self.began_at,
            "ended_at": self.ended_at,
            "detail": self.detail,
        }


@dataclass(frozen=True, slots=True)
class Request:
    """What the API decided to run, after validating and confirming it."""

    host_id: str
    operation: str
    label: str
    parameters: Mapping[str, object]
    plan: Mapping[str, object]
    confirmation: str
    #: Whether this run takes the Host's exclusive slot.
    mutating: bool
    #: Report keys this operation exists to reveal, exempt from redaction.
    allow_keys: tuple[str, ...]
    work: Callable[[ProgressSink], Evidence] = field(repr=False)


@dataclass(slots=True)
class Run:
    id: str
    host_id: str
    operation: str
    label: str
    parameters: Mapping[str, object]
    plan: Mapping[str, object]
    confirmation: str
    mutating: bool
    started_at: float
    status: RunStatus = RunStatus.RUNNING
    finished_at: float | None = None
    outcome: str | None = None
    evidence: Mapping[str, object] | None = None
    error: str | None = None
    phases: list[Phase] = field(default_factory=list)
    events: list[Event] = field(default_factory=list)

    def summary(self) -> dict[str, object]:
        """Everything but the report, for a list that stays small."""

        return {
            "id": self.id,
            "host_id": self.host_id,
            "operation": self.operation,
            "label": self.label,
            "parameters": dict(self.parameters),
            "confirmation": self.confirmation,
            "mutating": self.mutating,
            "status": str(self.status),
            "outcome": self.outcome,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "error": self.error,
            "phases": [phase.to_json() for phase in self.phases],
        }

    def to_json(self) -> dict[str, object]:
        return {
            **self.summary(),
            "plan": dict(self.plan),
            "evidence": None if self.evidence is None else dict(self.evidence),
        }


class RunStore:
    """Every run this console has started, and who is watching each one."""

    def __init__(self, *, retain: int = 200) -> None:
        self._lock = threading.Lock()
        self._runs: dict[str, Run] = {}
        self._order: list[str] = []
        self._busy: dict[str, str] = {}
        self._watchers: dict[str, list[queue.Queue[Event]]] = {}
        self._sequence = 0
        self._retain = retain

    # -- reading -------------------------------------------------------------

    def get(self, run_id: str) -> Run:
        with self._lock:
            return self._require(run_id)

    def listing(self, *, host_id: str | None = None, limit: int = 50) -> list[Run]:
        with self._lock:
            runs = [self._runs[key] for key in reversed(self._order)]
        if host_id is not None:
            runs = [run for run in runs if run.host_id == host_id]
        return runs[:limit]

    def active(self, host_id: str) -> str | None:
        with self._lock:
            return self._busy.get(host_id)

    # -- writing -------------------------------------------------------------

    def start(self, request: Request) -> Run:
        """Claim the Host if this run mutates it, then run it on its own thread."""

        run = Run(
            id=uuid.uuid4().hex,
            host_id=request.host_id,
            operation=request.operation,
            label=request.label,
            parameters=dict(request.parameters),
            plan=dict(request.plan),
            confirmation=request.confirmation,
            mutating=request.mutating,
            started_at=time.time(),
        )
        with self._lock:
            if request.mutating and request.host_id in self._busy:
                busy = self._runs[self._busy[request.host_id]]
                raise ConsoleError(
                    f"{request.host_id} is already running {busy.operation}; "
                    "one boundary action at a time",
                    status=409,
                )
            self._runs[run.id] = run
            self._order.append(run.id)
            if request.mutating:
                self._busy[request.host_id] = run.id
            self._prune()
            self._publish(run, EventKind.STARTED, {"run": run.summary()})
        thread = threading.Thread(
            target=self._execute,
            args=(run, request),
            name=f"eidolon-ops-console-{run.operation}",
            daemon=True,
        )
        thread.start()
        return run

    def stream(self, run_id: str) -> Iterator[Event | None]:
        """Replay what a run has already said, then follow it live.

        The backlog and the subscription are taken under one lock, so a browser
        that connects while a phase is being recorded sees that phase once.
        ``None`` is a keepalive: an idle release phase can take twenty minutes,
        which is longer than anything between here and the browser will hold a
        silent connection open.
        """

        with self._lock:
            run = self._require(run_id)
            backlog = list(run.events)
            channel: queue.Queue[Event] | None = None
            if run.status is RunStatus.RUNNING:
                channel = queue.Queue()
                self._watchers.setdefault(run_id, []).append(channel)
        yield from backlog
        if channel is None:
            return
        try:
            while True:
                try:
                    event = channel.get(timeout=_IDLE_SECONDS)
                except queue.Empty:
                    yield None
                    continue
                yield event
                if event.kind in _TERMINAL:
                    return
        finally:
            with self._lock:
                watchers = self._watchers.get(run_id, [])
                if channel in watchers:
                    watchers.remove(channel)

    # -- execution -----------------------------------------------------------

    def _execute(self, run: Run, request: Request) -> None:
        reporter = _Reporter(self, run, request.allow_keys)
        try:
            evidence = request.work(reporter)
        # A failed run is a result the operator has to be shown, not a crash
        # this thread gets to take the console down with.
        except Exception as exc:
            self._finish(
                run,
                status=RunStatus.FAILED,
                outcome=None,
                evidence=None,
                error=f"{type(exc).__name__}: {exc}",
            )
            return
        report = redacted(evidence.to_json(), allow=request.allow_keys)
        self._finish(
            run,
            status=RunStatus.COMPLETED,
            outcome=str(evidence.outcome),
            evidence=report if isinstance(report, dict) else {"report": report},
            error=None,
        )

    def _finish(
        self,
        run: Run,
        *,
        status: RunStatus,
        outcome: str | None,
        evidence: Mapping[str, object] | None,
        error: str | None,
    ) -> None:
        with self._lock:
            run.status = status
            run.outcome = outcome
            run.evidence = evidence
            run.error = error
            run.finished_at = time.time()
            for phase in run.phases:
                if phase.status is PhaseStatus.RUNNING:
                    # Whatever it was doing is what failed, or is what the
                    # operation finished without reporting evidence for.
                    phase.status = (
                        PhaseStatus.FAILED if status is RunStatus.FAILED else PhaseStatus.DONE
                    )
                    phase.ended_at = run.finished_at
            if run.host_id in self._busy and self._busy[run.host_id] == run.id:
                del self._busy[run.host_id]
            self._publish(
                run,
                EventKind.FINISHED,
                {
                    "status": str(status),
                    "outcome": outcome,
                    "error": error,
                    "evidence": None if evidence is None else dict(evidence),
                    "phases": [phase.to_json() for phase in run.phases],
                },
            )

    # -- progress ------------------------------------------------------------

    def _phase_began(self, run: Run, phase: str) -> None:
        with self._lock:
            now = time.time()
            for existing in run.phases:
                if existing.status is PhaseStatus.RUNNING:
                    # Phases are sequential, so a phase starting is also the
                    # previous one's completion — it just produced no evidence
                    # of its own to record.
                    existing.status = PhaseStatus.DONE
                    existing.ended_at = now
            run.phases.append(Phase(name=phase, status=PhaseStatus.RUNNING, began_at=now))
            self._publish(run, EventKind.PHASE_BEGAN, self._progress(run, phase))

    def _phase_recorded(
        self, run: Run, entry: Mapping[str, object], allow: tuple[str, ...]
    ) -> None:
        name = entry.get("phase")
        if not isinstance(name, str):
            return
        evidence = {key: value for key, value in entry.items() if key != "phase"}
        detail = redacted(evidence, allow=allow)
        with self._lock:
            now = time.time()
            record = next(
                (
                    item
                    for item in reversed(run.phases)
                    if item.name == name and item.status is PhaseStatus.RUNNING
                ),
                None,
            )
            if record is None:
                record = Phase(name=name, status=PhaseStatus.RUNNING, began_at=now)
                run.phases.append(record)
            record.status = PhaseStatus.DONE
            record.ended_at = now
            record.detail = detail
            self._publish(run, EventKind.PHASE_RECORDED, self._progress(run, name))

    # -- internals -----------------------------------------------------------

    @staticmethod
    def _progress(run: Run, phase: str) -> dict[str, object]:
        """The phase that just moved, and the whole list it moved within.

        The list travels with every event so that a browser holds no state
        machine of its own: replaying the server's phase model in TypeScript
        would be two implementations of one thing, and the one that drifts is
        always the copy.
        """

        return {"phase": phase, "phases": [item.to_json() for item in run.phases]}

    def _require(self, run_id: str) -> Run:
        try:
            return self._runs[run_id]
        except KeyError:
            raise ConsoleError(f"no such run: {run_id}", status=404) from None

    def _publish(self, run: Run, kind: EventKind, payload: Mapping[str, object]) -> None:
        """Record an event and hand it to everyone watching. Caller holds the lock."""

        self._sequence += 1
        event = Event(seq=self._sequence, at=time.time(), kind=kind, payload=payload)
        run.events.append(event)
        for channel in self._watchers.get(run.id, ()):
            channel.put(event)

    def _prune(self) -> None:
        """Forget the oldest finished runs. Caller holds the lock."""

        while len(self._order) > self._retain:
            for index, run_id in enumerate(self._order):
                if self._runs[run_id].status is not RunStatus.RUNNING:
                    del self._order[index]
                    del self._runs[run_id]
                    self._watchers.pop(run_id, None)
                    break
            else:
                return


class _Reporter:
    """A ``ProgressSink`` bound to one run.

    The other half of ``progress.Journal``: an operation announces its phases to
    whatever it was given, and what the console gives it is this.
    """

    __slots__ = ("_allow", "_run", "_store")

    def __init__(self, store: RunStore, run: Run, allow: tuple[str, ...] = ()) -> None:
        self._store = store
        self._run = run
        self._allow = allow

    def phase_began(self, phase: str) -> None:
        self._store._phase_began(self._run, phase)

    def phase_recorded(self, entry: Mapping[str, object]) -> None:
        self._store._phase_recorded(self._run, entry, self._allow)
