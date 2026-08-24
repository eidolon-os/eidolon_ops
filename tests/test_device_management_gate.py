from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

import pytest

from eidolon_ops.device_management_gate import (
    ARTIFACT_SOURCES,
    HIL_STEPS,
    PROVENANCE_CONTRACT,
    REPOSITORIES,
    GateError,
    capture_template,
    hil_template,
    main,
    verify_hil,
    verify_release,
)

_NON_PARTICIPANT = "eidolon-official-site"
_ARTIFACT_ONLY = {
    source: source
    for sources in ARTIFACT_SOURCES.values()
    for source in sources
    if source not in REPOSITORIES
}
_TEST_INVENTORY = {
    **REPOSITORIES,
    **_ARTIFACT_ONLY,
    _NON_PARTICIPANT: _NON_PARTICIPANT,
}


def _run(*command: str, cwd: Path) -> None:
    subprocess.run(command, cwd=cwd, check=True, capture_output=True)


def _workspace(tmp_path: Path) -> Path:
    for relative in _TEST_INVENTORY.values():
        repo = tmp_path / relative
        repo.mkdir(parents=True)
        _run("git", "init", "-b", "main", cwd=repo)
        _run("git", "config", "user.email", "ops-test@example.invalid", cwd=repo)
        _run("git", "config", "user.name", "Ops Test", cwd=repo)
        (repo / "source.txt").write_text(f"{relative}\n", encoding="utf-8")
        if relative == "eidolon_sdk":
            inventory = repo / "contracts/device_foundation/v1/baseline/cross-repo-heads.v1.json"
            inventory.parent.mkdir(parents=True)
            inventory.write_text(
                json.dumps(
                    {
                        "repositories": [
                            {"repo": name, "path": path, "reason": "test inventory"}
                            for name, path in _TEST_INVENTORY.items()
                        ]
                    }
                )
                + "\n",
                encoding="utf-8",
            )
        _run("git", "add", ".", cwd=repo)
        _run("git", "commit", "-m", "fixture", cwd=repo)
    return tmp_path


