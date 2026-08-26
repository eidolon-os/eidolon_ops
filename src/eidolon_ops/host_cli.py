"""Unified Eidolon host lifecycle CLI for macOS and Raspberry Pi."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable, Sequence
from pathlib import Path

from eidolon_ops.config import ConfigurationError
from eidolon_ops.environment import EnvironmentFileError
from eidolon_ops.errors import OperationsError
from eidolon_ops.host_controller import HostController
from eidolon_ops.hub_assets import HubAssetError
from eidolon_ops.model import Outcome
from eidolon_ops.paths import HostProfileError, load_host_profile
from eidolon_ops.process import ProcessError, SubprocessRunner
from eidolon_ops.readiness import ReadinessError
from eidolon_ops.run_ledger import RunLedger
from eidolon_ops.status_output import render_status, render_status_error
from eidolon_ops.transport import TransportError


def _lifecycle(controller: HostController, arguments: argparse.Namespace) -> object:
    return controller.lifecycle(
        arguments.operation,
        dry_run=arguments.dry_run,
        force_cleanup=getattr(arguments, "force_cleanup", False),
        strict=getattr(arguments, "strict", False),
        wait_ready=not getattr(arguments, "no_wait_ready", False),
    )


#: Every verb this CLI offers, and the controller call it is.
#:
#: A table rather than a chain of ``elif``, because the chain let a verb name a
#: method that did not exist: ``add-input-credentials`` was parsed, accepted, and
#: dispatched to ``controller.add_missing_input_credentials`` — which lives on
#: the release executor and was never ported to the facade. It raised
#: ``AttributeError`` on every Host, and it was the only supported way to give a
#: Host a credential the product had grown. Nothing caught it because nothing
#: compared the two lists.
#:
#: ``tests/test_cli_operations.py`` now does: every subcommand must appear here,
#: every entry here must be a subcommand, and every handler must resolve against
#: ``HostController``. A verb with no implementation is a failing test rather
#: than a stack trace in somebody's terminal.
OPERATIONS: dict[str, Callable[[HostController, argparse.Namespace], object]] = {
    "status": lambda controller, _: controller.status(),
    "app-ready": lambda controller, _: controller.app_ready(),
    "doctor": lambda controller, a: controller.doctor(release_id=a.release_id),
    "provision": lambda controller, a: controller.provision(apply=a.apply),
    "init-inputs": lambda controller, a: controller.initialize_inputs(
        new_identity=a.new_identity
    ),
    "converge-inputs": lambda controller, a: controller.converge_inputs(
        apply=a.apply
    ),
    "backup": lambda controller, a: controller.backup(output=a.output),
    "restore": lambda controller, a: controller.restore(
        source=a.source, apply=a.apply
    ),
    "commissioning-code": lambda controller, a: controller.commissioning_code(
        ttl_seconds=a.ttl_seconds
    ),
    "install": lambda controller, a: controller.install(
        release_id=a.release_id,
        resume=a.resume,
        apply=a.apply,
        reset_existing=a.reset_existing,
        wipe_authority_data=a.wipe_authority_data,
    ),
    "controller-reset": lambda controller, a: controller.controller_reset(
        apply=a.apply
    ),
    "authority-reset": lambda controller, a: controller.authority_reset(apply=a.apply),
    "authority-backup": lambda controller, a: controller.authority_backup(
        output=a.output
    ),
    "authority-restore": lambda controller, a: controller.authority_restore(
        source=a.source, apply=a.apply
    ),
    "reset": lambda controller, a: controller.reset(
        wipe_authority_data=a.wipe_authority_data, apply=a.apply
    ),
    "deploy": lambda controller, a: controller.deploy(
        release_id=a.release_id,
        resume=a.resume,
        activate=a.activate,
        cutover_mode=a.cutover_mode,
    ),
    "update": lambda controller, a: controller.deploy(
        release_id=a.release_id,
        resume=a.resume,
        activate=a.activate,
        cutover_mode=a.cutover_mode,
    ),
    "abandon": lambda controller, a: controller.abandon(release_id=a.release_id),
    "rollback": lambda controller, a: controller.rollback(
        release_id=a.release_id, snapshot=a.snapshot, apply=a.apply
    ),
    "diagnose": lambda controller, a: controller.diagnose(output=a.output),
    # One handler, named three times: the three verbs differ only in the word
    # they pass on, and three bodies would be three places to fix a flag.
    "start": _lifecycle,
    "stop": _lifecycle,
    "restart": _lifecycle,
    "debug": lambda controller, a: controller.local_profile(
        "product-source", a.profile_operation
    ),
    "logs": lambda controller, a: controller.logs(
        service=a.service, lines=a.lines, since=a.since
    ),
}


def _dispatch(controller: HostController, arguments: argparse.Namespace) -> object:
    try:
        handler = OPERATIONS[arguments.operation]
    except KeyError as exc:  # pragma: no cover - argparse rejects these first
        raise OperationsError(f"unknown operation: {arguments.operation}") from exc
    return handler(controller, arguments)


def main(argv: Sequence[str] | None = None) -> int:
    parser = _parser()
    arguments = parser.parse_args(argv)
    human_status = arguments.operation == "status" and arguments.human
    # Counts what stops a run. Every gate here is individually justified and
    # nothing counts them, so which ones fire often has only ever been argued
    # from whichever two failures the arguer remembered. It is the progress sink
    # as well, because phase timings already arrive through that seam.
    ledger = RunLedger.for_profile(Path(arguments.config))
    try:
        controller = HostController(
            load_host_profile(arguments.config),
            SubprocessRunner(),
            revision_overrides=tuple(arguments.revision),
            allow_dirty=arguments.allow_dirty,
            progress=ledger,
        )
        result = _dispatch(controller, arguments)
    except (
        ConfigurationError,
        EnvironmentFileError,
        HostProfileError,
        HubAssetError,
        OperationsError,
        ProcessError,
        ReadinessError,
        TransportError,
        OSError,
    ) as exc:
        if human_status:
            print(render_status_error(str(exc), profile=arguments.config), file=sys.stderr)
        else:
            _print(
                {"status": "failed", "outcome": str(Outcome.FAILED), "error": str(exc)},
                stream=sys.stderr,
            )
        ledger.record(
            operation=arguments.operation, outcome=str(Outcome.FAILED), error=str(exc)
        )
        return 1
    if human_status:
        print(render_status(result.to_json()))
    else:
        _print(result.to_json())
    # The verdict is the operation's own, not this file's guess at one. A set
    # of success words lived here and defaulted every new status string to a
    # failure — ``commissioning-code`` and ``backup`` both succeeded on the
    # Host and exited non-zero because nobody thought to extend it.
    ledger.record(
        operation=arguments.operation,
        outcome=str(result.outcome),
        report=result.report,
    )
    return 0 if result.outcome.successful else 1


def run() -> None:
    raise SystemExit(main())


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="eidolon-ops",
        description="Manage an Eidolon host through one path and lifecycle contract.",
    )
    parser.add_argument("--config", type=Path, required=True, help="Mac or Pi host profile")
    parser.add_argument(
        "--revision",
        action="append",
        default=[],
        metavar="SOURCE=40HEX",
        help=(
            "reproduce one source at an exact commit for this run only; nothing is "
            "written back to any file. Without it a source ships its repository HEAD"
        ),
    )
    parser.add_argument(
        "--allow-dirty",
        action="store_true",
        help=(
            "seal a release from repositories that have uncommitted changes. The "
            "changes are still not in it — only the committed HEAD ships — and the "
            "dirty state is recorded in the Host's release evidence"
        ),
    )
    operations = parser.add_subparsers(dest="operation", required=True)
    status = operations.add_parser("status")
    status_format = status.add_mutually_exclusive_group()
    status_format.add_argument(
        "--human",
        action="store_true",
        help="render a concise Host and service table instead of JSON",
    )
    status_format.add_argument(
        "--json",
        action="store_false",
        dest="human",
        help="emit the complete machine-readable Evidence document (default for eidolon-ops)",
    )
    status.set_defaults(human=False)
    operations.add_parser("app-ready")
    doctor = operations.add_parser("doctor")
    doctor.add_argument("--release-id")
    provision = operations.add_parser("provision")
    provision.add_argument("--apply", action="store_true")
    init_inputs = operations.add_parser("init-inputs")
    init_inputs.add_argument(
        "--new-identity",
        action="store_true",
        help="retire this machine's Host identity and mint a new one",
    )
    converge_inputs = operations.add_parser(
        "converge-inputs",
        help=(
            "give this machine and the Host the credentials the product declares "
            "and they are missing; existing values are never touched"
        ),
    )
    converge_inputs.add_argument("--apply", action="store_true")
    backup = operations.add_parser(
        "backup",
        help="snapshot every authority that can be snapshotted, and fetch it here",
    )
    backup.add_argument("--output", type=Path, required=True)
    restore = operations.add_parser(
        "restore",
        help="put a backup back on the Host it came from; the product stops for it",
    )
    restore.add_argument("--source", type=Path, required=True)
    restore.add_argument("--apply", action="store_true")
    commissioning_code = operations.add_parser("commissioning-code")
    commissioning_code.add_argument("--ttl-seconds", type=int, default=600)
    install = operations.add_parser("install")
    install.add_argument("--release-id", required=True)
    install.add_argument("--resume", action="store_true")
    install.add_argument("--apply", action="store_true")
    install.add_argument("--reset-existing", action="store_true")
    install.add_argument("--wipe-authority-data", action="store_true")
    controller_reset = operations.add_parser(
        "controller-reset",
        help="revoke every managing phone so a new one can claim this Host",
    )
    controller_reset.add_argument("--apply", action="store_true")
    authority_reset = operations.add_parser(
        "authority-reset",
        help="advance Owner Authority and replace only Hub-owned authority state",
    )
    authority_reset.add_argument("--apply", action="store_true")
    authority_backup = operations.add_parser(
        "authority-backup",
        help="capture a complete same-generation Owner Authority restore package",
    )
    authority_backup.add_argument("--output", type=Path, required=True)
    authority_restore = operations.add_parser(
        "authority-restore",
        help="restore one complete Owner Authority package without generation advance",
    )
    authority_restore.add_argument("--source", type=Path, required=True)
    authority_restore.add_argument("--apply", action="store_true")
    reset = operations.add_parser("reset")
    reset.add_argument("--wipe-authority-data", action="store_true")
    reset.add_argument("--apply", action="store_true")
    abandon = operations.add_parser(
        "abandon",
        help="give up a prepared candidate nobody will finish (never an activated one)",
    )
    abandon.add_argument("--release-id", required=True)

    for name in ("deploy", "update"):
        deploy = operations.add_parser(name)
        deploy.add_argument("--release-id", required=True)
        deploy.add_argument("--resume", action="store_true")
        deploy.add_argument("--activate", action="store_true")
        deploy.add_argument(
            "--cutover-mode",
            choices=("reversible", "forward-only"),
            default="reversible",
        )
    rollback = operations.add_parser("rollback")
    rollback.add_argument("--release-id", required=True)
    rollback.add_argument("--snapshot", type=Path, required=True)
    rollback.add_argument("--apply", action="store_true")
    diagnose = operations.add_parser("diagnose")
    diagnose.add_argument("--output", type=Path, required=True)
    for name in ("start", "stop", "restart"):
        command = operations.add_parser(name)
        command.add_argument("--dry-run", action="store_true")
        if name == "start":
            command.add_argument("--force-cleanup", action="store_true")
            command.add_argument("--strict", action="store_true")
            command.add_argument("--no-wait-ready", action="store_true")
    logs = operations.add_parser("logs")
    logs.add_argument("--service")
    logs.add_argument("--lines", type=int, default=200)
    logs.add_argument("--since")
    debug = operations.add_parser(
        "debug",
        help="macOS product-source diagnostics; not a product operation",
    )
    debug.add_argument(
        "profile_operation",
        choices=(
            "prepare",
            "validate",
            "status",
            "web-start",
            "web-stop",
            "web-restart",
            "web-status",
            "commissioning-code",
        ),
    )
    return parser


def _print(value: object, *, stream=None) -> None:
    print(json.dumps(value, indent=2, sort_keys=True), file=stream or sys.stdout)


if __name__ == "__main__":
    raise SystemExit(main())
