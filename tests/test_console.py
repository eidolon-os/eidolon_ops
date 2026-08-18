"""The workstation console: what it offers, what it refuses, and what it streams.

No Host is contacted anywhere in this file. The console's own contract is the
subject — which operations a composed Host is offered, how much confirmation a
set of parameters demands, and what a run says while it happens — so the
controller is a stub and the interesting assertions are about the refusals.
"""

from __future__ import annotations

import json
import re
import threading
from pathlib import Path

import pytest

from eidolon_ops.console import catalog
from eidolon_ops.console.catalog import Confirmation
from eidolon_ops.console.errors import ConsoleError
from eidolon_ops.console.hosts import HostRegistry, discover
from eidolon_ops.console.redaction import REDACTED, redacted
from eidolon_ops.console.runs import Request, RunStatus, RunStore
from eidolon_ops.model import Capability, Evidence, Outcome, Plan, Step
from eidolon_ops.progress import Journal

fastapi = pytest.importorskip("fastapi", reason="the console extra is not installed")
from fastapi.testclient import TestClient  # noqa: E402

pytestmark = pytest.mark.console

MAC_ID = "mac-console"
PI_ID = "pi-console"


# -- fixtures -----------------------------------------------------------------


@pytest.fixture
def profiles(tmp_path: Path, config_path: Path) -> Path:
    """A profile directory with one source Host, one product Host and one template."""

    directory = tmp_path / "hosts"
    directory.mkdir()
    workspace = tmp_path / "workspace"
    script = workspace / "deploy/dev/run_all.sh"
    script.parent.mkdir(parents=True)
    script.write_text("#!/bin/sh\n", encoding="utf-8")
    script.chmod(0o755)
    livekit = tmp_path / "livekit.generated.yaml"
    livekit.write_text("port: 7880\n", encoding="utf-8")
    logs = tmp_path / "logs"
    (logs / "hub").mkdir(parents=True)
    (logs / "kernel.log").write_text("started\n", encoding="utf-8")

    (directory / "mac.toml").write_text(
        f"""\
schema_version = 1

[host]
id = "{MAC_ID}"
platform = "macos"
driver = "local-supervisord"

[paths]
install_root = "{workspace}"
current_root = "{workspace}"
config_root = "{tmp_path / "config"}"
state_root = "{tmp_path / "state"}"
runtime_root = "{tmp_path / "run"}"
log_root = "{logs}"
cache_root = "{tmp_path / "cache"}"
bootstrap_state_root = "{tmp_path / "bootstrap-state"}"
bootstrap_runtime_root = "{tmp_path / "bootstrap-run"}"

[adapter]
lifecycle_script = "{script}"
operations_config = "{config_path}"
foundation_mode = "external"
external_livekit_config = "{livekit}"

[app]
hub_https_port = 8443
livekit_client_url = "ws://eidolon-hub-console.local:7880"
allow_insecure_livekit = true
""",
        encoding="utf-8",
    )
    (directory / "pi.toml").write_text(
        f"""\
schema_version = 1

[host]
id = "{PI_ID}"
platform = "raspberry-pi"
driver = "ssh-systemd"

[paths]
install_root = "/opt/eidolon"
current_root = "/opt/eidolon/current"
config_root = "/etc/eidolon"
state_root = "/var/lib/eidolon"
runtime_root = "/run/eidolon"
log_root = "/var/log/eidolon"
cache_root = "/var/cache/eidolon"
bootstrap_state_root = "/var/lib/eidolon-bootstrap"
bootstrap_runtime_root = "/run/eidolon-bootstrap"

[adapter]
operations_config = "{config_path}"

[app]
hub_https_port = 8443
livekit_client_url = "wss://eidolon-hub-console.local:7880"
allow_insecure_livekit = false
""",
        encoding="utf-8",
    )
    (directory / "pi.example.toml").write_text("schema_version = 1\n", encoding="utf-8")
    return directory


@pytest.fixture
def registry(profiles: Path) -> HostRegistry:
    return HostRegistry(discover(profiles))


