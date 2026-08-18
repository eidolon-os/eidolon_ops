"""Start the console on this workstation, and nowhere else.

Loopback, always. This process can install a release, wipe a Host's authority
data and revoke every phone that manages it; the operator's own machine is the
only place that set of buttons belongs, and an address flag would be an
invitation to put it somewhere else. A second machine that needs these buttons
needs its own checkout, its own SSH key and its own profiles — which is exactly
the boundary Ops already draws.
"""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from pathlib import Path

from eidolon_ops.console.api import STATIC_ROOT, create_app
from eidolon_ops.console.errors import ConsoleError
from eidolon_ops.console.hosts import HostRegistry, discover

#: Outside every port the product topology claims, and next to Admin's own
#: pair so an operator reading ``lsof`` can tell what they are looking at.
DEFAULT_PORT = 9010
LOOPBACK = "127.0.0.1"


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        registry = HostRegistry(_locations(arguments))
    except ConsoleError as exc:
        print(f"eidolon-ops-console: {exc}")
        return 1
    try:
        import uvicorn
    except ImportError:
        print(
            "eidolon-ops-console needs the console extra: uv sync --all-extras",
        )
        return 1
    entries = registry.entries()
    print(f"eidolon-ops console  http://{LOOPBACK}:{arguments.port}")
    for entry in entries:
        state = entry.error or f"{len(entry.capabilities)} capabilities"
        print(f"  {entry.host_id:<16} {entry.path}  [{state}]")
    if not STATIC_ROOT.joinpath("index.html").is_file():
        print("  interface not built yet: cd web && npm install && npm run build")
    uvicorn.run(
        create_app(registry),
        host=LOOPBACK,
        port=arguments.port,
        log_level=arguments.log_level,
        access_log=False,
    )
    return 0


def run() -> None:
    raise SystemExit(main())


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="eidolon-ops-console",
        description="A loopback console over the same Host lifecycle contract as eidolon-ops.",
    )
    parser.add_argument(
        "--config",
        type=Path,
        action="append",
        default=[],
        metavar="HOST.toml",
        help="one Host profile to manage; repeatable",
    )
    parser.add_argument(
        "--profiles",
        type=Path,
        help="a directory of Host profiles (default: config/hosts)",
    )
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument(
        "--log-level",
        default="warning",
        choices=("critical", "error", "warning", "info", "debug"),
    )
    return parser


def _locations(arguments: argparse.Namespace) -> tuple[Path, ...]:
    if arguments.config:
        return tuple(arguments.config)
    directory = arguments.profiles or Path("config/hosts")
    return discover(directory)


if __name__ == "__main__":
    raise SystemExit(main())
