from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from eidolon_ops.target_agent import MANAGED_SYSTEM_ASSETS, PRODUCT_UNITS, SECRET_INPUTS

KERNEL_ROOT = Path(__file__).resolve().parents[2] / "eidolon_kernel"

pytestmark = pytest.mark.contract


@pytest.fixture(scope="module")
def kernel_contract() -> SimpleNamespace:
    manifest = KERNEL_ROOT / "eidolon_deploy/manifest.py"
    if not manifest.is_file():
        pytest.skip("sibling eidolon_kernel checkout is unavailable")
    sys.path.insert(0, str(KERNEL_ROOT))
    from eidolon_deploy.manifest import (
        V2_AFFECTED_UNITS,
        V2_READINESS,
        V2_REQUIRED_SECRETS,
        V2_SYSTEM_ASSETS,
    )

    return SimpleNamespace(
        affected_units=V2_AFFECTED_UNITS,
        readiness=V2_READINESS,
        required_secrets=V2_REQUIRED_SECRETS,
        system_assets=V2_SYSTEM_ASSETS,
    )


def test_operator_topology_is_kernel_release_topology_plus_manager(kernel_contract) -> None:
    assert set(PRODUCT_UNITS) == set(kernel_contract.affected_units) | {"eidolond.service"}


def test_first_install_prerequisites_match_kernel_descriptor(kernel_contract) -> None:
    private = {
        destination for destination, _user, _group, mode in SECRET_INPUTS.values() if mode == 0o600
    }
    assert private == set(kernel_contract.required_secrets)
    assert {
        destination for destination, _user, _group, mode in SECRET_INPUTS.values() if mode == 0o640
    } == {
        Path("/etc/eidolon/agent.yaml"),
        Path("/etc/eidolon/channel.yaml"),
        Path("/etc/eidolon/memory.yaml"),
    }


def test_current_release_contract_counts_are_not_stale_document_counts(
    kernel_contract,
) -> None:
    assert len(kernel_contract.system_assets) == 22
    assert len(kernel_contract.required_secrets) == 11
    assert len(kernel_contract.affected_units) == 13
    assert len(kernel_contract.readiness) == 12


def test_reset_system_asset_allowlist_matches_kernel_release_contract(kernel_contract) -> None:
    assert set(MANAGED_SYSTEM_ASSETS) == set(kernel_contract.system_assets)


def test_data_v2_paths_are_fixed_in_systemd_assets() -> None:
    data_unit = KERNEL_ROOT / "deploy/systemd/eidolon-data.service"
    if not data_unit.is_file():
        pytest.skip("sibling eidolon_kernel checkout is unavailable")
    text = data_unit.read_text(encoding="utf-8")

    assert "EIDOLON_DATA_SQLITE_PATH=/var/lib/eidolon/eidolon-system.sqlite3" in text
    assert "eidolon.sqlite3" not in text
