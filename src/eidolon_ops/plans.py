"""What each operation intends, declared before it runs.

One table, so that "what does this command touch, and can it be walked back"
is answerable without reading the implementation — by an operator, by a
reviewer, and by the confirmation gradient a console will put in front of both.

The step ids are the phase names the Host reports back, which is what lets a
plan be checked against what actually ran rather than only approved beforehand.
"""

from __future__ import annotations

from eidolon_ops.model import ActionKind, DestructiveLevel, Plan, Step


def _steps(*items: tuple[str, str]) -> tuple[Step, ...]:
    return tuple(Step(id=name, description=description) for name, description in items)


def status(host_id: str) -> Plan:
    return Plan(
        operation="status",
        host_id=host_id,
        steps=_steps(("observe", "read unit, release and receipt state")),
    )


def app_ready(host_id: str) -> Plan:
    return Plan(
        operation="app-ready",
        host_id=host_id,
        steps=_steps(("probe", "attest every declared App readiness fact")),
    )


def doctor(host_id: str) -> Plan:
    return Plan(
        operation="doctor",
        host_id=host_id,
        steps=_steps(
            ("platform", "report the Host path contract and its capabilities"),
            ("foundation", "report the pinned non-Eidolon foundation"),
            ("host", "ask the Host for its own health"),
        ),
    )


def provision(host_id: str, *, apply: bool) -> Plan:
    return Plan(
        operation="provision",
        host_id=host_id,
        steps=_steps(
            ("python_probe", "detect a usable Python on the Host"),
            ("python_bootstrap", "bootstrap Python where it is missing"),
            ("doctor", "compare the Host against the pinned foundation"),
            ("install", "install the pinned packages and artifacts"),
        ),
        requires_flags=frozenset({"--apply"} if apply else set()),
        touches=frozenset({ActionKind.CONFIG}),
    )


def initialize_inputs(host_id: str, *, new_identity: bool = False) -> Plan:
    """Create the input set, or retire this machine's identity and create it again.

    The second is not a stronger version of the first. A machine keeps the
    identity it was given; retiring it changes the ``ehost-*`` name every phone
    pinned and cannot be walked back, so it is declared as what it is.
    """

    steps = _steps(
        *(
            (("retire", "retire this machine's Host identity"),)
            if new_identity
            else ()
        ),
        ("write", "create the private first-install input set, never overwriting"),
    )
    return Plan(
        operation="init-inputs",
        host_id=host_id,
        steps=steps,
        destructive=(
            DestructiveLevel.IRREVERSIBLE if new_identity else DestructiveLevel.NONE
        ),
        requires_flags=frozenset({"--new-identity"} if new_identity else set()),
        touches=frozenset({ActionKind.SECRET, ActionKind.CONFIG}),
    )


def commissioning_code(host_id: str) -> Plan:
    return Plan(
        operation="commissioning-code",
        host_id=host_id,
        steps=_steps(("issue", "mint a one-time Setup code with a bounded lifetime")),
        touches=frozenset({ActionKind.SECRET}),
    )


def install(
    host_id: str,
    *,
    apply: bool,
    reset_existing: bool,
    wipe_authority_data: bool,
) -> Plan:
    reset_steps = _steps(
        ("reset_existing", "stop and remove the existing Eidolon deployment"),
    )
    flags = {"--apply"} if apply else set()
    if reset_existing:
        flags |= {"--reset-existing", "--wipe-authority-data"}
    touches = {
        ActionKind.CODE,
        ActionKind.CONFIG,
        ActionKind.SECRET,
        ActionKind.SCHEMA,
        ActionKind.LIFECYCLE,
    }
    if wipe_authority_data:
        touches.add(ActionKind.DATA)
    return Plan(
        operation="install",
        host_id=host_id,
        steps=(
            *(reset_steps if reset_existing else ()),
            *_steps(
                ("bundle", "seal the commit-pinned release bundle"),
                ("upload_guard", "claim or resume the Host's upload directory"),
                ("upload_finalize", "prove the uploaded bundle digest"),
                ("prepare", "build the release natively on the Host"),
                ("install", "create identities, inputs, the Data baseline and the units"),
                ("secret_cleanup", "remove the private staging directory"),
            ),
        ),
        destructive=(
            DestructiveLevel.IRREVERSIBLE if wipe_authority_data else DestructiveLevel.NONE
        ),
        requires_flags=frozenset(flags),
        touches=frozenset(touches),
    )


