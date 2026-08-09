from __future__ import annotations

import pytest

from eidolon_ops.release_matrix import (
    SYSTEMD_ASSET_CONTRACTS,
    ReleaseMatrixError,
    validate_release_systemd_matrix,
)
from eidolon_ops.target_agent import PRODUCT_UNITS


def _revisions() -> dict[str, str]:
    return {
        "eidolon_kernel": "1" * 40,
        "eidolon_admin": "2" * 40,
    }


def _valid_asset(component_root: str | None) -> str:
    executable = (
        f"/opt/eidolon/current/{component_root}/.venv/bin/service"
        if component_root is not None
        else "/usr/local/bin/service"
    )
    return (
        "[Unit]\nDescription=test\n"
        "[Service]\nEnvironmentFile=/etc/eidolon/host.env\n"
        f"ExecStart={executable}\n"
    )


def test_release_matrix_covers_the_exact_product_unit_set() -> None:
    assert {contract.unit for contract in SYSTEMD_ASSET_CONTRACTS} == set(PRODUCT_UNITS)


def test_exact_systemd_matrix_accepts_all_fourteen_fhs_units() -> None:
    roots = {contract.path: contract.component_root for contract in SYSTEMD_ASSET_CONTRACTS}

    result = validate_release_systemd_matrix(
        _revisions(),
        lambda _source, _revision, path: _valid_asset(roots[path]),
    )

    assert result["status"] == "compatible"
    assert result["contract"] == "fhs-opt-host-profile-v1"
    assert set(result["units"]) == {contract.unit for contract in SYSTEMD_ASSET_CONTRACTS}


@pytest.mark.parametrize(
    ("replacement", "message"),
    [
        (
            "EnvironmentFile=/etc/eidolon/host.env",
            "EnvironmentFile=/etc/eidolon/component.env",
        ),
        ("/opt/eidolon/current", "/srv/eidolon/current"),
        ("[Service]", "[Install]"),
    ],
)
def test_exact_systemd_matrix_rejects_incompatible_asset(
    replacement: str,
    message: str,
) -> None:
    roots = {contract.path: contract.component_root for contract in SYSTEMD_ASSET_CONTRACTS}
    broken = SYSTEMD_ASSET_CONTRACTS[0].path

    def reader(_source: str, _revision: str, path: str) -> str:
        value = _valid_asset(roots[path])
        return value.replace(replacement, message) if path == broken else value

    with pytest.raises(ReleaseMatrixError, match="incompatible"):
        validate_release_systemd_matrix(_revisions(), reader)


def test_exact_systemd_matrix_wraps_missing_git_object_without_leaking_details() -> None:
    def missing(_source: str, _revision: str, _path: str) -> str:
        raise RuntimeError("sensitive stderr")

    with pytest.raises(ReleaseMatrixError, match="cannot read exact systemd asset") as exc:
        validate_release_systemd_matrix(_revisions(), missing)

    assert "sensitive stderr" not in str(exc.value)
