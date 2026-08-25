from __future__ import annotations

import json
import ssl
import stat
from collections.abc import Mapping
from dataclasses import dataclass, field
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
from eidolon_ops.owner_domain_assets import OwnerDomainAssets
from eidolon_ops.paths import AppAccess, HostDriver, HostPaths, HostPlatform, HostProfile
from eidolon_ops.probes import http_health as _http_health
from eidolon_ops.probes import unix_http_health as _unix_http_health
from eidolon_ops.process import ProcessResult, SubprocessRunner
from eidolon_ops.source_schema import migrate_data_schema


@dataclass(frozen=True)
class _FakeSource:
    path: Path
    revision: str | None = None
    tag: str | None = None


@dataclass(frozen=True)
class _FakeConfig:
    """Dataclasses, not namespaces: resolution hands the settings overlay a
    configuration with the shipped commits filled in, and that is a `replace`."""

    sources: Mapping[str, _FakeSource]
    install_files: Mapping[str, Path] = field(default_factory=dict)


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
        (path / ".git").mkdir()
        # No revision written down: a source run reads which commit its own
        # checkout is on, the same way the release path now does.
        sources[source_id] = _FakeSource(path=path)
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
    config = _FakeConfig(sources=sources, install_files={"data_env": inputs / "data.env"})

    class Runner:
        def __init__(self) -> None:
            self.calls: list[tuple[str, ...]] = []

        def run(self, command, **_kwargs):
            command = tuple(command)
            self.calls.append(command)
            if command[-2:] == ("rev-parse", "HEAD"):
                return ProcessResult(0, revision + "\n", "")
            if command[-2:] == ("branch", "--show-current"):
                return ProcessResult(0, "main\n", "")
            if "status" in command and "--porcelain" in command:
                return ProcessResult(0, "", "")
            if "rev-parse" in command and command[-1].endswith("^{commit}"):
                return ProcessResult(0, command[-1].removesuffix("^{commit}") + "\n", "")
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
                        "onboarding:\n  owner_domain_id: owner-local\n"
                        "  owner_domain_generation: 1\n"
                        "  descriptor_uri: https://eidolon-hub.local/api/device-onboarding/v1/descriptor\n"
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
    # The key pair travels install inputs -> env files -> the server Ops starts.
    # It used to be read back out of whatever LiveKit config happened to be on
    # disk, which made a clean Host unpreparable and let a months-old file decide
    # what Channel and Hub authenticated with.
    channel_env = (root / "env/channel.env").read_text(encoding="utf-8")
    assert channel_env.count("LIVEKIT_API_KEY=sealed-key") == 1
    assert "shared-key" not in channel_env
    assert "LIVEKIT_API_KEY=sealed-key\n" in (root / "env/livekit.env").read_text(
        encoding="utf-8"
    )
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
    product.profile.paths.config_root.chmod(0o700)
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


