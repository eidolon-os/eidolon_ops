"""One service is restarted one way: by asking eidolond (Ops 总纲 §1.5).

Before this, Ops had no single-service verb and a restart went around eidolond
— Admin's supervisor routes, run_all.sh's passthrough, supervisorctl by hand —
while eidolond went on reconciling the same program every five seconds. The
2026-09-29 Channel deploy got "already started" from exactly that race.
"""

from __future__ import annotations

import http.server
import json
import os
import shutil
import socketserver
import subprocess
import tempfile
import threading
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_host_controller import Runner, _profile

from eidolon_ops import host_cli, plans
from eidolon_ops.adapters.systemd import SystemdSupervisor
from eidolon_ops.console.catalog import CATALOG as CONSOLE_OPERATIONS
from eidolon_ops.controller import EidolonPiController
from eidolon_ops.host_controller import HostController
from eidolon_ops.hostagent import __main__ as hostagent_main
from eidolon_ops.hostagent import services
from eidolon_ops.hostagent.primitives import TargetError
from eidolon_ops.model import Capability, Outcome

ROOT = Path(__file__).resolve().parents[1]


class _Eidolond(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True


def _serve(socket_path: Path, *, restart_status: int = 200, read_status: int = 200):
    """A stand-in eidolond that answers the three requests a restart makes."""

    seen: list[tuple[str, str, object]] = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def address_string(self) -> str:  # a Unix peer has no host to print
            return "unix"

        def log_message(self, *_args) -> None:
            pass

        def _reply(self, status: int, document: object) -> None:
            raw = json.dumps(document).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def do_GET(self) -> None:
            seen.append(("GET", self.path, None))
            if read_status != 200:
                self._reply(read_status, {"detail": "unknown system service"})
                return
            self._reply(200, {
                "operation": "system.service-status",
                "service_id": "channel",
                "required": True,
                "desired": {"service_id": "channel", "enabled": True, "revision": 7,
                            "updated_at": "2026-09-29T12:21:18Z"},
                "runtime_state": "ready",
                "observed_at": "2026-09-29T12:21:18Z",
            })

        def do_POST(self) -> None:
            length = int(self.headers.get("Content-Length", "0"))
            seen.append(("POST", self.path, json.loads(self.rfile.read(length))))
            if restart_status != 200:
                self._reply(restart_status, {"detail": "system service is external on this host"})
                return
            self._reply(200, {
                "operation": "system.service-mutation-result",
                "state": {"service_id": "channel", "enabled": True, "revision": 7,
                          "updated_at": "2026-09-29T12:21:18Z"},
                "audit_position": 42,
                "replayed": False,
            })

    server = _Eidolond(str(socket_path), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, seen


@pytest.fixture
def short_dir():
    # A Unix socket path is limited to about 104 bytes on macOS; pytest's
    # tmp_path is longer than that there.
    directory = Path(tempfile.mkdtemp(prefix="eo-", dir="/tmp"))
    yield directory
    shutil.rmtree(directory, ignore_errors=True)


# -- the executor -------------------------------------------------------------


def test_a_restart_reads_asks_with_the_revision_and_reads_again(short_dir) -> None:
    server, seen = _serve(short_dir / "system.sock")
    try:
        report = services.restart_service(short_dir / "system.sock", "channel", "ops-abc")
    finally:
        server.shutdown()

    assert [(method, path) for method, path, _ in seen] == [
        ("GET", "/api/system/v1/services/channel"),
        ("POST", "/api/system/v1/services/channel/restart"),
        ("GET", "/api/system/v1/services/channel"),
    ]
    assert seen[1][2] == {
        "operation": "system.service.restart",
        "request_id": "ops-abc",
        "expected_revision": 7,
    }
    assert report["status"] == "restarted"
    assert report["audit_position"] == 42
    assert report["before"]["runtime_state"] == "ready"


@pytest.mark.parametrize("read_status, restart_status, stage", [(404, 200, "read"), (200, 409, "restart")])
def test_eidolond_refusing_is_reported_as_it_said_it_and_not_retried(
    short_dir, read_status, restart_status, stage
) -> None:
    server, seen = _serve(
        short_dir / "system.sock", read_status=read_status, restart_status=restart_status
    )
    try:
        report = services.restart_service(short_dir / "system.sock", "channel", "ops-abc")
    finally:
        server.shutdown()

    assert report["status"] == "refused"
    assert report["stage"] == stage
    assert report["http_status"] == (read_status if stage == "read" else restart_status)
    assert report["detail"]
    assert sum(1 for method, _, _ in seen if method == "POST") == (0 if stage == "read" else 1)


@pytest.mark.parametrize("service", ["../hub", "Hub", "", "channel/restart", "a" * 65])
def test_a_name_that_is_not_a_service_never_becomes_a_request(short_dir, service) -> None:
    with pytest.raises(TargetError):
        services.restart_service(short_dir / "system.sock", service, "ops-abc")


def test_nobody_answering_is_not_a_refusal(short_dir) -> None:
    with pytest.raises(TargetError, match="did not answer"):
        services.restart_service(short_dir / "absent.sock", "channel", "ops-abc")


def test_the_host_agent_offers_the_action_on_its_own_socket() -> None:
    assert hostagent_main.ACTIONS["service-restart"] == (services, "restart")
    assert Path("/run/eidolon/system.sock") == services.SYSTEM_SOCKET


# -- the one entry ------------------------------------------------------------


def _mac(short_dir: Path, tmp_path: Path) -> tuple[HostController, Runner]:
    profile = _profile(tmp_path)
    profile = replace(profile, paths=replace(profile.paths, runtime_root=short_dir))
    runner = Runner()
    return HostController(profile, runner), runner


def test_the_mac_asks_its_own_eidolond_and_never_supervisorctl(short_dir, tmp_path) -> None:
    controller, runner = _mac(short_dir, tmp_path)
    server, seen = _serve(short_dir / "system.sock")
    try:
        evidence = controller.service_restart("channel")
    finally:
        server.shutdown()

    assert evidence.outcome is Outcome.APPLIED
    assert evidence.plan.operation == "service-restart"
    assert evidence.report["audit_position"] == 42
    assert seen[1][2]["request_id"].startswith("ops-")
    # No process was run: not the lifecycle script, not supervisorctl.
    assert runner.calls == []


def test_a_dry_run_asks_nothing(short_dir, tmp_path) -> None:
    controller, runner = _mac(short_dir, tmp_path)
    server, seen = _serve(short_dir / "system.sock")
    try:
        evidence = controller.service_restart("channel", dry_run=True)
    finally:
        server.shutdown()

    assert evidence.outcome is Outcome.PLANNED
    assert seen == []
    assert runner.calls == []


@pytest.mark.parametrize("status, outcome", [(409, Outcome.REFUSED), (503, Outcome.FAILED)])
def test_what_eidolond_answers_decides_the_outcome(short_dir, tmp_path, status, outcome) -> None:
    controller, _runner = _mac(short_dir, tmp_path)
    server, _seen = _serve(short_dir / "system.sock", restart_status=status)
    try:
        evidence = controller.service_restart("channel")
    finally:
        server.shutdown()

    assert evidence.outcome is outcome
    assert not evidence.outcome.successful


def test_the_pi_asks_through_the_injected_host_agent() -> None:
    calls = []
    fake = SimpleNamespace(
        preflight=SimpleNamespace(validate_ssh_material=lambda: None),
        transport=SimpleNamespace(
            run_agent=lambda action, payload, **kwargs: calls.append((action, payload, kwargs))
            or {"status": "restarted"}
        ),
    )

    report = EidolonPiController.service_restart(fake, "hub", request_id="ops-1", dry_run=False)

    assert report == {"status": "restarted"}
    assert calls == [("service-restart", {"service_id": "hub", "request_id": "ops-1"},
                      {"timeout": 180})]


def test_both_hosts_offer_it_and_the_plan_says_it_only_moves_a_process() -> None:
    assert Capability.SERVICE_RESTART in SystemdSupervisor(release=None).capabilities
    plan = plans.service_restart("mac", "channel", dry_run=False)
    assert plan.operation == "service-restart"
    assert {str(kind) for kind in plan.touches} == {"lifecycle"}
    assert str(plan.destructive) == "reversible"


def test_the_console_offers_the_same_operation() -> None:
    operation = next(op for op in CONSOLE_OPERATIONS if op.name == "service-restart")
    assert operation.capability is Capability.SERVICE_RESTART
    built = operation.plan("mac", {"service": "channel", "dry_run": False})
    assert built == plans.service_restart("mac", "channel", dry_run=False)


def test_the_entry_script_offers_it_on_both_hosts() -> None:
    script = (ROOT / "eidolon").read_text(encoding="utf-8")
    for name in ("LOCAL_SUPERVISORD_COMMANDS", "SSH_SYSTEMD_COMMANDS", "ALL_COMMANDS"):
        line = next(line for line in script.splitlines() if line.startswith(f"{name}="))
        assert " service" in line.split("=", 1)[1] or line.endswith('service"')


def test_only_restart_is_offered() -> None:
    parser = host_cli._parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["--config", "/tmp/h.toml", "service", "disable", "channel"])


# -- no second way in -----------------------------------------------------------


@pytest.mark.parametrize(
    "arguments",
    [["sv", "status"], ["product-source", "sv", "status"], ["start"], ["restart"],
     ["foreground"], ["start-admin"], ["product-source", "owner-reset"]],
)
def test_the_mac_adapter_refuses_every_command_ops_does_not_call(tmp_path, arguments) -> None:
    environment = {"PATH": os.environ["PATH"], "HOME": str(tmp_path)}
    result = subprocess.run(
        ["bash", str(ROOT / "deploy/dev/run_all.sh"), *arguments],
        capture_output=True, text=True, env=environment, timeout=30, check=False,
    )
    assert result.returncode != 0
    assert "unknown" in result.stderr + result.stdout


def test_the_mac_adapter_keeps_no_supervisorctl_passthrough() -> None:
    script = (ROOT / "deploy/dev/run_all.sh").read_text(encoding="utf-8")
    assert "do_sv_passthrough" not in script
    assert "do_product_source_sv" not in script
    assert "Not an operator entry point" in script
