"""The product Host's side of the one readiness contract."""

from __future__ import annotations

import json
import socket
import subprocess
import tempfile
from pathlib import Path

import pytest

from eidolon_ops import readiness
from eidolon_ops.host_identity import derive_host_lan_identity
from eidolon_ops.hostagent import app_contract, contract, primitives, probe
from eidolon_ops.hostagent.primitives import TargetError
from eidolon_ops.owner_domain_assets import ensure_owner_domain_assets
from eidolon_ops.readiness import (
    HostKind,
    describe_failures,
    expected_facts,
    product_payload,
)


def _app() -> dict[str, object]:
    identity = derive_host_lan_identity(b"a" * 32)
    return {
        "host_id": identity.host_id,
        "owner_domain_id": "owner-0123456789abcdefabcd",
        "hub_hostname": identity.hub_hostname,
        "hub_https_port": 8443,
        "hub_origin": identity.hub_origin(8443),
        "lan_ipv4": "192.168.100.15",
        "livekit_client_url": "ws://192.168.100.15:7880",
        "allow_insecure_livekit": True,
    }


def _payload(app: dict[str, object]) -> dict[str, object]:
    return {
        "units": list(contract.PRODUCT_UNITS),
        "readiness": product_payload(),
        "app": app,
    }


def _materialize(monkeypatch, root: Path, app: dict[str, object]) -> None:
    """Point the agent's fixed Host paths at a directory a test can own."""

    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    root.chmod(0o700)
    identity = derive_host_lan_identity(b"a" * 32)
    owner = ensure_owner_domain_assets(root / "owner-private", identity, 8443)
    (root / "hub.crt").write_bytes(owner.tls_certificate)
    (root / "hub.key").write_bytes(owner.tls_private_key)
    (root / "owner.json").write_bytes(owner.descriptor)
    (root / "owner.pem").write_bytes(owner.owner_root_certificate)
    (root / "signer.pem").write_bytes(owner.authority_signing_certificate)
    (root / "hub.yaml").write_text(
        f"onboarding:\n  owner_domain_id: {app['owner_domain_id']}\n"
        f"  descriptor_uri: {app['hub_origin']}/api/device-onboarding/v1/descriptor\n",
        encoding="utf-8",
    )
    (root / "local-api.env").write_text(
        f"EIDOLON_LOCAL_API_OWNER_DOMAIN_ID={app['owner_domain_id']}\n"
        f"EIDOLON_LOCAL_API_OWNER_DOMAIN_DESCRIPTOR_URI={app['hub_origin']}"
        "/api/device-onboarding/v1/descriptor\n"
        f"EIDOLON_LOCAL_API_OWNER_DOMAIN_DESCRIPTOR={root / 'owner.json'}\n"
        f"EIDOLON_LOCAL_API_OWNER_ROOT_CERTIFICATE={root / 'owner.pem'}\n"
        f"EIDOLON_LOCAL_API_AUTHORITY_SIGNING_CERTIFICATE={root / 'signer.pem'}\n",
        encoding="utf-8",
    )
    (root / "channel.env").write_text(
        f"EIDOLON_LIVEKIT_CLIENT_URL={app['livekit_client_url']}\n"
        "EIDOLON_CHANNEL_PROVIDER_ALLOW_INSECURE_LAN_CLIENT_URL=1\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(probe, "HUB_SETTINGS", root / "hub.yaml")
    monkeypatch.setattr(probe, "HUB_CERTIFICATE", root / "hub.crt")
    monkeypatch.setattr(probe, "HUB_PRIVATE_KEY", root / "hub.key")
    monkeypatch.setattr(probe, "OWNER_DESCRIPTOR", root / "owner.json")
    monkeypatch.setattr(probe, "OWNER_ROOT_CERTIFICATE", root / "owner.pem")
    monkeypatch.setattr(probe, "AUTHORITY_SIGNING_CERTIFICATE", root / "signer.pem")
    monkeypatch.setattr(probe, "LOCAL_API_ENV", root / "local-api.env")
    monkeypatch.setattr(probe, "CHANNEL_ENV", root / "channel.env")
    monkeypatch.setattr(probe, "MDNS_DEFINITION", root / "hub.yaml")
    monkeypatch.setattr(probe, "AVAHI_STATIC_HOSTS", root / "avahi-hosts")
    (root / "avahi-hosts").write_text(
        f"{app['lan_ipv4']} {app['hub_hostname']}\n", encoding="utf-8"
    )


@pytest.fixture
def bootstrap_socket(monkeypatch):
    """A real control socket; the path has to stay short enough to bind."""

    with tempfile.TemporaryDirectory(prefix="eo-", dir="/tmp") as directory:
        path = Path(directory) / "control.sock"
        listener = socket.socket(socket.AF_UNIX)
        listener.bind(str(path))
        monkeypatch.setattr(probe, "BOOTSTRAP_SOCKET", path)
        yield path
        listener.close()


@pytest.fixture(autouse=True)
def lifecycle_workflow_socket(monkeypatch):
    """A real removal-workflow socket, so the healthy path is genuinely healthy."""

    with tempfile.TemporaryDirectory(prefix="eo-lw-", dir="/tmp") as directory:
        path = Path(directory) / "workflow.sock"
        listener = socket.socket(socket.AF_UNIX)
        listener.bind(str(path))
        monkeypatch.setattr(probe, "LIFECYCLE_WORKFLOW_SOCKET", path)
        yield path
        listener.close()


def _healthy_run(app: dict[str, object]):
    hub_record = (
        f"=;wlan0;IPv4;{app['owner_domain_id']};_eidolon-owner._tcp;local;"
        f"{app['hub_hostname']};{app['lan_ipv4']};8443;"
        f'"descriptor_uri={app["hub_origin"]}/api/device-onboarding/v1/descriptor"\n'
    )
    local_api_record = (
        "=;wlan0;IPv4;Eidolon Local API;_eidolon-local-api._tcp;local;"
        f"eidolon-pi5.local;{app['lan_ipv4']};9002;\"scheme=https\"\n"
    )

    def run(command, **_kwargs):
        program = command[0]
        if program.endswith("avahi-resolve-host-name"):
            output = f"{app['hub_hostname']}\t{app['lan_ipv4']}\n"
        elif program.endswith("avahi-browse"):
            output = hub_record if command[-1] == "_eidolon-owner._tcp" else local_api_record
        elif program.endswith("ip"):
            output = f"2: wlan0 inet {app['lan_ipv4']}/24 brd\n"
        elif program.endswith("ss"):
            output = '127.0.0.1:1 127.0.0.1:7880 users:(("python",pid=4242,fd=9))\n'
        else:
            output = json.dumps({"ok": True})
        return subprocess.CompletedProcess(command, 0, output, "")

    return run


def test_the_declared_check_set_is_the_same_on_both_sides() -> None:
    """The agent may only attest the contract the operator publishes."""

    assert expected_facts(HostKind.PRODUCT) == probe.READINESS_FACTS
    assert set(probe.SETUP_COMPLETABLE_STATES) == (
        readiness.HOST_SETUP_COMPLETABLE_STATES
    )
    assert (
        probe.SETUP_READINESS_SETTLE_SECONDS
        == readiness.SETUP_READINESS_SETTLE_SECONDS
    )
    assert product_payload()["channel_worker"] == {
        "port": readiness.CHANNEL_WORKER_PORT,
        "agent_name": readiness.CHANNEL_AGENT_NAME,
        "livekit_port": readiness.LIVEKIT_SIGNALLING_PORT,
        "settle_seconds": readiness.DEFAULT_CHANNEL_SETTLE_SECONDS,
    }


def test_owner_trust_readiness_consumes_the_host_application_security_contract(
    monkeypatch,
) -> None:
    observed: list[tuple[Path, int, str, str]] = []
    monkeypatch.setattr(
        primitives,
        "private_file_check",
        lambda path, mode, user, group: observed.append((path, mode, user, group))
        or {"healthy": True},
    )

    for name, path in (
        ("owner-domain-descriptor.json", probe.OWNER_DESCRIPTOR),
        ("owner-domain-root-ca.pem", probe.OWNER_ROOT_CERTIFICATE),
        ("authority-signing-certificate.pem", probe.AUTHORITY_SIGNING_CERTIFICATE),
    ):
        probe._host_application_file_check(name, path)

    assert observed == [
        (
            path,
            contract.HOST_APPLICATION_INPUTS[name][3],
            contract.HOST_APPLICATION_INPUTS[name][1],
            contract.HOST_APPLICATION_INPUTS[name][2],
        )
        for name, path in (
            ("owner-domain-descriptor.json", probe.OWNER_DESCRIPTOR),
            ("owner-domain-root-ca.pem", probe.OWNER_ROOT_CERTIFICATE),
            ("authority-signing-certificate.pem", probe.AUTHORITY_SIGNING_CERTIFICATE),
        )
    ]


def test_the_worker_window_is_the_host_readiness_budget() -> None:
    """An activation restarts LiveKit under the worker.

    The Host is given one budget for coming back; a second, smaller one owned
    here is how a healthy release gets rolled back — which is exactly what it
    did, twenty seconds being less than this board spends loading models.
    """

    assert product_payload(settle_seconds=240)["channel_worker"]["settle_seconds"] == 240


def test_readiness_payload_fails_closed_on_a_different_check_set() -> None:
    app = _app()
    with pytest.raises(TargetError, match="readiness contract is missing"):
        probe.app_ready({"units": list(contract.PRODUCT_UNITS), "app": app})

    payload = _payload(app)
    payload["readiness"] = {
        **product_payload(),
        "facts": list(probe.READINESS_FACTS[:-1]),
    }
    with pytest.raises(TargetError, match="differs from the reviewed check set"):
        probe.app_ready(payload)

    payload = _payload(app)
    payload["readiness"] = {
        **product_payload(),
        "channel_worker": {
            "port": 0,
            "agent_name": "eidolon",
            "livekit_port": 7880,
            "settle_seconds": 1,
        },
    }
    with pytest.raises(TargetError, match="Channel worker readiness contract"):
        probe.app_ready(payload)


def _healthy_local_api(path: str) -> dict[str, object]:
    """What the Local API on a Host nobody has broken answers.

    One copy, because two tests spoiling different facts both need every other
    answer to be the healthy one, and a route added to the probe has to reach
    both of them or the fact it attests silently reads as broken everywhere.
    """

    if path == "/healthz":
        return {"status": "ok", "bootstrap": "ready"}
    if path == "/api/local/v1/setup/readiness":
        return {
            "contract_version": "1",
            "operation_id": "06607258-a650-5570-8c91-880e8f2fb9a9",
            "state": "ready",
        }
    return {
        "contract_version": "1",
        "host_id": "ehost-0123456789abcdefabcd",
        "host_public_key_fingerprint": "sha256:test",
        "ble_service_uuid": "123e4567-e89b-42d3-a456-426614174000",
    }


def _healthy_probe(monkeypatch, app: dict[str, object], tmp_path: Path) -> None:
    """Every readiness input reporting health, so one test can spoil exactly one."""

    _materialize(monkeypatch, tmp_path / "host", app)
    monkeypatch.setattr(primitives, "private_file_check", lambda *_a, **_k: {"healthy": True})
    monkeypatch.setattr(
        primitives, "unit_status",
        lambda _unit: {"ActiveState": "active", "SubState": "running"},
    )
    monkeypatch.setattr(primitives, "run", _healthy_run(app))
    monkeypatch.setattr(primitives, "tcp_reachable", lambda *_a: True)
    monkeypatch.setattr(probe, "unit_processes", lambda _unit: {4242})
    monkeypatch.setattr(probe, "hub_tls_matches", lambda _hostname: True)
    monkeypatch.setattr(
        probe, "channel_worker_report",
        lambda worker: {
            "healthy": True,
            "http_status": 200,
            "agent_name": worker["agent_name"],
            "worker_type": "JT_PUBLISHER",
            "dispatch_identity": True,
            "expected_agent_name": worker["agent_name"],
        },
    )

    monkeypatch.setattr(probe, "local_api_json", _healthy_local_api)
    monkeypatch.setattr(
        primitives, "https_json_endpoint",
        lambda _host, _port, path, *, label: (
            {"status": "ok"}
            if path == "/health"
            else {
                "owner_domain_id": app["owner_domain_id"],
                "directory_revision": 1,
                "signature": "A" * 86,
            }
        ),
    )
    monkeypatch.setattr(
        primitives, "https_json",
        lambda _host, _port, _path: (200, {"status": "ready"}),
    )


def test_app_ready_attests_every_declared_fact(
    monkeypatch, tmp_path: Path, bootstrap_socket: Path
) -> None:
    app = _app()
    _healthy_probe(monkeypatch, app, tmp_path)

    result = probe.app_ready(_payload(app))

    assert tuple(result["checks"]) == expected_facts(HostKind.PRODUCT)
    assert [n for n, v in result["checks"].items() if not v] == []
    assert result["status"] == "app_ready"
    assert result["channel_worker"]["livekit_link"]["linked_processes"] == [4242]


def test_a_hub_that_cannot_admit_a_device_fails_the_gate(
    monkeypatch, tmp_path: Path, bootstrap_socket: Path
) -> None:
    """Every other fact reads healthy and no device can be added.

    A Hub with no commissioning proof verifier answers its own readiness with
    503 and names the reason, but nothing else about the Host looks wrong — so
    a release that dropped the verifier passed this gate and ran for hours
    admitting nothing.
    """

    app = _app()
    _materialize(monkeypatch, tmp_path / "host", app)
    monkeypatch.setattr(primitives, "private_file_check", lambda *_a, **_k: {"healthy": True})
    monkeypatch.setattr(
        primitives, "unit_status",
        lambda _unit: {"ActiveState": "active", "SubState": "running"},
    )
    monkeypatch.setattr(primitives, "run", _healthy_run(app))
    monkeypatch.setattr(primitives, "tcp_reachable", lambda *_a: True)
    monkeypatch.setattr(probe, "unit_processes", lambda _unit: {4242})
    monkeypatch.setattr(probe, "hub_tls_matches", lambda _hostname: True)
    monkeypatch.setattr(
        probe, "channel_worker_report",
        lambda worker: {
            "healthy": True,
            "http_status": 200,
            "agent_name": worker["agent_name"],
            "worker_type": "JT_PUBLISHER",
            "dispatch_identity": True,
            "expected_agent_name": worker["agent_name"],
        },
    )
    monkeypatch.setattr(probe, "local_api_json", _healthy_local_api)
    monkeypatch.setattr(
        primitives, "https_json_endpoint",
        lambda _host, _port, path, *, label: (
            {"status": "ok"}
            if path == "/health"
            else {
                "owner_domain_id": app["owner_domain_id"],
                "directory_revision": 1,
                "signature": "A" * 86,
            }
        ),
    )
    monkeypatch.setattr(
        primitives, "https_json",
        lambda _host, _port, _path: (
            503,
            {"status": "not-ready", "reason": "commissioning-proof-verifier-unavailable"},
        ),
    )

    result = probe.app_ready(_payload(app))

    assert result["status"] != "app_ready"
    assert [n for n, v in result["checks"].items() if not v] == ["hub_admits_devices"]


def test_a_worker_that_cannot_take_a_job_fails_the_gate(
    monkeypatch, tmp_path: Path, bootstrap_socket: Path
) -> None:
    """The unit is active, the port accepts, and the Host is still not ready.

    This is the shape of the real failure: everything a port probe can see was
    green while LiveKit could not hand the worker a job.
    """

    app = _app()
    _materialize(monkeypatch, tmp_path / "host", app)
    monkeypatch.setattr(primitives, "private_file_check", lambda *_a, **_k: {"healthy": True})
    monkeypatch.setattr(
        primitives, "unit_status",
        lambda _unit: {"ActiveState": "active", "SubState": "running"},
    )
    monkeypatch.setattr(primitives, "run", _healthy_run(app))
    monkeypatch.setattr(primitives, "tcp_reachable", lambda *_a: True)
    monkeypatch.setattr(probe, "hub_tls_matches", lambda _hostname: True)
    monkeypatch.setattr(probe, "local_api_json", lambda _path: {"status": "ok"})
    monkeypatch.setattr(
        primitives, "https_json_endpoint", lambda *_a, **_k: {"status": "ok"}
    )
    # The worker answers nothing at all: a wedged event loop still holds an
    # accepting socket, which is why the unit and the port both looked fine.
    monkeypatch.setattr(primitives, "http_json", lambda *_a: (None, None))
    # And it is not attached to LiveKit, so no job request could reach it.
    monkeypatch.setattr(probe, "unit_processes", lambda _unit: {99})

    payload = _payload(app)
    payload["readiness"] = {
        **product_payload(),
        "channel_worker": {**product_payload()["channel_worker"], "settle_seconds": 0},
    }
    result = probe.app_ready(payload)

    assert result["status"] == "degraded"
    assert result["checks"]["channel_worker_healthy"] is False
    assert result["checks"]["channel_worker_dispatch_identity"] is False
    assert result["checks"]["channel_worker_livekit_link"] is False
    assert result["checks"]["backend_healthy"] is True


def test_channel_worker_report_reads_the_workers_own_surface(monkeypatch) -> None:
    answers = {
        "/": (200, {}),
        "/worker": (200, {"agent_name": "eidolon", "worker_type": "JT_PUBLISHER"}),
    }
    monkeypatch.setattr(primitives, "http_json", lambda _h, _p, path: answers[path])

    report = probe.channel_worker_report(
        {"port": 8766, "agent_name": "eidolon", "livekit_port": 7880, "settle_seconds": 0}
    )
    assert report["healthy"] is True
    assert report["dispatch_identity"] is True

    answers["/worker"] = (200, {"agent_name": "someone-else"})
    assert (
        probe.channel_worker_report(
            {"port": 8766, "agent_name": "eidolon", "livekit_port": 7880, "settle_seconds": 0}
        )["dispatch_identity"]
        is False
    )


def test_livekit_link_matches_the_units_own_processes(monkeypatch) -> None:
    monkeypatch.setattr(probe, "unit_processes", lambda _unit: {10, 11})
    monkeypatch.setattr(
        primitives, "run",
        lambda *_a, **_k: subprocess.CompletedProcess(
            (), 0, 'users:(("python",pid=11,fd=9))\nusers:(("other",pid=77,fd=3))\n', ""
        ),
    )
    worker = {"port": 8766, "agent_name": "eidolon", "livekit_port": 7880, "settle_seconds": 0}

    assert probe.channel_worker_livekit_link(worker) == {
        "healthy": True,
        "linked_processes": [11],
        "unit_processes": [10, 11],
    }

    monkeypatch.setattr(probe, "unit_processes", lambda _unit: {10})
    assert probe.channel_worker_livekit_link(worker)["healthy"] is False


def test_target_environment_reader_rejects_missing_and_ambiguous_files(tmp_path: Path) -> None:
    with pytest.raises(TargetError, match="unreadable"):
        primitives.environment_values(tmp_path / "missing.env")
    invalid = tmp_path / "invalid.env"
    invalid.write_text("NOT-AN-ENV-LINE\n", encoding="utf-8")
    with pytest.raises(TargetError, match="invalid"):
        primitives.environment_values(invalid)
    valid = tmp_path / "valid.env"
    valid.write_text("\nKEY=value\n", encoding="utf-8")
    assert primitives.environment_values(valid) == {"KEY": "value"}
    assert primitives.environment_values_or_empty(tmp_path / "missing.env") == {}


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("host_id", "bad", "Host ID"),
        ("owner_domain_id", "owner-local", "Owner Domain ID"),
        ("hub_https_port", 0, "not Host-bound"),
        ("lan_ipv4", "not-an-ip", "LAN address"),
        ("lan_ipv4", "127.0.0.1", "private IPv4"),
        ("livekit_client_url", "http://bad", "LiveKit"),
        ("livekit_client_url", "ws://192.168.100.15:99999", "LiveKit"),
        ("livekit_client_url", "ws://192.168.100.16:7880", "LiveKit"),
        ("allow_insecure_livekit", False, "LiveKit"),
    ],
)
def test_target_rejects_non_host_bound_application_payload(
    field: str, value: object, message: str
) -> None:
    app = _app()
    app[field] = value

    with pytest.raises(TargetError, match=message):
        app_contract.fixed_app({"app": app})


