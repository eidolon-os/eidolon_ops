from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from urllib.parse import urlsplit

import pytest

from eidolon_ops import local_product as local_product_module
from eidolon_ops.controller import OperationsError
from eidolon_ops.local_product import (
    LocalProductSource,
    _http_health,
    _read_environment_file,
    _replace_environment_values,
    _serialize_plain_environment,
    _unix_http_health,
)
from eidolon_ops.paths import HostPaths, HostProfile
from eidolon_ops.process import ProcessResult


def _product(tmp_path: Path, *, foundation_mode: str) -> LocalProductSource:
    root = tmp_path / "product"
    profile = HostProfile(
        path=tmp_path / "mac.toml",
        host_id="mac-test",
        platform="macos",
        driver="local-supervisord",
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
    )
    return LocalProductSource(profile, cast(Any, None), cast(Any, None))


def test_external_foundation_keeps_existing_nats_and_livekit_ports(tmp_path: Path) -> None:
    product = _product(tmp_path, foundation_mode="external")

    rendered = product._translate_ports(
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

    assert product._external_livekit_credentials() == {
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

    monkeypatch.setattr("eidolon_ops.local_product.socket.socket", lambda *_args: Client())

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
    assert (root / "env/channel.env").read_text(encoding="utf-8").count(
        "LIVEKIT_API_KEY=shared-key"
    ) == 1
    identity = profile.paths.bootstrap_state_root / "host_identity.ed25519"
    assert identity.read_bytes() == b"i" * 32
    assert any(call[0] == str(alembic) for call in runner.calls)

    (inputs / "host_identity.ed25519").write_bytes(b"n" * 32)
    assert product.prepare()["status"] == "prepared"
    assert identity.read_bytes() == b"i" * 32


def test_product_health_uses_canonical_endpoints(monkeypatch, tmp_path: Path) -> None:
    product = _product(tmp_path, foundation_mode="external")
    urls: list[str] = []

    def healthy(url: str) -> dict[str, object]:
        urls.append(url)
        return {"healthy": True, "http_status": 200}

    monkeypatch.setattr(local_product_module, "_http_health", healthy)
    monkeypatch.setattr(
        local_product_module,
        "_unix_http_health",
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


def test_generated_environment_helpers_reject_ambiguous_inputs(tmp_path: Path) -> None:
    assert _serialize_plain_environment({"EIDOLON_OK": "value"}) == "EIDOLON_OK=value\n"
    with pytest.raises(OperationsError, match="profile key"):
        _serialize_plain_environment({"NOT_PRODUCT": "value"})
    with pytest.raises(OperationsError, match="profile value"):
        _serialize_plain_environment({"EIDOLON_BAD": "line\nbreak"})

    environment = tmp_path / "service.env"
    environment.write_text("\nEIDOLON_ONE=1\nEIDOLON_TWO=2\n", encoding="utf-8")
    assert _read_environment_file(environment) == {"EIDOLON_ONE": "1", "EIDOLON_TWO": "2"}
    environment.write_text("EIDOLON_ONE=1\nEIDOLON_ONE=2\n", encoding="utf-8")
    with pytest.raises(OperationsError, match="env file"):
        _read_environment_file(environment)

    assert _replace_environment_values("A=old\nB=kept\n", {"A": "new"}) == ("A=new\nB=kept\n")
    with pytest.raises(OperationsError, match="environment is invalid"):
        _replace_environment_values("A=one\nA=two\n", {})
    with pytest.raises(OperationsError, match="lacks MISSING"):
        _replace_environment_values("A=one\n", {"MISSING": "value"})


def test_external_livekit_and_migration_fail_closed(tmp_path: Path) -> None:
    product = _product(tmp_path, foundation_mode="external")
    livekit = cast(Path, product.profile.external_livekit_config)
    with pytest.raises(OperationsError, match="missing or unsafe"):
        product._external_livekit_credentials()
    livekit.write_text("keys:\n  one: secret-one\n  two: secret-two\n", encoding="utf-8")
    with pytest.raises(OperationsError, match="exactly one"):
        product._external_livekit_credentials()
    livekit.write_text("keys:\n  ok: short\n", encoding="utf-8")
    with pytest.raises(OperationsError, match="shape"):
        product._external_livekit_credentials()

    data = tmp_path / "data"
    data.mkdir()
    product.config = cast(
        Any, SimpleNamespace(sources={"eidolon_data": SimpleNamespace(path=data)})
    )
    with pytest.raises(OperationsError, match="migration entrypoint"):
        product._migrate_data_schema()


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

    monkeypatch.setattr(local_product_module.urllib.request, "build_opener", lambda *_a: Opener())
    assert _http_health("https://127.0.0.1/health") == {
        "healthy": False,
        "http_status": 204,
    }
    monkeypatch.setattr(
        local_product_module.urllib.request,
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

    monkeypatch.setattr(local_product_module.socket, "socket", lambda *_a: Client())
    assert _unix_http_health(tmp_path / "socket") == {
        "healthy": False,
        "http_status": None,
    }

    class FailingClient(Client):
        def connect(self, _target):
            raise OSError("missing")

    monkeypatch.setattr(local_product_module.socket, "socket", lambda *_a: FailingClient())
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
