"""The component contracts against the tables Ops used to be the source of.

Ops carried six tables of other people's operational facts, two of them twice
over. The contracts are the authority now. These tests are what makes that
claim checkable rather than aspirational: each one asserts that a built-in
table and the eight contracts still describe the same Host.

The tables are deliberately still here rather than deleted outright. A release
pins exact commits, so Ops has to be able to operate a source tree from before
the contracts existed; and until every one of these passes on every release
we care about, deleting the tables would trade a duplicate for a blind spot.
What has already been removed is the duplication Ops had with *itself* — the
install-name map that existed identically in two of its own modules.

Known, deliberate differences between the two are named in each test rather
than smoothed over. A difference nobody wrote down is drift; a difference with
a reason beside it is a decision.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from eidolon_ops import source_assets
from eidolon_ops.component_contract import FOUNDATION_UNITS, read_component_contracts
from eidolon_ops.config import (
    FIXED_DATA_PATHS,
    INSTALL_FILE_NAMES,
    PRODUCT_UNITS,
    SOURCE_IDS,
)
from eidolon_ops.hostagent import contract as host_contract
from eidolon_ops.hub_assets import HUB_SETTINGS_TEMPLATE
from eidolon_ops.private_inputs import INSTALL_DESTINATION_NAMES
from eidolon_ops.release_matrix import HUB_SETTINGS_DESTINATION, SYSTEMD_ASSET_CONTRACTS

pytestmark = pytest.mark.contract

#: The sibling checkouts a workstation has. A release operates whatever the
#: matrix pins, which may predate any of this, so an absent sibling skips
#: rather than fails — these tests watch for drift, they do not gate a build.
_CHECKOUT_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module")
def topology():
    sources = {source_id: _CHECKOUT_ROOT / source_id for source_id in SOURCE_IDS}
    missing = [
        source_id for source_id, path in sources.items() if not (path / "ops").is_dir()
    ]
    if missing:
        pytest.skip(f"no sibling checkout with a contract for: {', '.join(missing)}")
    return read_component_contracts(sources)


def test_every_component_answers(topology) -> None:
    # The precondition for any of the rest being meaningful.
    assert topology.silent == ()
    topology.requires_every_component("this test")


def test_the_declared_units_are_the_units_ops_expects(topology) -> None:
    assert set(topology.systemd_units) == set(PRODUCT_UNITS)


def test_the_host_agent_carries_the_same_unit_list(topology) -> None:
    # The Host agent is injected as one payload with no imports of its own, so
    # it cannot share this table with eidolon_ops.config — the duplicate is
    # structural. What was missing was anything checking the two still agree.
    assert set(host_contract.PRODUCT_UNITS) == set(PRODUCT_UNITS)
    assert set(host_contract.PRODUCT_UNITS) == set(topology.systemd_units)


def test_the_declared_ports_are_the_ports_ops_assigns(topology) -> None:
    # NATS and LiveKit are the platform's, not any component's, so they are in
    # the Ops table and in no contract.
    foundation = {"nats", "nats_http", "livekit"}

    assert set(topology.port_roles) | foundation == set(source_assets.PORTS)
    for role, port in topology.port_roles.items():
        assert source_assets.PORTS[role] == port, f"{role} disagrees"


def test_the_operator_inputs_are_the_ones_an_install_asks_for(topology) -> None:
    declared = {
        entry.name for entry in topology.install_inputs if entry.is_operator_supplied
    }

    # livekit.env configures the LiveKit server, which no component owns. It
    # stays an Ops-side entry until something publishes a contract for the
    # platform units, and naming it here is what keeps that visible.
    assert declared | {"livekit_env"} == set(INSTALL_FILE_NAMES)


def test_each_input_lands_where_and_as_ops_would_write_it(topology) -> None:
    for entry in topology.install_inputs:
        if not entry.is_operator_supplied:
            continue
        assert INSTALL_DESTINATION_NAMES[entry.name] == entry.install_as

        destination, owner, group, mode = host_contract.SECRET_INPUTS[entry.install_as]
        # Ownership and mode are the half of an install that decides who can
        # read a credential, and they were previously stated only on the Host
        # side, where the component that generated the file could not see them.
        assert destination == entry.install_path
        assert (owner, group, mode) == (entry.owner, entry.group, entry.mode)


def test_the_derived_input_is_rendered_from_the_template_its_component_declared(
    topology,
) -> None:
    derived = [entry for entry in topology.install_inputs if not entry.is_operator_supplied]

    # One derived input exists, and Ops renders it from a hard-coded address.
    # That address has to be the one the owning component published, because a
    # component that cannot move its own template is a component whose deployed
    # defaults someone else owns — which is where Hub's settings were, in
    # eidolon_kernel, until they moved back here.
    assert [(entry.component_id, entry.template) for entry in derived] == [
        HUB_SETTINGS_TEMPLATE
    ]
    assert derived[0].install_path == Path(HUB_SETTINGS_DESTINATION)
    destination, owner, group, mode = host_contract.HOST_APPLICATION_INPUTS[
        "hub.generated.yaml"
    ]
    assert destination == derived[0].install_path
    assert (owner, group, mode) == (derived[0].owner, derived[0].group, derived[0].mode)


def test_the_backed_up_authorities_are_the_ones_components_named(topology) -> None:
    declared = {
        state.path: (state.owner, state.group)
        for state in topology.authority
        if state.backup == "sqlite-online"
    }
    built_in = {
        path: (owner, group)
        for path, owner, group in host_contract.BACKED_UP_AUTHORITIES.values()
    }

    assert declared == built_in


def test_what_the_backup_leaves_out_is_still_what_it_leaves_out(topology) -> None:
    uncovered = {state.path for state in topology.authority if not state.is_covered}
    built_in = {path for path, _reason in host_contract.UNCOVERED_STATE.values()}

    # Two deliberate differences:
    #
    #   * JetStream is NATS's, and NATS is a platform server with no repository
    #     of ours to publish a contract. It stays in the Ops table.
    #   * Deployment evidence was in neither the covered nor the uncovered
    #     table — it was only in FIXED_DATA_PATHS, which says where a thing is
    #     and nothing about whether a backup carries it. Kernel now declares it
    #     as an uncovered authority, which is what it always was.
    assert built_in - uncovered == {Path("/var/lib/eidolon/nats/jetstream")}
    assert uncovered - built_in == {Path("/var/lib/eidolon/deployments")}


def test_every_uncovered_path_says_what_covering_it_would_take(topology) -> None:
    for state in topology.authority:
        if state.is_covered:
            continue
        # Not a restatement of the schema check: this asserts the reasons are
        # written for whoever reads a backup report, which is why length is a
        # crude but real proxy for "explains something".
        assert len(state.uncovered_reason or "") > 40, state.path


def test_the_fixed_data_paths_all_belong_to_some_component(topology) -> None:
    declared = {state.path for state in topology.authority}
    runtime = {
        path for contract in topology.contracts for path in contract.runtime_paths
    }

    for name, path in FIXED_DATA_PATHS.items():
        assert path in declared or path in runtime, f"{name} at {path} is unclaimed"
    # And the same table on the Host side.
    assert dict(host_contract.FIXED_DATA) == dict(FIXED_DATA_PATHS)


def test_a_factory_reset_reaches_everything_every_component_holds(topology) -> None:
    roots = [
        path for contract in topology.contracts for path in contract.factory_reset_paths
    ]

    for state in topology.authority:
        assert any(state.path.is_relative_to(root) for root in roots), (
            f"{state.path} would survive a factory reset"
        )
    # And the Host would actually reach those roots.
    for root in roots:
        assert any(
            root.is_relative_to(authority_root)
            for authority_root in host_contract.RESET_AUTHORITY_ROOTS
        ), f"{root} is outside every root the Host agent clears"


def test_the_shipped_unit_files_run_what_their_component_declared(topology) -> None:
    """The check that makes [[units]] worth declaring.

    The .service files for Hub, Data, Memory, Agent and Channel live in
    eidolon_kernel, not in those components' own repositories. Nothing
    previously compared what those files run against what the owning component
    says it needs — a unit pointing into the wrong venv, or at an entrypoint
    that was renamed, was something only a Host found out.
    """

    declared = {
        unit["id"]: (contract.component_id, unit["exec"])
        for contract in topology.contracts
        for unit in contract.units
    }

    checked = 0
    for asset in SYSTEMD_ASSET_CONTRACTS:
        unit_id = asset.unit.removesuffix(".service")
        if unit_id not in declared:
            # nats and livekit: platform units, no component, nothing to check.
            assert unit_id in FOUNDATION_UNITS
            continue
        source = _CHECKOUT_ROOT / asset.source_id / asset.path
        if not source.is_file():
            pytest.skip(f"{asset.source_id} checkout does not carry {asset.path}")

        component_id, executable = declared[unit_id]
        exec_start = next(
            line
            for line in source.read_text(encoding="utf-8").splitlines()
            if line.startswith("ExecStart=")
        )

        assert asset.component_root == component_id, (
            f"{asset.unit} is rooted in {asset.component_root} but "
            f"{component_id} declares it"
        )
        assert f"/{component_id}/{executable} " in f"{exec_start} ", (
            f"{asset.unit} runs {exec_start.split('=', 1)[1]}, but "
            f"{component_id} declares {executable}"
        )
        checked += 1

    # A loop that silently checked nothing would pass just as quietly.
    assert checked == len(PRODUCT_UNITS) - len(FOUNDATION_UNITS)
