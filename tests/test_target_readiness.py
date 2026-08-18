"""The product Host's side of the one readiness contract."""

from __future__ import annotations

import json
import socket
import subprocess
import tempfile
from pathlib import Path

import pytest

from eidolon_ops import readiness
from eidolon_ops.host_identity import derive_host_lan_identity, generate_hub_tls_identity
from eidolon_ops.hostagent import app_contract, contract, primitives, probe
from eidolon_ops.hostagent.primitives import TargetError
from eidolon_ops.readiness import HostKind, expected_facts, product_payload


def _app() -> dict[str, object]:
    identity = derive_host_lan_identity(b"a" * 32)
    return {
        "host_id": identity.host_id,
        "hub_id": identity.hub_id,
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

    root.mkdir(parents=True, exist_ok=True)
    identity = derive_host_lan_identity(b"a" * 32)
    certificate, private_key = generate_hub_tls_identity(identity)
    (root / "hub.crt").write_bytes(certificate)
    (root / "hub.key").write_bytes(private_key)
    (root / "hub.yaml").write_text(
        f"onboarding:\n  hub_id: {app['hub_id']}\n  public_base_url: {app['hub_origin']}\n",
        encoding="utf-8",
    )
    (root / "local-api.env").write_text(
        f"EIDOLON_LOCAL_API_HUB_ID={app['hub_id']}\n"
        f"EIDOLON_LOCAL_API_HUB_DESCRIPTOR_URI={app['hub_origin']}"
        "/api/device-onboarding/v1/descriptor\n"
        f"EIDOLON_LOCAL_API_HUB_TLS_CERTIFICATE={root / 'hub.crt'}\n",
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
    monkeypatch.setattr(probe, "LOCAL_API_ENV", root / "local-api.env")
    monkeypatch.setattr(probe, "CHANNEL_ENV", root / "channel.env")
    monkeypatch.setattr(probe, "MDNS_DEFINITION", root / "hub.yaml")


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


def _healthy_run(app: dict[str, object]):
    hub_record = (
        f"=;wlan0;IPv4;{app['hub_id']};_eidolon-hub._tcp;local;"
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
            output = hub_record if command[-1] == "_eidolon-hub._tcp" else local_api_record
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
    assert product_payload()["channel_worker"] == {
        "port": readiness.CHANNEL_WORKER_PORT,
        "agent_name": readiness.CHANNEL_AGENT_NAME,
        "livekit_port": readiness.LIVEKIT_SIGNALLING_PORT,
        "settle_seconds": readiness.DEFAULT_CHANNEL_SETTLE_SECONDS,
    }


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


def test_app_ready_attests_every_declared_fact(
    monkeypatch, tmp_path: Path, bootstrap_socket: Path
) -> None:
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

    def https(_path: str) -> dict[str, object]:
        if _path == "/healthz":
            return {"status": "ok", "bootstrap": "ready"}
        return {
            "contract_version": "1",
            "host_id": "ehost-0123456789abcdefabcd",
            "host_public_key_fingerprint": "sha256:test",
            "ble_service_uuid": "123e4567-e89b-42d3-a456-426614174000",
        }

    monkeypatch.setattr(probe, "local_api_json", https)
    monkeypatch.setattr(
        primitives, "https_json_endpoint",
        lambda _host, _port, path, *, label: (
            {"status": "ok"}
            if path == "/health"
            else {
                "hub_id": app["hub_id"],
                "descriptor_uri": app["hub_origin"] + "/api/device-onboarding/v1/descriptor",
            }
        ),
    )

    result = probe.app_ready(_payload(app))

    assert tuple(result["checks"]) == expected_facts(HostKind.PRODUCT)
    assert [n for n, v in result["checks"].items() if not v] == []
    assert result["status"] == "app_ready"
    assert result["channel_worker"]["livekit_link"]["linked_processes"] == [4242]


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
        ("hub_id", "eidolon-hub-local", "not Host-bound"),
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