class StubController:
    """Whatever the catalog asks of a controller, answered without a Host.

    Reports its phases through the sink it was built with, which is how the run
    store's progress path is exercised without an SSH connection.
    """

    def __init__(self, host_id: str, progress=None, report=None) -> None:
        self.host_id = host_id
        self.progress = progress
        self.report = report or {"status": "ok"}
        self.calls: list[tuple[str, dict]] = []

    def _evidence(self, operation: str, outcome: Outcome, **parameters) -> Evidence:
        self.calls.append((operation, parameters))
        journal = Journal(self.progress)
        journal.begin("observe")
        journal.append({"phase": "observe", "result": {"status": "ok"}})
        plan = Plan(
            operation=operation,
            host_id=self.host_id,
            steps=(Step(id="observe", description="stub step"),),
        )
        return Evidence(plan=plan, outcome=outcome, report=self.report)

    def status(self) -> Evidence:
        return self._evidence("status", Outcome.OBSERVED)

    def doctor(self, *, release_id=None) -> Evidence:
        return self._evidence("doctor", Outcome.DEGRADED, release_id=release_id)

    def app_ready(self) -> Evidence:
        return self._evidence("app-ready", Outcome.OBSERVED)

    def logs(self, *, service, lines, since) -> Evidence:
        return self._evidence("logs", Outcome.OBSERVED, service=service, lines=lines, since=since)

    def lifecycle(self, action, *, dry_run) -> Evidence:
        return self._evidence(action, Outcome.APPLIED, dry_run=dry_run)

    def commissioning_code(self, *, ttl_seconds) -> Evidence:
        return self._evidence("commissioning-code", Outcome.APPLIED, ttl_seconds=ttl_seconds)

    def reset(self, *, wipe_authority_data, apply) -> Evidence:
        return self._evidence(
            "reset", Outcome.APPLIED, wipe_authority_data=wipe_authority_data, apply=apply
        )

    def install(self, **parameters) -> Evidence:
        return self._evidence("install", Outcome.APPLIED, **parameters)

    def deploy(self, **parameters) -> Evidence:
        return self._evidence("deploy", Outcome.APPLIED, **parameters)


@pytest.fixture
def client(registry: HostRegistry, monkeypatch: pytest.MonkeyPatch):
    from eidolon_ops.console.api import create_app

    built: list[StubController] = []

    def controller(self, host_id, *, progress=None):
        stub = StubController(host_id, progress)
        built.append(stub)
        return stub

    monkeypatch.setattr(HostRegistry, "controller", controller)
    test_client = TestClient(create_app(registry))
    test_client.built = built  # type: ignore[attr-defined]
    return test_client


# -- the registry -------------------------------------------------------------


def test_discovery_skips_the_template_an_operator_is_meant_to_copy(profiles: Path) -> None:
    assert [path.name for path in discover(profiles)] == ["mac.toml", "pi.toml"]


def test_a_profile_that_does_not_load_is_listed_with_its_reason(profiles: Path) -> None:
    """A Host missing from the console looks like a Host that does not exist."""

    (profiles / "broken.toml").write_text("schema_version = 2\n", encoding="utf-8")
    entries = {entry.host_id: entry for entry in HostRegistry(discover(profiles)).entries()}

    assert entries["broken"].profile is None
    assert entries["broken"].capabilities == frozenset()
    assert "schema_version" in str(entries["broken"].error)


def test_capabilities_come_from_the_composed_adapter(registry: HostRegistry) -> None:
    mac = registry.entry(MAC_ID)
    pi = registry.entry(PI_ID)

    assert Capability.SOURCE_PROFILE in mac.capabilities
    assert Capability.INSTALL not in mac.capabilities
    assert Capability.INSTALL in pi.capabilities
    assert Capability.SOURCE_PROFILE not in pi.capabilities
    assert pi.adapter is not None
    assert pi.adapter["supervisor"] == "systemd"


def test_log_suggestions_follow_whichever_supervisor_owns_the_logs(
    registry: HostRegistry,
) -> None:
    assert registry.suggestions(MAC_ID)["service"] == ["hub", "kernel.log"]
    assert "eidolon-data.service" in registry.suggestions(PI_ID)["service"]


def test_a_suggested_artifact_path_is_on_this_workstation(registry: HostRegistry) -> None:
    """A product profile's cache root is a place on the board, not somewhere here."""

    suggested = Path(registry.artifact_default(PI_ID, "backup", ".tar.gz"))

    assert suggested.is_absolute()
    assert not str(suggested).startswith("/var/cache/eidolon")
    assert suggested.name.startswith(f"{PI_ID}-backup-")
    assert suggested.name.endswith(".tar.gz")


def test_a_console_with_no_profiles_refuses_to_start(tmp_path: Path) -> None:
    with pytest.raises(ConsoleError):
        HostRegistry([])


