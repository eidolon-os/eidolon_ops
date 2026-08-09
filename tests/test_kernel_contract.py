from __future__ import annotations

import sys
from pathlib import Path

import pytest

KERNEL_ROOT = Path(__file__).resolve().parents[2] / "eidolon_kernel"
sys.path.insert(0, str(KERNEL_ROOT))

from eidolon_deploy.manifest import (  # noqa: E402
    V2_AFFECTED_UNITS,
    V2_READINESS,
    V2_REQUIRED_SECRETS,
    V2_SYSTEM_ASSETS,
)

from eidolon_ops.target_agent import PRODUCT_UNITS, SECRET_INPUTS  # noqa: E402

pytestmark = pytest.mark.contract


def test_operator_topology_is_kernel_release_topology_plus_manager() -> None:
    assert set(PRODUCT_UNITS) == set(V2_AFFECTED_UNITS) | {"eidolond.service"}


def test_first_install_prerequisites_match_kernel_descriptor() -> None:
    private = {
        destination for destination, _user, _group, mode in SECRET_INPUTS.values() if mode == 0o600
    }
    assert private == set(V2_REQUIRED_SECRETS)
    assert {
        destination for destination, _user, _group, mode in SECRET_INPUTS.values() if mode == 0o640
    } == {
        Path("/etc/eidolon/agent.yaml"),
        Path("/etc/eidolon/channel.yaml"),
        Path("/etc/eidolon/memory.yaml"),
    }


def test_current_release_contract_counts_are_not_stale_document_counts() -> None:
    assert len(V2_SYSTEM_ASSETS) == 22
    assert len(V2_REQUIRED_SECRETS) == 11
    assert len(V2_AFFECTED_UNITS) == 13
    assert len(V2_READINESS) == 12


def test_data_v2_paths_are_fixed_in_systemd_assets() -> None:
    data_unit = Path("../eidolon_kernel/deploy/systemd/eidolon-data.service").resolve()
    text = data_unit.read_text(encoding="utf-8")

    assert "EIDOLON_DATA_SQLITE_PATH=/var/lib/eidolon/eidolon-system.sqlite3" in text
    assert "eidolon.sqlite3" not in text
