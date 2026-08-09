from __future__ import annotations

import ast
import subprocess
import tomllib
from pathlib import Path
from types import SimpleNamespace

import pytest

from eidolon_ops.target_agent import MANAGED_SYSTEM_ASSETS, PRODUCT_UNITS, SECRET_INPUTS

KERNEL_ROOT = Path(__file__).resolve().parents[2] / "eidolon_kernel"

pytestmark = pytest.mark.contract


@pytest.fixture(scope="module")
def kernel_contract() -> SimpleNamespace:
    if not (KERNEL_ROOT / ".git").exists():
        pytest.skip("sibling eidolon_kernel checkout is unavailable")
    config = tomllib.loads(
        (Path(__file__).resolve().parents[1] / "config/eidolon-pi.example.toml").read_text()
    )
    revision = config["sources"]["eidolon_kernel"]["revision"]
    result = subprocess.run(
        ["git", "-C", str(KERNEL_ROOT), "show", f"{revision}:eidolon_deploy/manifest.py"],
        check=True,
        capture_output=True,
        text=True,
    )
    assignments = {
        node.targets[0].id: node.value
        for node in ast.parse(result.stdout).body
        if isinstance(node, ast.Assign)
        and len(node.targets) == 1
        and isinstance(node.targets[0], ast.Name)
    }

    def exact(name: str):
        return eval(
            compile(ast.Expression(assignments[name]), "<pinned-manifest>", "eval"), {"Path": Path}
        )

    return SimpleNamespace(
        affected_units=exact("V2_AFFECTED_UNITS"),
        readiness=exact("V2_READINESS"),
        required_secrets=exact("V2_REQUIRED_SECRETS"),
        system_assets=exact("V2_SYSTEM_ASSETS"),
        revision=revision,
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
    assert len(kernel_contract.system_assets) == 23
    assert len(kernel_contract.required_secrets) == 11
    assert len(kernel_contract.affected_units) == 14
    assert len(kernel_contract.readiness) == 13


def test_reset_system_asset_allowlist_matches_kernel_release_contract(kernel_contract) -> None:
    assert set(MANAGED_SYSTEM_ASSETS) == set(kernel_contract.system_assets)


def test_data_v2_paths_are_fixed_in_systemd_assets(kernel_contract) -> None:
    text = subprocess.run(
        [
            "git",
            "-C",
            str(KERNEL_ROOT),
            "show",
            f"{kernel_contract.revision}:deploy/systemd/eidolon-data.service",
        ],
        check=True,
        capture_output=True,
        text=True,
    ).stdout

    assert "EIDOLON_DATA_SQLITE_PATH=/var/lib/eidolon/eidolon-system.sqlite3" in text
    assert "eidolon.sqlite3" not in text