def test_owner_domain_private_material_fails_closed_for_partial_files(tmp_path: Path) -> None:
    product = _product(tmp_path, foundation_mode="external")
    material = product._owner_material_root()
    material.mkdir(mode=0o700, parents=True)
    certificate = material / "owner-domain-root-ca.pem"
    certificate.write_text("partial", encoding="utf-8")
    certificate.chmod(0o600)
    with pytest.raises(OperationsError, match="incomplete"):
        product._ensure_hub_tls_identity()


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
    root.chmod(0o700)
    product.profile.paths.log_root.joinpath("admin").mkdir(parents=True)
    identity = product._host_lan_identity()
    origin = identity.hub_origin(8443)
    product._ensure_hub_tls_identity()
    owner_domain_id = product._owner_domain_id()
    (root / "env/local-api.env").write_text(
        f"EIDOLON_LOCAL_API_OWNER_DOMAIN_ID={owner_domain_id}\n"
        "EIDOLON_LOCAL_API_OWNER_DOMAIN_DESCRIPTOR_URI="
        f"{origin}/api/device-onboarding/v1/descriptor\n"
        f"EIDOLON_LOCAL_API_OWNER_DOMAIN_DESCRIPTOR={product._owner_descriptor_path()}\n"
        f"EIDOLON_LOCAL_API_OWNER_ROOT_CERTIFICATE={product._owner_root_certificate_path()}\n"
        f"EIDOLON_LOCAL_API_AUTHORITY_SIGNING_CERTIFICATE={product._authority_signing_certificate_path()}\n",
        encoding="utf-8",
    )
    (root / "env/channel.env").write_text(
        "EIDOLON_LIVEKIT_CLIENT_URL=ws://192.168.1.25:7880\n"
        "EIDOLON_CHANNEL_PROVIDER_ALLOW_INSECURE_LAN_CLIENT_URL=1\n",
        encoding="utf-8",
    )
    (root / "settings/hub.yaml").write_text(
        f"onboarding:\n  owner_domain_id: {owner_domain_id}\n"
        "  owner_domain_generation: 1\n"
        f"  descriptor_uri: {origin}/api/device-onboarding/v1/descriptor\n",
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


class _WorktreeRunner:
    """A git that answers for a worktree sitting on one commit."""

    def __init__(self, head: str) -> None:
        self.head = head

    def run(self, command, **_kwargs):
        command = tuple(command)
        if command[-2:] == ("branch", "--show-current"):
            return ProcessResult(0, "main\n", "")
        if "status" in command and "--porcelain" in command:
            return ProcessResult(0, "", "")
        if command[-1].endswith("^{commit}"):
            return ProcessResult(0, command[-1].removesuffix("^{commit}") + "\n", "")
        if "rev-list" in command:
            return ProcessResult(0, "0\t4\n", "")
        return ProcessResult(0, self.head + "\n", "")


def _one_source(path: Path, revision: str | None) -> Any:
    return cast(Any, _FakeConfig(sources={"eidolon_data": _FakeSource(path, revision)}))


def test_a_source_run_refuses_a_checkout_it_cannot_read(tmp_path: Path) -> None:
    product = _product(tmp_path, foundation_mode="external")
    product.config = _one_source(tmp_path / "missing-source", None)

    with pytest.raises(OperationsError, match="worktree is missing"):
        product._validate_exact_worktrees()


def test_a_source_run_ships_whatever_its_worktree_is_on(tmp_path: Path) -> None:
    source = tmp_path / "source"
    (source / ".git").mkdir(parents=True)
    product = _product(tmp_path, foundation_mode="external")
    product.config = _one_source(source, None)
    product.runner = cast(Any, _WorktreeRunner("b" * 40))

    product._validate_exact_worktrees()

    assert product._source_revisions() == {"eidolon_data": "b" * 40}


def test_a_source_run_refuses_a_pin_that_is_not_what_it_would_run(tmp_path: Path) -> None:
    """The one HEAD-equals-pin check that carries information.

    A source run starts processes out of the worktree, so a pinned commit that
    is not the worktree's HEAD does not describe what is running — it
    contradicts it. On the release path the same comparison was two copies of a
    declaration, and requiring them to agree was pure tax.
    """

    source = tmp_path / "source"
    (source / ".git").mkdir(parents=True)
    product = _product(tmp_path, foundation_mode="external")
    product.config = _one_source(source, "a" * 40)
    product.runner = cast(Any, _WorktreeRunner("b" * 40))

    with pytest.raises(OperationsError, match="must be its HEAD"):
        product._validate_exact_worktrees()


def test_product_source_revision_and_generated_inputs_fail_closed(
    monkeypatch, tmp_path: Path
) -> None:
    source = tmp_path / "source"
    (source / ".git").mkdir(parents=True)
    product = _product(tmp_path, foundation_mode="external")
    product.config = _one_source(source, None)
    product.runner = cast(Any, _WorktreeRunner("b" * 40))

    # Reading a file at a revision this run did not resolve must fail: the
    # release matrix and the settings overlay both read by revision, and a
    # silent mismatch there is a Host configured from the wrong commit.
    with pytest.raises(OperationsError, match="revision drifted"):
        product._read_exact_file("eidolon_data", "a" * 40, "config.yaml")

    # A profile with no sources at all still has to fail on its own inputs
    # rather than on resolution; the memo is dropped so the new config is read.
    product.config = cast(Any, _FakeConfig(sources={}))
    product._sources = None
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


def test_owner_domain_material_reaches_the_host_including_hub_bootstrap(tmp_path: Path) -> None:
    """Every byte the issuer produces is placed, and Hub's capability with it.

    ``authority-bootstrap.json`` is what lets Hub initialize an empty authority
    database. The product install has always sent it; this path issued it and
    dropped it, so a source run could start Hub from neither an empty database
    (no capability) nor a stale one (no migrations). Both failures happened at
    Hub startup, long after prepare reported success.
    """

    product = _product(tmp_path, foundation_mode="external")
    product.profile.paths.state_root.mkdir(parents=True, exist_ok=True)

    product._ensure_hub_tls_identity()

    bootstrap = product._authority_bootstrap_path()
    assert bootstrap.is_file()
    assert stat.S_IMODE(bootstrap.stat().st_mode) == 0o600
    document = json.loads(bootstrap.read_text(encoding="utf-8"))
    assert document["operation"] == "owner-authority.bootstrap"
    assert document["owner_domain_id"] == product._owner_domain_id()
    assert document["owner_domain_generation"] == product._owner_domain_generation()
    assert document["state_id"].startswith("authority-state_")
    # The generated Hub settings must name this same file, or Hub looks
    # somewhere else and the capability is placed for nobody.
    assert str(bootstrap) == str(
        product.profile.paths.state_root / "hub/authority-bootstrap.json"
    )


def test_a_new_owner_domain_asset_cannot_be_silently_dropped(tmp_path: Path) -> None:
    """The completeness rule, not the current list, is what prevents recurrence."""

    product = _product(tmp_path, foundation_mode="external")
    with pytest.raises(OperationsError, match="issued but never placed"):
        product._require_every_owner_domain_asset_is_placed(
            OwnerDomainAssets(
                owner_domain_id="owner-x",
                owner_domain_generation=1,
                authority_state_id="authority-state_x",
                bootstrap_pending=True,
                authority_bootstrap=b"{}",
                descriptor=b"{}",
                owner_root_certificate=b"pem",
                authority_signing_certificate=b"pem",
                tls_certificate=b"pem",
                tls_private_key=b"pem",
            ),
            {"descriptor": tmp_path / "descriptor.json"},
        )


def _with_sources(product: LocalProductSource, tmp_path: Path) -> Path:
    """Give the product a source set, which a reset has to prove it cannot reach."""

    worktree = tmp_path / "workspace/eidolon_kernel"
    worktree.mkdir(parents=True, exist_ok=True)
    product.config = cast(Any, _FakeConfig(sources={"eidolon_kernel": _FakeSource(path=worktree)}))
    return worktree

def test_reset_clears_what_it_generated_and_keeps_what_it_did_not(tmp_path: Path) -> None:
    product = _product(tmp_path, foundation_mode="external")
    _with_sources(product, tmp_path)
    paths = product.profile.paths
    for directory in (
        paths.config_root / "env",
        paths.config_root / "settings",
        paths.config_root / "owner-domain",
        paths.runtime_root / "ops",
        paths.state_root / "hub",
        paths.log_root,
        paths.bootstrap_state_root,
    ):
        directory.mkdir(parents=True, exist_ok=True)
    (paths.config_root / "product-source.env").write_text("EIDOLON_X=1\n", encoding="utf-8")
    paths.config_root.chmod(0o700)
    product._ensure_hub_tls_identity()
    owner_before = product._owner_domain_id()
    identity = paths.bootstrap_state_root / "host_identity.ed25519"
    (paths.state_root / "hub/eidolon-hub.sqlite3").write_text("db", encoding="utf-8")
    (paths.log_root / "keep.log").write_text("log", encoding="utf-8")

    planned = product.reset(wipe_authority_data=False, apply=False)
    assert planned["status"] == "planned"
    assert (paths.config_root / "env").is_dir()

    applied = product.reset(wipe_authority_data=False, apply=True)

    assert applied["status"] == "reset"
    assert not (paths.config_root / "env").exists()
    assert not (paths.config_root / "settings").exists()
    assert not (paths.config_root / "owner-domain").exists()
    assert not (paths.config_root / "product-source.env").exists()
    assert not paths.runtime_root.exists()
    # Authority data, logs and the Host identity are a different question.
    assert (paths.state_root / "hub/eidolon-hub.sqlite3").is_file()
    assert (paths.log_root / "keep.log").is_file()
    assert identity.is_file()
    # And the Owner root key stays, so the same Owner Domain comes back rather
    # than a new one every enrolled device would fail to recognize.
    assert product._owner_material_root().is_dir()
    product._ensure_hub_tls_identity()
    assert product._owner_domain_id() == owner_before


def test_reset_adds_the_authority_data_only_when_asked(tmp_path: Path) -> None:
    product = _product(tmp_path, foundation_mode="external")
    _with_sources(product, tmp_path)
    paths = product.profile.paths
    (paths.state_root / "hub").mkdir(parents=True, exist_ok=True)
    database = paths.state_root / "hub/eidolon-hub.sqlite3"
    database.write_text("db", encoding="utf-8")
    product._ensure_hub_tls_identity()
    owner_before = product._owner_domain_id()

    plan = product.reset(wipe_authority_data=True, apply=False)
    assert str(paths.state_root) in plan["targets"]
    assert str(paths.state_root) not in plan["kept"]

    product.reset(wipe_authority_data=True, apply=True)

    assert not database.exists()
    # This is the recovery for a Host behind the schema, and it must not also
    # retire the Owner: prepare has to come back to the same domain.
    product._ensure_hub_tls_identity()
    assert product._owner_domain_id() == owner_before


def test_reset_refuses_to_reach_code_or_anything_outside_the_profile(tmp_path: Path) -> None:
    """The assertion, not the current target list, is what keeps this safe.

    A source run's roots live inside the workspace that holds the eight
    checkouts, so "under the install root" is true of everything and cannot be
    the test. What must hold is that no target is a worktree, a parent of one,
    or a root the profile is anchored on.
    """

    product = _product(tmp_path, foundation_mode="external")
    worktree = _with_sources(product, tmp_path)
    paths = product.profile.paths

    for target in (
        paths.install_root,
        paths.current_root,
        Path(worktree),
        Path(worktree).parent,
        product._owner_material_root(),
        paths.bootstrap_state_root,
    ):
        with pytest.raises(OperationsError, match="does not own|outside this profile"):
            product._require_removable([target])

    with pytest.raises(OperationsError, match="outside this profile"):
        product._require_removable([tmp_path / "somewhere-else"])