def test_target_rejects_missing_application_payload() -> None:
    with pytest.raises(TargetError, match="missing or malformed"):
        app_contract.fixed_app({})


def test_two_waiting_probes_share_one_window_rather_than_taking_one_each(
    monkeypatch,
) -> None:
    """The bug that made a degraded Host look like a dead connection.

    Each waiting probe used to be handed the whole settle window. With an
    unhealthy worker, two of them at 240 seconds ran to 480 while the
    operator's side gave up at 300 — so what came back was a timeout, which
    says nothing about the Host, instead of a report naming the failed check.
    """

    ticks = iter(range(0, 10_000))
    monkeypatch.setattr(primitives.time, "monotonic", lambda: next(ticks))
    monkeypatch.setattr(primitives.time, "sleep", lambda _seconds: None)

    budget = primitives.Budget(10)
    first = budget.remaining()
    primitives.settle(lambda: {"healthy": False}, lambda _report: False, seconds=first)
    second = budget.remaining()
    primitives.settle(lambda: {"healthy": False}, lambda _report: False, seconds=second)

    # The first probe waited out the window, so the second gets what is left:
    # nothing. Under the old code it would have been handed another full one,
    # and the two together would have outlasted the operator's deadline.
    assert first > 0
    assert second == 0.0
    assert budget.exhausted()


