from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from eidolon_ops import target_agent
from eidolon_ops.host_identity import (
    derive_host_lan_identity,
    generate_hub_tls_identity,
)
from eidolon_ops.target_agent import TargetError


def _app() -> dict[str, object]:
    identity = derive_host_lan_identity(b"a" * 32)
    origin = identity.hub_origin(8443)
    return {
        "host_id": identity.host_id,
        "hub_id": identity.hub_id,
        "hub_hostname": identity.hub_hostname,
        "hub_https_port": 8443,
        "hub_origin": origin,
        "lan_ipv4": "192.168.100.15",
        "livekit_client_url": "ws://192.168.100.15:7880",
        "allow_insecure_livekit": True,
    }


def _materialize(root: Path, app: dict[str, object]) -> None:
    config = root / "etc/eidolon"
    (config / "generated").mkdir(parents=True)
    (config / "tls").mkdir()
    identity = derive_host_lan_identity(b"a" * 32)
    certificate, private_key = generate_hub_tls_identity(identity)
    (config / "tls/hub.crt").write_bytes(certificate)
    (config / "tls/hub.key").write_bytes(private_key)
    (config / "generated/hub.yaml").write_text(
        f"onboarding:\n  hub_id: {app['hub_id']}\n  public_base_url: {app['hub_origin']}\n",
        encoding="utf-8",
    )
    (config / "local-api.env").write_text(
        f"EIDOLON_LOCAL_API_HUB_ID={app['hub_id']}\n"
        f"EIDOLON_LOCAL_API_HUB_DESCRIPTOR_URI={app['hub_origin']}"
        "/api/device-onboarding/v1/descriptor\n"
        "EIDOLON_LOCAL_API_HUB_TLS_CERTIFICATE=/etc/eidolon/tls/hub.crt\n",
        encoding="utf-8",
    )
    (config / "channel.env").write_text(
        f"EIDOLON_LIVEKIT_CLIENT_URL={app['livekit_client_url']}\n"
        "EIDOLON_CHANNEL_PROVIDER_ALLOW_INSECURE_LAN_CLIENT_URL=1\n",
        encoding="utf-8",
    )


def test_target_host_application_gate_proves_identity_tls_lan_and_mdns(
    monkeypatch, tmp_path: Path
) -> None:
    app = _app()
    _materialize(tmp_path, app)
    monkeypatch.setattr(
        target_agent,
        "_private_file_check",
        lambda *_args, **_kwargs: {"healthy": True},
    )

    def https(_host: str, _port: int, path: str, *, label: str):
        assert label.startswith("Hub LAN")
        if path == "/health":
            return {"status": "ok"}
        return {
            "hub_id": app["hub_id"],
            "descriptor_uri": app["hub_origin"] + "/api/device-onboarding/v1/descriptor",
        }

    def run(command, **_kwargs):
        if "avahi-resolve-host-name" in command[0]:
            return subprocess.CompletedProcess(
                command, 0, f"{app['hub_hostname']}\t{app['lan_ipv4']}\n", ""
            )
        return subprocess.CompletedProcess(
            command,
            0,
            f"=;wlan0;IPv4;{app['hub_id']};_eidolon-hub._tcp;local;"
            f"{app['hub_hostname']};{app['lan_ipv4']};8443;"
            f'"descriptor_uri={app["hub_origin"]}/api/device-onboarding/v1/descriptor"\n',
            "",
        )

    monkeypatch.setattr(target_agent, "_https_json_endpoint", https)
    monkeypatch.setattr(target_agent, "_run", run)

    result = target_agent._host_application_ready(app, root=tmp_path)

    assert result["healthy"] is True
    assert all(result["checks"].values())
    assert result["resolution"] == ["192.168.100.15"]


def test_target_host_application_gate_returns_evidence_when_unreachable(
    monkeypatch, tmp_path: Path
) -> None:
    app = _app()
    _materialize(tmp_path, app)
    monkeypatch.setattr(
        target_agent,
        "_private_file_check",
        lambda *_args, **_kwargs: {"healthy": False},
    )
    monkeypatch.setattr(
        target_agent,
        "_https_json_endpoint",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(TargetError("offline")),
    )
    monkeypatch.setattr(
        target_agent,
        "_run",
        lambda command, **_kwargs: subprocess.CompletedProcess(command, 2, "", "missing"),
    )

    result = target_agent._host_application_ready(app, root=tmp_path)

    assert result["healthy"] is False
    assert result["checks"]["hub_lan_health"] is False
    assert result["checks"]["mdns_contract"] is False


def test_target_host_application_gate_rejects_invalid_certificate_and_environment(
    monkeypatch, tmp_path: Path
) -> None:
    app = _app()
    _materialize(tmp_path, app)
    (tmp_path / "etc/eidolon/tls/hub.crt").write_text("invalid", encoding="utf-8")
    (tmp_path / "etc/eidolon/local-api.env").write_text("invalid", encoding="utf-8")
    monkeypatch.setattr(
        target_agent,
        "_private_file_check",
        lambda *_args, **_kwargs: {"healthy": True},
    )
    monkeypatch.setattr(
        target_agent,
        "_https_json_endpoint",
        lambda *_args, **_kwargs: {"status": "ok"},
    )
    monkeypatch.setattr(
        target_agent,
        "_run",
        lambda command, **_kwargs: subprocess.CompletedProcess(command, 2, "", "missing"),
    )

    result = target_agent._host_application_ready(app, root=tmp_path)

    assert result["checks"]["certificate"] is False
    assert result["checks"]["local_api_target"] is False


def test_target_environment_reader_rejects_missing_and_ambiguous_files(tmp_path: Path) -> None:
    with pytest.raises(TargetError, match="unreadable"):
        target_agent._environment_values(tmp_path / "missing.env")
    invalid = tmp_path / "invalid.env"
    invalid.write_text("NOT-AN-ENV-LINE\n", encoding="utf-8")
    with pytest.raises(TargetError, match="invalid"):
        target_agent._environment_values(invalid)
    valid = tmp_path / "valid.env"
    valid.write_text("\nKEY=value\n", encoding="utf-8")
    assert target_agent._environment_values(valid) == {"KEY": "value"}


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
        target_agent._fixed_app({"app": app})


def test_target_rejects_missing_application_payload() -> None:
    with pytest.raises(TargetError, match="missing or malformed"):
        target_agent._fixed_app({})
