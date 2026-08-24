"""Fail-closed release evidence for the device-management vertical slice.

The native Pi release remains owned by :mod:`release_transaction`.  This
module only binds that release to the Mobile APK and Box3 firmware built from
the same reviewed source set, and validates the manual HIL evidence produced
after activation.  It deliberately does not deploy or talk to a device.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

CONTRACT = "eidolon.device-management.release-gate.v1"
HIL_CONTRACT = "eidolon.device-management.hil-evidence.v1"
_HEX_40 = frozenset("0123456789abcdef")
_HEX_64 = _HEX_40

REPOSITORIES: Mapping[str, str] = {
    "docs": "docs",
    "eidolon_sdk": "eidolon_sdk",
    "eidolon_hub": "eidolon_hub",
    "eidolon_kernel": "eidolon_kernel",
    "eidolon_admin": "eidolon_admin",
    "eidolon_channel": "eidolon_channel",
    "eidolon_esp32": "eidolon-client-esp32",
    "eidolon_client_mobile": "eidolon_client_mobile",
    "eidolon_ops": "eidolon_ops",
}

REQUIRED_TESTS: Mapping[str, tuple[str, ...]] = {
    "docs": ("device-management-design-gate",),
    "eidolon_sdk": ("conformance", "generator-clean", "contract-tests"),
    "eidolon_hub": ("tests", "ruff"),
    "eidolon_kernel": ("tests",),
    "eidolon_admin": ("server-tests", "web-tests", "web-typecheck"),
    "eidolon_channel": ("tests",),
    "eidolon_esp32": ("host-tests", "box3-build"),
    "eidolon_client_mobile": ("analyze", "tests", "apk-build"),
    "eidolon_ops": ("tests", "ruff", "device-management-gate"),
}

ARTIFACT_SOURCES: Mapping[str, tuple[str, ...]] = {
    "pi5_release": (
        "eidolon_sdk",
        "eidolon_hub",
        "eidolon_kernel",
        "eidolon_admin",
        "eidolon_channel",
        "eidolon_ops",
    ),
    "mobile_apk": ("eidolon_sdk", "eidolon_client_mobile"),
    "esp32_firmware": ("eidolon_sdk", "eidolon_esp32"),
}

HIL_STEPS = (
    "release_identity_proven",
    "commissioning_started",
    "proposal_observed",
    "approval_recorded",
    "grant_ack_observed",
    "claim_active_observed",
    "mount_active_observed",
    "confirm_online_remove",
    "online_remove_requested",
    "platform_revoked_observed",
    "unmount_observed",
    "online_erase_ack_observed",
    "device_rejoined",
    "device_taken_offline",
    "confirm_offline_remove",
    "offline_remove_requested",
    "offline_platform_revoked_observed",
    "offline_unmount_observed",
    "erase_pending_observed",
    "device_reconnected",
    "offline_erase_ack_observed",
    "old_request_rejected",
    "old_generation_rejected",
)
_CONFIRMATION_STEPS = {"confirm_online_remove", "confirm_offline_remove"}


class GateError(ValueError):
    """Release or HIL evidence is incomplete, stale, or ambiguous."""


def _json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise GateError(f"cannot read one JSON document: {path}") from exc
    if not isinstance(value, dict):
        raise GateError(f"JSON document must be an object: {path}")
    return value


def _git(repo: Path, *arguments: str, binary: bool = False) -> bytes | str:
    result = subprocess.run(
        ("git", "-C", str(repo), *arguments),
        check=False,
        capture_output=True,
    )
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", errors="replace").strip()
        raise GateError(f"git {' '.join(arguments)} failed for {repo}: {detail}")
    return result.stdout if binary else result.stdout.decode("utf-8").strip()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(block)
    except OSError as exc:
        raise GateError(f"cannot hash artifact: {path}") from exc
    return digest.hexdigest()


def _source_digest(repo: Path, revision: str) -> str:
    archive = _git(repo, "archive", "--format=tar", revision, binary=True)
    assert isinstance(archive, bytes)
    return hashlib.sha256(archive).hexdigest()


def _is_hex(value: object, length: int) -> bool:
    return isinstance(value, str) and len(value) == length and set(value) <= _HEX_64


def _canonical_identity(repositories: Mapping[str, object]) -> str:
    identity_input: dict[str, object] = {}
    for name in REPOSITORIES:
        item = repositories.get(name)
        if not isinstance(item, dict):
            raise GateError(f"repository entry must be an object: {name}")
        identity_input[name] = {
            "branch": item.get("branch"),
            "sha": item.get("sha"),
            "artifact_digest": item.get("artifact_digest"),
            # Pass/fail evidence is verified below but is intentionally not
            # part of the identity handed to builds: completing a test after
            # capture must not change the identity embedded in its artifacts.
            "required_tests": [
                receipt.get("id")
                for receipt in item.get("required_tests", [])
                if isinstance(receipt, dict)
            ],
        }
    encoded = json.dumps(
        identity_input, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def capture_template(workspace_root: Path, release_id: str) -> dict[str, object]:
    """Capture exact source facts and leave tests/artifacts visibly pending."""

    if not release_id or len(release_id) > 64:
        raise GateError("release_id must contain 1..64 characters")
    repositories: dict[str, object] = {}
    for name, relative in REPOSITORIES.items():
        repo = workspace_root / relative
        if not (repo / ".git").exists():
            raise GateError(f"required repository is missing: {name}: {repo}")
        sha = _git(repo, "rev-parse", "HEAD")
        branch = _git(repo, "branch", "--show-current")
        dirty = bool(_git(repo, "status", "--porcelain"))
        assert isinstance(sha, str) and isinstance(branch, str)
        repositories[name] = {
            "path": str(repo.resolve()),
            "branch": branch,
            "sha": sha,
            "dirty": dirty,
            "participation": "required",
            "reason": "device-management vertical release participant",
            "required_tests": [
                {"id": test_id, "status": "pending", "evidence_digest": None}
                for test_id in REQUIRED_TESTS[name]
            ],
            "artifact_digest": _source_digest(repo, sha),
        }
    identity = _canonical_identity(repositories)
    return {
        "contract": CONTRACT,
        "release_id": release_id,
        "release_identity": identity,
        "repositories": repositories,
        "artifacts": {
            name: {
                "path": None,
                "sha256": None,
                "release_identity": identity,
                "sources": {
                    source: repositories[source]["sha"]  # type: ignore[index]
                    for source in sources
                },
            }
            for name, sources in ARTIFACT_SOURCES.items()
        },
    }


def verify_release(document: Mapping[str, object], workspace_root: Path) -> dict[str, object]:
    """Validate sources, test receipts and all three installable artifacts."""

    if document.get("contract") != CONTRACT:
        raise GateError(f"contract must be {CONTRACT}")
    repositories = document.get("repositories")
    if not isinstance(repositories, dict):
        raise GateError("repositories must be an object")
    if set(repositories) != set(REPOSITORIES):
        missing = sorted(set(REPOSITORIES) - set(repositories))
        added = sorted(set(repositories) - set(REPOSITORIES))
        raise GateError(f"repository set drifted; missing={missing}, added={added}")

    for name, relative in REPOSITORIES.items():
        item = repositories[name]
        if not isinstance(item, dict):
            raise GateError(f"repository entry must be an object: {name}")
        repo = workspace_root / relative
        if Path(str(item.get("path"))).resolve() != repo.resolve():
            raise GateError(f"repository path drifted: {name}")
        branch = _git(repo, "branch", "--show-current")
        sha = _git(repo, "rev-parse", "HEAD")
        dirty = bool(_git(repo, "status", "--porcelain"))
        assert isinstance(branch, str) and isinstance(sha, str)
        if branch != "main" or item.get("branch") != branch:
            raise GateError(f"repository is not on captured main: {name}")
        if not _is_hex(item.get("sha"), 40) or item.get("sha") != sha:
            raise GateError(f"repository HEAD drifted: {name}")
        if dirty or item.get("dirty") is not False:
            raise GateError(f"repository is dirty: {name}")
        if item.get("participation") != "required" or not item.get("reason"):
            raise GateError(f"repository participation is not explicit: {name}")
        expected_digest = _source_digest(repo, sha)
        if item.get("artifact_digest") != expected_digest:
            raise GateError(f"source artifact digest drifted: {name}")
        receipts = item.get("required_tests")
        if not isinstance(receipts, list):
            raise GateError(f"required_tests must be a list: {name}")
        receipt_ids = [receipt.get("id") for receipt in receipts if isinstance(receipt, dict)]
        if receipt_ids != list(REQUIRED_TESTS[name]) or len(receipt_ids) != len(receipts):
            raise GateError(f"required test set drifted: {name}")
        for receipt in receipts:
            assert isinstance(receipt, dict)
            if receipt.get("status") != "passed" or not _is_hex(receipt.get("evidence_digest"), 64):
                raise GateError(f"required test has no passing receipt: {name}:{receipt.get('id')}")

    identity = _canonical_identity(repositories)
    if document.get("release_identity") != identity:
        raise GateError("release_identity does not bind the captured source/test policy")
    artifacts = document.get("artifacts")
    if not isinstance(artifacts, dict) or set(artifacts) != set(ARTIFACT_SOURCES):
        raise GateError("artifact set must be exactly pi5_release, mobile_apk and esp32_firmware")
    for name, expected_sources in ARTIFACT_SOURCES.items():
        item = artifacts[name]
        if not isinstance(item, dict):
            raise GateError(f"artifact entry must be an object: {name}")
        path_value = item.get("path")
        if not isinstance(path_value, str):
            raise GateError(f"artifact path is missing: {name}")
        path = Path(path_value)
        if not path.is_absolute() or not path.is_file() or path.is_symlink():
            raise GateError(f"artifact must be an absolute regular file: {name}")
        if item.get("sha256") != _sha256_file(path):
            raise GateError(f"installed artifact digest drifted: {name}")
        if item.get("release_identity") != identity:
            raise GateError(f"artifact release identity drifted: {name}")
        sources = item.get("sources")
        expected = {source: repositories[source]["sha"] for source in expected_sources}
        if sources != expected:
            raise GateError(f"artifact source commits drifted: {name}")
    return {
        "status": "ready_for_device_management_release",
        "release_id": document.get("release_id"),
        "release_identity": identity,
        "repositories": len(repositories),
        "artifacts": sorted(artifacts),
    }


def hil_template(release_document: Mapping[str, object], device_id: str) -> dict[str, object]:
    identity = release_document.get("release_identity")
    if not _is_hex(identity, 64):
        raise GateError("release manifest has no valid release_identity")
    if not device_id:
        raise GateError("device_id is required")
    return {
        "contract": HIL_CONTRACT,
        "release_identity": identity,
        "device_id": device_id,
        "steps": [
            {
                "id": step,
                "status": "paused" if step in _CONFIRMATION_STEPS else "pending",
                "evidence_digest": None,
                **(
                    {"confirmation": f"ERASE {device_id} FOR {identity}"}
                    if step in _CONFIRMATION_STEPS
                    else {}
                ),
            }
            for step in HIL_STEPS
        ],
    }


def verify_hil(
    evidence: Mapping[str, object], release_document: Mapping[str, object]
) -> dict[str, object]:
    if evidence.get("contract") != HIL_CONTRACT:
        raise GateError(f"HIL contract must be {HIL_CONTRACT}")
    identity = release_document.get("release_identity")
    if evidence.get("release_identity") != identity:
        raise GateError("HIL evidence belongs to another release identity")
    device_id = evidence.get("device_id")
    if not isinstance(device_id, str) or not device_id:
        raise GateError("HIL device_id is missing")
    steps = evidence.get("steps")
    if not isinstance(steps, list):
        raise GateError("HIL steps must be a list")
    ids = [step.get("id") for step in steps if isinstance(step, dict)]
    if ids != list(HIL_STEPS) or len(ids) != len(steps):
        raise GateError("HIL steps are missing, added, duplicated, or reordered")
    for step in steps:
        assert isinstance(step, dict)
        step_id = step["id"]
        if step.get("status") != "passed" or not _is_hex(step.get("evidence_digest"), 64):
            raise GateError(f"HIL step is not backed by passing evidence: {step_id}")
        if step_id in _CONFIRMATION_STEPS:
            expected = f"ERASE {device_id} FOR {identity}"
            if step.get("confirmation") != expected:
                raise GateError(f"destructive erase was not explicitly confirmed: {step_id}")
    return {
        "status": "device_management_hil_passed",
        "release_identity": identity,
        "device_id": device_id,
        "steps": len(steps),
    }


def _write(path: Path, document: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Eidolon device-management release/HIL gate")
    commands = parser.add_subparsers(dest="command", required=True)
    template = commands.add_parser("template", help="capture exact source HEADs")
    template.add_argument("--workspace-root", type=Path, required=True)
    template.add_argument("--release-id", required=True)
    template.add_argument("--output", type=Path, required=True)
    verify = commands.add_parser("verify", help="verify final release evidence")
    verify.add_argument("--workspace-root", type=Path, required=True)
    verify.add_argument("manifest", type=Path)
    hil = commands.add_parser("hil-template", help="create a pausable HIL checklist")
    hil.add_argument("--device-id", required=True)
    hil.add_argument("--output", type=Path, required=True)
    hil.add_argument("manifest", type=Path)
    hil_verify = commands.add_parser("hil-verify", help="verify completed ordered HIL evidence")
    hil_verify.add_argument("manifest", type=Path)
    hil_verify.add_argument("evidence", type=Path)
    return parser


def main(arguments: Sequence[str] | None = None) -> int:
    parsed = _parser().parse_args(arguments)
    try:
        if parsed.command == "template":
            document = capture_template(parsed.workspace_root, parsed.release_id)
            _write(parsed.output, document)
            result = {"status": "captured_pending_evidence", "output": str(parsed.output)}
        elif parsed.command == "verify":
            result = verify_release(_json(parsed.manifest), parsed.workspace_root)
        elif parsed.command == "hil-template":
            document = hil_template(_json(parsed.manifest), parsed.device_id)
            _write(parsed.output, document)
            result = {"status": "hil_paused", "output": str(parsed.output)}
        else:
            result = verify_hil(_json(parsed.evidence), _json(parsed.manifest))
    except GateError as exc:
        print(json.dumps({"status": "blocked", "error": str(exc)}, sort_keys=True))
        return 2
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
