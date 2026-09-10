"""Read and validate persisted Hub authorization state without changing it."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping
from pathlib import Path

from . import contract, primitives
from .primitives import TargetError

HUB_DATABASE = Path("/var/lib/eidolon/hub/eidolon-hub.sqlite3")
AUTHORITY_ANCHOR = Path("/var/lib/eidolon/hub/authority-lineage.json")
AUTHORITY_BOOTSTRAP = Path("/var/lib/eidolon/hub/authority-bootstrap.json")
OWNER_DESCRIPTOR = Path(
    "/etc/eidolon/owner-domain/owner_domain_descriptor.json"
)
HUB_UNIT = "eidolon-hub.service"
HUB_INGRESS_UNIT = "eidolon-hub-ingress.service"


def _read_json(path: Path, *, label: str) -> dict[str, object] | None:
    if not path.exists() and not path.is_symlink():
        return None
    if path.is_symlink() or not path.is_file():
        raise TargetError(f"{label} is not a safe regular file")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise TargetError(f"{label} is invalid") from exc
    if not isinstance(value, dict):
        raise TargetError(f"{label} is invalid")
    return value


def _marker(database: Path) -> dict[str, object] | None:
    if not database.exists() and not database.is_symlink():
        return None
    if database.is_symlink() or not database.is_file():
        raise TargetError("Hub database is not a safe regular file")
    try:
        connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
        try:
            present = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                ("hub_authority_state",),
            ).fetchone()
            if present is None:
                raise TargetError("AUTHORITY_RECOVERY_REQUIRED: existing Hub database has no Authority marker; restore its backup")
            row = connection.execute(
                "SELECT owner_domain_id, owner_domain_generation, state_id "
                "FROM hub_authority_state WHERE singleton_id=1"
            ).fetchone()
        finally:
            connection.close()
    except sqlite3.Error as exc:
        raise TargetError("Hub Authority marker is unreadable") from exc
    if row is None:
        raise TargetError("Hub Authority marker is missing")
    return {
        "contract_version": 1,
        "owner_domain_id": row[0],
        "owner_domain_generation": row[1],
        "state_id": row[2],
    }


def lineage_evidence(*, database: Path, anchor: Path) -> dict[str, object]:
    """Read both copies of authorization identity; neither missing copy proves a new Host."""

    found = _marker(database)
    recorded = _read_json(anchor, label="Owner Authority lineage anchor")
    return {
        "marker": found,
        "anchor": recorded,
        "established": found if found is not None and found == recorded else None,
    }


def established_lineage(*, root: Path = Path("/")) -> dict[str, object]:
    """The product Host's lineage evidence, at the product locations."""

    root = root.resolve()
    return lineage_evidence(
        database=primitives.host_path(root, HUB_DATABASE),
        anchor=primitives.host_path(root, AUTHORITY_ANCHOR),
    )


def authority_lineage(
    payload: Mapping[str, object], *, root: Path = Path("/")
) -> dict[str, object]:
    """The read-only op behind ``established_lineage``."""

    contract.fixed_units(payload)
    return {"status": "observed", **established_lineage(root=root)}