# -- the catalog --------------------------------------------------------------


ALL = frozenset(Capability)


@pytest.mark.parametrize(
    ("operation", "parameters", "expected"),
    [
        ("status", {}, Confirmation.NONE),
        ("diagnose", {"output": "/tmp/report.tar.gz"}, Confirmation.NONE),
        ("install", {"release_id": "r-1"}, Confirmation.NONE),
        ("install", {"release_id": "r-1", "apply": True}, Confirmation.ACKNOWLEDGE),
        (
            "install",
            {
                "release_id": "r-1",
                "apply": True,
                "reset_existing": True,
                "wipe_authority_data": True,
            },
            Confirmation.TYPED_HOST_ID,
        ),
        ("deploy", {"release_id": "r-1"}, Confirmation.NONE),
        ("deploy", {"release_id": "r-1", "activate": True}, Confirmation.ACKNOWLEDGE),
        ("restart", {"dry_run": True}, Confirmation.NONE),
        ("restart", {}, Confirmation.ACKNOWLEDGE),
        ("reset", {"apply": True}, Confirmation.TYPED_HOST_ID),
        ("controller-reset", {"apply": True}, Confirmation.TYPED_HOST_ID),
        ("init-inputs", {}, Confirmation.ACKNOWLEDGE),
        ("init-inputs", {"new_identity": True}, Confirmation.TYPED_HOST_ID),
    ],
)
def test_confirmation_is_read_off_the_plan_these_parameters_produce(
    operation: str, parameters: dict, expected: Confirmation
) -> None:
    spec = catalog.operation(operation)
    coerced = catalog.coerce_parameters(spec, parameters, capabilities=ALL)

    assert catalog.required_confirmation(spec, coerced) is expected


def test_missing_parameters_are_filled_from_the_operation_itself() -> None:
    spec = catalog.operation("logs")

    values = catalog.coerce_parameters(spec, {}, capabilities=ALL)

    assert values == {"service": None, "lines": 200, "since": None}


def test_a_parameter_the_operation_never_declared_is_refused() -> None:
    """A stale client is told, rather than being quietly given a different operation."""

    with pytest.raises(ConsoleError, match="no such parameter: force"):
        catalog.coerce_parameters(
            catalog.operation("reset"), {"force": True}, capabilities=ALL
        )


@pytest.mark.parametrize(
    ("operation", "parameters", "message"),
    [
        ("install", {}, "requires release_id"),
        ("logs", {"lines": 0}, "at least 1"),
        ("logs", {"lines": 99999}, "at most 5000"),
        ("logs", {"lines": "200"}, "must be an integer"),
        ("backup", {"output": "relative/path.tar.gz"}, "absolute path"),
        ("commissioning-code", {"ttl_seconds": 5}, "at least 60"),
        ("debug", {"profile_operation": "rm -rf"}, "must be one of"),
        ("reset", {"apply": "yes"}, "must be a boolean"),
    ],
)
def test_invalid_parameters_are_refused_before_any_host_is_contacted(
    operation: str, parameters: dict, message: str
) -> None:
    with pytest.raises(ConsoleError, match=message):
        catalog.coerce_parameters(
            catalog.operation(operation), parameters, capabilities=ALL
        )


def test_a_field_needing_a_capability_is_refused_on_a_host_without_it() -> None:
    """Only a journal keeps history far enough back to select by time."""

    without_history = ALL - {Capability.LOG_HISTORY}

    with pytest.raises(ConsoleError, match="log-history"):
        catalog.coerce_parameters(
            catalog.operation("logs"), {"since": "-30min"}, capabilities=without_history
        )
    assert catalog.coerce_parameters(
        catalog.operation("logs"), {}, capabilities=without_history
    )["since"] is None


def test_every_catalog_entry_can_build_its_plan_from_its_own_defaults() -> None:
    for spec in catalog.CATALOG:
        parameters = {
            item.name: _placeholder(item) for item in spec.fields if item.required
        }
        coerced = catalog.coerce_parameters(spec, parameters, capabilities=ALL)
        plan = spec.plan("host", coerced)
        assert plan.operation
        assert plan.steps


def _placeholder(field: catalog.Field) -> object:
    if field.choices:
        return field.choices[0]
    if field.kind is catalog.Kind.PATH:
        return "/tmp/placeholder.tar.gz"
    return "r-1"


