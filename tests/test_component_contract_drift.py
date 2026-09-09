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

import re
from pathlib import Path

import pytest

from eidolon_ops import source_assets
from eidolon_ops.component_contract import PLATFORM_COMPONENT_ID, read_component_contracts
from eidolon_ops.config import (
    FIXED_DATA_PATHS,
    INSTALL_FILE_NAMES,
    PRODUCT_UNITS,
)
from eidolon_ops.hostagent import contract as host_contract
from eidolon_ops.hostagent import memory_realms
from eidolon_ops.hub_assets import HUB_SETTINGS_TEMPLATE
from eidolon_ops.private_inputs import INSTALL_DESTINATION_NAMES
from eidolon_ops.release_matrix import HUB_SETTINGS_DESTINATION, SYSTEMD_ASSET_CONTRACTS

pytestmark = pytest.mark.contract

#: The sibling checkouts a workstation has. A release operates whatever the
#: matrix pins, which may predate any of this, so an absent sibling skips
#: rather than fails — these tests watch for drift, they do not gate a build.
_CHECKOUT_ROOT = Path(__file__).resolve().parents[2]


def _read(capabilities):
    from eidolon_ops.config import expected_sources

    sources = {
        source_id: _CHECKOUT_ROOT / source_id
        for source_id in sorted(expected_sources(capabilities))
    }
    missing = [source_id for source_id, path in sources.items() if not (path / "ops").is_dir()]
    if missing:
        pytest.skip(f"no sibling checkout with a contract for: {', '.join(missing)}")
    return read_component_contracts(sources, capabilities)


@pytest.fixture(scope="module")
def topology():
    """Everything any Host can install.

    Built with every capability, not none. Read with none, a conditional unit
    or port simply is not there, and each check below passes by not seeing it —
    which is how eidolon_models' recognizer sat on the Channel Provider's port
    with a drift test in the repository that compares port numbers.
    """

    from eidolon_ops.capabilities import HOST_CAPABILITIES

    return _read(HOST_CAPABILITIES)


@pytest.fixture(scope="module")
def baseline_topology():
    """What a Host that declares no capability installs."""

    return _read(frozenset())


def test_every_component_answers(topology) -> None:
    # The precondition for any of the rest being meaningful.
    assert topology.silent == ()
    topology.requires_every_component("this test")


def test_the_declared_units_are_the_units_ops_expects(topology) -> None:
    from eidolon_ops.capabilities import HOST_CAPABILITIES
    from eidolon_ops.config import expected_units

    # Against what a Host with every capability installs, not the baseline:
    # comparing the full topology to PRODUCT_UNITS would fail on every
    # conditional unit, and comparing a baseline topology to it would pass by
    # not looking at them.
    assert set(topology.systemd_units) == set(expected_units(HOST_CAPABILITIES))


def test_the_host_agent_carries_the_same_unit_list(baseline_topology) -> None:
    # The Host agent is injected as one payload with no imports of its own, so
    # it cannot share this table with eidolon_ops.config — the duplicate is
    # structural. What was missing was anything checking the two still agree.
    assert set(host_contract.PRODUCT_UNITS) == set(PRODUCT_UNITS)
    assert set(host_contract.PRODUCT_UNITS) == set(baseline_topology.systemd_units)


def test_the_declared_ports_are_the_ports_ops_assigns(topology) -> None:
    # Equality, with nothing set aside. NATS and LiveKit used to be exempted
    # here because they belong to no component; they belong to the platform,
    # and the platform declares them now.
    assert set(topology.port_roles) == set(source_assets.PORTS)
    for role, port in topology.port_roles.items():
        assert source_assets.PORTS[role] == port, f"{role} disagrees"


def test_the_operator_inputs_are_the_ones_an_install_asks_for(topology) -> None:
    declared = {entry.name for entry in topology.install_inputs if entry.is_operator_supplied}

    # Also equality. livekit.env was the exemption here — an install input
    # belonging to no component — until the platform declared it.
    assert declared == set(INSTALL_FILE_NAMES)


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
    templated = [entry for entry in derived if entry.template is not None]

    # One derived input exists, and Ops renders it from a hard-coded address.
    # That address has to be the one the owning component published, because a
    # component that cannot move its own template is a component whose deployed
    # defaults someone else owns — which is where Hub's settings were, in
    # eidolon_kernel, until they moved back here.
    assert [(entry.component_id, entry.template) for entry in templated] == [HUB_SETTINGS_TEMPLATE]
    assert templated[0].install_path == Path(HUB_SETTINGS_DESTINATION)
    destination, owner, group, mode = host_contract.HOST_APPLICATION_INPUTS["hub.generated.yaml"]
    assert destination == templated[0].install_path
    assert (owner, group, mode) == (
        templated[0].owner,
        templated[0].group,
        templated[0].mode,
    )
    public_owner_material = {entry.name: entry for entry in derived if entry.kind == "identity"}
    assert set(public_owner_material) == {
        "owner_domain_descriptor",
        "owner_domain_root_certificate",
        "authority_signing_certificate",
    }
    assert all(
        (entry.owner, entry.group, entry.mode) == ("root", "root", 0o644)
        for entry in public_owner_material.values()
    )


def test_the_backed_up_authorities_are_the_ones_components_named(topology) -> None:
    declared = {
        state.path: (state.owner, state.group)
        for state in topology.authority
        if state.backup == "sqlite-online"
    }
    built_in = {
        path: (owner, group) for path, owner, group in host_contract.BACKED_UP_AUTHORITIES.values()
    }

    assert declared == built_in


