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
from dataclasses import MISSING
from pathlib import Path
from types import SimpleNamespace

import pytest

from eidolon_ops.foundation import (
    APT_COMMAND_OPTIONS,
    APT_MIRRORS,
    APT_PACKAGES,
    FOUNDATION_ARTIFACTS,
    foundation_payload,
    python_bootstrap_script,
    python_probe_script,
)
from eidolon_ops.hostagent import __main__ as agent_main
from eidolon_ops.hostagent import contract, primitives, probe
from eidolon_ops.hostagent import foundation as host_foundation
from eidolon_ops.hostagent import foundation_install as host_foundation_install
from eidolon_ops.hostagent.primitives import TargetError

pytestmark = pytest.mark.component


def _foundation_request() -> dict[str, object]:
    return {"foundation": foundation_payload()}


def test_foundation_scripts_and_target_contract_are_exact() -> None:
    assert b'"python3":true' in python_probe_script()
    assert b"install -y --no-install-recommends" in python_bootstrap_script()
    assert b"Acquire::ForceIPv4=true" in python_bootstrap_script()
    assert b"https://mirror.nju.edu.cn/debian/" in python_bootstrap_script()
    assert b"https://archive.raspberrypi.com/debian/" in python_bootstrap_script()
    assert host_foundation.APT_COMMAND_OPTIONS == APT_COMMAND_OPTIONS
    assert host_foundation.FOUNDATION_APT_MIRRORS == APT_MIRRORS
    assert Path("/var/lib/eidolon-ops/foundation-v2.json") == (host_foundation.FOUNDATION_EVIDENCE)
    assert not any(
        root == host_foundation.FOUNDATION_EVIDENCE
        or root in host_foundation.FOUNDATION_EVIDENCE.parents
        for root in contract.RESET_AUTHORITY_ROOTS
    )
    assert host_foundation.foundation_contract(_foundation_request()) == foundation_payload()

    changed = _foundation_request()
    changed["foundation"] = {**foundation_payload(), "profile": "unreviewed"}
    with pytest.raises(TargetError, match="reviewed pinned"):
        host_foundation.foundation_contract(changed)


def test_foundation_uses_direct_debian_13_package_names() -> None:
    assert "policykit-1" not in APT_PACKAGES
    assert {"polkitd", "pkexec"} <= set(APT_PACKAGES)
    assert "libglib2.0-0" not in APT_PACKAGES
    assert "libglib2.0-0t64" in APT_PACKAGES


def test_platform_checks_cover_os_init_memory_and_disk(monkeypatch) -> None:
    monkeypatch.setattr(
        host_foundation,
        "os_release",
        lambda: {"ID": "raspbian", "ID_LIKE": "debian", "VERSION_ID": "13"},
    )
    monkeypatch.setattr(host_foundation.platform, "system", lambda: "Linux")
    monkeypatch.setattr(host_foundation.platform, "machine", lambda: "aarch64")
    monkeypatch.setattr(
        primitives,
        "read_text",
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
        host_foundation.shutil,
        "disk_usage",
        lambda _path: SimpleNamespace(free=20 * 1024**3),
    )

    assert all(host_foundation.foundation_platform_checks().values())


