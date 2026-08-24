from __future__ import annotations

import json
from pathlib import Path

import pytest

from eidolon_ops.hostagent import contract, cutover
from eidolon_ops.hostagent.primitives import TargetError

_AUTHORITY = {
    "contract_version": 1,
    "operation": "owner-authority.bootstrap-consumed",
    "owner_domain_id": "owner-0123456789abcdef",
    "owner_domain_generation": 2,
    "state_id": "authority-state_0123456789abcdef",
}


def _path(root: Path, absolute: Path) -> Path:
    return root / absolute.relative_to("/")


def _host(root: Path) -> None:
    for component, absolute in contract.CURRENT_LINKS.items():
        link = _path(root, absolute)
        link.parent.mkdir(parents=True, exist_ok=True)
        link.symlink_to(f"/opt/eidolon/releases/old/{component}")
    marker = _path(root, contract.INSTALL_INPUTS["authority-bootstrap.json"][0])
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(json.dumps(_AUTHORITY), encoding="utf-8")
    marker.chmod(0o600)
    settings = _path(root, contract.INSTALL_INPUTS["hub.generated.yaml"][0])
    settings.parent.mkdir(parents=True, exist_ok=True)
    settings.write_text("previous\n", encoding="utf-8")
    settings.chmod(0o640)


def _payload(result: dict[str, object], mode: str = "reversible") -> dict[str, object]:
    return {
        "release_id": "release-9",
        "cutover_mode": mode,
        "host_snapshot": result["host_snapshot"],
    }


def _switch(root: Path) -> None:
    for component, absolute in contract.CURRENT_LINKS.items():
        link = _path(root, absolute)
        link.unlink()
        link.symlink_to(f"/opt/eidolon/releases/release-9/{component}")


def test_cutover_receipt_joins_host_graph_schema_and_authority(tmp_path: Path) -> None:
    _host(tmp_path)
    captured = cutover.snapshot(
        {"release_id": "release-9", "cutover_mode": "forward-only"}, root=tmp_path
    )
    _switch(tmp_path)

    result = cutover.finalize(
        {
            **_payload(captured, "forward-only"),
            "activation": {
                "status": "activated",
                "transaction_id": "a" * 32,
                "cutover_mode": "forward-only",
                "persistent_state_mutated": True,
                "database_migrations": [],
            },
        },
        root=tmp_path,
    )

    assert result["status"] == "cutover_recorded"
    assert result["authority"] == _AUTHORITY
    assert result["schema_migration"] == {
        "state": "persistent_state_barrier_crossed",
        "database_migrations": [],
    }
    receipt = json.loads(
        (_path(tmp_path, Path(str(captured["host_snapshot"]))) / "cutover.json").read_text()
    )
    assert receipt["previous_targets"]["eidolon_hub"].endswith("/old/eidolon_hub")
    assert receipt["active_targets"]["eidolon_hub"].endswith(
        "/release-9/eidolon_hub"
    )
    assert receipt["authority_before"] == receipt["authority_after"]


def test_reversible_cutover_restores_host_files_and_absence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _host(tmp_path)
    captured = cutover.snapshot(
        {"release_id": "release-9", "cutover_mode": "reversible"}, root=tmp_path
    )
    settings = _path(tmp_path, contract.INSTALL_INPUTS["hub.generated.yaml"][0])
    settings.write_text("candidate\n", encoding="utf-8")
    certificate = _path(tmp_path, contract.INSTALL_INPUTS["hub.crt"][0])
    certificate.parent.mkdir(parents=True, exist_ok=True)
    certificate.write_text("candidate certificate", encoding="utf-8")
    monkeypatch.setattr(cutover.primitives, "checked", lambda *args, **kwargs: None)

    restored = cutover.restore(_payload(captured), root=tmp_path)

    assert restored["status"] == "host_cutover_restored"
    assert settings.read_text() == "previous\n"
    assert not certificate.exists()


def test_forward_only_cutover_restores_only_while_previous_graph_proves_pre_barrier(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _host(tmp_path)
    captured = cutover.snapshot(
        {"release_id": "release-9", "cutover_mode": "forward-only"}, root=tmp_path
    )
    settings = _path(tmp_path, contract.INSTALL_INPUTS["hub.generated.yaml"][0])
    settings.write_text("candidate\n", encoding="utf-8")
    monkeypatch.setattr(cutover.primitives, "checked", lambda *args, **kwargs: None)

    restored = cutover.restore(_payload(captured, "forward-only"), root=tmp_path)

    assert restored["persistent_state_barrier"] == "not_crossed"
    assert settings.read_text() == "previous\n"


def test_forward_only_cutover_cannot_restore_after_component_switch(
    tmp_path: Path,
) -> None:
    _host(tmp_path)
    captured = cutover.snapshot(
        {"release_id": "release-9", "cutover_mode": "forward-only"}, root=tmp_path
    )
    _switch(tmp_path)

    with pytest.raises(TargetError, match="persistent-state barrier"):
        cutover.restore(_payload(captured, "forward-only"), root=tmp_path)


def test_finalize_fails_closed_on_authority_or_graph_drift(tmp_path: Path) -> None:
    _host(tmp_path)
    captured = cutover.snapshot(
        {"release_id": "release-9", "cutover_mode": "reversible"}, root=tmp_path
    )
    marker = _path(tmp_path, contract.INSTALL_INPUTS["authority-bootstrap.json"][0])
    marker.write_text(json.dumps({**_AUTHORITY, "owner_domain_generation": 3}))

    with pytest.raises(TargetError, match="Authority lineage changed"):
        cutover.finalize(
            {
                **_payload(captured),
                "activation": {
                    "status": "activated",
                    "transaction_id": "a" * 32,
                    "cutover_mode": "reversible",
                    "persistent_state_mutated": False,
                },
            },
            root=tmp_path,
        )


def test_restore_rejects_tampered_snapshot_before_live_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _host(tmp_path)
    captured = cutover.snapshot(
        {"release_id": "release-9", "cutover_mode": "reversible"}, root=tmp_path
    )
    settings = _path(tmp_path, contract.INSTALL_INPUTS["hub.generated.yaml"][0])
    settings.write_text("live candidate\n")
    snapshot = _path(tmp_path, Path(str(captured["host_snapshot"])))
    (snapshot / "host-files/hub.generated.yaml").write_text("tampered\n")
    monkeypatch.setattr(cutover.primitives, "checked", lambda *args, **kwargs: None)

    with pytest.raises(TargetError, match="snapshot content"):
        cutover.restore(_payload(captured), root=tmp_path)
    assert settings.read_text() == "live candidate\n"
