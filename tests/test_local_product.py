from __future__ import annotations

import ssl
from ipaddress import IPv4Address
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from urllib.parse import urlsplit

import pytest

from eidolon_ops import environment as environment_module
from eidolon_ops import local_product as local_product_module
from eidolon_ops import probes, source_assets
from eidolon_ops.environment import EnvironmentFileError
from eidolon_ops.errors import OperationsError
from eidolon_ops.hub_assets import HUB_SETTINGS_TEMPLATE
from eidolon_ops.local_product import LocalProductSource
from eidolon_ops.paths import AppAccess, HostDriver, HostPaths, HostPlatform, HostProfile
from eidolon_ops.probes import http_health as _http_health
from eidolon_ops.probes import unix_http_health as _unix_http_health
from eidolon_ops.process import ProcessResult, SubprocessRunner
from eidolon_ops.source_schema import migrate_data_schema


def _product(tmp_path: Path, *, foundation_mode: str) -> LocalProductSource:
    root = tmp_path / "product"
    profile = HostProfile(
        path=tmp_path / "mac.toml",
        host_id="mac-test",
        platform=HostPlatform.MACOS,
        driver=HostDriver.LOCAL_SUPERVISORD,
        paths=HostPaths(
            install_root=tmp_path / "workspace",
            current_root=tmp_path / "workspace",
            config_root=root / "config",
            state_root=root / "state",
            runtime_root=root / "run",
            log_root=root / "logs",
            cache_root=root / "cache",
            bootstrap_state_root=root / "bootstrap/state",
            bootstrap_runtime_root=root / "bootstrap/run",
        ),
        lifecycle_script=tmp_path / "run_all.sh",
        operations_config=tmp_path / "operations.toml",
        foundation_mode=cast(Any, foundation_mode),
        external_livekit_config=tmp_path / "livekit.yaml",
        app=AppAccess(
            lan_ipv4=IPv4Address("192.168.1.25"),
            hub_https_port=8443,
            livekit_client_url="ws://192.168.1.25:7880",
            allow_insecure_livekit=True,
        ),
    )
    profile.paths.bootstrap_state_root.mkdir(parents=True)
    identity = profile.paths.bootstrap_state_root / "host_identity.ed25519"
    identity.write_bytes(b"i" * 32)
    identity.chmod(0o600)
    return LocalProductSource(profile, cast(Any, None), cast(Any, None))


def test_external_foundation_keeps_existing_nats_and_livekit_ports() -> None:
    rendered = source_assets.translate_ports(
        "nats://127.0.0.1:4222\n"
        "ws://127.0.0.1:7880\n"
        "port: 4222\n"
        "port: 7880\n"
        "http://127.0.0.1:8084\n"
    )

    assert "127.0.0.1:4222" in rendered
    assert "127.0.0.1:7880" in rendered
    assert "port: 4222" in rendered
    assert "port: 7880" in rendered
    assert "127.0.0.1:8084" in rendered
    assert "14222" not in rendered
    assert "17880" not in rendered


def test_external_livekit_credential_import_requires_one_pair(tmp_path: Path) -> None:
    product = _product(tmp_path, foundation_mode="external")
    livekit = cast(Path, product.profile.external_livekit_config)
    livekit.write_text("port: 7880\nkeys:\n  product-key: product-secret\n", encoding="utf-8")

    assert source_assets.external_livekit_credentials(product.profile.external_livekit_config) == {
        "LIVEKIT_API_KEY": "product-key",
        "LIVEKIT_API_SECRET": "product-secret",
    }


def test_eidolond_health_uses_unix_socket(monkeypatch, tmp_path: Path) -> None:
    path = tmp_path / "system.sock"
    calls: list[tuple[str, object]] = []

    class Client:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def settimeout(self, timeout):
            calls.append(("timeout", timeout))

        def connect(self, target):
            calls.append(("connect", target))

        def sendall(self, request):
            calls.append(("send", request))

        def recv(self, size):
            calls.append(("recv", size))
            return b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\n{}"

    monkeypatch.setattr(probes.socket, "socket", lambda *_args: Client())

    assert _unix_http_health(path) == {"healthy": True, "http_status": 200}
    assert ("connect", str(path)) in calls
    assert any(call[0] == "send" and b"GET /health " in call[1] for call in calls)


