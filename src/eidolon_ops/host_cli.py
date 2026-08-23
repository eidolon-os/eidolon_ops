"""Unified Eidolon host lifecycle CLI for macOS and Raspberry Pi."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
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
from eidolon_ops.transport import TransportError


def main(argv: Sequence[str] | None = None) -> int:
    parser = _parser()
    arguments = parser.parse_args(argv)
    try:
        controller = HostController(
            load_host_profile(arguments.config),
            SubprocessRunner(),
            revision_overrides=tuple(arguments.revision),
        )
        if arguments.operation == "status":
            result = controller.status()
        elif arguments.operation == "app-ready":
            result = controller.app_ready()
        elif arguments.operation == "doctor":
            result = controller.doctor(release_id=arguments.release_id)
        elif arguments.operation == "provision":
            result = controller.provision(apply=arguments.apply)
        elif arguments.operation == "init-inputs":
            result = controller.initialize_inputs(new_identity=arguments.new_identity)
        elif arguments.operation == "backup":
            result = controller.backup(output=arguments.output)
        elif arguments.operation == "restore":
            result = controller.restore(source=arguments.source, apply=arguments.apply)
        elif arguments.operation == "commissioning-code":
            result = controller.commissioning_code(ttl_seconds=arguments.ttl_seconds)
        elif arguments.operation == "install":
            result = controller.install(
                release_id=arguments.release_id,
                resume=arguments.resume,
                apply=arguments.apply,
                reset_existing=arguments.reset_existing,
                wipe_authority_data=arguments.wipe_authority_data,
            )
        elif arguments.operation == "controller-reset":
            result = controller.controller_reset(apply=arguments.apply)
        elif arguments.operation == "authority-reset":
            result = controller.authority_reset(apply=arguments.apply)
        elif arguments.operation == "reset":
            result = controller.reset(
                wipe_authority_data=arguments.wipe_authority_data,
                apply=arguments.apply,
            )
        elif arguments.operation in {"deploy", "update"}:
            result = controller.deploy(
                release_id=arguments.release_id,
                resume=arguments.resume,
                activate=arguments.activate,
            )
        elif arguments.operation == "rollback":
            result = controller.rollback(
                release_id=arguments.release_id,
                snapshot=arguments.snapshot,
                apply=arguments.apply,
            )
        elif arguments.operation == "diagnose":
            result = controller.diagnose(output=arguments.output)
        elif arguments.operation in {"start", "stop", "restart"}:
            result = controller.lifecycle(
                arguments.operation,
                dry_run=arguments.dry_run,
                force_cleanup=getattr(arguments, "force_cleanup", False),
                strict=getattr(arguments, "strict", False),
                wait_ready=not getattr(arguments, "no_wait_ready", False),
            )
        elif arguments.operation == "debug":
            result = controller.local_profile("product-source", arguments.profile_operation)
        else:
            result = controller.logs(
                service=arguments.service,
                lines=arguments.lines,
                since=arguments.since,
            )
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
        _print(
            {"status": "failed", "outcome": str(Outcome.FAILED), "error": str(exc)},
            stream=sys.stderr,
        )
        return 1
    _print(result.to_json())
    # The verdict is the operation's own, not this file's guess at one. A set
    # of success words lived here and defaulted every new status string to a
    # failure — ``commissioning-code`` and ``backup`` both succeeded on the
    # Host and exited non-zero because nobody thought to extend it.
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
        help="override one reviewed Pi source revision",
    )
    operations = parser.add_subparsers(dest="operation", required=True)
    operations.add_parser("status")
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
    reset = operations.add_parser("reset")
    reset.add_argument("--wipe-authority-data", action="store_true")
    reset.add_argument("--apply", action="store_true")
    for name in ("deploy", "update"):
        deploy = operations.add_parser(name)
        deploy.add_argument("--release-id", required=True)
        deploy.add_argument("--resume", action="store_true")
        deploy.add_argument("--activate", action="store_true")
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