def test_what_the_backup_leaves_out_is_still_what_it_leaves_out(topology) -> None:
    uncovered = {state.path for state in topology.authority if not state.is_covered}
    built_in = {path for path, _reason in host_contract.UNCOVERED_STATE.values()}

    # Nothing in the old table is unaccounted for now: JetStream was the last
    # entry that belonged to no component, and the platform declares it.
    assert built_in - uncovered == set()
    # Two things the contracts say that the old table did not. Deployment
    # evidence was only ever in FIXED_DATA_PATHS, which says where a thing is
    # and nothing about whether a backup carries it; LiveKit's session state
    # was in no table at all.
    assert uncovered - built_in == {
        Path("/var/lib/eidolon/deployments"),
        Path("/var/lib/eidolon/livekit"),
    }


def test_state_a_component_copies_itself_is_asked_for_at_the_route_it_declared(
    topology,
) -> None:
    """The only cross-repository call a backup makes, held against its author.

    ``component-action`` state is state Ops cannot copy: memory's spaces are a
    palace whose layout is MemPalace's and an embedder identity without which a
    copy cannot be restored. So the component produces it, and Ops calls a route
    it does not own. A rename on the other side would otherwise surface as a
    404 that reads like memory being down, on the day someone needed a backup.
    """

    declared = {
        state.component_id: (state.snapshot_action, state.restore_action)
        for state in topology.authority
        if state.backup == "component-action"
    }

    # One today. If a second component ever declares this, the agent needs a
    # collector for it too, and this is where that becomes visible.
    assert set(declared) == {"eidolon_memory"}
    assert declared["eidolon_memory"] == (
        memory_realms.SNAPSHOT_ACTION,
        memory_realms.RESTORE_ACTION,
    )


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
    runtime = {path for contract in topology.declared for path in contract.runtime_paths}

    for name, path in FIXED_DATA_PATHS.items():
        assert path in declared or path in runtime, f"{name} at {path} is unclaimed"
    # And the same table on the Host side.
    assert dict(host_contract.FIXED_DATA) == dict(FIXED_DATA_PATHS)


def test_a_factory_reset_reaches_everything_every_component_holds(topology) -> None:
    roots = [path for contract in topology.declared for path in contract.factory_reset_paths]

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
        for contract in topology.declared
        for unit in contract.units
    }

    checked = 0
    sockets = 0
    for asset in SYSTEMD_ASSET_CONTRACTS:
        if asset.unit.endswith(".socket"):
            # A .socket file runs nothing; it names a listener its service is
            # activated through. What there is to check about it — owner, mode,
            # and that the service is not separately enabled — belongs to the
            # unit's own deployment test in eidolon_kernel.
            sockets += 1
            continue
        unit_id = asset.unit.removesuffix(".service")
        component_id, executable = declared[unit_id]
        if component_id == PLATFORM_COMPONENT_ID:
            # The platform's units run binaries the host profile installed, at
            # absolute paths that are the platform's own business. There is no
            # component venv for them to be rooted in.
            assert asset.component_root is None
            assert executable.startswith("/")
            continue
        source = _CHECKOUT_ROOT / asset.source_id / asset.path
        if not source.is_file():
            pytest.skip(f"{asset.source_id} checkout does not carry {asset.path}")

        exec_start = next(
            line
            for line in source.read_text(encoding="utf-8").splitlines()
            if line.startswith("ExecStart=")
        )

        assert asset.component_root == component_id, (
            f"{asset.unit} is rooted in {asset.component_root} but {component_id} declares it"
        )
        assert f"/{component_id}/{executable} " in f"{exec_start} ", (
            f"{asset.unit} runs {exec_start.split('=', 1)[1]}, but "
            f"{component_id} declares {executable}"
        )
        checked += 1

    # A loop that silently checked nothing would pass just as quietly.
    platform_units = len(topology.platform.unit_ids)
    assert checked == len(PRODUCT_UNITS) - platform_units - sockets


def test_the_dev_port_registry_and_the_contracts_use_the_same_numbers(
    topology,
) -> None:
    """The registry a Host is sent, against what each component declared.

    ``ports.yaml`` is a richer document than the contracts — it carries hosts,
    ranges, and entries for things that are not components at all — so this is
    not a set comparison. What it catches is the change that actually happens:
    a component renumbers itself in its contract and the registry Admin builds
    its service catalog from keeps the old number.
    """

    from eidolon_ops.host_layer import _PORT_REGISTRY

    registry = set(
        int(match)
        for match in re.findall(
            r"^\s*(?:port|http_port|admin_port|turn_udp_port):\s*(\d+)\s*$",
            _PORT_REGISTRY.read_text(encoding="utf-8"),
            re.MULTILINE,
        )
    )

    #: Declared by a component, absent from the dev registry on purpose. The
    #: registry describes the macOS stack that runs under supervisord, and
    #: neither of these runs there: the Channel Provider is reached through
    #: Hub, and the Local API is the product Host's own door.
    NOT_IN_THE_DEV_STACK = {
        "channel_provider": 8767,
        "local_api": 9002,
        # Local recognition runs on a board with an NPU, not in the macOS
        # stack this registry describes.
        "asr_stream": 8768,
        # Local generation, for the same reason: the dev stack reaches a cloud
        # model, and this one is CPU-pinned to a board's little cores.
        "llm_api": 8769,
    }

    for role, port in topology.port_roles.items():
        if role in NOT_IN_THE_DEV_STACK:
            # Kept honest in both directions: when one of these does join the
            # dev stack, this fails and the exemption has to go rather than
            # quietly covering a real entry.
            assert NOT_IN_THE_DEV_STACK[role] == port
            assert port not in registry, f"{role} is in the registry now"
            continue
        assert port in registry, f"{role} declares {port}, which the registry does not"
