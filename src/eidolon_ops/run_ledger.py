"""What actually stops a run, recorded so the answer stops being anecdotal.

Every gate in this repository is individually justified: its docstring names the
incident it came from, and nearly all of them earned their place. The problem is
the total. The workstation side of a deploy holds around 150 refusal points and
the Host agent around 260, spread over 29 phases, and a run that stops tells you
which one fired *this time* and nothing about which ones fire *often*.

So the same conversation happens after every stuck deploy: someone reasons from
the two or three failures they personally remember and proposes a change. That
is how a gate protecting a real invariant and a gate reporting a stale derived
copy came to look equally important — both are a ``raise``, and neither is
counted.

This records one line per run: the operation, the outcome, how long it took,
which phase was open if it failed, and a fingerprint of the refusal that groups
the same gate across runs. Two weeks of it turns "deploy keeps getting stuck"
into a ranked list, which is the difference between simplifying the mechanism
and simplifying the part of it that was in the way.

It records nothing a report does not already print. Ops' own discipline is that
evidence names keys and never values, so the refusal text is the refusal text;
the file is still written mode-0600 beside the profile it belongs to, because a
ledger of one operator's runs is that operator's.
"""

from __future__ import annotations

import json
import os
import re
import time
from collections.abc import Mapping
from pathlib import Path

__all__ = ["RunLedger", "fingerprint"]

#: Where a profile's ledger lives, unless the operator says otherwise. Beside
#: the profile, because that is the path they named: a rule that needs one
#: sentence beats one that climbs three directories to find a shared root.
_LEDGER_DIRECTORY = "runs"
_LEDGER_ENV = "EIDOLON_OPS_RUN_LEDGER"

#: How much of a refusal is kept in the grouping key. Long enough that two
#: different gates do not collide, short enough that one gate whose message
#: grew a clause is still the same gate.
_FINGERPRINT_LENGTH = 96

_SUBSTITUTIONS = (
    # Order matters: paths before the digits inside them. Anchored at a real
    # absolute path, so `macos-dev/supervisord/none` -- a Host's composition,
    # which is exactly the kind of thing worth grouping by -- survives instead
    # of being read as a filename.
    (re.compile(r"(?<![\w.-])/[^\s,;:'\"()]{2,}"), "<path>"),
    (re.compile(r"\b[0-9a-f]{40}\b"), "<commit>"),
    (re.compile(r"\b[0-9a-f]{7,}\b"), "<hex>"),
    (re.compile(r"\d+"), "<n>"),
    (re.compile(r"\s+"), " "),
)


def fingerprint(message: str) -> str:
    """A grouping key for one refusal, stable across runs.

    The gates have no identity of their own — a refusal is only its message —
    so the message becomes the identity with the parts that vary per run taken
    out: paths, commits, counts. Two runs stopped by the same gate land on the
    same key even when they name different repositories.
    """

    value = message.strip().lower()
    for pattern, replacement in _SUBSTITUTIONS:
        value = pattern.sub(replacement, value)
    return value[:_FINGERPRINT_LENGTH].strip()


class RunLedger:
    """One line per run, and the progress sink that times its phases.

    A :class:`~eidolon_ops.progress.ProgressSink` by shape, so phase timings
    come from the seam that already exists for them rather than from a second
    mechanism: ``Journal`` announces each phase as it begins and again when it
    records evidence, and it already tracks which phase was left open when an
    operation raised. That open phase is the single most useful field here —
    "where in the 29 phases did it die" — and nothing had to be added to get it.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self.started = time.time()
        self.phases: list[dict[str, object]] = []
        self._open: tuple[str, float] | None = None

    @classmethod
    def for_profile(cls, profile: Path) -> RunLedger:
        override = os.environ.get(_LEDGER_ENV, "").strip()
        if override:
            return cls(Path(override).expanduser())
        return cls(profile.parent / _LEDGER_DIRECTORY / f"{profile.stem}.jsonl")

    # -- progress sink -------------------------------------------------------

    def phase_began(self, phase: str) -> None:
        self._close()
        self._open = (phase, time.time())

    def phase_recorded(self, entry: Mapping[str, object]) -> None:
        name = entry.get("phase")
        if isinstance(name, str) and (self._open is None or self._open[0] != name):
            # Evidence for a phase that never announced its start. Real work,
            # so it is recorded; with no start there is no duration to claim.
            self._close()
            self.phases.append({"phase": name, "seconds": None})
            return
        self._close()

    def _close(self) -> None:
        if self._open is None:
            return
        name, began = self._open
        self._open = None
        self.phases.append({"phase": name, "seconds": round(time.time() - began, 1)})

    @property
    def open_phase(self) -> str | None:
        """The phase that had begun and not finished. What failed, if anything."""

        return self._open[0] if self._open else None

    # -- the line ------------------------------------------------------------

    def record(
        self,
        *,
        operation: str,
        outcome: str,
        error: str | None = None,
        report: Mapping[str, object] | None = None,
    ) -> None:
        """Append this run. Never raises: a ledger must not fail an operation."""

        entry: dict[str, object] = {
            "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "profile": self.path.stem,
            "operation": operation,
            "outcome": outcome,
            "seconds": round(time.time() - self.started, 1),
        }
        if self.open_phase is not None:
            entry["stopped_in"] = self.open_phase
        if self.phases:
            entry["phases"] = self.phases
        if error:
            entry["gate"] = fingerprint(error)
            entry["error"] = error
        if report is not None:
            # The two facts a slow or surprising run is usually explained by,
            # already computed by the operations that have them.
            for key in ("link", "source_advance"):
                value = report.get(key)
                if value:
                    entry[key] = value
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            with open(
                os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600),
                "a",
                encoding="utf-8",
            ) as stream:
                stream.write(json.dumps(entry, sort_keys=True) + "\n")
        except OSError:
            return