# -- redaction ----------------------------------------------------------------


def test_a_key_that_should_never_be_read_in_a_browser_is_masked() -> None:
    document = {
        "service_token": "abcd",
        "password": "hunter2",
        "secret_staging": "/var/tmp/eidolon-secrets-r1",
        "phases": [{"phase": "secret_cleanup", "result": {"api_key": "k"}}],
    }

    assert redacted(document) == {
        "service_token": REDACTED,
        "password": REDACTED,
        # A path is not a credential, and hiding it would hide the evidence.
        "secret_staging": "/var/tmp/eidolon-secrets-r1",
        "phases": [{"phase": "secret_cleanup", "result": {"api_key": REDACTED}}],
    }


def test_the_one_value_an_operation_exists_to_reveal_is_let_through() -> None:
    assert redacted({"setup_code": "123456", "token": "t"}, allow=("setup_code",)) == {
        "setup_code": "123456",
        "token": REDACTED,
    }


# -- the run store ------------------------------------------------------------


def _request(**overrides) -> Request:
    base = {
        "host_id": PI_ID,
        "operation": "deploy",
        "label": "发布/更新",
        "parameters": {},
        "plan": {"steps": []},
        "confirmation": "acknowledge",
        "mutating": True,
        "allow_keys": (),
        "work": lambda sink: Evidence(
            plan=Plan(operation="deploy", host_id=PI_ID, steps=()),
            outcome=Outcome.APPLIED,
        ),
    }
    return Request(**{**base, **overrides})


def _settled(store: RunStore, run_id: str) -> None:
    """Drain the run's stream, which ends when the run does."""

    for _event in store.stream(run_id):
        pass


def test_a_run_reports_each_phase_and_then_its_verdict() -> None:
    store = RunStore()

    def work(sink):
        journal = Journal(sink)
        journal.begin("bundle")
        journal.append({"phase": "bundle", "result": {"status": "sealed"}})
        journal.begin("prepare")
        journal.append({"phase": "prepare", "result": {"status": "prepared"}})
        return Evidence(
            plan=Plan(operation="deploy", host_id=PI_ID, steps=()),
            outcome=Outcome.APPLIED,
        )

    run = store.start(_request(work=work))
    _settled(store, run.id)

    assert run.status is RunStatus.COMPLETED
    assert run.outcome == "applied"
    assert [(phase.name, str(phase.status)) for phase in run.phases] == [
        ("bundle", "done"),
        ("prepare", "done"),
    ]
    assert [str(event.kind) for event in run.events] == [
        "run.started",
        "phase.began",
        "phase.recorded",
        "phase.began",
        "phase.recorded",
        "run.finished",
    ]


def test_the_phase_that_was_running_is_the_one_shown_as_failed() -> None:
    store = RunStore()

    def work(sink):
        journal = Journal(sink)
        journal.begin("bundle")
        journal.append({"phase": "bundle", "result": {}})
        journal.begin("prepare")
        raise RuntimeError("native build failed on the Host")

    run = store.start(_request(work=work))
    _settled(store, run.id)

    assert run.status is RunStatus.FAILED
    assert run.outcome is None
    assert run.error is not None
    assert "native build failed" in run.error
    assert [(phase.name, str(phase.status)) for phase in run.phases] == [
        ("bundle", "done"),
        ("prepare", "failed"),
    ]


def test_a_degraded_read_completed_and_says_so() -> None:
    """The run happened. What it found is the outcome's business, not the run's."""

    store = RunStore()
    run = store.start(
        _request(
            mutating=False,
            work=lambda _sink: Evidence(
                plan=Plan(operation="status", host_id=PI_ID, steps=()),
                outcome=Outcome.DEGRADED,
                report={"status": "degraded"},
            ),
        )
    )
    _settled(store, run.id)

    assert run.status is RunStatus.COMPLETED
    assert run.outcome == "degraded"


def test_one_boundary_action_at_a_time_per_host() -> None:
    store = RunStore()
    release = threading.Event()

    def slow(_sink):
        assert release.wait(10)
        return _applied()

    first = store.start(_request(work=slow))

    with pytest.raises(ConsoleError) as refusal:
        store.start(_request())
    assert refusal.value.status == 409

    # A read-only run is not a boundary action, so it is never blocked by one.
    reading = store.start(_request(mutating=False, work=lambda _sink: _applied()))
    _settled(store, reading.id)

    release.set()
    _settled(store, first.id)
    assert store.active(PI_ID) is None


