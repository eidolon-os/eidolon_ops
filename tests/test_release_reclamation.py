from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from eidolon_ops.hostagent import contract, reclamation
from eidolon_ops.hostagent.primitives import TargetError

pytestmark = pytest.mark.component


def _host(root: Path, release_ids: tuple[str, ...]) -> None:
    for release_id in release_ids:
        component = root / contract.RELEASES.relative_to("/") / release_id / "kernel"
        component.mkdir(parents=True)
        (component / "code.py").write_text(release_id, encoding="utf-8")
    (root / contract.VAR_TMP.relative_to("/")).mkdir(parents=True, exist_ok=True)


def _link(root: Path, name: str, release_id: str, component: str = "kernel") -> Path:
    current = root / contract.CURRENT_ROOT.relative_to("/")
    current.mkdir(parents=True, exist_ok=True)
    link = current / name
    link.unlink(missing_ok=True)
    link.symlink_to(root / contract.RELEASES.relative_to("/") / release_id / component)
    return link


def _stage(root: Path, kind: str, release_id: str) -> Path:
    path = root / contract.VAR_TMP.relative_to("/") / f"eidolon-{kind}-{release_id}"
    path.mkdir(parents=True, mode=0o700)
    path.chmod(0o700)
    payload = path / "payload"
    payload.write_bytes(b"payload")
    payload.chmod(0o600)
    return path


def _seal(root: Path, release_id: str) -> None:
    release = root / contract.RELEASES.relative_to("/") / release_id
    (release / "release.json").write_text("{}", encoding="utf-8")
    (release / "release.json.sha256").write_text("digest", encoding="utf-8")


def _payload(release_id: str, phase: str = "prepare") -> dict[str, object]:
    return {
        "release_id": release_id,
        "phase": phase,
        "required_bytes": 1024,
        "reserve_bytes": 1024,
    }


def test_prepare_reclaims_only_old_release_and_staging(tmp_path: Path) -> None:
    _host(tmp_path, ("active", "candidate", "stale"))
    _link(tmp_path, "eidolon_kernel", "active")
    for release_id in ("candidate", "stale"):
        _stage(tmp_path, "release", release_id)
        _stage(tmp_path, "secrets", release_id)
    authority = tmp_path / "var/lib/eidolon/eidolon-system.sqlite3"
    log = tmp_path / "var/log/eidolon/service.log"
    model = tmp_path / "var/lib/eidolon/models/encoder/model.onnx"
    for path in (authority, log, model):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("keep", encoding="utf-8")

    result = reclamation.reclaim(_payload("candidate"), root=tmp_path)

    assert result["status"] == "ready"
    assert result["active_release_ids"] == ["active"]
    assert result["protected_release_ids"] == ["active", "candidate"]
    assert result["removed"] == {
        "releases": ["/opt/eidolon/releases/stale"],
        "uploads": ["/var/tmp/eidolon-release-stale"],
        "secrets": ["/var/tmp/eidolon-secrets-stale"],
    }
    assert (tmp_path / "opt/eidolon/releases/active").is_dir()
    assert (tmp_path / "opt/eidolon/releases/candidate").is_dir()
    assert (tmp_path / "var/tmp/eidolon-release-candidate").is_dir()
    assert (tmp_path / "var/tmp/eidolon-secrets-candidate").is_dir()
    assert all(path.read_text(encoding="utf-8") == "keep" for path in (authority, log, model))


def test_commit_keeps_only_new_active_and_cleans_candidate_staging(tmp_path: Path) -> None:
    _host(tmp_path, ("old", "candidate"))
    _link(tmp_path, "eidolon_kernel", "old")
    _stage(tmp_path, "release", "candidate")
    _stage(tmp_path, "secrets", "candidate")
    _seal(tmp_path, "candidate")
    reclamation.reclaim(_payload("candidate"), root=tmp_path)
    _link(tmp_path, "eidolon_kernel", "candidate")

    result = reclamation.reclaim(_payload("candidate", "commit"), root=tmp_path)

    assert result["status"] == "committed"
    assert sorted(path.name for path in (tmp_path / "opt/eidolon/releases").iterdir()) == [
        "candidate"
    ]
    assert not (tmp_path / "var/tmp/eidolon-release-candidate").exists()
    assert not (tmp_path / "var/tmp/eidolon-secrets-candidate").exists()


