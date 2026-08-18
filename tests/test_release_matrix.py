from __future__ import annotations

import hashlib

import pytest

from eidolon_ops.hostagent.contract import PRODUCT_UNITS
from eidolon_ops.hub_assets import HUB_SETTINGS_TEMPLATE, LEGACY_HUB_SETTINGS_TEMPLATE
from eidolon_ops.release_matrix import (
    HUB_SETTINGS_DESTINATION,
    SYSTEMD_ASSET_CONTRACTS,
    ReleaseMatrixError,
    validate_release_matrix,
    validate_release_settings_matrix,
    validate_release_systemd_matrix,
)


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


#: A Hub settings template only has to carry the two lines Ops rewrites to be a
#: faithful stand-in for one.
_HUB_TEMPLATE = (
    "onboarding:\n"
    "  hub_id: eidolon-hub-local\n"
    "  public_base_url: https://eidolon-hub.local\n"
    "persistence:\n"
    "  path: $EIDOLON_STATE_ROOT/hub/eidolon-hub.sqlite3\n"
)


def _settings_revisions() -> dict[str, str]:
    return {**_revisions(), "eidolon_hub": "3" * 40}


def _reader(answers: dict[tuple[str, str], str]):
    def read(source: str, _revision: str, path: str) -> str:
        try:
            return answers[(source, path)]
        except KeyError:
            raise RuntimeError(f"fatal: path '{path}' does not exist") from None

    return read


def test_the_settings_gate_reads_the_template_out_of_hubs_own_commit() -> None:
    result = validate_release_settings_matrix(
        _settings_revisions(),
        _reader({HUB_SETTINGS_TEMPLATE: _HUB_TEMPLATE}),
    )

    entry = result[HUB_SETTINGS_DESTINATION]

    assert isinstance(entry, dict)
    assert (entry["source_id"], entry["path"]) == HUB_SETTINGS_TEMPLATE
    assert entry["revision"] == "3" * 40
    assert entry["sha256"] == hashlib.sha256(_HUB_TEMPLATE.encode("utf-8")).hexdigest()
    # The whole point of the migration: the bytes came from the component the
    # settings belong to, and the evidence says so rather than implying it.
    assert entry["legacy_template"] is False


@pytest.mark.parametrize(
    ("pinned_hub", "reason"),
    [
        (None, "a release from before the template moved has no file at the new address"),
        ("onboarding:\n  hub_id: eidolon-hub-local\n", "half a template cannot be rendered"),
    ],
)
def test_the_settings_gate_falls_back_to_the_address_the_template_had_before_it_moved(
    pinned_hub: str | None,
    reason: str,
) -> None:
    answers = {LEGACY_HUB_SETTINGS_TEMPLATE: _HUB_TEMPLATE}
    if pinned_hub is not None:
        answers[HUB_SETTINGS_TEMPLATE] = pinned_hub

    entry = validate_release_settings_matrix(_settings_revisions(), _reader(answers))[
        HUB_SETTINGS_DESTINATION
    ]

    assert isinstance(entry, dict)
    assert (entry["source_id"], entry["path"]) == LEGACY_HUB_SETTINGS_TEMPLATE, reason
    # Recorded, not silent: operating an older release is allowed, and reading
    # it out of the component that no longer owns these settings is the kind of
    # thing an operator should be able to see in the evidence.
    assert entry["legacy_template"] is True


def test_the_settings_gate_refuses_a_release_with_no_renderable_template() -> None:
    doubled = _HUB_TEMPLATE + "  hub_id: eidolon-hub-local\n"

    with pytest.raises(ReleaseMatrixError) as exc:
        validate_release_settings_matrix(
            _settings_revisions(),
            _reader({HUB_SETTINGS_TEMPLATE: doubled}),
        )

    message = str(exc.value)

    # A template Ops cannot instantiate is refused while the release is still a
    # plan, and the report names every address that was tried rather than the
    # last one that failed.
    assert "does not carry the two lines Ops rewrites" in message
    assert HUB_SETTINGS_TEMPLATE[1] in message
    assert LEGACY_HUB_SETTINGS_TEMPLATE[1] in message
    assert "fatal:" not in message


def test_the_settings_gate_says_when_the_component_is_not_in_the_release() -> None:
    with pytest.raises(ReleaseMatrixError, match="not pinned by this release"):
        validate_release_settings_matrix({}, _reader({HUB_SETTINGS_TEMPLATE: _HUB_TEMPLATE}))


def test_the_release_matrix_carries_the_unit_and_the_settings_gate_together() -> None:
    roots = {contract.path: contract.component_root for contract in SYSTEMD_ASSET_CONTRACTS}

    def read(source: str, revision: str, path: str) -> str:
        if (source, path) == HUB_SETTINGS_TEMPLATE:
            return _HUB_TEMPLATE
        return _valid_asset(roots[path])

    result = validate_release_matrix(_settings_revisions(), read)

    assert result["status"] == "compatible"
    assert set(result["units"]) == {contract.unit for contract in SYSTEMD_ASSET_CONTRACTS}
    assert set(result["settings"]) == {HUB_SETTINGS_DESTINATION}
