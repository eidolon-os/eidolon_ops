#!/usr/bin/env python3
"""Drive the ordered device-management HIL sequence and record its evidence.

``device_management_gate`` already declares the sequence and verifies the
evidence. What was missing was the thing that *produces* it: every run of these
twenty-four steps was done by hand — adb taps read off a screen dump, serial
logs grepped in a terminal, provider rows read out of sqlite — so nothing was
repeatable and none of it reached the gate.

Three design commitments, each of them a defect this session actually hit:

**A step observes the surface its own client uses.** Device facts come from the
device's serial log, Owner-facing facts from the phone's own screen, Host facts
from the operator's readiness surface. Reading the Host's databases instead
would let the harness pass while the surface a real client uses is broken —
which is exactly how a Host reported ``lan_name_resolves`` green for hours
while no device could resolve that name.

**No step passes by absence.** An observer that cannot see its fact fails. A
step that has no observer yet fails too, by name, rather than being skipped:
a baseline that overstates its own coverage is worse than a small honest one.

**What happened is recorded even when it fails.** Every observation writes its
evidence file before the step's verdict is decided, so a failed run says what
was seen rather than only that something went wrong.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from eidolon_ops.device_management_gate import (
    _CONFIRMATION_STEPS,
    HIL_CONTRACT,
    HIL_STEPS,
)

#: How long any single observation may wait for its fact to appear.
#:
#: One ceiling rather than one per step: a step that needs longer is telling us
#: something about the system, not about the harness, and a per-step number is
#: where "it usually passes" gets encoded as a tuning constant.
_OBSERVE_TIMEOUT_SECONDS = 180.0
_POLL_SECONDS = 2.0


class BaselineError(RuntimeError):
    """A step could not be observed, or the harness was asked for the impossible."""


@dataclass(frozen=True, slots=True)
class Surfaces:
    """The three clients this harness observes through, and nothing else."""

    #: Serial port the device under test logs to.
    device_serial: Path
    #: adb serial of the phone acting as Controller.
    android_serial: str
    #: Host profile the operator's own tooling reads.
    host_config: Path
    #: Where evidence files are written.
    evidence_root: Path
    adb: str = "adb"
    ops: str = "eidolon-ops"


@dataclass
class Observation:
    """What a step saw. Written before the verdict, always."""

    step_id: str
    surface: str
    detail: dict[str, object] = field(default_factory=dict)
    passed: bool = False
    reason: str = ""

    def document(self) -> dict[str, object]:
        return {
            "step_id": self.step_id,
            "surface": self.surface,
            "passed": self.passed,
            "reason": self.reason,
            "detail": self.detail,
        }


Observer = Callable[[Surfaces], Observation]


def _run(command: Sequence[str], *, timeout: float = 60.0) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            list(command), check=False, capture_output=True, text=True, timeout=timeout
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise BaselineError(f"could not run {command[0]}: {exc}") from exc


def _until(predicate: Callable[[], Observation | None]) -> Observation:
    """Poll one predicate to its deadline, and fail loudly rather than silently."""

    deadline = time.monotonic() + _OBSERVE_TIMEOUT_SECONDS
    last: Observation | None = None
    while True:
        seen = predicate()
        if seen is not None and seen.passed:
            return seen
        last = seen or last
        if time.monotonic() >= deadline:
            if last is None:
                raise BaselineError("observation produced nothing at all")
            last.passed = False
            if not last.reason:
                last.reason = f"not observed within {_OBSERVE_TIMEOUT_SECONDS:.0f}s"
            return last
        time.sleep(_POLL_SECONDS)


# --------------------------------------------------------------------------
# Device surface: what the board itself says, read from its serial log.
# --------------------------------------------------------------------------


def device_log(surfaces: Surfaces, *, seconds: float) -> str:
    """A bounded window of the device's own log.

    One reader at a time, by construction: concurrent readers on a macOS
    ``/dev/cu.*`` steal the port from each other, which this session mistook
    for an unreachable device more than once.
    """

    port = surfaces.device_serial
    if not port.exists():
        raise BaselineError(f"device serial port is absent: {port}")
    _run(["stty", "-f", str(port), "115200", "raw", "-echo"], timeout=10)
    captured = surfaces.evidence_root / "device-serial.log"
    captured.parent.mkdir(parents=True, exist_ok=True)
    with captured.open("ab") as sink:
        reader = subprocess.Popen(["cat", str(port)], stdout=sink, stderr=subprocess.DEVNULL)
        try:
            time.sleep(seconds)
        finally:
            reader.terminate()
            reader.wait(timeout=10)
    return captured.read_text(encoding="utf-8", errors="replace")


#: What a device that has nothing to say emits anyway.
#:
#: A window containing only these means the device is idle, which is a
#: different answer from "the thing I was looking for did not happen" — and the
#: reason matters: several of these steps are transient, so they can only be
#: seen while the sequence is actually running, never by asking an idle board
#: afterwards. The firmware has no way to state its current binding on request;
#: until it does, these observations have to be made in the moment.
_HEARTBEAT_ONLY = re.compile(r"^(?:I \(\d+\) SystemInfo:.*|\s*)$")


def _device_says(step_id: str, pattern: str, *, window: float = 20.0) -> Observer:
    expression = re.compile(pattern)

    def observe(surfaces: Surfaces) -> Observation:
        def once() -> Observation:
            text = device_log(surfaces, seconds=window)
            match = expression.search(text)
            lines = text.splitlines()
            idle = bool(lines) and all(_HEARTBEAT_ONLY.match(line) for line in lines)
            if match is not None:
                reason = ""
            elif not lines:
                reason = "the device said nothing at all on this port"
            elif idle:
                reason = (
                    "the device logged nothing but heartbeats: it is idle, and this "
                    "step can only be observed while the sequence is running"
                )
            else:
                reason = f"device log never matched {pattern!r}"
            return Observation(
                step_id=step_id,
                surface="device",
                passed=match is not None,
                reason=reason,
                detail={
                    "pattern": pattern,
                    "matched": match.group(0) if match else None,
                    "lines_seen": len(lines),
                    "idle": idle,
                },
            )

        return _until(once)

    return observe


# --------------------------------------------------------------------------
# Owner surface: what the phone shows its Owner, read from the phone.
# --------------------------------------------------------------------------


def phone_screen(surfaces: Surfaces) -> str:
    """Every label the phone is currently showing, as one string.

    Flutter renders into a single view, so the readable text is in
    ``content-desc`` rather than ``text`` — reading ``text`` returns nothing at
    all, which looks exactly like a blank screen.
    """

    dumped = _run(
        [
            surfaces.adb,
            "-s",
            surfaces.android_serial,
            "shell",
            "uiautomator",
            "dump",
            "/sdcard/ui.xml",
        ]
    )
    if dumped.returncode != 0:
        raise BaselineError(f"could not dump the phone's screen: {dumped.stderr.strip()}")
    read = _run([surfaces.adb, "-s", surfaces.android_serial, "shell", "cat", "/sdcard/ui.xml"])
    if read.returncode != 0 or not read.stdout:
        raise BaselineError("the phone's screen dump could not be read back")
    labels = re.findall(r'content-desc="([^"]*)"', read.stdout)
    return "\n".join(label for label in labels if label.strip())


def _phone_shows(step_id: str, *needles: str) -> Observer:
    def observe(surfaces: Surfaces) -> Observation:
        def once() -> Observation:
            screen = phone_screen(surfaces)
            missing = [needle for needle in needles if needle not in screen]
            return Observation(
                step_id=step_id,
                surface="owner",
                passed=not missing,
                reason="" if not missing else f"the phone never showed: {missing}",
                detail={"expected": list(needles), "missing": missing, "screen": screen},
            )

        return _until(once)

    return observe


# --------------------------------------------------------------------------
# Operator surface: what the Host attests about itself.
# --------------------------------------------------------------------------


def host_readiness(surfaces: Surfaces) -> dict[str, object]:
    result = _run([surfaces.ops, "--config", str(surfaces.host_config), "app-ready"], timeout=600)
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise BaselineError(f"the Host's readiness surface did not answer JSON: {exc}") from exc


def _host_is_ready(step_id: str) -> Observer:
    def observe(surfaces: Surfaces) -> Observation:
        def once() -> Observation:
            report = host_readiness(surfaces)
            checks = report.get("checks")
            failing = (
                sorted(name for name, value in checks.items() if value is not True)
                if isinstance(checks, dict)
                else ["the Host reported no checks at all"]
            )
            return Observation(
                step_id=step_id,
                surface="operator",
                passed=report.get("status") == "app_ready" and not failing,
                reason="" if not failing else f"Host readiness facts not met: {failing}",
                detail={"status": report.get("status"), "failing": failing},
            )

        return _until(once)

    return observe


# --------------------------------------------------------------------------
# The sequence. Every step names its observer, or names why it has none.
# --------------------------------------------------------------------------


def _unautomated(step_id: str, what_is_missing: str) -> Observer:
    """A step nobody has taught this harness to observe yet.

    It fails. Marking it passed, or skipping it, would make the gate report a
    coverage it does not have — and the whole reason this harness exists is
    that a green report which skipped the interesting part is what let every
    cross-boundary defect through.
    """

    def observe(_surfaces: Surfaces) -> Observation:
        return Observation(
            step_id=step_id,
            surface="none",
            passed=False,
            reason=f"no observer yet: {what_is_missing}",
        )

    # Marked rather than inferred from the function's name: coverage is a fact
    # this module states about itself, and inferring it from a name is how the
    # statement and the truth come apart.
    observe.observes_nothing = True  # type: ignore[attr-defined]
    return observe


OBSERVERS: dict[str, Observer] = {
    "release_identity_proven": _host_is_ready("release_identity_proven"),
    # What the firmware actually says. The first guess at this pattern named
    # things the current CommissioningRuntime does not log, so the step would
    # have failed on a device that was commissioning correctly — a harness that
    # is wrong about the words is a harness that reports the wrong fact.
    "commissioning_started": _device_says(
        "commissioning_started",
        r"CommissioningRuntime: Confirmed state=advertising|Provisioning session open as",
    ),
    # Read off the admission screen. The needles are the app's own sentences,
    # copied from its source rather than invented here — an assertion about
    # what a person sees can only be written in the words they see, so if the
    # app rewords them this fails and points at the harness, which is the right
    # place for that argument to happen.
    "proposal_observed": _phone_shows("proposal_observed", "明确批准这次 Enrollment"),
    "approval_recorded": _phone_shows("approval_recorded", "已批准，等待设备领取 Grant"),
    "grant_ack_observed": _device_says("grant_ack_observed", r"claim-grants:collect|ClaimGrant"),
    "claim_active_observed": _phone_shows("claim_active_observed", "已接入"),
    "mount_active_observed": _phone_shows("mount_active_observed", "挂载 revision"),
    # A durable fact the device writes down, not only the transient one. The
    # device stores its Hub config with the room it was given; while it had no
    # binding that field was empty (`status=waiting-binding room= server=`),
    # which is exactly how the two-hour channel loss showed up.
    "channel_ready_observed": _device_says(
        "channel_ready_observed", r"room=\S|session_state=InRoom"
    ),
    "confirm_online_remove": _unautomated(
        "confirm_online_remove", "the destructive confirmation is deliberately a human step"
    ),
    "online_remove_requested": _unautomated(
        "online_remove_requested", "removing from the phone is not driven yet"
    ),
    "platform_revoked_observed": _phone_shows("platform_revoked_observed", "已失去访问"),
    "unmount_observed": _unautomated(
        "unmount_observed",
        "the phone reports mount_removed as a condition, but no screen states it "
        "in words this can read",
    ),
    "online_erase_ack_observed": _unautomated(
        "online_erase_ack_observed", "the device has no real secure-erase adapter yet"
    ),
    "device_rejoined": _device_says("device_rejoined", r"lifecycle HUB -> REGISTER"),
    "device_taken_offline": _unautomated(
        "device_taken_offline", "taking the board off the network is a human step"
    ),
    "confirm_offline_remove": _unautomated(
        "confirm_offline_remove", "the destructive confirmation is deliberately a human step"
    ),
    "offline_remove_requested": _unautomated(
        "offline_remove_requested", "removing from the phone is not driven yet"
    ),
    "offline_platform_revoked_observed": _phone_shows(
        "offline_platform_revoked_observed", "已失去访问"
    ),
    "offline_unmount_observed": _unautomated(
        "offline_unmount_observed", "Kernel's unmount is not observed yet"
    ),
    # The fact the dead-device fix turns on: removal is complete while the
    # local erase is still unconfirmed, and the phone says both.
    "erase_pending_observed": _phone_shows("erase_pending_observed", "尚未确认擦除"),
    "device_reconnected": _device_says("device_reconnected", r"lifecycle HUB -> REGISTER"),
    "offline_erase_ack_observed": _unautomated(
        "offline_erase_ack_observed", "the device has no real secure-erase adapter yet"
    ),
    "old_request_rejected": _unautomated(
        "old_request_rejected", "replaying a spent request is not driven yet"
    ),
    "old_generation_rejected": _unautomated(
        "old_generation_rejected", "replaying a stale generation is not driven yet"
    ),
}


def coverage() -> dict[str, list[str]]:
    """Which steps this harness can observe, stated rather than implied."""

    observed: list[str] = []
    unobserved: list[str] = []
    for step in HIL_STEPS:
        target = unobserved if getattr(OBSERVERS[step], "observes_nothing", False) else observed
        target.append(step)
    return {"observed": observed, "unobserved": unobserved}


def run(
    surfaces: Surfaces,
    release_document: Mapping[str, object],
    device_id: str,
    *,
    steps: Sequence[str] | None = None,
) -> dict[str, object]:
    """Run the ordered sequence and produce gate-shaped evidence."""

    if set(OBSERVERS) != set(HIL_STEPS):
        raise BaselineError(
            "the harness and the gate disagree about the sequence; "
            f"harness only: {sorted(set(OBSERVERS) - set(HIL_STEPS))}, "
            f"gate only: {sorted(set(HIL_STEPS) - set(OBSERVERS))}"
        )
    identity = release_document.get("release_identity")
    if not isinstance(identity, str) or not identity:
        raise BaselineError("release manifest has no release_identity to bind evidence to")
    requested = tuple(steps) if steps is not None else HIL_STEPS
    unknown = [step for step in requested if step not in OBSERVERS]
    if unknown:
        raise BaselineError(f"no such steps in the sequence: {unknown}")

    surfaces.evidence_root.mkdir(parents=True, exist_ok=True)
    recorded: list[dict[str, object]] = []
    # Every step is recorded, in order, whatever happened. A document that
    # simply stops is rejected by the gate for the wrong reason — "steps are
    # missing" instead of "this step has no passing evidence" — and the wrong
    # reason is what sends an operator looking in the wrong place.
    stopped = False
    for step in HIL_STEPS:
        if stopped or step not in requested:
            recorded.append(
                {
                    "id": step,
                    "status": "pending",
                    "evidence_path": None,
                    "evidence_digest": None,
                    **_confirmation(step, device_id, identity),
                }
            )
            continue
        try:
            observation = OBSERVERS[step](surfaces)
        except BaselineError as exc:
            observation = Observation(step_id=step, surface="none", passed=False, reason=str(exc))
        path = surfaces.evidence_root / f"{step}.json"
        payload = json.dumps(observation.document(), indent=2, sort_keys=True) + "\n"
        path.write_text(payload, encoding="utf-8")
        recorded.append(
            {
                "id": step,
                "status": "passed" if observation.passed else "failed",
                "evidence_path": str(path),
                "evidence_digest": hashlib.sha256(payload.encode("utf-8")).hexdigest(),
                **_confirmation(step, device_id, identity),
            }
        )
        if not observation.passed:
            # Forward only as far as the truth goes: continuing past a step that
            # did not happen would attribute later observations to a sequence
            # that never reached them.
            stopped = True

    return {
        # The gate owns this string; a second copy here is the very drift
        # this module keeps finding elsewhere.
        "contract": HIL_CONTRACT,
        "release_identity": identity,
        "device_id": device_id,
        "steps": recorded,
    }


def _confirmation(step: str, device_id: str, identity: str) -> dict[str, object]:
    if step not in _CONFIRMATION_STEPS:
        return {}
    return {"confirmation": f"ERASE {device_id} FOR {identity}"}


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run the ordered device-management HIL sequence and record its evidence"
    )
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("coverage", help="report which steps this harness can observe")
    run_command = commands.add_parser("run", help="observe the sequence and write evidence")
    run_command.add_argument("manifest", type=Path, help="verified release manifest")
    run_command.add_argument("--device-id", required=True)
    run_command.add_argument("--device-serial", type=Path, required=True)
    run_command.add_argument("--android-serial", required=True)
    run_command.add_argument("--host-config", type=Path, required=True)
    run_command.add_argument("--evidence-root", type=Path, required=True)
    run_command.add_argument("--output", type=Path, required=True)
    run_command.add_argument(
        "--only",
        default=None,
        help="comma-separated subset of steps to observe; the rest stay pending",
    )
    run_command.add_argument("--adb", default="adb")
    run_command.add_argument("--ops", default="eidolon-ops")
    return parser


def main(arguments: Sequence[str] | None = None) -> int:
    parsed = _parser().parse_args(arguments)
    try:
        if parsed.command == "coverage":
            print(json.dumps(coverage(), indent=2, sort_keys=True))
            return 0
        document = run(
            Surfaces(
                device_serial=parsed.device_serial,
                android_serial=parsed.android_serial,
                host_config=parsed.host_config,
                evidence_root=parsed.evidence_root,
                adb=parsed.adb,
                ops=parsed.ops,
            ),
            json.loads(parsed.manifest.read_text(encoding="utf-8")),
            parsed.device_id,
            steps=(parsed.only.split(",") if parsed.only else None),
        )
    except BaselineError as exc:
        print(json.dumps({"status": "blocked", "error": str(exc)}, sort_keys=True))
        return 2
    parsed.output.parent.mkdir(parents=True, exist_ok=True)
    parsed.output.write_text(
        json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    failed = [step["id"] for step in document["steps"] if step.get("status") == "failed"]
    print(
        json.dumps(
            {
                "status": "observed" if not failed else "stopped",
                "first_failure": failed[0] if failed else None,
                "output": str(parsed.output),
            },
            sort_keys=True,
        )
    )
    return 0 if not failed else 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
