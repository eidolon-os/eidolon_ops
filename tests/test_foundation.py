from __future__ import annotations

import base64
import grp
import hashlib
import io
import json
import os
import pwd
import socket
import subprocess
import tarfile
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from eidolon_ops import target_agent
from eidolon_ops.foundation import (
    APT_COMMAND_OPTIONS,
    APT_MIRRORS,
    APT_PACKAGES,
    FOUNDATION_ARTIFACTS,
    foundation_payload,
    python_bootstrap_script,
    python_probe_script,
)
from eidolon_ops.target_agent import TargetError

pytestmark = pytest.mark.component


def _foundation_request() -> dict[str, object]:
    return {"foundation": foundation_payload()}


def test_foundation_scripts_and_target_contract_are_exact() -> None:
    assert b'"python3":true' in python_probe_script()
    assert b"install -y --no-install-recommends" in python_bootstrap_script()
    assert b"Acquire::ForceIPv4=true" in python_bootstrap_script()
    assert b"https://mirror.nju.edu.cn/debian/" in python_bootstrap_script()
    assert b"https://archive.raspberrypi.com/debian/" in python_bootstrap_script()
    assert target_agent._APT_COMMAND_OPTIONS == APT_COMMAND_OPTIONS
    assert target_agent._FOUNDATION_APT_MIRRORS == APT_MIRRORS
    assert Path("/var/lib/eidolon-ops/foundation-v2.json") == (target_agent._FOUNDATION_EVIDENCE)
    assert not any(
        root == target_agent._FOUNDATION_EVIDENCE
        or root in target_agent._FOUNDATION_EVIDENCE.parents
        for root in target_agent.RESET_AUTHORITY_ROOTS
    )
    assert target_agent._foundation_contract(_foundation_request()) == foundation_payload()

    changed = _foundation_request()
    changed["foundation"] = {**foundation_payload(), "profile": "unreviewed"}
    with pytest.raises(TargetError, match="reviewed pinned"):
        target_agent._foundation_contract(changed)


def test_foundation_uses_direct_debian_13_package_names() -> None:
    assert "policykit-1" not in APT_PACKAGES
    assert {"polkitd", "pkexec"} <= set(APT_PACKAGES)
    assert "libglib2.0-0" not in APT_PACKAGES
    assert "libglib2.0-0t64" in APT_PACKAGES


def test_platform_checks_cover_os_init_memory_and_disk(monkeypatch) -> None:
    monkeypatch.setattr(
        target_agent,
        "_os_release",
        lambda: {"ID": "raspbian", "ID_LIKE": "debian", "VERSION_ID": "13"},
    )
    monkeypatch.setattr(target_agent.platform, "system", lambda: "Linux")
    monkeypatch.setattr(target_agent.platform, "machine", lambda: "aarch64")
    monkeypatch.setattr(
        target_agent,
        "_read_text",
        lambda path: (
            "systemd"
            if path.name == "comm"
            else (
                "Raspberry Pi 5 Model B Rev 1.0\x00"
                if path.name == "model"
                else "MemTotal: 8388608 kB"
            )
        ),
    )
    monkeypatch.setattr(
        target_agent.shutil,
        "disk_usage",
        lambda _path: SimpleNamespace(free=20 * 1024**3),
    )

    assert all(target_agent._foundation_platform_checks().values())


def test_foundation_doctor_composes_every_gate(monkeypatch, tmp_path: Path) -> None:
    evidence = tmp_path / "foundation.json"
    contract = foundation_payload()
    evidence_document = {
        "schema_version": 1,
        "profile": contract["profile"],
        "status": "installed",
        "phase": "completed",
        "error": None,
        "artifacts": {
            artifact["artifact_id"]: {
                "version": artifact["version"],
                "sha256": artifact["sha256"],
            }
            for artifact in contract["artifacts"]
        },
    }
    evidence.write_text(json.dumps(evidence_document), encoding="utf-8")
    monkeypatch.setattr(target_agent, "_FOUNDATION_EVIDENCE", evidence)
    monkeypatch.setattr(
        target_agent,
        "_foundation_platform_checks",
        lambda: {"linux": True, "capacity": True},
    )
    monkeypatch.setattr(target_agent, "_package_installed", lambda _package: True)
    monkeypatch.setattr(
        target_agent,
        "_binary_version",
        lambda executable: {"healthy": True, "version": executable},
    )
    monkeypatch.setattr(
        target_agent,
        "_service_status",
        lambda unit: {"healthy": True, "active": unit},
    )

    result = target_agent.foundation_doctor(_foundation_request())

    assert result["status"] == "healthy"
    assert result["evidence"] == evidence_document
    assert result["evidence_healthy"] is True
    assert set(result["artifacts"]) == {item.artifact_id for item in FOUNDATION_ARTIFACTS}

    evidence.unlink()
    missing_evidence = target_agent.foundation_doctor(_foundation_request())
    assert missing_evidence["status"] == "degraded"
    assert missing_evidence["evidence_healthy"] is False

    evidence.write_text(json.dumps(evidence_document), encoding="utf-8")
    monkeypatch.setattr(target_agent, "_package_installed", lambda _package: False)
    assert target_agent.foundation_doctor(_foundation_request())["status"] == "degraded"


