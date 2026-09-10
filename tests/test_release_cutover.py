from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from eidolon_ops.hostagent import authority_state, contract, cutover
from eidolon_ops.hostagent.primitives import TargetError

_AUTHORITY = {
    "contract_version": 1,
    "owner_domain_id": "owner-0123456789abcdef",
    "owner_domain_generation": 2,
    "state_id": "authority-state_0123456789abcdef",
}


def _path(root: Path, absolute: Path) -> Path:
    return root / absolute.relative_to("/")


def _bootstrapped_hub(root: Path, authority: dict[str, object]) -> None:
    """A Host whose Hub has already come up, which is every installed Host.

    Hub consumes the one-shot ``authority-bootstrap.json`` by deleting it, so
    this fixture deliberately never writes that file.  What the Host keeps is
    the pair a lineage is actually proved from: the database marker and the
    external lineage anchor.
    """

    database = _path(root, authority_state.HUB_DATABASE)
    database.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(database)
    try:
        connection.execute(
            "CREATE TABLE IF NOT EXISTS hub_authority_state ("
            "singleton_id INTEGER PRIMARY KEY, owner_domain_id TEXT, "
            "owner_domain_generation INTEGER, state_id TEXT)"
        )
        connection.execute("DELETE FROM hub_authority_state")
        connection.execute(
            "INSERT INTO hub_authority_state VALUES (1, ?, ?, ?)",
            (
                authority["owner_domain_id"],
                authority["owner_domain_generation"],
                authority["state_id"],
            ),
        )
        connection.commit()
    finally:
        connection.close()
    anchor = _path(root, authority_state.AUTHORITY_ANCHOR)
    anchor.write_text(json.dumps(authority), encoding="utf-8")
    anchor.chmod(0o600)


def _host(root: Path) -> None:
    for component, absolute in contract.CURRENT_LINKS.items():
        link = _path(root, absolute)
        link.parent.mkdir(parents=True, exist_ok=True)
        link.symlink_to(f"/opt/eidolon/releases/old/{component}")
    _bootstrapped_hub(root, _AUTHORITY)
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
    _bootstrapped_hub(tmp_path, {**_AUTHORITY, "owner_domain_generation": 3})

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


_PROVENANCE = {
    "eidolon_hub": {
        "revision": "a" * 40,
        "head": "a" * 40,
        "branch": "main",
        "pinned": False,
        "dirty": False,
    },
    "eidolon_sdk": {
        "revision": "b" * 40,
        "head": "b" * 40,
        "branch": "feature/session",
        "pinned": False,
        "dirty": True,
    },
}


def test_the_snapshot_records_which_commits_this_release_is(tmp_path: Path) -> None:
    """Otherwise nobody can say what shipped three weeks ago.

    The only artifact that carried commits lived in the release directory, and
    the commit phase deletes that. This document is explicitly outside
    reclamation, which makes it the place the answer has to live.
    """

    _host(tmp_path)

    captured = cutover.snapshot(
        {
            "release_id": "release-9",
            "cutover_mode": "reversible",
            "sources": _PROVENANCE,
        },
        root=tmp_path,
    )

    receipt = json.loads(
        (_path(tmp_path, Path(str(captured["host_snapshot"]))) / "cutover.json").read_text()
    )
    assert receipt["sources"] == _PROVENANCE
    # A dirty worktree was allowed past the gate only with an explicit flag, so
    # the fact that it was has to outlive the operator's terminal.
    assert receipt["sources"]["eidolon_sdk"]["dirty"] is True


def test_a_release_without_provenance_is_recorded_as_having_none(tmp_path: Path) -> None:
    """An older workstation must still be able to cut over."""

    _host(tmp_path)

    captured = cutover.snapshot(
        {"release_id": "release-9", "cutover_mode": "reversible"}, root=tmp_path
    )

    receipt = json.loads(
        (_path(tmp_path, Path(str(captured["host_snapshot"]))) / "cutover.json").read_text()
    )
    assert receipt["sources"] is None


