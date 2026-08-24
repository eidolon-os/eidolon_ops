from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

import pytest

from eidolon_ops.device_management_gate import (
    ARTIFACT_SOURCES,
    HIL_STEPS,
    REPOSITORIES,
    GateError,
    capture_template,
    hil_template,
    main,
    verify_hil,
    verify_release,
)


def _run(*command: str, cwd: Path) -> None:
    subprocess.run(command, cwd=cwd, check=True, capture_output=True)


def _workspace(tmp_path: Path) -> Path:
    for relative in REPOSITORIES.values():
        repo = tmp_path / relative
        repo.mkdir(parents=True)
        _run("git", "init", "-b", "main", cwd=repo)
        _run("git", "config", "user.email", "ops-test@example.invalid", cwd=repo)
        _run("git", "config", "user.name", "Ops Test", cwd=repo)
        (repo / "source.txt").write_text(f"{relative}\n", encoding="utf-8")
        _run("git", "add", "source.txt", cwd=repo)
        _run("git", "commit", "-m", "fixture", cwd=repo)
    return tmp_path


def _passing_release(tmp_path: Path) -> dict[str, object]:
    document = capture_template(_workspace(tmp_path), "dm-v1")
    repositories = document["repositories"]
    assert isinstance(repositories, dict)
    for item in repositories.values():
        assert isinstance(item, dict)
        receipts = item["required_tests"]
        assert isinstance(receipts, list)
        for receipt in receipts:
            receipt["status"] = "passed"
            receipt["evidence_digest"] = "a" * 64
    artifacts = document["artifacts"]
    assert isinstance(artifacts, dict)
    for name, sources in ARTIFACT_SOURCES.items():
        artifact = tmp_path / f"{name}.artifact"
        artifact.write_bytes(f"{name}\n".encode())
        item = artifacts[name]
        assert isinstance(item, dict)
        item["path"] = str(artifact)
        item["sha256"] = hashlib.sha256(artifact.read_bytes()).hexdigest()
        item["sources"] = {
            source: repositories[source]["sha"]  # type: ignore[index]
            for source in sources
        }
    return document


def test_release_gate_accepts_one_exact_identity_across_all_artifacts(tmp_path: Path) -> None:
    document = _passing_release(tmp_path)

    result = verify_release(document, tmp_path)

    assert result == {
        "status": "ready_for_device_management_release",
        "release_id": "dm-v1",
        "release_identity": document["release_identity"],
        "repositories": 9,
        "artifacts": ["esp32_firmware", "mobile_apk", "pi5_release"],
    }


def test_release_gate_fails_closed_on_pending_test(tmp_path: Path) -> None:
    document = _passing_release(tmp_path)
    document["repositories"]["eidolon_sdk"]["required_tests"][0]["status"] = "pending"  # type: ignore[index]

    with pytest.raises(GateError, match="required test has no passing receipt"):
        verify_release(document, tmp_path)


def test_release_gate_fails_closed_on_repository_set_drift(tmp_path: Path) -> None:
    document = _passing_release(tmp_path)
    del document["repositories"]["eidolon_hub"]  # type: ignore[index]

    with pytest.raises(GateError, match="repository set drifted"):
        verify_release(document, tmp_path)


def test_release_gate_fails_closed_on_dirty_source(tmp_path: Path) -> None:
    document = _passing_release(tmp_path)
    (tmp_path / "eidolon_hub/untracked.txt").write_text("drift\n", encoding="utf-8")

    with pytest.raises(GateError, match="repository is dirty: eidolon_hub"):
        verify_release(document, tmp_path)


def test_release_gate_fails_closed_on_artifact_or_source_drift(tmp_path: Path) -> None:
    document = _passing_release(tmp_path)
    Path(document["artifacts"]["mobile_apk"]["path"]).write_text("changed\n")  # type: ignore[index]

    with pytest.raises(GateError, match="installed artifact digest drifted: mobile_apk"):
        verify_release(document, tmp_path)


def test_hil_gate_requires_ordered_evidence_and_explicit_erase_confirmation(
    tmp_path: Path,
) -> None:
    release = _passing_release(tmp_path)
    evidence = hil_template(release, "box-3")
    steps = evidence["steps"]
    assert isinstance(steps, list)
    for step in steps:
        step["status"] = "passed"
        step["evidence_digest"] = "b" * 64

    result = verify_hil(evidence, release)

    assert result["status"] == "device_management_hil_passed"
    assert result["steps"] == len(HIL_STEPS)

    steps[7]["confirmation"] = "yes"
    with pytest.raises(GateError, match="destructive erase was not explicitly confirmed"):
        verify_hil(evidence, release)


def test_hil_gate_refuses_pause_or_reordering(tmp_path: Path) -> None:
    release = _passing_release(tmp_path)
    evidence = hil_template(release, "box-3")

    with pytest.raises(GateError, match="not backed by passing evidence"):
        verify_hil(evidence, release)

    evidence["steps"][0], evidence["steps"][1] = evidence["steps"][1], evidence["steps"][0]  # type: ignore[index]
    with pytest.raises(GateError, match="reordered"):
        verify_hil(evidence, release)


def test_cli_template_is_non_deploying_and_verify_reports_blocked(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    workspace = _workspace(tmp_path / "workspace")
    manifest = tmp_path / "release.json"
    assert (
        main(
            (
                "template",
                "--workspace-root",
                str(workspace),
                "--release-id",
                "dm-v1",
                "--output",
                str(manifest),
            )
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["status"] == "captured_pending_evidence"

    assert main(("verify", "--workspace-root", str(workspace), str(manifest))) == 2
    assert json.loads(capsys.readouterr().out)["status"] == "blocked"