def test_the_operators_deadline_is_derived_from_the_hosts_budget() -> None:
    from eidolon_ops.readiness import (
        DEFAULT_CHANNEL_SETTLE_SECONDS,
        READINESS_TRANSPORT_TIMEOUT_SECONDS,
    )

    # Written down separately, these drift: raising the Host's budget without
    # raising the deadline turns every slow-but-honest report into a timeout.
    assert READINESS_TRANSPORT_TIMEOUT_SECONDS > DEFAULT_CHANNEL_SETTLE_SECONDS


def test_a_slow_app_ready_says_which_check_spent_the_time(
    monkeypatch, tmp_path: Path, bootstrap_socket: Path
) -> None:
    """Otherwise the operator is given a number they cannot act on.

    "app-ready took four minutes" is a stopwatch reading. "it waited on the
    channel worker" names the thing to go look at.
    """

    app = _app()
    _materialize(monkeypatch, tmp_path / "host", app)
    monkeypatch.setattr(primitives, "private_file_check", lambda *_a, **_k: {"healthy": True})
    monkeypatch.setattr(
        primitives, "unit_status",
        lambda _unit: {"ActiveState": "active", "SubState": "running"},
    )
    monkeypatch.setattr(primitives, "run", _healthy_run(app))
    monkeypatch.setattr(primitives, "tcp_reachable", lambda *_a: True)
    monkeypatch.setattr(probe, "hub_tls_matches", lambda _hostname: True)
    monkeypatch.setattr(probe, "local_api_json", lambda _path: {"status": "ok"})
    monkeypatch.setattr(
        primitives, "https_json_endpoint", lambda *_a, **_k: {"status": "ok"}
    )
    monkeypatch.setattr(primitives, "http_json", lambda *_a: (None, None))
    monkeypatch.setattr(probe, "unit_processes", lambda _unit: {99})

    payload = _payload(app)
    payload["readiness"] = {
        **product_payload(),
        "channel_worker": {**product_payload()["channel_worker"], "settle_seconds": 0},
    }
    result = probe.app_ready(payload)

    waiting = result["waiting"]
    assert waiting["budget_seconds"] == 0
    assert set(waiting["spent_on"]) == {
        "channel_worker",
        "channel_worker_livekit_link",
    }
    assert waiting["exhausted"] is True