def test_prepare_materializes_one_canonical_mac_product_contract(
    monkeypatch, tmp_path: Path
) -> None:
    revision = "a" * 40
    source_ids = (
        "eidolon_kernel",
        "eidolon_data",
        "eidolon_hub",
        "eidolon_admin",
        "eidolon_agent",
        "eidolon_channel",
        "eidolon_memory",
        "eidolon_sdk",
    )
    sources = {}
    for source_id in source_ids:
        path = tmp_path / "sources" / source_id
        path.mkdir(parents=True)
        sources[source_id] = SimpleNamespace(path=path, revision=revision)
    alembic = sources["eidolon_data"].path / ".venv/bin/alembic"
    alembic.parent.mkdir(parents=True)
    alembic.write_text("#!/bin/sh\n", encoding="utf-8")
    alembic.chmod(0o755)

    inputs = tmp_path / "inputs"
    inputs.mkdir()
    for name in (
        "data.env",
        "hub.env",
        "kernel.env",
        "admin.env",
        "local-api.env",
        "bootstrap.env",
        "agent.env",
        "memory.env",
    ):
        (inputs / name).write_text("EIDOLON_TEST=value\n", encoding="utf-8")
    for name in ("channel.env", "livekit.env"):
        (inputs / name).write_text(
            "EIDOLON_TEST=value\nLIVEKIT_API_KEY=sealed-key\nLIVEKIT_API_SECRET=sealed-secret\n",
            encoding="utf-8",
        )
    (inputs / "agent.yaml").write_text(
        "http:\n  port: 8180\n  admin_port: 8081\n",
        encoding="utf-8",
    )
    (inputs / "channel.yaml").write_text(
        "livekit_url: ws://127.0.0.1:7880\n",
        encoding="utf-8",
    )
    (inputs / "memory.yaml").write_text(
        "nats:\n  url: nats://127.0.0.1:4222\n",
        encoding="utf-8",
    )
    (inputs / "host_identity.ed25519").write_bytes(b"i" * 32)

    profile = _product(tmp_path, foundation_mode="external").profile
    livekit = cast(Path, profile.external_livekit_config)
    livekit.write_text("keys:\n  shared-key: shared-secret\n", encoding="utf-8")
    config = SimpleNamespace(
        sources=sources,
        install_files={"data_env": inputs / "data.env"},
    )

    class Runner:
        def __init__(self) -> None:
            self.calls: list[tuple[str, ...]] = []

        def run(self, command, **_kwargs):
            command = tuple(command)
            self.calls.append(command)
            if command[-2:] == ("rev-parse", "HEAD"):
                return ProcessResult(0, revision + "\n", "")
            if "show" in command:
                target = command[-1]
                if target.endswith("config/channel-provider.yaml"):
                    return ProcessResult(
                        0,
                        "server:\n  host: 127.0.0.1\n  port: 8767\n"
                        "livekit:\n  api_url: http://127.0.0.1:7880\n",
                        "",
                    )
                source, path = command[command.index("-C") + 1], target.partition(":")[2]
                if (Path(source).name, path) == HUB_SETTINGS_TEMPLATE:
                    # A source run renders the same template a Host is sent, out
                    # of Hub's own pinned commit — including the state root Hub
                    # states as a variable and this profile resolves.
                    return ProcessResult(
                        0,
                        "onboarding:\n  hub_id: eidolon-hub-local\n"
                        "  public_base_url: https://eidolon-hub.local\n"
                        "persistence:\n  path: $EIDOLON_STATE_ROOT/hub/eidolon-hub.sqlite3\n",
                        "",
                    )
                return ProcessResult(0, "service: product\n", "")
            return ProcessResult(0, "", "")

    runner = Runner()
    monkeypatch.setattr(
        local_product_module, "validate_install_input_contract", lambda *_a, **_k: None
    )
    product = LocalProductSource(profile, cast(Any, config), runner)

    def create_test_tls_identity() -> None:
        tls = profile.paths.config_root / "tls"
        tls.mkdir(parents=True, exist_ok=True)
        for name in ("hub.crt", "hub.key"):
            path = tls / name
            path.write_text("test identity\n", encoding="utf-8")
            path.chmod(0o600)

    monkeypatch.setattr(product, "_ensure_hub_tls_identity", create_test_tls_identity)

    result = product.prepare()

    assert result["status"] == "prepared"
    root = profile.paths.config_root
    environment = (root / "product-source.env").read_text(encoding="utf-8")
    assert "EIDOLON_ADMIN_API_PORT=9000\n" in environment
    assert "EIDOLON_PRODUCT_DATA_PORT=8084\n" in environment
    assert "18084" not in environment
    assert "NATS (external)" in (root / "settings/services.yaml").read_text(encoding="utf-8")
    assert "port: 8767" in (root / "settings/channel-provider.yaml").read_text(encoding="utf-8")
    assert f"path: {profile.paths.state_root}/hub/eidolon-hub.sqlite3" in (
        root / "settings/hub.yaml"
    ).read_text(encoding="utf-8")
    assert (root / "env/channel.env").read_text(encoding="utf-8").count(
        "LIVEKIT_API_KEY=shared-key"
    ) == 1
    identity = profile.paths.bootstrap_state_root / "host_identity.ed25519"
    assert identity.read_bytes() == b"i" * 32
    assert any(call[0] == str(alembic) for call in runner.calls)

    (inputs / "host_identity.ed25519").write_bytes(b"n" * 32)
    assert product.prepare()["status"] == "prepared"
    assert identity.read_bytes() == b"i" * 32