def test_abort_preserves_restored_active_and_removes_failed_candidate(tmp_path: Path) -> None:
    _host(tmp_path, ("old", "candidate"))
    _link(tmp_path, "eidolon_kernel", "old")
    _stage(tmp_path, "release", "candidate")
    _stage(tmp_path, "secrets", "candidate")
    reclamation.reclaim(_payload("candidate"), root=tmp_path)

    result = reclamation.reclaim(_payload("candidate", "abort"), root=tmp_path)

    assert result["status"] == "aborted"
    assert (tmp_path / "opt/eidolon/releases/old").is_dir()
    assert not (tmp_path / "opt/eidolon/releases/candidate").exists()
    assert not (tmp_path / "var/tmp/eidolon-release-candidate").exists()
    assert not (tmp_path / "var/tmp/eidolon-secrets-candidate").exists()


def test_abort_refuses_to_delete_a_candidate_still_referenced_by_current(
    tmp_path: Path,
) -> None:
    _host(tmp_path, ("old", "candidate"))
    _link(tmp_path, "eidolon_kernel", "old")
    reclamation.reclaim(_payload("candidate"), root=tmp_path)
    _link(tmp_path, "eidolon_kernel", "candidate")

    with pytest.raises(TargetError, match="still referenced"):
        reclamation.reclaim(_payload("candidate", "abort"), root=tmp_path)

    assert (tmp_path / "opt/eidolon/releases/candidate").is_dir()


def test_all_partial_current_links_protect_their_real_release_targets(tmp_path: Path) -> None:
    _host(tmp_path, ("core", "expanded", "candidate", "stale"))
    _link(tmp_path, "eidolon_kernel", "core")
    _link(tmp_path, "future_component", "expanded")

    result = reclamation.reclaim(_payload("candidate"), root=tmp_path)

    assert result["active_release_ids"] == ["core", "expanded"]
    assert result["active_targets"]["future_component"]["release_id"] == "expanded"
    assert (tmp_path / "opt/eidolon/releases/core").is_dir()
    assert (tmp_path / "opt/eidolon/releases/expanded").is_dir()
    assert not (tmp_path / "opt/eidolon/releases/stale").exists()
    with pytest.raises(TargetError, match="every current link"):
        reclamation.reclaim(_payload("candidate", "commit"), root=tmp_path)


@pytest.mark.parametrize("attack", ["release_symlink", "staging_symlink", "outside_current"])
def test_reclamation_rejects_path_attacks(tmp_path: Path, attack: str) -> None:
    _host(tmp_path, ("active", "candidate"))
    _link(tmp_path, "eidolon_kernel", "active")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep").write_text("keep", encoding="utf-8")
    if attack == "release_symlink":
        (tmp_path / "opt/eidolon/releases/evil").symlink_to(outside, target_is_directory=True)
    elif attack == "staging_symlink":
        (tmp_path / "var/tmp/eidolon-release-evil").symlink_to(
            outside, target_is_directory=True
        )
    else:
        _link(tmp_path, "eidolon_kernel", "active").unlink()
        (tmp_path / "opt/eidolon/current/eidolon_kernel").symlink_to(
            outside, target_is_directory=True
        )

    with pytest.raises(TargetError, match=r"symlink|outside releases|real directory"):
        reclamation.reclaim(_payload("candidate"), root=tmp_path)

    assert (outside / "keep").read_text(encoding="utf-8") == "keep"


def test_reclamation_rejects_non_conventional_release_children(tmp_path: Path) -> None:
    _host(tmp_path, ("active", "candidate"))
    _link(tmp_path, "eidolon_kernel", "active")
    (tmp_path / "opt/eidolon/releases/.unowned").mkdir()

    with pytest.raises(TargetError, match="non-conventional"):
        reclamation.reclaim(_payload("candidate"), root=tmp_path)