def _applied() -> Evidence:
    return Evidence(
        plan=Plan(operation="deploy", host_id=PI_ID, steps=()), outcome=Outcome.APPLIED
    )


def test_a_watcher_that_arrives_late_is_replayed_the_whole_run() -> None:
    store = RunStore()
    run = store.start(_request(work=lambda _sink: _applied()))
    _settled(store, run.id)

    replayed = [str(event.kind) for event in store.stream(run.id) if event is not None]

    assert replayed[0] == "run.started"
    assert replayed[-1] == "run.finished"


def test_a_finished_run_keeps_only_what_the_console_is_allowed_to_show() -> None:
    store = RunStore()
    run = store.start(
        _request(
            work=lambda _sink: Evidence(
                plan=Plan(operation="deploy", host_id=PI_ID, steps=()),
                outcome=Outcome.APPLIED,
                report={"service_token": "abcd", "status": "activated"},
            )
        )
    )
    _settled(store, run.id)

    assert run.evidence is not None
    assert run.evidence["service_token"] == REDACTED
    assert run.evidence["status"] == "activated"


def test_an_unknown_run_is_a_404_not_an_empty_stream() -> None:
    store = RunStore()

    with pytest.raises(ConsoleError) as refusal:
        store.get("0" * 32)
    assert refusal.value.status == 404


def test_old_finished_runs_are_forgotten_and_running_ones_are_not() -> None:
    store = RunStore(retain=3)
    for _ in range(6):
        run = store.start(_request(mutating=False, work=lambda _sink: _applied()))
        _settled(store, run.id)

    assert len(store.listing(limit=100)) == 3


# -- the HTTP surface ---------------------------------------------------------


def test_hosts_are_listed_with_their_composition(client) -> None:
    hosts = {item["host_id"]: item for item in client.get("/api/hosts").json()["hosts"]}

    assert set(hosts) == {MAC_ID, PI_ID}
    assert hosts[PI_ID]["adapter"]["transport"] == "ssh"
    assert hosts[MAC_ID]["driver"] == "local-supervisord"
    assert hosts[PI_ID]["error"] is None


def test_a_host_is_offered_only_what_it_says_it_can_do(client) -> None:
    mac = {item["name"] for item in client.get(f"/api/hosts/{MAC_ID}").json()["operations"]}
    pi = {item["name"] for item in client.get(f"/api/hosts/{PI_ID}").json()["operations"]}

    assert "debug" in mac and "install" not in mac
    assert "install" in pi and "debug" not in pi


def test_the_readiness_contract_travels_with_the_host(client) -> None:
    """So a red fact can be labelled without the browser keeping its own copy."""

    pi = client.get(f"/api/hosts/{PI_ID}").json()

    assert pi["readiness"]["channel_worker_livekit_link"]
    assert "backend_healthy" in pi["readiness"]


def test_asking_a_host_for_an_operation_it_cannot_do_is_refused(client) -> None:
    response = client.post(
        f"/api/hosts/{MAC_ID}/plan", json={"operation": "install", "parameters": {}}
    )

    assert response.status_code == 409
    assert "install" in response.json()["error"]


def test_the_plan_endpoint_shows_the_gradient_before_anything_runs(client) -> None:
    response = client.post(
        f"/api/hosts/{PI_ID}/plan",
        json={
            "operation": "install",
            "parameters": {
                "release_id": "20260807-product-1",
                "apply": True,
                "reset_existing": True,
                "wipe_authority_data": True,
            },
        },
    )

    body = response.json()
    assert body["confirmation"] == "typed-host-id"
    assert body["plan"]["destructive"] == "irreversible"
    assert "data" in body["plan"]["touches"]
    assert body["plan"]["steps"][0]["id"] == "reset_existing"
    assert client.built == []  # no controller was ever built


def test_a_mutation_without_acknowledgement_is_refused(client) -> None:
    response = client.post(
        f"/api/hosts/{PI_ID}/runs",
        json={"operation": "reset", "parameters": {"apply": True}},
    )

    assert response.status_code == 428
    assert client.built == []