def test_hub_tls_identity_is_generated_validated_and_reused(tmp_path: Path) -> None:
    product = _product(tmp_path, foundation_mode="external")
    tls = product.profile.paths.config_root / "tls"
    tls.mkdir(parents=True)
    product.runner = SubprocessRunner()

    product._ensure_hub_tls_identity()

    certificate = tls / "hub.crt"
    private_key = tls / "hub.key"
    assert certificate.is_file()
    assert private_key.is_file()
    assert certificate.stat().st_mode & 0o777 == 0o600
    assert private_key.stat().st_mode & 0o777 == 0o600
    decoded = ssl._ssl._test_decode_cert(str(certificate))
    assert ("DNS", product._host_lan_identity().hub_hostname) in decoded["subjectAltName"]
    original = certificate.read_bytes(), private_key.read_bytes()

    product._ensure_hub_tls_identity()
    assert (certificate.read_bytes(), private_key.read_bytes()) == original


def test_hub_tls_identity_fails_closed_for_partial_or_invalid_files(tmp_path: Path) -> None:
    product = _product(tmp_path, foundation_mode="external")
    tls = product.profile.paths.config_root / "tls"
    tls.mkdir(parents=True)
    certificate = tls / "hub.crt"
    private_key = tls / "hub.key"
    certificate.write_text("partial", encoding="utf-8")
    with pytest.raises(OperationsError, match="incomplete"):
        product._ensure_hub_tls_identity()

    private_key.write_text("invalid", encoding="utf-8")
    product._ensure_hub_tls_identity()
    decoded = ssl._ssl._test_decode_cert(str(certificate))
    assert ("DNS", product._host_lan_identity().hub_hostname) in decoded["subjectAltName"]


def test_product_health_uses_canonical_endpoints(monkeypatch, tmp_path: Path) -> None:
    product = _product(tmp_path, foundation_mode="external")
    urls: list[str] = []

    def healthy(url: str) -> dict[str, object]:
        urls.append(url)
        return {"healthy": True, "http_status": 200}

    monkeypatch.setattr(probes, "http_health", healthy)
    monkeypatch.setattr(
        probes,
        "unix_http_health",
        lambda _path: {"healthy": True, "http_status": 200},
    )

    result = product.health()

    assert result["status"] == "healthy"
    assert "http://127.0.0.1:8084/health" in urls
    assert "http://127.0.0.1:8019/api/admin/health" not in urls
    assert "http://127.0.0.1:8020/api/discovery/agent-routing" in urls
    assert "http://127.0.0.1:8767/health" in urls
    assert not {18019, 18020, 18084, 18767, 19000}.intersection(
        {urlsplit(url).port for url in urls}
    )