def _passing_release(tmp_path: Path) -> dict[str, object]:
    document = capture_template(_workspace(tmp_path), "dm-v1")
    repositories = document["repositories"]
    assert isinstance(repositories, dict)
    for name, item in repositories.items():
        assert isinstance(item, dict)
        receipts = item["required_tests"]
        assert isinstance(receipts, list)
        for receipt in receipts:
            evidence = tmp_path / f"test-{name}-{receipt['id']}.log"
            evidence.write_text(f"PASS {name}:{receipt['id']}\n", encoding="utf-8")
            receipt["status"] = "passed"
            receipt["evidence_path"] = str(evidence)
            receipt["evidence_digest"] = hashlib.sha256(evidence.read_bytes()).hexdigest()
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
        provenance = tmp_path / f"{name}.provenance.json"
        provenance.write_text(
            json.dumps(
                {
                    "contract": PROVENANCE_CONTRACT,
                    "artifact_sha256": item["sha256"],
                    "release_identity": document["release_identity"],
                    "sources": item["sources"],
                },
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        item["provenance_path"] = str(provenance)
        item["provenance_sha256"] = hashlib.sha256(provenance.read_bytes()).hexdigest()
    return document


def test_release_gate_accepts_one_exact_identity_across_all_artifacts(tmp_path: Path) -> None:
    document = _passing_release(tmp_path)

    result = verify_release(document, tmp_path)

    assert result == {
        "status": "ready_for_device_management_release",
        "release_id": "dm-v1",
        "release_identity": document["release_identity"],
        "repositories": 13,
        "artifacts": ["esp32_firmware", "mobile_apk", "pi5_release"],
    }


def test_formal_non_participant_is_explicit_and_has_no_release_tests(tmp_path: Path) -> None:
    document = capture_template(_workspace(tmp_path), "dm-v1")
    entry = document["repositories"][_NON_PARTICIPANT]  # type: ignore[index]

    assert entry["participation"] is False
    assert entry["reason"]
    assert entry["required_tests"] == []


def test_release_gate_fails_closed_on_pending_test(tmp_path: Path) -> None:
    document = _passing_release(tmp_path)
    document["repositories"]["eidolon_sdk"]["required_tests"][0]["status"] = "pending"  # type: ignore[index]

    with pytest.raises(GateError, match="required test has no passing receipt"):
        verify_release(document, tmp_path)


def test_release_gate_hashes_real_test_evidence(tmp_path: Path) -> None:
    document = _passing_release(tmp_path)
    receipt = document["repositories"]["eidolon_sdk"]["required_tests"][0]  # type: ignore[index]
    Path(receipt["evidence_path"]).write_text("not the tested output\n", encoding="utf-8")

    with pytest.raises(GateError, match="required test evidence drifted"):
        verify_release(document, tmp_path)


def test_release_gate_fails_closed_on_repository_set_drift(tmp_path: Path) -> None:
    document = _passing_release(tmp_path)
    del document["repositories"]["eidolon_hub"]  # type: ignore[index]

    with pytest.raises(GateError, match="repository set drifted"):
        verify_release(document, tmp_path)


def test_capture_fails_on_git_repository_missing_from_foundation_inventory(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    extra = workspace / "unexpected-checkout"
    extra.mkdir()
    _run("git", "init", "-b", "main", cwd=extra)

    with pytest.raises(GateError, match=r"added=\['unexpected-checkout'\]"):
        capture_template(workspace, "dm-v1")


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


def test_release_gate_reads_build_provenance_instead_of_trusting_manifest(
    tmp_path: Path,
) -> None:
    document = _passing_release(tmp_path)
    artifact = document["artifacts"]["esp32_firmware"]  # type: ignore[index]
    provenance_path = Path(artifact["provenance_path"])
    provenance = json.loads(provenance_path.read_text(encoding="utf-8"))
    provenance["sources"]["eidolon_esp32"] = "0" * 40
    provenance_path.write_text(json.dumps(provenance) + "\n", encoding="utf-8")
    artifact["provenance_sha256"] = hashlib.sha256(provenance_path.read_bytes()).hexdigest()

    with pytest.raises(GateError, match="provenance does not bind release inputs"):
        verify_release(document, tmp_path)


def test_hil_gate_requires_ordered_evidence_and_explicit_erase_confirmation(
    tmp_path: Path,
) -> None:
    release = _passing_release(tmp_path)
    evidence = hil_template(release, "box-3")
    steps = evidence["steps"]
    assert isinstance(steps, list)
    for step in steps:
        evidence_path = tmp_path / f"hil-{step['id']}.log"
        evidence_path.write_text(f"PASS {step['id']}\n", encoding="utf-8")
        step["status"] = "passed"
        step["evidence_path"] = str(evidence_path)
        step["evidence_digest"] = hashlib.sha256(evidence_path.read_bytes()).hexdigest()

    result = verify_hil(evidence, release)

    assert result["status"] == "device_management_hil_passed"
    assert result["steps"] == len(HIL_STEPS)

    online_confirmation = next(
        step for step in steps if step["id"] == "confirm_online_remove"
    )
    online_confirmation["confirmation"] = "yes"
    with pytest.raises(GateError, match="destructive erase was not explicitly confirmed"):
        verify_hil(evidence, release)


def test_hil_gate_hashes_each_step_evidence(tmp_path: Path) -> None:
    release = _passing_release(tmp_path)
    evidence = hil_template(release, "box-3")
    steps = evidence["steps"]
    assert isinstance(steps, list)
    for step in steps:
        path = tmp_path / f"hil-{step['id']}.log"
        path.write_text(f"PASS {step['id']}\n", encoding="utf-8")
        step["status"] = "passed"
        step["evidence_path"] = str(path)
        step["evidence_digest"] = hashlib.sha256(path.read_bytes()).hexdigest()
    Path(steps[-1]["evidence_path"]).write_text("tampered\n", encoding="utf-8")

    with pytest.raises(GateError, match="HIL evidence drifted: old_generation_rejected"):
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
