"""Mac operator command-line interface."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path

from eidolon_ops.config import PRODUCT_UNITS, ConfigurationError, load_config
from eidolon_ops.controller import EidolonPiController, OperationsError
from eidolon_ops.process import ProcessError, SubprocessRunner
from eidolon_ops.transport import TransportError


def main(argv: Sequence[str] | None = None) -> int:
    parser = _parser()
    arguments = parser.parse_args(argv)
    try:
        config = load_config(arguments.config).with_revision_overrides(tuple(arguments.revision))
        controller = EidolonPiController(config, SubprocessRunner())
        if arguments.operation == "status":
            result = controller.status()
        elif arguments.operation == "doctor":
            result = controller.doctor(release_id=arguments.release_id)
        elif arguments.operation == "install":
            result = controller.install(
                release_id=arguments.release_id,
                resume=arguments.resume,
                apply=arguments.apply,
            )
        elif arguments.operation in {"deploy", "update"}:
            result = controller.deploy(
                release_id=arguments.release_id,
                resume=arguments.resume,
                activate=arguments.activate,
            )
        elif arguments.operation in {"start", "stop", "restart"}:
            result = controller.lifecycle(arguments.operation, dry_run=arguments.dry_run)
        elif arguments.operation == "rollback":
            result = controller.rollback(
                release_id=arguments.release_id,
                snapshot=arguments.snapshot,
                apply=arguments.apply,
            )
        elif arguments.operation == "logs":
            result = controller.logs(
                unit=arguments.unit,
                lines=arguments.lines,
                since=arguments.since,
            )
        else:
            result = controller.diagnose(output=arguments.output)
    except (
        ConfigurationError,
        OperationsError,
        ProcessError,
        TransportError,
        OSError,
    ) as exc:
        _print_json({"status": "failed", "error": str(exc)}, stream=sys.stderr)
        return 1
    _print_json(result)
    return 1 if arguments.operation == "doctor" and result.get("status") != "healthy" else 0


def run() -> None:
    raise SystemExit(main())


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="eidolon-pi",
        description="Commit-pinned Raspberry Pi deployment and remote operations for Eidolon OS.",
    )
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument(
        "--revision",
        action="append",
        default=[],
        metavar="SOURCE=40HEX",
        help="override one reviewed source revision without reading its working tree",
    )
    operations = parser.add_subparsers(dest="operation", required=True)
    operations.add_parser("status", help="read unit, current-link and receipt status")
    doctor = operations.add_parser("doctor", help="run local and target read-only preflight")
    doctor.add_argument("--release-id")

    install = operations.add_parser("install", help="first Eidolon installation on a clean host")
    install.add_argument("--release-id", required=True)
    install.add_argument("--resume", action="store_true")
    install.add_argument("--apply", action="store_true")

    for name in ("deploy", "update"):
        deploy = operations.add_parser(
            name, help="prepare/dry-run or activate an immutable release"
        )
        deploy.add_argument("--release-id", required=True)
        deploy.add_argument("--resume", action="store_true")
        deploy.add_argument("--activate", action="store_true")

    for name in ("start", "stop", "restart"):
        lifecycle = operations.add_parser(name, help=f"{name} the reviewed product topology")
        lifecycle.add_argument("--dry-run", action="store_true")

    rollback = operations.add_parser("rollback", help="restore one exact activation snapshot")
    rollback.add_argument("--release-id", required=True)
    rollback.add_argument("--snapshot", type=Path, required=True)
    rollback.add_argument("--apply", action="store_true")

    logs = operations.add_parser("logs", help="read bounded product journals")
    logs.add_argument("--unit", choices=PRODUCT_UNITS)
    logs.add_argument("--lines", type=int, default=200)
    logs.add_argument("--since")

    diagnose = operations.add_parser("diagnose", help="write a redacted diagnostic archive")
    diagnose.add_argument("--output", type=Path, required=True)
    return parser


def _print_json(document: object, *, stream=None) -> None:
    print(json.dumps(document, indent=2, sort_keys=True), file=stream or sys.stdout)


if __name__ == "__main__":
    raise SystemExit(main())