def test_app_ready_requires_device_reachable_contract(monkeypatch, tmp_path: Path) -> None:
    product = _product(tmp_path, foundation_mode="external")

    class InterfaceRunner:
        def run(self, command, **_kwargs):
            command = tuple(command)
            if command[:2] == ("/sbin/route", "-n"):
                return ProcessResult(0, "   interface: en0\n", "")
            if command[0] == "dscacheutil":
                return ProcessResult(0, "ip_address: 192.168.1.25\n", "")
            assert command[0] == "ifconfig"
            return ProcessResult(0, "en0: flags\n\tinet 192.168.1.25 netmask 0xffffff00\n", "")

    product.runner = InterfaceRunner()
    root = product.profile.paths.config_root
    (root / "env").mkdir(parents=True)
    (root / "settings").mkdir(parents=True)
    product.profile.paths.log_root.joinpath("admin").mkdir(parents=True)
    certificate = root / "tls/hub.crt"
    identity = product._host_lan_identity()
    origin = identity.hub_origin(8443)
    product._ensure_hub_tls_identity()
    (root / "env/local-api.env").write_text(
        f"EIDOLON_LOCAL_API_HUB_ID={identity.hub_id}\n"
        "EIDOLON_LOCAL_API_HUB_DESCRIPTOR_URI="
        f"{origin}/api/device-onboarding/v1/descriptor\n"
        f"EIDOLON_LOCAL_API_HUB_TLS_CERTIFICATE={certificate}\n",
        encoding="utf-8",
    )
    (root / "env/channel.env").write_text(
        "EIDOLON_LIVEKIT_CLIENT_URL=ws://192.168.1.25:7880\n"
        "EIDOLON_CHANNEL_PROVIDER_ALLOW_INSECURE_LAN_CLIENT_URL=1\n",
        encoding="utf-8",
    )
    (root / "settings/hub.yaml").write_text(
        f"onboarding:\n  hub_id: {identity.hub_id}\n  public_base_url: {origin}\n",
        encoding="utf-8",
    )
    external_livekit = cast(Path, product.profile.external_livekit_config)
    external_livekit.write_text("rtc:\n  node_ip: 192.168.1.25\n", encoding="utf-8")
    product.profile.paths.log_root.joinpath("admin/local-api-mdns.log").write_text(
        "Got a reply for service Eidolon Local API: Name now registered and active\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(product, "health", lambda: {"status": "healthy"})
    monkeypatch.setattr(
        probes,
        "http_health",
        lambda _url: {"healthy": True, "http_status": 200},
    )
    monkeypatch.setattr(probes, "tcp_health", lambda *_a: {"healthy": True})
    monkeypatch.setattr(
        probes,
        "channel_worker_report",
        lambda _port, *, agent_name: {
            "healthy": True,
            "http_status": 200,
            "agent_name": agent_name,
            "worker_type": "JT_PUBLISHER",
            "dispatch_identity": True,
            "expected_agent_name": agent_name,
        },
    )

    result = product.app_ready()

    assert result["status"] == "app_ready"
    assert all(result["checks"].values())

    product.runner = SimpleNamespace(
        run=lambda *_a, **_k: ProcessResult(0, "inet 172.16.20.211\n", "")
    )
    degraded = product.app_ready()
    assert degraded["status"] == "degraded"
    assert degraded["checks"]["lan_address_observed"] is False


def test_generated_environment_helpers_reject_ambiguous_inputs() -> None:
    assert environment_module.serialize_ops({"EIDOLON_OK": "value"}) == "EIDOLON_OK=value\n"
    with pytest.raises(EnvironmentFileError, match="profile key"):
        environment_module.serialize_ops({"NOT_PRODUCT": "value"})
    with pytest.raises(EnvironmentFileError, match="value is unsafe"):
        environment_module.serialize_ops({"EIDOLON_BAD": "line\nbreak"})

    ops_key = environment_module.OPS_KEY
    assert environment_module.parse("\nEIDOLON_ONE=1\nEIDOLON_TWO=2\n", key=ops_key) == {
        "EIDOLON_ONE": "1",
        "EIDOLON_TWO": "2",
    }
    with pytest.raises(EnvironmentFileError, match="env file"):
        environment_module.parse(
            "EIDOLON_ONE=1\nEIDOLON_ONE=2\n", label="generated env file", key=ops_key
        )

    service_key = environment_module.SERVICE_KEY
    assert environment_module.parse(
        "LIVEKIT_API_KEY=key\nEIDOLON_ONE=1\n", key=service_key
    ) == {"LIVEKIT_API_KEY": "key", "EIDOLON_ONE": "1"}
    with pytest.raises(EnvironmentFileError, match="service env file"):
        environment_module.parse(
            "lowercase=value\n", label="generated service env file", key=service_key
        )

    assert environment_module.replace("A=old\nB=kept\n", {"A": "new"}) == "A=new\nB=kept\n"
    with pytest.raises(EnvironmentFileError, match="environment is invalid"):
        environment_module.replace("A=one\nA=two\n", {})
    with pytest.raises(EnvironmentFileError, match="lacks MISSING"):
        environment_module.replace("A=one\n", {"MISSING": "value"})


def test_external_livekit_and_migration_fail_closed(tmp_path: Path) -> None:
    product = _product(tmp_path, foundation_mode="external")
    livekit = cast(Path, product.profile.external_livekit_config)
    with pytest.raises(OperationsError, match="missing or unsafe"):
        source_assets.external_livekit_credentials(product.profile.external_livekit_config)
    livekit.write_text("keys:\n  one: secret-one\n  two: secret-two\n", encoding="utf-8")
    with pytest.raises(OperationsError, match="exactly one"):
        source_assets.external_livekit_credentials(product.profile.external_livekit_config)
    livekit.write_text("keys:\n  ok: short\n", encoding="utf-8")
    with pytest.raises(OperationsError, match="shape"):
        source_assets.external_livekit_credentials(product.profile.external_livekit_config)

    data = tmp_path / "data"
    data.mkdir()
    product.config = cast(
        Any, SimpleNamespace(sources={"eidolon_data": SimpleNamespace(path=data)})
    )
    with pytest.raises(OperationsError, match="migration entrypoint"):
        migrate_data_schema(product.profile, product.config, product.runner)


def test_http_health_reports_success_and_transport_failure(monkeypatch) -> None:
    class Response:
        status = 204

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

    class Opener:
        def __init__(self, *, fail: bool = False) -> None:
            self.fail = fail

        def open(self, _url, *, timeout):
            assert timeout == 1.5
            if self.fail:
                raise OSError("unreachable")
            return Response()

    monkeypatch.setattr(probes.urllib.request, "build_opener", lambda *_a: Opener())
    assert _http_health("https://127.0.0.1/health") == {
        "healthy": False,
        "http_status": 204,
    }
    monkeypatch.setattr(
        probes.urllib.request,
        "build_opener",
        lambda *_a: Opener(fail=True),
    )
    assert _http_health("http://127.0.0.1/health") == {
        "healthy": False,
        "http_status": None,
    }


def test_unix_health_rejects_transport_and_malformed_response(monkeypatch, tmp_path: Path) -> None:
    class Client:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def settimeout(self, _timeout):
            return None

        def connect(self, _target):
            return None

        def sendall(self, _request):
            return None

        def recv(self, _size):
            return b"not-http"

    monkeypatch.setattr(probes.socket, "socket", lambda *_a: Client())
    assert _unix_http_health(tmp_path / "socket") == {
        "healthy": False,
        "http_status": None,
    }

    class FailingClient(Client):
        def connect(self, _target):
            raise OSError("missing")

    monkeypatch.setattr(probes.socket, "socket", lambda *_a: FailingClient())
    assert _unix_http_health(tmp_path / "missing") == {
        "healthy": False,
        "http_status": None,
    }


def test_product_source_revision_and_generated_inputs_fail_closed(
    monkeypatch, tmp_path: Path
) -> None:
    product = _product(tmp_path, foundation_mode="external")
    missing = tmp_path / "missing-source"
    product.config = cast(
        Any,
        SimpleNamespace(sources={"eidolon_data": SimpleNamespace(path=missing, revision="a" * 40)}),
    )
    with pytest.raises(OperationsError, match="worktree is missing"):
        product._validate_exact_worktrees()

    source = tmp_path / "source"
    source.mkdir()

    class DriftRunner:
        def run(self, _command, **_kwargs):
            return ProcessResult(0, "b" * 40 + "\n", "")

    product.config = cast(
        Any,
        SimpleNamespace(sources={"eidolon_data": SimpleNamespace(path=source, revision="a" * 40)}),
    )
    product.runner = DriftRunner()
    with pytest.raises(OperationsError, match="release pin"):
        product._validate_exact_worktrees()
    with pytest.raises(OperationsError, match="revision drifted"):
        product._read_exact_file("eidolon_data", "b" * 40, "config.yaml")

    product.config = cast(Any, SimpleNamespace(sources={}))
    with pytest.raises(OperationsError, match="inputs are missing"):
        product.validate()

    monkeypatch.setattr(
        local_product_module,
        "validate_install_input_contract",
        lambda *_a, **_k: (_ for _ in ()).throw(
            local_product_module.InstallInputError("sealed input rejected")
        ),
    )
    with pytest.raises(OperationsError, match="sealed input rejected"):
        product.prepare()