def test_an_irreversible_operation_needs_this_hosts_id_typed_out(client) -> None:
    body = {
        "operation": "reset",
        "parameters": {"apply": True, "wipe_authority_data": True},
        "confirm": {"acknowledge": True, "host_id": MAC_ID},
    }

    wrong = client.post(f"/api/hosts/{PI_ID}/runs", json=body)
    assert wrong.status_code == 428
    assert PI_ID in wrong.json()["error"]

    body["confirm"]["host_id"] = PI_ID
    right = client.post(f"/api/hosts/{PI_ID}/runs", json=body)
    assert right.status_code == 202


def test_a_read_only_run_needs_no_confirmation_and_reports_its_verdict(client) -> None:
    started = client.post(f"/api/hosts/{PI_ID}/runs", json={"operation": "status"})
    assert started.status_code == 202
    run_id = started.json()["id"]

    stream = client.get(f"/api/runs/{run_id}/events")
    assert stream.status_code == 200
    assert "run.finished" in stream.text

    finished = client.get(f"/api/runs/{run_id}").json()
    assert finished["status"] == "completed"
    assert finished["outcome"] == "observed"
    assert [phase["name"] for phase in finished["phases"]] == ["observe"]


def test_the_event_stream_is_server_sent_events_a_browser_can_follow(client) -> None:
    run_id = client.post(f"/api/hosts/{PI_ID}/runs", json={"operation": "status"}).json()["id"]

    body = client.get(f"/api/runs/{run_id}/events").text
    kinds = [line.removeprefix("event: ") for line in body.splitlines() if line.startswith("event: ")]
    payloads = [
        json.loads(line.removeprefix("data: "))
        for line in body.splitlines()
        if line.startswith("data: ")
    ]

    assert kinds[0] == "run.started"
    assert kinds[-1] == "run.finished"
    assert any("phases" in payload for payload in payloads)


def test_parameters_reach_the_controller_as_the_operation_declared_them(client) -> None:
    client.post(
        f"/api/hosts/{PI_ID}/runs",
        json={"operation": "logs", "parameters": {"service": "eidolon-hub.service", "lines": 40}},
    )
    client.get("/api/runs")  # settle

    operation, parameters = client.built[0].calls[0]
    assert operation == "logs"
    assert parameters == {"service": "eidolon-hub.service", "lines": 40, "since": None}


def test_runs_can_be_listed_for_one_host(client) -> None:
    client.post(f"/api/hosts/{PI_ID}/runs", json={"operation": "status"})
    client.post(f"/api/hosts/{MAC_ID}/runs", json={"operation": "status"})

    listed = client.get(f"/api/runs?host_id={PI_ID}").json()["runs"]

    assert [item["host_id"] for item in listed] == [PI_ID]


def test_unknown_hosts_runs_and_endpoints_are_404(client) -> None:
    assert client.get("/api/hosts/nowhere").status_code == 404
    assert client.get("/api/runs/deadbeef").status_code == 404
    assert client.post(
        f"/api/hosts/{PI_ID}/plan", json={"operation": "sudo", "parameters": {}}
    ).status_code == 404
    assert client.get("/api/nothing-here").status_code == 404


def test_the_page_says_plainly_when_the_interface_has_not_been_built(
    registry: HostRegistry, monkeypatch: pytest.MonkeyPatch
) -> None:
    from eidolon_ops.console import api

    monkeypatch.setattr(api, "STATIC_ROOT", Path("/nonexistent-console-build"))
    unbuilt = TestClient(api.create_app(registry))

    response = unbuilt.get("/")

    assert response.status_code == 503
    assert "npm run build" in response.text


# -- the entry point ----------------------------------------------------------


