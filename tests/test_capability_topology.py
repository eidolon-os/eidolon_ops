"""The three places that state which units a Host runs, held together.

Ops validates a config before any component contract is read; the injected
agent validates a payload on a Host with no access to those contracts at all.
Both therefore hold their own table, and the contracts hold a third statement
of the same fact in `requires_capability`. Duplication is the design — refusing
early, and refusing independently, is what the copies buy. These tests are what
stop the copies from drifting into three different answers.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from eidolon_ops import config as ops_config
from eidolon_ops.capabilities import HOST_CAPABILITIES
from eidolon_ops.component_contract import read_component_contracts
from eidolon_ops.hostagent import contract as agent_contract

pytestmark = pytest.mark.unit

_REPOSITORIES = Path(__file__).resolve().parents[2]


def test_the_agent_knows_the_same_capabilities_ops_does() -> None:
    assert agent_contract.HOST_CAPABILITIES == HOST_CAPABILITIES


def test_the_agent_derives_the_same_topology_ops_requires() -> None:
    assert agent_contract.PRODUCT_UNITS == ops_config.PRODUCT_UNITS
    assert agent_contract.CAPABILITY_UNITS == ops_config.CAPABILITY_UNITS
    for capability in sorted(HOST_CAPABILITIES):
        selected = frozenset({capability})
        assert agent_contract.expected_units(selected) == ops_config.expected_units(selected)


def test_every_capability_unit_is_one_a_contract_claims() -> None:
    """The table and the contracts must name the same units for the same reason.

    A unit listed here but not required by any contract would be installed and
    owned by nobody; a unit whose contract requires a capability but which this
    table omits would never reach a Host that asked for it.
    """

    for capability, units in ops_config.CAPABILITY_UNITS.items():
        sources = {
            source_id: _REPOSITORIES / source_id
            for source_id in ops_config.CAPABILITY_SOURCES.get(capability, ())
        }
        if not all(path.exists() for path in sources.values()):
            pytest.skip(f"{capability} needs sibling repositories to check")
        without = read_component_contracts(sources, frozenset())
        with_it = read_component_contracts(sources, frozenset({capability}))
        gained = set(with_it.systemd_units) - set(without.systemd_units)
        assert gained == set(units), (
            f"{capability}: the config table says {sorted(units)}, "
            f"the contracts say {sorted(gained)}"
        )


def test_a_capability_that_adds_units_adds_the_source_that_owns_them() -> None:
    """A unit cannot come from a repository the release does not pin."""

    for capability, units in ops_config.CAPABILITY_UNITS.items():
        assert ops_config.CAPABILITY_SOURCES.get(capability), (
            f"{capability} installs {sorted(units)} but pins no repository to take them from"
        )


def test_a_host_without_capabilities_gets_exactly_what_it_always_did() -> None:
    assert ops_config.expected_units(frozenset()) == ops_config.PRODUCT_UNITS
    assert ops_config.expected_sources(frozenset()) == frozenset(ops_config.SOURCE_IDS)
    assert agent_contract.fixed_units({"units": list(ops_config.PRODUCT_UNITS)}) == (
        ops_config.PRODUCT_UNITS
    )


def test_the_agent_refuses_a_capability_it_was_not_built_for() -> None:
    """It may be one that should have brought units this agent will not install."""

    with pytest.raises(agent_contract.TargetError, match="not one this agent"):
        agent_contract.fixed_units(
            {"units": list(ops_config.PRODUCT_UNITS), "capabilities": ["local_vision"]}
        )


def test_the_agent_refuses_a_unit_list_that_does_not_match_the_capabilities() -> None:
    """The payload states both; disagreement is what the agent exists to catch."""

    with pytest.raises(agent_contract.TargetError, match="unit set differs"):
        agent_contract.fixed_units(
            {"units": list(ops_config.PRODUCT_UNITS), "capabilities": ["local_asr"]}
        )


def test_no_two_names_claim_one_port_number() -> None:
    """Role names are checked by the loader; the numbers behind them were not.

    eidolon_models defaulted its recognizer to 8767, which source_assets had
    already given to channel_provider. Both bind loopback on one Host, so the
    two would have raced and one would simply not have come up. That collision
    was found by reading, which is not a mechanism.

    One number carrying one name in both registries is that name declared
    twice, which is fine and is how most roles appear. Two names is the bug.
    """

    from eidolon_ops.source_assets import PORTS

    sources = {
        source_id: _REPOSITORIES / source_id
        for source_id in (*ops_config.SOURCE_IDS, "eidolon_models")
    }
    if not all(path.exists() for path in sources.values()):
        pytest.skip("needs every sibling repository to check every claim")

    names_by_port: dict[int, set[str]] = {}
    for role, port in PORTS.items():
        names_by_port.setdefault(int(port), set()).add(role)
    topology = read_component_contracts(sources, HOST_CAPABILITIES)
    for role, port in topology.port_roles.items():
        names_by_port.setdefault(int(port), set()).add(role)

    collisions = {port: sorted(names) for port, names in names_by_port.items() if len(names) > 1}
    assert not collisions, f"one port, more than one claimant: {collisions}"


def test_every_capability_unit_has_a_file_to_install() -> None:
    """A declared unit with no unit file is a service that cannot start.

    The systemd matrix reads unit bytes from Git at the pinned commits. A
    capability that adds a unit to the topology and no contract to that table
    would pass every check here and produce a Host missing one service.
    """

    from eidolon_ops.release_matrix import CAPABILITY_SYSTEMD_ASSETS

    for capability, units in ops_config.CAPABILITY_UNITS.items():
        provided = {asset.unit for asset in CAPABILITY_SYSTEMD_ASSETS.get(capability, ())}
        assert provided == set(units), (
            f"{capability}: topology says {sorted(units)}, "
            f"the systemd matrix provides {sorted(provided)}"
        )


def test_a_capability_unit_file_comes_from_a_repository_the_host_pins() -> None:
    from eidolon_ops.release_matrix import CAPABILITY_SYSTEMD_ASSETS

    for capability, assets in CAPABILITY_SYSTEMD_ASSETS.items():
        pinned = set(ops_config.CAPABILITY_SOURCES.get(capability, ()))
        for asset in assets:
            assert asset.source_id in pinned | set(ops_config.SOURCE_IDS), (
                f"{capability} installs {asset.unit} from {asset.source_id}, "
                f"which this Host would not pin"
            )


def test_every_agent_payload_that_names_units_also_names_capabilities() -> None:
    """The invariant, rather than the three places that have broken it.

    The agent checks a unit list by deriving one from its own table and the
    capabilities it is told. A payload carrying units and no capabilities asks
    it to derive the baseline, so a Host that installs anything conditional is
    refused by the check meant to protect it. That has now happened in
    provision, in the release matrix and in the service-identity cutover, which
    is enough times to test the rule instead of the instances.
    """

    import ast
    from pathlib import Path

    source_root = Path(__file__).resolve().parents[1] / "src" / "eidolon_ops"
    offenders: list[str] = []
    for path in sorted(source_root.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            callee = node.func
            name = getattr(callee, "attr", None) or getattr(callee, "id", None)
            if name != "run_agent":
                continue
            for argument in node.args:
                if not isinstance(argument, ast.Dict):
                    continue
                keys = {
                    key.value
                    for key in argument.keys
                    if isinstance(key, ast.Constant)
                }
                if "units" in keys and "capabilities" not in keys:
                    offenders.append(f"{path.name}:{node.lineno}")
    assert offenders == [], (
        "these send the agent a unit list with no capabilities, so it will "
        f"derive the baseline and refuse anything conditional: {offenders}"
    )


def _deploy_module(name: str):
    """Load one dependency-free module out of the sibling Kernel repository.

    Imported by path rather than as a package: `eidolon_deploy` is installed on
    a Host, not on this workstation, and importing the package would pull in
    `manifest`, which needs jsonschema. `capabilities` deliberately imports
    nothing, which is what makes this possible at all.
    """

    import importlib.util

    path = _REPOSITORIES / "eidolon_kernel" / "eidolon_deploy" / f"{name}.py"
    if not path.is_file():
        pytest.skip("the release contract needs the sibling Kernel repository to check")
    specification = importlib.util.spec_from_file_location(f"_deploy_{name}", path)
    assert specification is not None and specification.loader is not None
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    return module


def test_the_release_contract_knows_the_same_capabilities_ops_does() -> None:
    """The fourth statement of the same closed set, and why it exists.

    A release descriptor is validated before any component contract is read and
    before Ops' own config is loaded — on a Host, by whoever holds the
    descriptor. That is the point of validating it, so the release contract
    cannot ask Ops what a capability is. It holds its own copy, and this is
    what stops the copy from becoming a different answer.
    """

    assert _deploy_module("capabilities").HOST_CAPABILITIES == HOST_CAPABILITIES


def test_the_release_contract_adds_units_for_the_same_capabilities() -> None:
    """A capability Ops can install units for must be one a release can carry.

    Read out of the source rather than imported, because `manifest` needs
    jsonschema and this workstation has no reason to have it. `ast` rather than
    a regex so the comparison is against the value, not against its formatting.
    """

    import ast

    path = _REPOSITORIES / "eidolon_kernel" / "eidolon_deploy" / "manifest.py"
    if not path.is_file():
        pytest.skip("the release contract needs the sibling Kernel repository to check")
    tree = ast.parse(path.read_text(encoding="utf-8"))
    tables: dict[str, object] = {}
    for node in tree.body:
        target = None
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            target = node.target.id
        elif isinstance(node, ast.Assign) and len(node.targets) == 1:
            first = node.targets[0]
            target = first.id if isinstance(first, ast.Name) else None
        if target == "CAPABILITY_AFFECTED_UNITS" and node.value is not None:
            tables[target] = ast.literal_eval(node.value)
    assert "CAPABILITY_AFFECTED_UNITS" in tables, (
        "the release contract no longer states which units a capability adds"
    )
    release_units = {
        capability: tuple(units)
        for capability, units in tables["CAPABILITY_AFFECTED_UNITS"].items()
    }
    assert release_units == {
        capability: tuple(units)
        for capability, units in ops_config.CAPABILITY_UNITS.items()
        if units
    }
