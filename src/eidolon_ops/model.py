"""What an operation intends, and what it turned out to be.

Two types and four enumerations, deliberately not a domain layer. ``Plan`` is
what an operator is asked to approve; ``Evidence`` is what came back. Between
them they replace two things that used to be spelled as free strings: the
verdict of an operation, and the question of whether a Host can perform it at
all.

The verdict matters most. It used to live in the CLI as a set of two dozen
success words, which meant every new status string was silently a failure until
someone remembered to add it — ``commissioning-code`` and ``backup`` both
succeeded on the Host and exited non-zero here. An operation now says what it
did in a closed enumeration, and the exit code is a property of that value.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum


class Outcome(StrEnum):
    """The closed set of verdicts an operation can reach."""

    #: Read-only, and what was read is what it should be.
    OBSERVED = "observed"
    #: A plan was produced and nothing was changed.
    PLANNED = "planned"
    #: The plan was applied.
    APPLIED = "applied"
    #: Read-only, and the fact the operation asserts is not true.
    DEGRADED = "degraded"
    #: Fail closed: a precondition, a capability or a contract was missing.
    REFUSED = "refused"
    #: Execution reached the Host and did not complete.
    FAILED = "failed"

    @property
    def successful(self) -> bool:
        return self in {Outcome.OBSERVED, Outcome.PLANNED, Outcome.APPLIED}


class DestructiveLevel(StrEnum):
    """How far an operation can be walked back."""

    NONE = "none"
    REVERSIBLE = "reversible"
    IRREVERSIBLE = "irreversible"


class ActionKind(StrEnum):
    """Which authority an operation touches.

    Configuration, secrets, schema, data and lifecycle are five independent
    acts. Naming them per operation is what lets a confirmation gradient — and
    a reviewer — treat "restart the product" and "wipe its authorities"
    differently without reading the implementation.
    """

    CONFIG = "config"
    SECRET = "secret"
    SCHEMA = "schema"
    DATA = "data"
    LIFECYCLE = "lifecycle"
    CODE = "code"


class Capability(StrEnum):
    """An operation a composed Host adapter can actually perform.

    A Host answers "can you do this" from its composition — transport,
    supervisor, package manager — rather than from a chain of driver
    comparisons scattered through the controller.
    """

    STATUS = "status"
    DOCTOR = "doctor"
    APP_READY = "app-ready"
    LIFECYCLE = "lifecycle"
    LOGS = "logs"
    #: Only a journal keeps history far enough back to select by time.
    LOG_HISTORY = "log-history"
    COMMISSIONING_CODE = "commissioning-code"
    PROVISION = "provision"
    INIT_INPUTS = "init-inputs"
    INSTALL = "install"
    DEPLOY = "deploy"
    ROLLBACK = "rollback"
    BACKUP = "backup"
    RESTORE = "restore"
    RESET = "reset"
    CONTROLLER_RESET = "controller-reset"
    AUTHORITY_RESET = "authority-reset"
    DIAGNOSE = "diagnose"
    #: Workstation-only inspection of the source-run profile.
    SOURCE_PROFILE = "debug"


@dataclass(frozen=True, slots=True)
class Step:
    """One declared unit of an operation, named before it runs."""

    id: str
    description: str

    def to_json(self) -> dict[str, object]:
        return {"id": self.id, "description": self.description}


@dataclass(frozen=True, slots=True)
class Plan:
    """What the operator is being asked to approve."""

    operation: str
    host_id: str
    steps: tuple[Step, ...]
    destructive: DestructiveLevel = DestructiveLevel.NONE
    requires_flags: frozenset[str] = field(default_factory=frozenset)
    touches: frozenset[ActionKind] = field(default_factory=frozenset)

    def to_json(self) -> dict[str, object]:
        return {
            "operation": self.operation,
            "host_id": self.host_id,
            "steps": [step.to_json() for step in self.steps],
            "destructive": str(self.destructive),
            "requires_flags": sorted(self.requires_flags),
            "touches": sorted(str(kind) for kind in self.touches),
        }


@dataclass(frozen=True, slots=True)
class StepResult:
    """What one declared step turned out to be."""

    step: Step
    outcome: Outcome
    detail: Mapping[str, object] | None = None

    def to_json(self) -> dict[str, object]:
        document: dict[str, object] = {
            "id": self.step.id,
            "outcome": str(self.outcome),
        }
        if self.detail is not None:
            document["detail"] = dict(self.detail)
        return document


@dataclass(frozen=True, slots=True)
class Evidence:
    """A plan, its verdict, and the report the Host produced under it."""

    plan: Plan
    outcome: Outcome
    steps: tuple[StepResult, ...] = ()
    report: Mapping[str, object] = field(default_factory=dict)

    def to_json(self) -> dict[str, object]:
        """Render the report the operator reads.

        The Host-produced report stays at the top level exactly as the Host
        wrote it — the operations that consume this output already read those
        keys. ``plan`` and ``outcome`` are added beside it, and ``outcome`` is
        what the exit code is derived from.
        """

        return {
            **dict(self.report),
            "plan": self.plan.to_json(),
            "outcome": str(self.outcome),
            "steps": [step.to_json() for step in self.steps],
        }


def steps_from_phases(plan: Plan, report: Mapping[str, object]) -> tuple[StepResult, ...]:
    """Match a report's executed phases back onto the plan's declared steps.

    Operations that run on the Host report their phases; the plan named the
    same work before it ran. Pairing them is what makes the plan reviewable
    afterwards rather than only beforehand — an unreported step is visible as
    one that never produced a result.
    """

    phases = report.get("phases")
    reported: set[str] = set()
    if isinstance(phases, Sequence) and not isinstance(phases, str | bytes):
        for item in phases:
            if isinstance(item, Mapping) and isinstance(item.get("phase"), str):
                reported.add(str(item["phase"]))
    return tuple(
        StepResult(step=step, outcome=Outcome.APPLIED)
        for step in plan.steps
        if step.id in reported
    )