def test_foundation_doctor_composes_every_gate(monkeypatch, tmp_path: Path) -> None:
    # A Host that keeps its journal in RAM is degraded, so the gate has to be
    # given a Host that does not — otherwise this asserts the wrong failure.
    monkeypatch.setattr(host_foundation, "journal_is_persistent", lambda: True)
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
    monkeypatch.setattr(host_foundation, "FOUNDATION_EVIDENCE", evidence)
    monkeypatch.setattr(
        host_foundation,
        "foundation_platform_checks",
        lambda: {"linux": True, "capacity": True},
    )
    monkeypatch.setattr(host_foundation, "package_installed", lambda _package: True)
    monkeypatch.setattr(
        host_foundation,
        "binary_version",
        lambda executable: {"healthy": True, "version": executable},
    )
    monkeypatch.setattr(
        primitives,
        "service_status",
        lambda unit: {"healthy": True, "active": unit},
    )

    result = host_foundation.foundation_doctor(_foundation_request())

    assert result["status"] == "healthy"
    assert result["evidence"] == evidence_document
    assert result["evidence_healthy"] is True
    assert set(result["artifacts"]) == {item.artifact_id for item in FOUNDATION_ARTIFACTS}

    evidence.unlink()
    missing_evidence = host_foundation.foundation_doctor(_foundation_request())
    assert missing_evidence["status"] == "degraded"
    assert missing_evidence["evidence_healthy"] is False

    evidence.write_text(json.dumps(evidence_document), encoding="utf-8")
    monkeypatch.setattr(host_foundation, "package_installed", lambda _package: False)
    assert host_foundation.foundation_doctor(_foundation_request())["status"] == "degraded"


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
    monkeypatch.setattr(host_foundation, "FOUNDATION_CACHE", tmp_path / "cache")
    calls: list[str] = []
    commands: list[tuple[str, ...]] = []

    def fake_checked(operation, command, **_kwargs):
        calls.append(operation)
        commands.append(tuple(command))
        destination = Path(command[command.index("--output") + 1])
        destination.write_bytes(payload)
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(primitives, "checked", fake_checked)

    first = host_foundation_install.download_verified(artifact)
    second = host_foundation_install.download_verified(artifact)

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
    monkeypatch.setattr(host_foundation, "FOUNDATION_CACHE", cache)

    def interrupted(_operation, command, **_kwargs):
        Path(command[command.index("--output") + 1]).write_bytes(b"partial")
        raise TargetError("network interrupted")

    monkeypatch.setattr(primitives, "checked", interrupted)

    with pytest.raises(TargetError, match="network interrupted"):
        host_foundation_install.download_verified(artifact)

    assert (cache / ".test-1.tar.gz.partial").read_bytes() == b"partial"


def test_download_uses_wheel_suffix_and_rejects_unknown_kind(monkeypatch, tmp_path: Path) -> None:
    payload = b"wheel"
    cache = tmp_path / "cache"
    monkeypatch.setattr(host_foundation, "FOUNDATION_CACHE", cache)

    def downloaded(_operation, command, **_kwargs):
        Path(command[command.index("--output") + 1]).write_bytes(payload)
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(primitives, "checked", downloaded)
    artifact = {
        "artifact_id": "uv",
        "version": "1",
        "url": "https://example.invalid/uv.whl",
        "sha256": hashlib.sha256(payload).hexdigest(),
        "kind": "pip-wheel",
        "executable": "uv",
    }

    assert host_foundation_install.download_verified(artifact).name == "uv.whl"
    with pytest.raises(TargetError, match="unsupported foundation artifact kind"):
        host_foundation_install.download_verified({**artifact, "kind": "unknown"})
    with pytest.raises(TargetError, match="valid filename"):
        host_foundation_install.download_verified(
            {**artifact, "url": "https://example.invalid/not-a-wheel"}
        )


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
    monkeypatch.setattr(host_foundation, "FOUNDATION_LIBRARY", library)
    monkeypatch.setattr(host_foundation, "LOCAL_BIN", binary)
    monkeypatch.setattr(host_foundation_install, "download_verified", lambda _artifact: archive)
    artifact = {
        "artifact_id": "sample",
        "version": "1.2.3",
        "url": "https://example.invalid/sample.tar.gz",
        "sha256": "0" * 64,
        "kind": "tar-binary",
        "executable": "sample",
    }

    host_foundation_install.install_tar_binary(artifact)

    link = binary / "sample"
    assert link.is_symlink()
    assert link.resolve().read_bytes() == b"#!/bin/sh\n"
    conflict = binary / "conflict"
    conflict.write_text("unmanaged", encoding="utf-8")
    with pytest.raises(TargetError, match="unmanaged"):
        host_foundation_install.install_managed_link(conflict, link.resolve())


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
    monkeypatch.setattr(host_foundation, "LOCAL_LIB", local_lib)
    monkeypatch.setattr(host_foundation, "LOCAL_BIN", local_bin)
    monkeypatch.setattr(host_foundation_install, "download_verified", lambda _artifact: archive)
    artifact = {
        "artifact_id": "node",
        "version": version,
        "url": "https://example.invalid/node.tar.xz",
        "sha256": "0" * 64,
        "kind": "node-tar",
        "executable": "node",
    }

    host_foundation_install.install_node(artifact)

    assert (local_bin / "node").resolve().read_text(encoding="utf-8") == "node\n"
    unsafe = tarfile.TarInfo("../escape")
    assert not host_foundation_install.safe_node_member(unsafe, "node-v22-linux-arm64")