def test_download_is_hash_verified_cached_and_atomic(monkeypatch, tmp_path: Path) -> None:
    payload = b"verified archive"
    artifact = {
        "artifact_id": "test",
        "version": "1",
        "url": "https://api.github.com/repos/example/releases/assets/1",
        "sha256": hashlib.sha256(payload).hexdigest(),
        "kind": "tar-binary",
        "executable": "test",
    }
    monkeypatch.setattr(target_agent, "_FOUNDATION_CACHE", tmp_path / "cache")
    calls: list[str] = []
    commands: list[tuple[str, ...]] = []

    def fake_checked(operation, command, **_kwargs):
        calls.append(operation)
        commands.append(tuple(command))
        destination = Path(command[command.index("--output") + 1])
        destination.write_bytes(payload)
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(target_agent, "_checked", fake_checked)

    first = target_agent._download_verified(artifact)
    second = target_agent._download_verified(artifact)

    assert first == second
    assert first.read_bytes() == payload
    assert calls == ["download test"]
    assert "Accept: application/octet-stream" in commands[0]
    assert "--continue-at" in commands[0]


def test_download_keeps_stable_partial_for_next_provision(monkeypatch, tmp_path: Path) -> None:
    artifact = {
        "artifact_id": "test",
        "version": "1",
        "url": "https://example.invalid/test.tar.gz",
        "sha256": "0" * 64,
        "kind": "tar-binary",
        "executable": "test",
    }
    cache = tmp_path / "cache"
    monkeypatch.setattr(target_agent, "_FOUNDATION_CACHE", cache)

    def interrupted(_operation, command, **_kwargs):
        Path(command[command.index("--output") + 1]).write_bytes(b"partial")
        raise TargetError("network interrupted")

    monkeypatch.setattr(target_agent, "_checked", interrupted)

    with pytest.raises(TargetError, match="network interrupted"):
        target_agent._download_verified(artifact)

    assert (cache / ".test-1.tar.gz.partial").read_bytes() == b"partial"


def test_download_uses_wheel_suffix_and_rejects_unknown_kind(monkeypatch, tmp_path: Path) -> None:
    payload = b"wheel"
    cache = tmp_path / "cache"
    monkeypatch.setattr(target_agent, "_FOUNDATION_CACHE", cache)

    def downloaded(_operation, command, **_kwargs):
        Path(command[command.index("--output") + 1]).write_bytes(payload)
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(target_agent, "_checked", downloaded)
    artifact = {
        "artifact_id": "uv",
        "version": "1",
        "url": "https://example.invalid/uv.whl",
        "sha256": hashlib.sha256(payload).hexdigest(),
        "kind": "pip-wheel",
        "executable": "uv",
    }

    assert target_agent._download_verified(artifact).name == "uv.whl"
    with pytest.raises(TargetError, match="unsupported foundation artifact kind"):
        target_agent._download_verified({**artifact, "kind": "unknown"})
    with pytest.raises(TargetError, match="valid filename"):
        target_agent._download_verified({**artifact, "url": "https://example.invalid/not-a-wheel"})


def _binary_archive(path: Path, name: str, payload: bytes) -> None:
    with tarfile.open(path, "w:gz") as archive:
        info = tarfile.TarInfo(f"release/bin/{name}")
        info.mode = 0o755
        info.size = len(payload)
        archive.addfile(info, io.BytesIO(payload))