def test_a_host_that_cannot_remove_a_device_is_not_ready(
    monkeypatch, tmp_path: Path, bootstrap_socket: Path, lifecycle_workflow_socket: Path
) -> None:
    """Removal is the one operation with no second way to reach it.

    The lifecycle workflow runs as its own uid precisely because the network-
    facing Local API must not be able to revoke a device by itself. That
    separation is real, and it means a workflow that is not listening removes
    the capability from the product entirely — which is what happened: the unit
    crash-looped at boot on a missing constructor argument, device removal was
    unavailable for a whole session, and the readiness gate stayed green because
    nothing here had ever asked.
    """

    app = _app()
    _healthy_probe(monkeypatch, app, tmp_path)
    assert probe.app_ready(_payload(app))["status"] == "app_ready"

    lifecycle_workflow_socket.unlink()
    result = probe.app_ready(_payload(app))
    assert result["status"] != "app_ready"
    assert [n for n, v in result["checks"].items() if not v] == ["device_removal_available"]


@pytest.mark.parametrize(
    ("answer", "ready"),
    [
        ({"contract_version": "1", "state": "ready"}, True),
        ({"contract_version": "1", "state": "absent"}, True),
        # Could not be asked is not a check that passed.
        ({"contract_version": "1", "state": "unknown"}, False),
        ({"state": "ready"}, False),
        (None, False),
    ],
)
def test_both_kinds_of_host_grade_one_setup_answer_the_same_way(
    answer: object, ready: bool
) -> None:
    """The rule the workstation applies and the rule the agent applies.

    Written once and copied onto the Host, like the check set itself, so this
    is where the copy is held to it.
    """

    assert readiness.setup_is_completable(answer) is ready
    # Both probes report the same shape, so a `degraded` says which failure it
    # was: a Host whose halves disagree and one whose Local API predates the
    # route both score false, and only the evidence tells them apart.
    evidence = readiness.setup_readiness_evidence(answer)
    assert evidence["healthy"] is ready
    if isinstance(answer, dict):
        assert evidence["state"] == answer.get("state")
        assert (
            answer.get("contract_version") == "1"
            and answer.get("state") in probe.SETUP_COMPLETABLE_STATES
        ) is ready
    else:
        assert evidence["state"] == "unknown"
        assert "restarted rather than repaired" in evidence["error"]