def test_pinned_uv_install_uses_hash_requirement(monkeypatch, tmp_path: Path) -> None:
    local_bin = tmp_path / "bin"
    local_bin.mkdir()
    monkeypatch.setattr(host_foundation, "LOCAL_BIN", local_bin)
    monkeypatch.setattr(contract, "VAR_TMP", tmp_path)
    monkeypatch.setattr(host_foundation, "binary_version", lambda _name: {"healthy": False})
    wheel = tmp_path / "uv.whl"
    wheel.write_bytes(b"wheel")
    monkeypatch.setattr(host_foundation_install, "download_verified", lambda _artifact: wheel)
    calls: list[tuple[str, ...]] = []

    def fake_checked(_operation, command, **_kwargs):
        calls.append(tuple(command))
        (local_bin / "uv").write_text("uv", encoding="utf-8")
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(primitives, "checked", fake_checked)
    artifact = next(
        item for item in host_foundation.FOUNDATION_ARTIFACTS if item["artifact_id"] == "uv"
    )

    host_foundation_install.install_uv(artifact)

    assert "--require-hashes" in calls[0]
    assert "--no-index" in calls[0]
    requirement = next(part for part in calls[0] if part.endswith(".txt"))
    assert not Path(requirement).exists()


def test_foundation_install_runs_locked_idempotent_phases(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(host_foundation_install.os, "geteuid", lambda: 0)
    monkeypatch.setattr(host_foundation, "os_release", lambda: {"VERSION_ID": "13"})
    monkeypatch.setattr(host_foundation, "FOUNDATION_LOCK", tmp_path / "foundation.lock")
    monkeypatch.setattr(host_foundation, "FOUNDATION_EVIDENCE", tmp_path / "evidence.json")
    monkeypatch.setattr(
        host_foundation, "JOURNAL_PERSISTENCE", tmp_path / "journald.conf.d/50-eidolon.conf"
    )
    monkeypatch.setattr(host_foundation, "JOURNAL_DIRECTORY", tmp_path / "journal")
    monkeypatch.setattr(
        host_foundation,
        "foundation_platform_checks",
        lambda: {"linux": True, "aarch64": True, "capacity": True},
    )
    commands: list[tuple[str, ...]] = []
    monkeypatch.setattr(
        primitives,
        "checked",
        lambda _operation, command, **_kwargs: (
            commands.append(tuple(command)) or subprocess.CompletedProcess(command, 0, "", "")
        ),
    )
    installed: list[str] = []
    monkeypatch.setattr(
        host_foundation_install,
        "install_tar_binary",
        lambda artifact: installed.append(str(artifact["artifact_id"])),
    )
    monkeypatch.setattr(
        host_foundation_install,
        "install_uv",
        lambda artifact: installed.append(str(artifact["artifact_id"])),
    )
    monkeypatch.setattr(
        host_foundation_install,
        "install_node",
        lambda artifact: installed.append(str(artifact["artifact_id"])),
    )
    monkeypatch.setattr(
        host_foundation,
        "foundation_doctor",
        lambda _payload: {"status": "healthy"},
    )

    result = host_foundation_install.foundation_install(_foundation_request())

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

    monkeypatch.setattr(primitives.http.client, "HTTPSConnection", Connection)

    assert probe.local_api_json("/healthz") == {"status": "ok"}


def test_https_json_wraps_transport_failure_and_closes(monkeypatch) -> None:
    closed: list[bool] = []

    class Connection:
        def __init__(self, *args, **kwargs):
            pass

        def request(self, _method, _path):
            raise OSError("offline")

        def close(self):
            closed.append(True)

    monkeypatch.setattr(primitives.http.client, "HTTPSConnection", Connection)

    with pytest.raises(TargetError, match="self-check failed"):
        primitives.https_json_endpoint("192.168.100.15", 8443, "/health", label="Hub")
    assert closed == [True]


def test_private_file_and_app_ready_gate(monkeypatch, tmp_path: Path) -> None:
    user = pwd.getpwuid(os.getuid()).pw_name
    group = grp.getgrgid(os.getgid()).gr_name
    private = tmp_path / "private"
    private.write_text("value", encoding="utf-8")
    private.chmod(0o600)
    assert primitives.private_file_check(private, 0o600, user, group)["healthy"]

    mdns = tmp_path / "eidolon.service"
    mdns.write_text("service", encoding="utf-8")
    with tempfile.TemporaryDirectory(prefix="eo-", dir="/tmp") as short_tmp:
        control = Path(short_tmp) / "control.sock"
        listener = socket.socket(socket.AF_UNIX)
        listener.bind(str(control))
        monkeypatch.setattr(probe, "MDNS_DEFINITION", mdns)
        monkeypatch.setattr(probe, "BOOTSTRAP_SOCKET", control)
        monkeypatch.setattr(
            primitives,
            "run",
            lambda *_args, **_kwargs: subprocess.CompletedProcess(
                (), 0, json.dumps({"ok": True}), ""
            ),
        )
        monkeypatch.setattr(
            primitives,
            "unit_status",
            lambda _unit: {"ActiveState": "active", "SubState": "running"},
        )
        monkeypatch.setattr(
            primitives,
            "private_file_check",
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

        monkeypatch.setattr(probe, "local_api_json", https)
        result = probe.local_api_report()
        socket_present = probe.BOOTSTRAP_SOCKET.is_socket()
        listener.close()

    assert socket_present
    assert result["healthy"] is True
    assert result["host_id"] == "ehost-0123456789abcdefabcd"


def test_target_main_dispatches_foundation_doctor(monkeypatch, capsys) -> None:
    monkeypatch.setattr(
        host_foundation,
        "foundation_doctor",
        lambda _payload: {"status": "healthy"},
    )
    encoded = base64.urlsafe_b64encode(json.dumps(_foundation_request()).encode()).decode()

    assert agent_main.main(("foundation-doctor", encoded)) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "healthy"


def test_foundation_low_level_checks_and_failure_evidence(monkeypatch, tmp_path: Path) -> None:
    os_release = tmp_path / "etc/os-release"
    os_release.parent.mkdir()
    os_release.write_text('ID="raspbian"\nVERSION_ID=12\nBAD-KEY=x\n', encoding="utf-8")
    assert host_foundation.os_release(tmp_path) == {"ID": "raspbian", "VERSION_ID": "12"}
    assert host_foundation.os_release(tmp_path / "missing") == {}

    monkeypatch.setattr(
        primitives,
        "run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess((), 0, "install ok installed", ""),
    )
    assert host_foundation.package_installed("bluez")

    local_bin = tmp_path / "bin"
    local_bin.mkdir()
    monkeypatch.setattr(host_foundation, "LOCAL_BIN", local_bin)
    assert host_foundation.binary_version("uv")["error"] == "missing"
    uv = local_bin / "uv"
    uv.write_text("#!/bin/sh\n", encoding="utf-8")
    uv.chmod(0o755)
    monkeypatch.setattr(
        primitives,
        "run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess((), 0, "uv 0.11.15", ""),
    )
    assert host_foundation.binary_version("uv")["healthy"]
    assert primitives.service_status("bluetooth.service")["healthy"]

    monkeypatch.setattr(host_foundation_install.os, "geteuid", lambda: 0)
    monkeypatch.setattr(host_foundation, "os_release", lambda: {"VERSION_ID": "13"})
    monkeypatch.setattr(host_foundation, "FOUNDATION_LOCK", tmp_path / "foundation.lock")
    evidence = tmp_path / "foundation.json"
    monkeypatch.setattr(host_foundation, "FOUNDATION_EVIDENCE", evidence)
    monkeypatch.setattr(host_foundation, "foundation_platform_checks", lambda: {"ok": True})
    monkeypatch.setattr(
        primitives,
        "checked",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(TargetError("apt failed")),
    )

    with pytest.raises(TargetError, match="apt failed"):
        host_foundation_install.foundation_install(_foundation_request())
    failure = json.loads(evidence.read_text(encoding="utf-8"))
    assert (failure["status"], failure["phase"], failure["error"]) == (
        "failed",
        "validated",
        "apt failed",
    )


def test_foundation_rejects_non_root_and_unsupported_platform(monkeypatch) -> None:
    monkeypatch.setattr(host_foundation_install.os, "geteuid", lambda: 501)
    with pytest.raises(TargetError, match="requires root"):
        host_foundation_install.foundation_install(_foundation_request())

    monkeypatch.setattr(host_foundation_install.os, "geteuid", lambda: 0)
    monkeypatch.setattr(
        host_foundation,
        "foundation_platform_checks",
        lambda: {"raspberry_pi_hardware": False},
    )
    with pytest.raises(TargetError, match="unsupported Raspberry Pi"):
        host_foundation_install.foundation_install(_foundation_request())


def test_download_hash_mismatch_is_removed(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(host_foundation, "FOUNDATION_CACHE", tmp_path / "cache")
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

    monkeypatch.setattr(primitives, "checked", fake_checked)
    with pytest.raises(TargetError, match="hash mismatch"):
        host_foundation_install.download_verified(artifact)
    assert not any((tmp_path / "cache").iterdir())


def test_install_helpers_are_idempotent_and_reject_unsafe_inputs(
    monkeypatch, tmp_path: Path
) -> None:
    target = tmp_path / "target"
    target.write_text("binary", encoding="utf-8")
    link = tmp_path / "link"
    link.symlink_to(target)
    host_foundation_install.install_managed_link(link, target)

    monkeypatch.setattr(host_foundation, "binary_version", lambda _name: {"healthy": True})
    host_foundation_install.install_uv({"artifact_id": "uv"})

    absolute = tarfile.TarInfo("release/link")
    absolute.type = tarfile.SYMTYPE
    absolute.linkname = "/etc/passwd"
    assert not host_foundation_install.safe_node_member(absolute, "release")

    escaping = tarfile.TarInfo("release/bin/link")
    escaping.type = tarfile.SYMTYPE
    escaping.linkname = "../../outside"
    assert not host_foundation_install.safe_node_member(escaping, "release")


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

    monkeypatch.setattr(primitives.http.client, "HTTPSConnection", Connection)
    with pytest.raises(TargetError, match=message):
        probe.local_api_json("/healthz")


def test_readiness_evidence_names_what_it_could_not_reach(monkeypatch) -> None:
    monkeypatch.setattr(
        primitives,
        "run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess((), 2, "", "missing"),
    )
    monkeypatch.setattr(
        probe,
        "local_api_json",
        lambda _path: (_ for _ in ()).throw(TargetError("offline")),
    )

    assert probe.bootstrap_preflight()["error"] == "missing"
    assert probe.local_api_report()["error"] == "offline"

    monkeypatch.setattr(
        primitives,
        "run",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(TargetError("not installed")),
    )
    assert probe.bootstrap_preflight()["error"] == "not installed"


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
        "active-release": "active_release",
        "controller-reset": "controller_reset",
        "rollback-plan": "rollback_plan",
        "logs": "logs",
        "diagnose": "diagnose",
    }
    encoded = base64.urlsafe_b64encode(b"{}").decode()
    for action, function in action_functions.items():
        module, name = agent_main.ACTIONS[action]
        assert name == function
        monkeypatch.setattr(module, name, lambda _payload, value=action: {"action": value})
        assert agent_main.main((action, encoded)) == 0
        assert json.loads(capsys.readouterr().out)["action"] == action
    monkeypatch.setattr(
        agent_main.lifecycle, "lifecycle", lambda action, _payload: {"action": action}
    )
    assert agent_main.main(("restart", encoded)) == 0
    capsys.readouterr()
    assert agent_main.main(("unknown", encoded)) == 1
    assert json.loads(capsys.readouterr().err)["status"] == "failed"
    assert agent_main.main(("only-one",)) == 2


def _merged_journald_config(*storage: str) -> str:
    """What `systemd-analyze cat-config` prints: drop-ins, in order."""

    blocks = ["# /usr/lib/systemd/journald.conf.d/40-rpi-volatile-storage.conf"]
    for value in storage:
        blocks.append(f"[Journal]\nStorage={value}")
    return "\n".join(blocks) + "\n"


def test_a_host_that_forgets_why_it_failed_is_degraded(monkeypatch) -> None:
    """The check that turns an unanswerable incident into a reported fault.

    Raspberry Pi OS keeps the journal in RAM to spare the SD card, so every
    restart erases the record of whatever went wrong before it. A Host in that
    state looks perfectly healthy right up until someone asks it a question
    about the past — which is exactly when a Channel worker that stopped
    taking jobs became uninvestigable.
    """

    def merged(output: str):
        return lambda *_a, **_k: subprocess.CompletedProcess([], 0, output, "")

    monkeypatch.setattr(primitives, "run", merged(_merged_journald_config("volatile")))
    assert host_foundation.journal_is_persistent() is False

    # systemd's own rule: every drop-in is concatenated and the last Storage=
    # wins. The check has to agree with that or it answers a question systemd
    # is not asking.
    monkeypatch.setattr(
        primitives, "run", merged(_merged_journald_config("volatile", "persistent"))
    )
    assert host_foundation.journal_is_persistent() is True

    # And ours being outranked by a later drop-in means not persistent, even
    # though our file is present and correct.
    monkeypatch.setattr(
        primitives, "run", merged(_merged_journald_config("persistent", "volatile"))
    )
    assert host_foundation.journal_is_persistent() is False


def test_journal_files_left_by_an_old_configuration_do_not_count(
    monkeypatch,
) -> None:
    """The defect this check was shipped with, kept from coming back.

    The first version asked the filesystem: does /var/log/journal hold a
    system.journal? Reverting to volatile storage leaves those files exactly
    where they are, so a Host that had stopped persisting still had one — and
    because this gates the foundation installer, such a Host was declared
    healthy and never given its drop-in back. Found on the real Pi by deleting
    the drop-in and watching `provision --apply` do nothing.
    """

    monkeypatch.setattr(
        primitives,
        "run",
        lambda *_a, **_k: subprocess.CompletedProcess(
            [], 0, _merged_journald_config("volatile"), ""
        ),
    )

    assert host_foundation.journal_is_persistent() is False


def test_persistent_journal_is_bounded_because_the_default_it_replaces_had_a_reason() -> None:
    content = host_foundation.JOURNAL_PERSISTENCE_CONTENT

    assert "Storage=persistent" in content
    # The Pi OS default exists to spare a finite card. Buying back the ability
    # to diagnose must not turn into an unbounded log that fills it.
    for bound in ("SystemMaxUse=", "SystemKeepFree=", "MaxRetentionSec="):
        assert bound in content
    # /etc outranks the /usr/lib drop-in it is there to override.
    assert str(host_foundation.JOURNAL_PERSISTENCE).startswith("/etc/")


def test_installing_persistence_tells_journald_rather_than_only_writing_a_file(
    monkeypatch, tmp_path: Path
) -> None:
    commands: list[tuple[str, ...]] = []
    monkeypatch.setattr(host_foundation, "JOURNAL_PERSISTENCE", tmp_path / "conf.d/50-eidolon.conf")
    monkeypatch.setattr(host_foundation, "JOURNAL_DIRECTORY", tmp_path / "journal")
    monkeypatch.setattr(
        primitives,
        "checked",
        lambda _operation, command, **_kwargs: (
            commands.append(tuple(command)) or subprocess.CompletedProcess(command, 0, "", "")
        ),
    )
    monkeypatch.setattr(primitives, "run", lambda *_a, **_k: None)

    host_foundation_install.install_journal_persistence(host_foundation.JOURNAL_PERSISTENCE_CONTENT)

    # Writing the file and stopping there would leave a Host that reads as
    # fixed while its logs are still in memory.
    assert any("systemd-journald" in " ".join(command) for command in commands)
    assert host_foundation.JOURNAL_PERSISTENCE.is_file()


def test_journal_persistence_survives_a_reset_like_the_rest_of_the_foundation() -> None:
    """A reset returns the product to clean, not the machine to factory.

    The foundation — packages, pinned binaries, the evidence file — is
    deliberately outside the reset roots, and journal persistence belongs with
    it. Removing it would make a Host undiagnosable again at exactly the
    moment someone is reinstalling because something went wrong, which is the
    opposite of why it is installed.
    """

    assert host_foundation.JOURNAL_PERSISTENCE not in set(contract.MANAGED_SYSTEM_ASSETS)
    assert not any(
        root == host_foundation.JOURNAL_PERSISTENCE
        or root in host_foundation.JOURNAL_PERSISTENCE.parents
        for root in contract.RESET_AUTHORITY_ROOTS
    )
    assert not any(
        root in host_foundation.JOURNAL_PERSISTENCE.parents
        for root in contract.RESET_DEPLOYMENT_ROOTS
    )


def test_a_profile_is_a_row_and_not_a_rewrite() -> None:
    """What a second board has to fill in, stated as a test.

    Every field here was once an assumption the first board made silently. The
    list is the cost of adding a board, and it should stay visible: a field
    that quietly acquires a default is a field the next board inherits from
    the Raspberry Pi without anyone deciding it should.
    """

    from dataclasses import fields

    from eidolon_ops.foundation import FoundationProfile

    assert {field.name for field in fields(FoundationProfile)} == {
        "id",
        "display_name",
        "architecture",
        "os_ids",
        "os_versions",
        "hardware_model_match",
        "hardware_display_name",
        "minimum_memory_kib",
        "minimum_disk_kib",
        "minimum_memory_label",
        "minimum_disk_label",
        "apt_suite",
        "apt_mirrors",
        "apt_sources",
        "apt_packages",
        "bootstrap_packages",
        "services",
        "journal_persistence",
        "journal_persistence_content",
        "artifacts",
    }
    assert all(field.default is MISSING for field in fields(FoundationProfile)), (
        "a default would let the next board inherit this one's answer silently"
    )


def test_the_registry_is_keyed_by_the_id_a_host_config_names() -> None:
    from eidolon_ops.foundation import FOUNDATION_PROFILES

    for profile_id, profile in FOUNDATION_PROFILES.items():
        assert profile_id == profile.id


def test_an_unregistered_profile_is_refused_with_the_choices() -> None:
    from eidolon_ops.foundation import foundation_profile

    with pytest.raises(KeyError, match="must be a reviewed profile"):
        foundation_profile("ubuntu-rk3588-arm64-v1")


def test_the_two_profiles_gate_on_different_boards() -> None:
    """Each refuses the other's hardware, which is the point of having two.

    Both gate scripts were run on the real Orange Pi 5 Max: the RK3588 profile
    passed every check, and the Raspberry Pi profile refused it on the OS gate.
    """

    from eidolon_ops.foundation import (
        RASPBERRY_PI_OS_TRIXIE,
        UBUNTU_2604_RK3588,
        python_bootstrap_script,
    )

    pi = python_bootstrap_script(RASPBERRY_PI_OS_TRIXIE).decode()
    rk = python_bootstrap_script(UBUNTU_2604_RK3588).decode()

    assert "debian:13|raspbian:13" in pi
    assert "ubuntu:26" in rk
    assert "Raspberry\\ Pi*" in pi
    assert "RK3588*" in rk
    # 26.04 must appear as its major version, because the script truncates.
    assert "ubuntu:26.04" not in rk


def test_each_profile_carries_its_own_journal_override_path() -> None:
    """Not a detail: where volatile is set differs, so the drop-in must win.

    Raspberry Pi OS sets it in a vendor drop-in; Armbian sets it in the main
    journald.conf. Verified on the board that a drop-in still wins there, and
    that a vendor file named syslog.conf sorts after a 50- prefix — hence 99-.
    """

    from eidolon_ops.foundation import RASPBERRY_PI_OS_TRIXIE, UBUNTU_2604_RK3588

    assert RASPBERRY_PI_OS_TRIXIE.journal_persistence.name.startswith("50-")
    assert UBUNTU_2604_RK3588.journal_persistence.name.startswith("99-")
    for profile in (RASPBERRY_PI_OS_TRIXIE, UBUNTU_2604_RK3588):
        assert "Storage=persistent" in profile.journal_persistence_content


def test_a_profile_names_a_mirror_that_serves_its_own_suite() -> None:
    """A stanza whose suite the mirror does not carry fails at apt update."""

    from eidolon_ops.foundation import FOUNDATION_PROFILES

    for profile in FOUNDATION_PROFILES.values():
        for source in profile.apt_sources:
            rendered = source.suites.format(suite=profile.apt_suite)
            assert profile.apt_suite in rendered
            assert source.uris in profile.apt_mirrors.values()