def test_tar_binary_install_is_versioned_and_refuses_unmanaged_link(
    monkeypatch, tmp_path: Path
) -> None:
    archive = tmp_path / "binary.tar.gz"
    _binary_archive(archive, "sample", b"#!/bin/sh\n")
    library = tmp_path / "lib"
    binary = tmp_path / "bin"
    binary.mkdir()
    monkeypatch.setattr(target_agent, "_FOUNDATION_LIBRARY", library)
    monkeypatch.setattr(target_agent, "_LOCAL_BIN", binary)
    monkeypatch.setattr(target_agent, "_download_verified", lambda _artifact: archive)
    artifact = {
        "artifact_id": "sample",
        "version": "1.2.3",
        "url": "https://example.invalid/sample.tar.gz",
        "sha256": "0" * 64,
        "kind": "tar-binary",
        "executable": "sample",
    }

    target_agent._install_tar_binary(artifact)

    link = binary / "sample"
    assert link.is_symlink()
    assert link.resolve().read_bytes() == b"#!/bin/sh\n"
    conflict = binary / "conflict"
    conflict.write_text("unmanaged", encoding="utf-8")
    with pytest.raises(TargetError, match="unmanaged"):
        target_agent._install_managed_link(conflict, link.resolve())


def _node_archive(path: Path, version: str) -> None:
    top = f"node-v{version}-linux-arm64"
    with tarfile.open(path, "w:xz") as archive:
        for executable in ("node", "npm", "npx", "corepack"):
            payload = f"{executable}\n".encode()
            info = tarfile.TarInfo(f"{top}/bin/{executable}")
            info.mode = 0o755
            info.size = len(payload)
            archive.addfile(info, io.BytesIO(payload))


def test_node_archive_is_safely_extracted(monkeypatch, tmp_path: Path) -> None:
    version = "22.23.2"
    archive = tmp_path / "node.tar.xz"
    _node_archive(archive, version)
    local_lib = tmp_path / "local-lib"
    local_bin = tmp_path / "local-bin"
    local_lib.mkdir()
    local_bin.mkdir()
    monkeypatch.setattr(target_agent, "_LOCAL_LIB", local_lib)
    monkeypatch.setattr(target_agent, "_LOCAL_BIN", local_bin)
    monkeypatch.setattr(target_agent, "_download_verified", lambda _artifact: archive)
    artifact = {
        "artifact_id": "node",
        "version": version,
        "url": "https://example.invalid/node.tar.xz",
        "sha256": "0" * 64,
        "kind": "node-tar",
        "executable": "node",
    }

    target_agent._install_node(artifact)

    assert (local_bin / "node").resolve().read_text(encoding="utf-8") == "node\n"
    unsafe = tarfile.TarInfo("../escape")
    assert not target_agent._safe_node_member(unsafe, "node-v22-linux-arm64")


def test_pinned_uv_install_uses_hash_requirement(monkeypatch, tmp_path: Path) -> None:
    local_bin = tmp_path / "bin"
    local_bin.mkdir()
    monkeypatch.setattr(target_agent, "_LOCAL_BIN", local_bin)
    monkeypatch.setattr(target_agent, "_VAR_TMP", tmp_path)
    monkeypatch.setattr(target_agent, "_binary_version", lambda _name: {"healthy": False})
    wheel = tmp_path / "uv.whl"
    wheel.write_bytes(b"wheel")
    monkeypatch.setattr(target_agent, "_download_verified", lambda _artifact: wheel)
    calls: list[tuple[str, ...]] = []

    def fake_checked(_operation, command, **_kwargs):
        calls.append(tuple(command))
        (local_bin / "uv").write_text("uv", encoding="utf-8")
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(target_agent, "_checked", fake_checked)
    artifact = next(
        item for item in target_agent._FOUNDATION_ARTIFACTS if item["artifact_id"] == "uv"
    )

    target_agent._install_uv(artifact)

    assert "--require-hashes" in calls[0]
    assert "--no-index" in calls[0]
    requirement = next(part for part in calls[0] if part.endswith(".txt"))
    assert not Path(requirement).exists()