def test_a_host_no_phone_can_finish_setting_up_is_not_ready(
    monkeypatch, tmp_path: Path, bootstrap_socket: Path, lifecycle_workflow_socket: Path
) -> None:
    """Twelve services healthy, and nobody can use the product.

    A real Host reported app-ready with every unit active while no phone could
    finish setting it up. The units are not the fact setup depends on, so this
    asks the component that owns that fact, and a Host whose answer cannot be
    obtained fails the gate rather than being scored on the checks that could.
    """

    app = _app()
    _healthy_probe(monkeypatch, app, tmp_path)
    monkeypatch.setattr(
        probe,
        "local_api_json",
        lambda path: (
            {
                "contract_version": "1",
                "operation_id": "06607258-a650-5570-8c91-880e8f2fb9a9",
                "state": "unknown",
            }
            if path == "/api/local/v1/setup/readiness"
            else _healthy_local_api(path)
        ),
    )

    result = probe.app_ready(_payload(app))

    assert result["status"] == "degraded"
    assert [n for n, v in result["checks"].items() if not v] == [
        "host_setup_completable"
    ]
    assert result["setup"]["state"] == "unknown"
    assert "host_setup_completable" in describe_failures(result["checks"])


def test_the_states_this_gate_grades_are_the_states_admin_publishes() -> None:
    """The vocabulary is the Local API's; this repository only grades it.

    A gate that scores an enum another repository owns, with nothing linking
    the two, is the same shape as the defect it was added for: two halves that
    can disagree while both look right. A state added to the contract has to
    be given a verdict here, and a state graded here that Admin cannot produce
    is a verdict nobody will ever read.
    """

    contract = (
        Path(__file__).resolve().parents[2]
        / "eidolon_admin/contracts/local-api/v1/setup-readiness.schema.json"
    )
    if not contract.exists():
        pytest.skip("needs the sibling eidolon_admin repository")
    published = set(
        json.loads(contract.read_text(encoding="utf-8"))["properties"]["state"]["enum"]
    )

    assert published >= readiness.HOST_SETUP_COMPLETABLE_STATES
    # Every published state is decided, not merely the passing ones. There
    # were three failing states once; ``orphaned`` went when Bootstrap stopped
    # keeping a copy of what the Data plane held, because nothing could
    # produce a disagreement any more. This assertion is what noticed.
    assert published - readiness.HOST_SETUP_COMPLETABLE_STATES == {"unknown"}


@pytest.mark.parametrize(
    ("state", "settled"),
    [("ready", True), ("absent", True), ("unknown", False)],
)
def test_only_an_unanswerable_setup_check_is_worth_asking_again(
    state: str, settled: bool
) -> None:
    """A broken Host's report must not be the slow one.

    ``absent`` is a Host nobody has set up, and it is a settled fact about it.
    Settling on ``healthy`` instead would spend the whole retry window on
    every Host awaiting its first setup and arrive at the same answer.
    """

    assert readiness.setup_answer_is_settled({"state": state}) is settled