@pytest.mark.parametrize("drift", ["directory-mode", "file-mode", "symlink"])
def test_secret_reclamation_fails_closed_on_private_stage_drift(
    tmp_path: Path, drift: str
) -> None:
    _host(tmp_path, ("active", "candidate"))
    _link(tmp_path, "eidolon_kernel", "active")
    first = _stage(tmp_path, "secrets", "first")
    unsafe = _stage(tmp_path, "secrets", "unsafe")
    if drift == "directory-mode":
        unsafe.chmod(0o755)
    elif drift == "file-mode":
        (unsafe / "payload").chmod(0o644)
    else:
        (unsafe / "payload").unlink()
        (unsafe / "payload").symlink_to(tmp_path / "outside")

    with pytest.raises(TargetError, match=r"ownership or mode|symlink"):
        reclamation.reclaim(_payload("candidate"), root=tmp_path)

    assert first.is_dir(), "validation must complete before any secret path is removed"
    assert unsafe.exists()


def test_reclamation_never_enters_deployment_receipts(tmp_path: Path) -> None:
    _host(tmp_path, ("active", "candidate"))
    _link(tmp_path, "eidolon_kernel", "active")
    receipt = tmp_path / "var/lib/eidolon/deployments/candidate-tx/receipt.json"
    receipt.parent.mkdir(parents=True)
    receipt.write_text('{"status":"activated"}\n', encoding="utf-8")
    _stage(tmp_path, "secrets", "stale")

    result = reclamation.reclaim(_payload("candidate"), root=tmp_path)

    assert result["removed"]["secrets"] == ["/var/tmp/eidolon-secrets-stale"]
    assert receipt.is_file()


def test_reclamation_is_idempotent(tmp_path: Path) -> None:
    _host(tmp_path, ("active", "candidate", "stale"))
    _link(tmp_path, "eidolon_kernel", "active")

    first = reclamation.reclaim(_payload("candidate"), root=tmp_path)
    second = reclamation.reclaim(_payload("candidate"), root=tmp_path)
    aborted = reclamation.reclaim(_payload("candidate", "abort"), root=tmp_path)
    repeated = reclamation.reclaim(_payload("candidate", "abort"), root=tmp_path)

    assert first["removed"]["releases"] == ["/opt/eidolon/releases/stale"]
    assert second["removed"] == {"releases": [], "uploads": [], "secrets": []}
    assert aborted["status"] == repeated["status"] == "aborted"
    assert repeated["removed"] == {"releases": [], "uploads": [], "secrets": []}


def test_successful_dry_run_retain_preserves_candidate_and_closes_in_flight_state(
    tmp_path: Path,
) -> None:
    _host(tmp_path, ("active", "candidate"))
    _link(tmp_path, "eidolon_kernel", "active")
    _stage(tmp_path, "release", "candidate")
    reclamation.reclaim(_payload("candidate"), root=tmp_path)

    result = reclamation.reclaim(_payload("candidate", "retain"), root=tmp_path)

    assert result["status"] == "retained"
    assert (tmp_path / "opt/eidolon/releases/candidate").is_dir()
    assert (tmp_path / "var/tmp/eidolon-release-candidate").is_dir()
    assert not (tmp_path / contract.RECLAMATION_STATE.relative_to("/")).exists()


def test_capacity_gate_returns_structured_insufficient_evidence(tmp_path: Path) -> None:
    _host(tmp_path, ("active", "stale"))
    _link(tmp_path, "eidolon_kernel", "active")
    usage = SimpleNamespace(total=10_000, used=9_500, free=500)

    result = reclamation.reclaim(
        _payload("candidate"), root=tmp_path, disk_usage=lambda _path: usage
    )

    assert result["status"] == "insufficient_capacity"
    assert result["capacity"] == {
        "path": "/opt/eidolon/releases",
        "total_bytes": 10_000,
        "free_bytes_before": 500,
        "free_bytes_after": 500,
        "requested_bytes": 1024,
        "effective_required_bytes": 1024,
        "reserve_bytes": 1024,
        "sufficient": False,
    }
    assert not (tmp_path / contract.RECLAMATION_STATE.relative_to("/")).exists()
    assert (tmp_path / "opt/eidolon/releases/active").is_dir()
    assert not (tmp_path / "opt/eidolon/releases/stale").exists()