def test_foundation_install_runs_locked_idempotent_phases(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(target_agent.os, "geteuid", lambda: 0)
    monkeypatch.setattr(target_agent, "_os_release", lambda: {"VERSION_ID": "13"})
    monkeypatch.setattr(target_agent, "_FOUNDATION_LOCK", tmp_path / "foundation.lock")
    monkeypatch.setattr(target_agent, "_FOUNDATION_EVIDENCE", tmp_path / "evidence.json")
    monkeypatch.setattr(
        target_agent,
        "_foundation_platform_checks",
        lambda: {"linux": True, "aarch64": True, "capacity": True},
    )
    commands: list[tuple[str, ...]] = []
    monkeypatch.setattr(
        target_agent,
        "_checked",
        lambda _operation, command, **_kwargs: (
            commands.append(tuple(command)) or subprocess.CompletedProcess(command, 0, "", "")
        ),
    )
    installed: list[str] = []
    monkeypatch.setattr(
        target_agent,
        "_install_tar_binary",
        lambda artifact: installed.append(str(artifact["artifact_id"])),
    )
    monkeypatch.setattr(
        target_agent,
        "_install_uv",
        lambda artifact: installed.append(str(artifact["artifact_id"])),
    )
    monkeypatch.setattr(
        target_agent,
        "_install_node",
        lambda artifact: installed.append(str(artifact["artifact_id"])),
    )
    monkeypatch.setattr(
        target_agent,
        "foundation_doctor",
        lambda _payload: {"status": "healthy"},
    )

    result = target_agent.foundation_install(_foundation_request())

    assert result["status"] == "installed"
    assert installed == ["nats-server", "livekit-server", "uv", "node"]
    assert any("apt-get" in call[0] for call in commands)
    assert any(any(part.startswith("Dir::Etc::sourcelist=") for part in call) for call in commands)
    assert json.loads((tmp_path / "evidence.json").read_text())["profile"] == result["profile"]


def test_https_json_is_bounded_and_validates_status(monkeypatch) -> None:
    class Response:
        status = 200

        @staticmethod
        def read(_limit):
            return b'{"status":"ok"}'

    class Connection:
        def __init__(self, *args, **kwargs):
            pass

        def request(self, method, path):
            assert (method, path) == ("GET", "/healthz")

        def getresponse(self):
            return Response()

        def close(self):
            pass

    monkeypatch.setattr(target_agent.http.client, "HTTPSConnection", Connection)

    assert target_agent._https_json("/healthz") == {"status": "ok"}


def test_https_json_wraps_transport_failure_and_closes(monkeypatch) -> None:
    closed: list[bool] = []

    class Connection:
        def __init__(self, *args, **kwargs):
            pass

        def request(self, _method, _path):
            raise OSError("offline")

        def close(self):
            closed.append(True)

    monkeypatch.setattr(target_agent.http.client, "HTTPSConnection", Connection)

    with pytest.raises(TargetError, match="self-check failed"):
        target_agent._https_json_endpoint("192.168.100.15", 8443, "/health", label="Hub")
    assert closed == [True]


def test_private_file_and_app_ready_gate(monkeypatch, tmp_path: Path) -> None:
    user = pwd.getpwuid(os.getuid()).pw_name
    group = grp.getgrgid(os.getgid()).gr_name
    private = tmp_path / "private"
    private.write_text("value", encoding="utf-8")
    private.chmod(0o600)
    assert target_agent._private_file_check(private, 0o600, user, group)["healthy"]

    mdns = tmp_path / "eidolon.service"
    mdns.write_text("service", encoding="utf-8")
    with tempfile.TemporaryDirectory(prefix="eo-", dir="/tmp") as short_tmp:
        control = Path(short_tmp) / "control.sock"
        listener = socket.socket(socket.AF_UNIX)
        listener.bind(str(control))
        monkeypatch.setattr(target_agent, "_MDNS_DEFINITION", mdns)
        monkeypatch.setattr(target_agent, "_BOOTSTRAP_SOCKET", control)
        monkeypatch.setattr(
            target_agent,
            "_run",
            lambda *_args, **_kwargs: subprocess.CompletedProcess(
                (), 0, json.dumps({"ok": True}), ""
            ),
        )
        monkeypatch.setattr(
            target_agent,
            "_unit_status",
            lambda _unit: {"ActiveState": "active", "SubState": "running"},
        )
        monkeypatch.setattr(
            target_agent,
            "_private_file_check",
            lambda *_args: {"healthy": True},
        )

        def https(path: str) -> dict[str, object]:
            if path == "/healthz":
                return {"status": "ok", "bootstrap": "ready"}
            return {
                "contract_version": "1",
                "host_id": "ehost-0123456789abcdefabcd",
                "host_public_key_fingerprint": "sha256:test",
                "ble_service_uuid": "123e4567-e89b-42d3-a456-426614174000",
            }

        monkeypatch.setattr(target_agent, "_https_json", https)
        result = target_agent.app_ready({"units": list(target_agent.PRODUCT_UNITS)})
        listener.close()

    assert result["status"] == "app_ready"
    assert result["local_api"]["host_id"] == "ehost-0123456789abcdefabcd"


def test_target_main_dispatches_foundation_doctor(monkeypatch, capsys) -> None:
    monkeypatch.setattr(
        target_agent,
        "foundation_doctor",
        lambda _payload: {"status": "healthy"},
    )
    encoded = base64.urlsafe_b64encode(json.dumps(_foundation_request()).encode()).decode()

    assert target_agent.main(("foundation-doctor", encoded)) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "healthy"


def test_foundation_low_level_checks_and_failure_evidence(monkeypatch, tmp_path: Path) -> None:
    os_release = tmp_path / "etc/os-release"
    os_release.parent.mkdir()
    os_release.write_text('ID="raspbian"\nVERSION_ID=12\nBAD-KEY=x\n', encoding="utf-8")
    assert target_agent._os_release(tmp_path) == {"ID": "raspbian", "VERSION_ID": "12"}
    assert target_agent._os_release(tmp_path / "missing") == {}

    monkeypatch.setattr(
        target_agent,
        "_run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess((), 0, "install ok installed", ""),
    )
    assert target_agent._package_installed("bluez")

    local_bin = tmp_path / "bin"
    local_bin.mkdir()
    monkeypatch.setattr(target_agent, "_LOCAL_BIN", local_bin)
    assert target_agent._binary_version("uv")["error"] == "missing"
    uv = local_bin / "uv"
    uv.write_text("#!/bin/sh\n", encoding="utf-8")
    uv.chmod(0o755)
    monkeypatch.setattr(
        target_agent,
        "_run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess((), 0, "uv 0.11.15", ""),
    )
    assert target_agent._binary_version("uv")["healthy"]
    assert target_agent._service_status("bluetooth.service")["healthy"]

    monkeypatch.setattr(target_agent.os, "geteuid", lambda: 0)
    monkeypatch.setattr(target_agent, "_os_release", lambda: {"VERSION_ID": "13"})
    monkeypatch.setattr(target_agent, "_FOUNDATION_LOCK", tmp_path / "foundation.lock")
    evidence = tmp_path / "foundation.json"
    monkeypatch.setattr(target_agent, "_FOUNDATION_EVIDENCE", evidence)
    monkeypatch.setattr(target_agent, "_foundation_platform_checks", lambda: {"ok": True})
    monkeypatch.setattr(
        target_agent,
        "_checked",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(TargetError("apt failed")),
    )

    with pytest.raises(TargetError, match="apt failed"):
        target_agent.foundation_install(_foundation_request())
    failure = json.loads(evidence.read_text(encoding="utf-8"))
    assert (failure["status"], failure["phase"], failure["error"]) == (
        "failed",
        "validated",
        "apt failed",
    )


def test_foundation_rejects_non_root_and_unsupported_platform(monkeypatch) -> None:
    monkeypatch.setattr(target_agent.os, "geteuid", lambda: 501)
    with pytest.raises(TargetError, match="requires root"):
        target_agent.foundation_install(_foundation_request())

    monkeypatch.setattr(target_agent.os, "geteuid", lambda: 0)
    monkeypatch.setattr(
        target_agent,
        "_foundation_platform_checks",
        lambda: {"raspberry_pi_hardware": False},
    )
    with pytest.raises(TargetError, match="unsupported Raspberry Pi"):
        target_agent.foundation_install(_foundation_request())


def test_download_hash_mismatch_is_removed(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(target_agent, "_FOUNDATION_CACHE", tmp_path / "cache")
    artifact = {
        "artifact_id": "wrong",
        "version": "1",
        "url": "https://example.invalid/wrong.tar.gz",
        "sha256": "0" * 64,
        "kind": "tar-binary",
        "executable": "wrong",
    }

    def fake_checked(_operation, command, **_kwargs):
        Path(command[command.index("--output") + 1]).write_bytes(b"wrong")
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(target_agent, "_checked", fake_checked)
    with pytest.raises(TargetError, match="hash mismatch"):
        target_agent._download_verified(artifact)
    assert not any((tmp_path / "cache").iterdir())


def test_install_helpers_are_idempotent_and_reject_unsafe_inputs(
    monkeypatch, tmp_path: Path
) -> None:
    target = tmp_path / "target"
    target.write_text("binary", encoding="utf-8")
    link = tmp_path / "link"
    link.symlink_to(target)
    target_agent._install_managed_link(link, target)

    monkeypatch.setattr(target_agent, "_binary_version", lambda _name: {"healthy": True})
    target_agent._install_uv({"artifact_id": "uv"})

    absolute = tarfile.TarInfo("release/link")
    absolute.type = tarfile.SYMTYPE
    absolute.linkname = "/etc/passwd"
    assert not target_agent._safe_node_member(absolute, "release")

    escaping = tarfile.TarInfo("release/bin/link")
    escaping.type = tarfile.SYMTYPE
    escaping.linkname = "../../outside"
    assert not target_agent._safe_node_member(escaping, "release")


@pytest.mark.parametrize(
    ("status", "body", "message"),
    [
        (503, b"{}", "HTTP 503"),
        (200, b"not-json", "invalid JSON"),
        (200, b"[]", "non-object"),
    ],
)
def test_https_json_rejects_bad_responses(
    monkeypatch, status: int, body: bytes, message: str
) -> None:
    class Response:
        def read(self, _limit):
            return body

    Response.status = status

    class Connection:
        def __init__(self, *args, **kwargs):
            pass

        def request(self, _method, _path):
            pass

        def getresponse(self):
            return Response()

        def close(self):
            pass

    monkeypatch.setattr(target_agent.http.client, "HTTPSConnection", Connection)
    with pytest.raises(TargetError, match=message):
        target_agent._https_json("/healthz")


def test_app_ready_returns_degraded_evidence(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(
        target_agent,
        "_run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess((), 2, "", "missing"),
    )
    monkeypatch.setattr(
        target_agent,
        "_unit_status",
        lambda _unit: {"ActiveState": "failed", "SubState": "dead"},
    )
    monkeypatch.setattr(target_agent, "_private_file_check", lambda *_args: {"healthy": False})
    monkeypatch.setattr(target_agent, "_BOOTSTRAP_SOCKET", tmp_path / "missing.sock")
    monkeypatch.setattr(target_agent, "_MDNS_DEFINITION", tmp_path / "missing.service")
    monkeypatch.setattr(
        target_agent,
        "_https_json",
        lambda _path: (_ for _ in ()).throw(TargetError("offline")),
    )

    result = target_agent.app_ready({"units": list(target_agent.PRODUCT_UNITS)})

    assert result["status"] == "degraded"
    assert result["preflight"]["error"] == "missing"
    assert result["local_api"]["error"] == "offline"

    monkeypatch.setattr(
        target_agent,
        "_run",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(TargetError("not installed")),
    )
    absent = target_agent.app_ready({"units": list(target_agent.PRODUCT_UNITS)})
    assert absent["status"] == "degraded"
    assert absent["preflight"]["error"] == "not installed"


def test_target_main_routes_all_actions_and_errors(monkeypatch, capsys) -> None:
    action_functions = {
        "status": "status",
        "foundation-install": "foundation_install",
        "app-ready": "app_ready",
        "doctor-host": "doctor_host",
        "guard-upload": "guard_upload",
        "finalize-upload": "finalize_upload",
        "cleanup-stage": "cleanup_stage",
        "install": "install",
        "rollback-plan": "rollback_plan",
        "logs": "logs",
        "diagnose": "diagnose",
    }
    encoded = base64.urlsafe_b64encode(b"{}").decode()
    for action, function in action_functions.items():
        monkeypatch.setattr(
            target_agent, function, lambda _payload, value=action: {"action": value}
        )
        assert target_agent.main((action, encoded)) == 0
        assert json.loads(capsys.readouterr().out)["action"] == action
    monkeypatch.setattr(
        target_agent,
        "lifecycle",
        lambda action, _payload: {"action": action},
    )
    assert target_agent.main(("restart", encoded)) == 0
    capsys.readouterr()
    assert target_agent.main(("unknown", encoded)) == 1
    assert json.loads(capsys.readouterr().err)["status"] == "failed"
    assert target_agent.main(("only-one",)) == 2