@pytest.mark.parametrize(
    "sources",
    [
        {"eidolon_hub": {"revision": "a" * 40}},
        {"eidolon_hub": "not-an-object"},
        {"eidolon_hub": {**_PROVENANCE["eidolon_hub"], "revision": "abc"}},
        {"eidolon_hub": {**_PROVENANCE["eidolon_hub"], "dirty": "yes"}},
        # An extra key on its own: a record with more in it than the contract
        # says is a record written by something this Host does not understand.
        {"eidolon_hub": {**_PROVENANCE["eidolon_hub"], "worktree": "/somewhere"}},
        "not-a-table",
    ],
)
def test_malformed_provenance_is_refused_rather_than_stored(tmp_path: Path, sources) -> None:
    """Evidence nobody can trust the shape of is not evidence."""

    _host(tmp_path)

    with pytest.raises(TargetError, match="source provenance"):
        cutover.snapshot(
            {
                "release_id": "release-9",
                "cutover_mode": "reversible",
                "sources": sources,
            },
            root=tmp_path,
        )


def test_cutover_proves_lineage_after_hub_consumed_the_bootstrap_capability(
    tmp_path: Path,
) -> None:
    """A Host whose Hub has already bootstrapped is still deployable.

    Hub deletes ``authority-bootstrap.json`` on use, so from the first
    successful Hub start onwards it is absent.  This cutover once read that
    file as the Authority lineage and refused every release such a Host would
    ever receive, leaving a wipe that destroys the Owner, the Companions and
    the Claims as the only way to ship newer code.
    """

    _host(tmp_path)
    capability = _path(tmp_path, contract.INSTALL_INPUTS["authority-bootstrap.json"][0])
    assert not capability.exists()

    captured = cutover.snapshot(
        {"release_id": "release-9", "cutover_mode": "reversible"}, root=tmp_path
    )

    assert captured["authority"] == _AUTHORITY
    _switch(tmp_path)
    recorded = cutover.finalize(
        {
            **_payload(captured),
            "activation": {
                "status": "activated",
                "transaction_id": "b" * 32,
                "cutover_mode": "reversible",
                "persistent_state_mutated": False,
            },
        },
        root=tmp_path,
    )
    assert recorded["authority"] == _AUTHORITY


def test_cutover_ignores_a_respent_bootstrap_capability_file(tmp_path: Path) -> None:
    """The capability is a refreshable Host layer input this very transaction re-ships.

    Its presence, absence or content therefore cannot be what a cutover proves
    the Authority lineage from — the transaction would be comparing a file it
    rewrites between the two readings.
    """

    _host(tmp_path)
    captured = cutover.snapshot(
        {"release_id": "release-9", "cutover_mode": "reversible"}, root=tmp_path
    )
    capability = _path(tmp_path, contract.INSTALL_INPUTS["authority-bootstrap.json"][0])
    capability.write_text(
        json.dumps({"operation": "owner-authority.bootstrap-consumed", **_AUTHORITY}),
        encoding="utf-8",
    )
    capability.chmod(0o600)
    _switch(tmp_path)

    recorded = cutover.finalize(
        {
            **_payload(captured),
            "activation": {
                "status": "activated",
                "transaction_id": "c" * 32,
                "cutover_mode": "reversible",
                "persistent_state_mutated": False,
            },
        },
        root=tmp_path,
    )
    assert recorded["authority"] == _AUTHORITY


def test_cutover_refuses_a_host_that_cannot_prove_one_lineage(tmp_path: Path) -> None:
    _host(tmp_path)
    anchor = _path(tmp_path, authority_state.AUTHORITY_ANCHOR)
    anchor.write_text(
        json.dumps({**_AUTHORITY, "owner_domain_generation": 3}), encoding="utf-8"
    )

    with pytest.raises(TargetError, match="no established Owner Authority lineage"):
        cutover.snapshot(
            {"release_id": "release-9", "cutover_mode": "reversible"}, root=tmp_path
        )
