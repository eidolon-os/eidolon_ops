"""What a workstation can ask a component, and what it can ask its own network."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from eidolon_ops import lan_observation, probes
from eidolon_ops.process import ProcessResult

pytestmark = pytest.mark.unit


class Response:
    def __init__(self, status: int, body: bytes) -> None:
        self.status = status
        self._body = body

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def read(self, _size: int) -> bytes:
        return self._body


class Opener:
    def __init__(self, answers: dict[str, Response] | None = None, error: Exception | None = None):
        self.answers = answers or {}
        self.error = error
        self.urls: list[str] = []

    def open(self, url, *, timeout):
        self.urls.append(url)
        if self.error is not None:
            raise self.error
        return self.answers[url]


class Runner:
    def __init__(self, answers: dict[str, str]) -> None:
        self.answers = answers
        self.calls: list[tuple[str, ...]] = []

    def run(self, command, *, input_bytes=None, cwd=None, env=None, timeout=120):
        command = tuple(command)
        self.calls.append(command)
        for marker, output in self.answers.items():
            if marker in " ".join(command):
                return ProcessResult(0, output, "")
        return ProcessResult(0, "", "")


def test_http_json_returns_a_body_only_for_a_json_object(monkeypatch) -> None:
    opener = Opener(
        {
            "http://host/object": Response(200, b'{"agent_name": "eidolon"}'),
            "http://host/list": Response(200, b"[1, 2]"),
            "http://host/invalid": Response(200, b"not-json"),
            "https://host/refused": Response(503, b""),
        }
    )
    monkeypatch.setattr(probes.urllib.request, "build_opener", lambda *_a: opener)

    assert probes.http_json("http://host/object") == {"agent_name": "eidolon"}
    assert probes.http_json("http://host/list") is None
    assert probes.http_json("http://host/invalid") is None
    assert probes.http_json("https://host/refused") is None

    monkeypatch.setattr(
        probes.urllib.request, "build_opener", lambda *_a: Opener(error=OSError("offline"))
    )
    assert probes.http_json("http://host/object") is None


def test_a_worker_reports_its_own_verdict_and_the_agent_it_serves(monkeypatch) -> None:
    """The two facts the Channel worker publishes, read rather than inferred."""

    monkeypatch.setattr(probes, "http_health", lambda _url: {"healthy": True, "http_status": 200})
    monkeypatch.setattr(
        probes,
        "http_json",
        lambda _url: {"agent_name": "eidolon", "worker_type": "JT_PUBLISHER"},
    )

    report = probes.channel_worker_report(8766, agent_name="eidolon")
    assert report["healthy"] is True
    assert report["dispatch_identity"] is True
    assert report["worker_type"] == "JT_PUBLISHER"

    monkeypatch.setattr(probes, "http_json", lambda _url: None)
    unnamed = probes.channel_worker_report(8766, agent_name="eidolon")
    assert unnamed["dispatch_identity"] is False
    assert unnamed["agent_name"] is None

    monkeypatch.setattr(probes, "http_health", lambda _url: {"healthy": False, "http_status": None})
    assert probes.channel_worker_report(8766, agent_name="eidolon")["healthy"] is False


def test_a_probe_is_given_a_bounded_chance_to_settle() -> None:
    attempts: list[int] = []

    def probe() -> dict[str, object]:
        attempts.append(len(attempts))
        return {"healthy": len(attempts) >= 2}

    assert probes.settle(probe, lambda report: bool(report["healthy"]), seconds=5)["healthy"]
    assert len(attempts) == 2

    attempts.clear()
    never = probes.settle(lambda: {"healthy": False}, lambda report: False, seconds=0)
    assert never == {"healthy": False}


def test_tcp_and_unix_probes_report_a_refusal_rather_than_raising(tmp_path: Path) -> None:
    assert probes.tcp_health("127.0.0.1", 1)["healthy"] is False
    assert probes.unix_http_health(tmp_path / "absent.sock")["healthy"] is False


def test_the_address_this_host_answers_on_comes_from_its_default_route() -> None:
    runner = Runner(
        {
            "/sbin/route": "   interface: en0\n",
            "ifconfig en0": "\tinet 192.168.1.25 netmask 0xfffffc00\n",
            "ifconfig": "\tinet 127.0.0.1\n\tinet 192.168.1.25\n",
        }
    )

    assert lan_observation.observed_lan_address(runner) == "192.168.1.25"


def test_a_host_with_no_default_route_falls_back_to_a_routable_address() -> None:
    runner = Runner({"ifconfig": "\tinet 127.0.0.1\n\tinet 10.0.0.4\n"})

    assert lan_observation.interface_addresses(runner) == {"127.0.0.1", "10.0.0.4"}
    assert lan_observation.observed_lan_address(runner) == "10.0.0.4"
    assert lan_observation.observed_lan_address(Runner({}), set()) == ""


def test_a_published_name_is_resolved_rather_than_read_out_of_a_log() -> None:
    runner = Runner({"dscacheutil": "name: host\nip_address: 192.168.1.25\n"})

    assert lan_observation.name_resolves_to(runner, "host.local", "192.168.1.25") is True
    assert lan_observation.name_resolves_to(runner, "host.local", "192.168.1.26") is False
    # No address to compare against is not a passing check.
    assert lan_observation.name_resolves_to(runner, "host.local", "") is False
    assert any(call[0] == "dscacheutil" for call in runner.calls)


def test_livekit_advertises_the_address_it_wrote_down(tmp_path: Path) -> None:
    generated = tmp_path / "livekit.generated.yaml"
    assert lan_observation.livekit_node_ip(None) == ""
    assert lan_observation.livekit_node_ip(generated) == ""
    generated.write_text("rtc:\n  node_ip: 192.168.1.25\n", encoding="utf-8")
    assert lan_observation.livekit_node_ip(generated) == "192.168.1.25"
    generated.write_text("rtc:\n  use_external_ip: false\n", encoding="utf-8")
    assert lan_observation.livekit_node_ip(generated) == ""


def test_the_delivered_agent_carries_every_module_of_the_package() -> None:
    """The injection contract is one payload, so the package has to be in it."""

    from eidolon_ops.hostagent_delivery import PACKAGE, injected_script

    script = injected_script().decode("utf-8")
    encoded = script.split("_MODULES = ", 1)[1]
    modules = json.JSONDecoder().raw_decode(encoded)[0]
    root = Path(__file__).resolve().parents[1] / "src/eidolon_ops/hostagent"
    expected = {
        PACKAGE if path.stem == "__init__" else f"{PACKAGE}.{path.stem}"
        for path in root.glob("*.py")
    }
    assert set(modules) == expected
    assert f"{PACKAGE}.__main__" in modules


def test_the_readiness_ports_do_not_drift_from_the_ops_port_registry() -> None:
    """The registry is the authority; the payload carries two of its values.

    A Host cannot parse YAML, so the readiness payload states these as numbers.
    That copy is only safe while it cannot disagree with the file it came from.
    """

    from eidolon_ops.readiness import CHANNEL_WORKER_PORT, LIVEKIT_SIGNALLING_PORT

    from eidolon_ops.host_layer import _PORT_REGISTRY

    registry = _PORT_REGISTRY.read_text(encoding="utf-8")
    channel = re.search(r"(?ms)^channel:\n  worker:\n    port: (\d+)$", registry)
    livekit = re.search(r"(?ms)^livekit:\n  port: (\d+)$", registry)
    assert channel is not None and livekit is not None
    assert int(channel.group(1)) == CHANNEL_WORKER_PORT
    assert int(livekit.group(1)) == LIVEKIT_SIGNALLING_PORT