def test_the_console_binds_loopback_and_nothing_else(
    profiles: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """There is no address flag, and this is the assertion that keeps it that way."""

    from eidolon_ops.console import server

    served: dict[str, object] = {}

    class Uvicorn:
        @staticmethod
        def run(app, **options):
            served.update(options)
            served["app"] = app

    monkeypatch.setitem(__import__("sys").modules, "uvicorn", Uvicorn)

    assert server.main(["--profiles", str(profiles), "--port", "9099"]) == 0
    assert served["host"] == "127.0.0.1"
    assert served["port"] == 9099
    printed = capsys.readouterr().out
    assert "http://127.0.0.1:9099" in printed
    assert MAC_ID in printed and PI_ID in printed


def test_named_profiles_win_over_the_default_directory(profiles: Path) -> None:
    from eidolon_ops.console import server

    arguments = server._parser().parse_args(["--config", str(profiles / "pi.toml")])

    assert server._locations(arguments) == (profiles / "pi.toml",)


def test_a_missing_profile_directory_is_reported_rather_than_crashed(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from eidolon_ops.console import server

    assert server.main(["--profiles", str(tmp_path / "nowhere")]) == 1
    assert "no Host profile" in capsys.readouterr().out


def test_without_the_console_extra_the_operator_is_told_which_command_fixes_it(
    profiles: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from eidolon_ops.console import server

    monkeypatch.setitem(__import__("sys").modules, "uvicorn", None)

    assert server.main(["--profiles", str(profiles)]) == 1
    assert "uv sync --all-extras" in capsys.readouterr().out


# -- registry edges -----------------------------------------------------------


def test_one_named_profile_is_a_valid_console(profiles: Path) -> None:
    assert discover(profiles / "pi.toml") == (profiles / "pi.toml",)


def test_a_missing_location_is_a_404(tmp_path: Path) -> None:
    with pytest.raises(ConsoleError) as refusal:
        discover(tmp_path / "absent")
    assert refusal.value.status == 404


def test_a_host_that_does_not_load_can_be_asked_for_nothing(profiles: Path) -> None:
    (profiles / "broken.toml").write_text("schema_version = 2\n", encoding="utf-8")
    registry = HostRegistry(discover(profiles))

    assert registry.suggestions("broken") == {}
    with pytest.raises(ConsoleError) as refusal:
        registry.profile("broken")
    assert refusal.value.status == 409


def test_a_profile_whose_operations_config_is_unusable_is_still_a_listed_host(
    profiles: Path, tmp_path: Path
) -> None:
    """Readable profile, uncomposable Host: listed, with the reason it is inert."""

    broken_config = tmp_path / "broken-ops.toml"
    broken_config.write_text("schema_version = 1\n", encoding="utf-8")
    profile = (profiles / "pi.toml").read_text(encoding="utf-8")
    (profiles / "pi.toml").write_text(
        profile.split("[adapter]")[0] + f'[adapter]\noperations_config = "{broken_config}"\n',
        encoding="utf-8",
    )
    entry = HostRegistry(discover(profiles)).entry(PI_ID)

    assert entry.profile is not None
    assert entry.capabilities == frozenset()
    assert entry.adapter is None
    assert entry.error is not None
    assert entry.to_json()["app"] is None


def test_a_suggested_path_falls_back_to_the_profiles_own_cache_root(
    profiles: Path, tmp_path: Path
) -> None:
    broken_config = tmp_path / "broken-ops.toml"
    broken_config.write_text("schema_version = 1\n", encoding="utf-8")
    profile = (profiles / "mac.toml").read_text(encoding="utf-8")
    (profiles / "mac.toml").write_text(
        profile.replace(str(profiles.parent / "eidolon-pi.toml"), str(broken_config)).replace(
            f'operations_config = "{profiles.parent / "eidolon-pi.toml"}"',
            f'operations_config = "{broken_config}"',
        ),
        encoding="utf-8",
    )
    registry = HostRegistry(discover(profiles))

    suggested = registry.artifact_default(MAC_ID, "diagnose", ".tar.gz")

    assert str(tmp_path / "cache") in suggested


def test_log_suggestions_survive_a_log_root_that_does_not_exist_yet(
    profiles: Path, tmp_path: Path
) -> None:
    profile = (profiles / "mac.toml").read_text(encoding="utf-8")
    (profiles / "mac.toml").write_text(
        profile.replace(f'log_root = "{tmp_path / "logs"}"', 'log_root = "/nonexistent/logs"'),
        encoding="utf-8",
    )

    assert HostRegistry(discover(profiles)).suggestions(MAC_ID) == {"service": []}


def test_the_page_is_never_cached_and_its_hashed_assets_always_are(client) -> None:
    """A cached page pins the old asset hash, and a rebuild looks ignored.

    This is the failure mode that has no error message: the build lands, the
    browser refreshes, and the console keeps serving what it read last time.
    """

    page = client.get("/")
    assert page.headers["cache-control"] == "no-store"

    referenced = re.search(r"/assets/(index-[A-Za-z0-9_-]+\.js)", page.text)
    if referenced is None:  # the interface has not been built in this checkout
        assert page.status_code == 503
        return
    asset = client.get(f"/assets/{referenced.group(1)}")
    assert asset.status_code == 200
    assert asset.headers["cache-control"] == "public, max-age=31536000, immutable"