@pytest.mark.parametrize(
    "payload",
    [
        {"release_id": "candidate", "phase": "unknown", "required_bytes": 0, "reserve_bytes": 0},
        {"release_id": "candidate", "phase": "prepare", "required_bytes": -1, "reserve_bytes": 0},
        {"release_id": "candidate", "phase": "prepare", "required_bytes": 0, "reserve_bytes": True},
    ],
)
def test_reclamation_rejects_invalid_phase_and_capacity_payloads(
    tmp_path: Path, payload: dict[str, object]
) -> None:
    with pytest.raises(TargetError, match=r"phase|capacity reserve|required capacity"):
        reclamation.reclaim(payload, root=tmp_path)


def test_empty_host_namespace_is_a_valid_prepare_start(tmp_path: Path) -> None:
    result = reclamation.reclaim(
        _payload("candidate"),
        root=tmp_path,
        disk_usage=lambda _path: SimpleNamespace(total=10_000, used=0, free=10_000),
    )

    assert result["status"] == "ready"
    assert result["active_targets"] == {}
    assert result["removed"] == {"releases": [], "uploads": [], "secrets": []}


def test_prepared_candidate_needs_only_operating_reserve(tmp_path: Path) -> None:
    _host(tmp_path, ("candidate",))
    _seal(tmp_path, "candidate")
    usage = SimpleNamespace(total=10_000, used=8_500, free=1_500)

    result = reclamation.reclaim(
        _payload("candidate"), root=tmp_path, disk_usage=lambda _path: usage
    )

    assert result["status"] == "ready"
    assert result["capacity"]["effective_required_bytes"] == 0


def test_incomplete_candidate_markers_are_rejected(tmp_path: Path) -> None:
    _host(tmp_path, ("candidate",))
    (tmp_path / "opt/eidolon/releases/candidate/release.json").write_text(
        "{}", encoding="utf-8"
    )

    with pytest.raises(TargetError, match="markers"):
        reclamation.reclaim(_payload("candidate"), root=tmp_path)


@pytest.mark.parametrize("state_shape", ["symlink", "unreadable", "other_candidate"])
def test_reclamation_state_fails_closed(tmp_path: Path, state_shape: str) -> None:
    state = tmp_path / contract.RECLAMATION_STATE.relative_to("/")
    state.parent.mkdir(parents=True)
    if state_shape == "symlink":
        state.symlink_to(tmp_path)
        match = "unsafe"
    elif state_shape == "unreadable":
        state.write_text("not-json", encoding="utf-8")
        match = "unreadable"
    else:
        state.write_text(
            json.dumps({"schema_version": 1, "candidate_release_id": "other"}),
            encoding="utf-8",
        )
        match = "another release candidate"

    with pytest.raises(TargetError, match=match):
        reclamation.reclaim(_payload("candidate"), root=tmp_path)


def test_current_entry_must_be_a_symlink_to_a_component(tmp_path: Path) -> None:
    _host(tmp_path, ("active", "candidate"))
    current = tmp_path / contract.CURRENT_ROOT.relative_to("/")
    current.mkdir(parents=True)
    (current / "eidolon_kernel").write_text("not a link", encoding="utf-8")

    with pytest.raises(TargetError, match="conventional symlink"):
        reclamation.reclaim(_payload("candidate"), root=tmp_path)

    (current / "eidolon_kernel").unlink()
    (current / "eidolon_kernel").symlink_to(tmp_path / "opt/eidolon/releases/active")
    with pytest.raises(TargetError, match="invalid release layout"):
        reclamation.reclaim(_payload("candidate"), root=tmp_path)


def test_deletion_candidates_require_conventional_real_directories(tmp_path: Path) -> None:
    _host(tmp_path, ("active", "candidate"))
    _link(tmp_path, "eidolon_kernel", "active")
    (tmp_path / "opt/eidolon/releases/file-release").write_text("unsafe", encoding="utf-8")

    with pytest.raises(TargetError, match="real directory"):
        reclamation.reclaim(_payload("candidate"), root=tmp_path)

    (tmp_path / "opt/eidolon/releases/file-release").unlink()
    (tmp_path / "var/tmp/eidolon-release-!invalid").mkdir()
    with pytest.raises(TargetError, match="non-conventional"):
        reclamation.reclaim(_payload("candidate"), root=tmp_path)