def deploy(host_id: str, *, activate: bool) -> Plan:
    return Plan(
        operation="deploy",
        host_id=host_id,
        steps=_steps(
            ("bundle", "seal the commit-pinned release bundle"),
            ("upload_guard", "claim or resume the Host's upload directory"),
            ("upload_finalize", "prove the uploaded bundle digest"),
            ("prepare", "build the release natively on the Host"),
            ("dry_run", "have the Host's own activator rehearse the switch"),
            ("activate", "switch the release atomically"),
            ("host_application", "deliver the derived Host layer"),
            ("doctor", "require the release's own health gate"),
            ("app_ready", "require every declared App readiness fact"),
            ("health_gate_rollback", "restore the exact snapshot if a gate fails"),
        ),
        destructive=DestructiveLevel.REVERSIBLE,
        requires_flags=frozenset({"--activate"} if activate else set()),
        touches=frozenset({ActionKind.CODE, ActionKind.LIFECYCLE}),
    )


def rollback(host_id: str, *, apply: bool) -> Plan:
    return Plan(
        operation="rollback",
        host_id=host_id,
        steps=_steps(("restore", "restore one exact release snapshot")),
        destructive=DestructiveLevel.REVERSIBLE,
        requires_flags=frozenset({"--apply"} if apply else set()),
        touches=frozenset({ActionKind.CODE, ActionKind.LIFECYCLE}),
    )


def backup(host_id: str) -> Plan:
    return Plan(
        operation="backup",
        host_id=host_id,
        steps=_steps(
            ("snapshot", "snapshot every authority that declares how it is snapshotted"),
            ("fetch", "bring the snapshot to this workstation"),
        ),
        touches=frozenset({ActionKind.DATA}),
    )


def restore(host_id: str, *, apply: bool) -> Plan:
    return Plan(
        operation="restore",
        host_id=host_id,
        steps=_steps(
            ("upload", "stage the backup on the Host it came from"),
            ("replace", "stop the product and replace every authority"),
            ("start", "start the product again"),
        ),
        destructive=DestructiveLevel.IRREVERSIBLE,
        requires_flags=frozenset({"--apply"} if apply else set()),
        touches=frozenset({ActionKind.DATA, ActionKind.LIFECYCLE}),
    )


def reset(host_id: str, *, apply: bool, wipe_authority_data: bool) -> Plan:
    touches = {ActionKind.CODE, ActionKind.CONFIG, ActionKind.SECRET, ActionKind.LIFECYCLE}
    if wipe_authority_data:
        touches.add(ActionKind.DATA)
    flags = {"--apply"} if apply else set()
    if wipe_authority_data:
        flags.add("--wipe-authority-data")
    return Plan(
        operation="reset",
        host_id=host_id,
        steps=_steps(
            ("stop", "disable and stop every product unit"),
            ("remove", "remove the fixed Eidolon deployment namespace"),
        ),
        destructive=DestructiveLevel.IRREVERSIBLE,
        requires_flags=frozenset(flags),
        touches=frozenset(touches),
    )


def controller_reset(host_id: str, *, apply: bool) -> Plan:
    return Plan(
        operation="controller-reset",
        host_id=host_id,
        steps=_steps(("revoke", "revoke every Controller Grant on this Host")),
        destructive=DestructiveLevel.IRREVERSIBLE,
        requires_flags=frozenset({"--apply"} if apply else set()),
        touches=frozenset({ActionKind.SECRET}),
    )


def diagnose(host_id: str) -> Plan:
    return Plan(
        operation="diagnose",
        host_id=host_id,
        steps=_steps(("collect", "collect a redacted diagnostic bundle")),
    )


def lifecycle(host_id: str, action: str, *, dry_run: bool) -> Plan:
    return Plan(
        operation=action,
        host_id=host_id,
        steps=_steps((action, f"{action} the product as one boundary action")),
        destructive=DestructiveLevel.REVERSIBLE,
        requires_flags=frozenset() if dry_run else frozenset({"--apply"}),
        touches=frozenset({ActionKind.LIFECYCLE}),
    )


def logs(host_id: str) -> Plan:
    return Plan(
        operation="logs",
        host_id=host_id,
        steps=_steps(("read", "read bounded log output")),
    )


def source_profile(host_id: str, operation: str) -> Plan:
    return Plan(
        operation=f"debug {operation}",
        host_id=host_id,
        steps=_steps((operation, f"run the source-run profile's {operation} operation")),
        touches=frozenset({ActionKind.CONFIG} if operation == "prepare" else set()),
    )
