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


def test_the_target_preparer_knows_the_same_capabilities_ops_does() -> None:
    """The fifth statement of the same closed set.

    `prepare_target.py` runs on a Host from inside the bundle, before anything
    is installed and with nothing to import, so it holds its own copy too. It
    was written checking membership against the table of what each capability
    *adds*, which refused a Host that legitimately declared `rknpu2` — a
    capability that adds no source — as declaring something unknown, after the
    whole bundle had been transferred to it.
    """

    import ast

    path = _REPOSITORIES / "eidolon_kernel" / "eidolon_deploy" / "prepare_target.py"
    if not path.is_file():
        pytest.skip("the target preparer needs the sibling Kernel repository to check")
    tree = ast.parse(path.read_text(encoding="utf-8"))
    declared: object | None = None
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            first = node.targets[0]
            if isinstance(first, ast.Name) and first.id == "_HOST_CAPABILITIES":
                value = node.value
                # `frozenset({...})` is a call, not a literal, so the set
                # inside it is what gets read.
                if isinstance(value, ast.Call) and len(value.args) == 1:
                    value = value.args[0]
                declared = ast.literal_eval(value)
    assert declared is not None, "the target preparer no longer states the capability set"
    assert set(declared) == set(HOST_CAPABILITIES)


def test_the_service_manifest_requires_the_same_capability_the_contract_does() -> None:
    """The sixth statement, and the one whose absence broke the board.

    eidolond starts what its own service manifest names, and nothing else. The
    manifest had no entry for a conditional service and no notion of one, so a
    unit was installed that nothing started, and the release's readiness check
    for it timed out and rolled the Host back.

    The manifest states the requirement in the same words the component's own
    `ops/component.toml` does. This is what keeps the two from drifting into
    different answers about the same service.
    """

    import yaml

    manifest = _REPOSITORIES / "eidolon_kernel" / "config" / "system-services.yaml"
    if not manifest.is_file():
        pytest.skip("the service manifest needs the sibling Kernel repository to check")
    document = yaml.safe_load(manifest.read_text(encoding="utf-8"))
    by_unit = {
        service["host_targets"].get("systemd"): service.get("requires_capability")
        for service in document["services"]
    }

    for capability, units in ops_config.CAPABILITY_UNITS.items():
        for unit in units:
            assert unit in by_unit, (
                f"{unit} is in the topology {capability} brings and in no service the "
                "manifest names, so nothing on the Host would ever start it"
            )
            assert by_unit[unit] == capability, (
                f"the manifest has {unit} requiring {by_unit[unit]!r}; the config "
                f"table brings it for {capability!r}"
            )

    # And the other direction: a manifest service that requires a capability
    # must be one this config table knows brings it, or a Host that declares the
    # capability would install no unit for a service it now expects to start.
    conditional = {unit: needed for unit, needed in by_unit.items() if needed is not None}
    brought = {
        unit: capability
        for capability, units in ops_config.CAPABILITY_UNITS.items()
        for unit in units
    }
    assert conditional == brought


def test_a_capability_port_reaches_the_host_registry(pinned_config, monkeypatch) -> None:
    """The number is stated once, by the component that reserves it.

    Otherwise every consumer writes 8768 into its own configuration — the class
    of defect this repository spent a day removing. `asr_stream` exists on a
    board with an NPU and on no other Host, so it cannot live in the curated
    baseline file; it is derived per Host and added under its own key.
    """

    from dataclasses import replace

    from eidolon_ops.config import SourceConfig
    from eidolon_ops.host_layer import HostLayer

    def registry(capabilities: frozenset[str]) -> str:
        sources = dict(pinned_config.sources)
        sources["eidolon_models"] = SourceConfig(path=_REPOSITORIES / "eidolon_models")
        config = replace(pinned_config, sources=sources, capabilities=capabilities)
        layer = HostLayer(
            config,
            transport=object(),
            app=None,
            read_exact_source_file=lambda *_a: "",
            source_revisions=lambda: {},
        )
        return layer._port_registry()

    if not (_REPOSITORIES / "eidolon_models" / "ops" / "component.toml").is_file():
        pytest.skip("the port role needs the sibling eidolon_models repository")

    plain = registry(frozenset())
    npu = registry(frozenset({"local_asr"}))

    assert "asr_stream" not in plain
    assert "port_roles:" not in plain, (
        "a Host that declares nothing gets the curated baseline unchanged"
    )
    assert "port_roles:" in npu
    assert "asr_stream: 8768" in npu
    # Added, never folded into the curated nesting: the existing keys are
    # hand-chosen (`nats_http` lives at `nats.http_port`), so there is no
    # role-name-to-path rule to apply.
    assert npu.startswith(plain.rstrip("\n"))


def test_the_baseline_ports_are_not_restated_per_capability(pinned_config) -> None:
    """Two answers to one question is the thing being avoided.

    Every port every Host binds is already in the curated file. Only what a
    capability *adds* is derived, so nothing appears twice with a chance to
    disagree.
    """

    from dataclasses import replace

    from eidolon_ops.config import SourceConfig
    from eidolon_ops.host_layer import HostLayer

    if not (_REPOSITORIES / "eidolon_models" / "ops" / "component.toml").is_file():
        pytest.skip("the port role needs the sibling eidolon_models repository")

    sources = dict(pinned_config.sources)
    sources["eidolon_models"] = SourceConfig(path=_REPOSITORIES / "eidolon_models")
    config = replace(
        pinned_config, sources=sources, capabilities=frozenset({"local_asr"})
    )
    layer = HostLayer(
        config,
        transport=object(),
        app=None,
        read_exact_source_file=lambda *_a: "",
        source_revisions=lambda: {},
    )

    # livekit and nats are baseline roles: present in the curated file, and so
    # not repeated in the derived section.
    assert layer._capability_port_roles() == {"asr_stream": 8768}


def test_an_overlay_cannot_ask_a_host_for_what_it_does_not_declare(tmp_path) -> None:
    """The check the shared name buys, in one string comparison.

    Pointing Channel at local recognition on a Host that does not declare
    `local_asr` produces a Host that installs no such unit, starts no such
    service, and then fails every utterance at the first connection — with the
    configuration reading as though someone meant it.
    """

    from eidolon_ops.config import ConfigurationError, _require_declared_capability_for_overlay
    from eidolon_ops.settings_overlay import OverlayAssignment

    asks_for_local = (
        OverlayAssignment(
            "channel.yaml",
            (("providers", None), ("stt_provider", None)),
            "local_asr",
        ),
    )

    _require_declared_capability_for_overlay(asks_for_local, frozenset({"local_asr"}))

    with pytest.raises(ConfigurationError, match="does not declare"):
        _require_declared_capability_for_overlay(asks_for_local, frozenset({"rknpu2"}))


def test_a_provider_that_is_not_a_capability_is_left_alone(tmp_path) -> None:
    """`bailian` is a service outside this Host; no capability gates it."""

    from eidolon_ops.config import _require_declared_capability_for_overlay
    from eidolon_ops.settings_overlay import OverlayAssignment

    cloud = (
        OverlayAssignment(
            "channel.yaml",
            (("providers", None), ("stt_provider", None)),
            "bailian",
        ),
    )

    _require_declared_capability_for_overlay(cloud, frozenset())


def test_settings_that_are_not_provider_choices_are_left_alone() -> None:
    """The table is keyed by document and path, so a value that happens to read
    like a capability elsewhere is not second-guessed."""

    from eidolon_ops.config import _require_declared_capability_for_overlay
    from eidolon_ops.settings_overlay import OverlayAssignment

    unrelated = (
        OverlayAssignment(
            "agent.yaml", (("notes", None),), "local_asr"
        ),
    )

    _require_declared_capability_for_overlay(unrelated, frozenset())


def test_an_address_on_a_host_that_does_not_run_the_service_is_refused() -> None:
    """A URL cannot be compared to a capability name, so the pair is declared.

    Without this the Host installs no such unit, starts no such service, and
    then fails every turn at the first request — with the configuration
    reading as though someone meant it.
    """

    from eidolon_ops.config import (
        ConfigurationError,
        _require_declared_capability_for_overlay,
    )
    from eidolon_ops.settings_overlay import OverlayAssignment, parse_path

    local = (
        OverlayAssignment(
            "agent.yaml",
            parse_path("llm.models[0].api_base", label="agent.yaml"),
            "http://127.0.0.1:8769/v1",
        ),
    )

    with pytest.raises(ConfigurationError) as error:
        _require_declared_capability_for_overlay(local, frozenset())

    assert "local_llm" in str(error.value)
    # And accepted on a Host that does declare it.
    _require_declared_capability_for_overlay(local, frozenset({"local_llm"}))


def test_a_local_address_naming_another_port_than_the_registry_is_refused() -> None:
    """The port is assigned in one place. This is the copy with nothing holding
    it there, so it is held here."""

    from eidolon_ops.config import (
        ConfigurationError,
        _require_declared_capability_for_overlay,
    )
    from eidolon_ops.settings_overlay import OverlayAssignment, parse_path
    from eidolon_ops.source_assets import PORTS

    wrong = (
        OverlayAssignment(
            "agent.yaml",
            parse_path("llm.models[0].api_base", label="agent.yaml"),
            f"http://127.0.0.1:{PORTS['llm_api'] + 1}/v1",
        ),
    )

    with pytest.raises(ConfigurationError) as error:
        _require_declared_capability_for_overlay(wrong, frozenset({"local_llm"}))

    assert str(PORTS["llm_api"]) in str(error.value)


def test_every_local_address_setting_names_a_capability_and_a_port_that_exist() -> None:
    """Both halves of the pair are looked up, so neither may be a typo."""

    from eidolon_ops.config import CAPABILITY_LOCAL_ADDRESS_SETTINGS
    from eidolon_ops.source_assets import PORTS

    assert CAPABILITY_LOCAL_ADDRESS_SETTINGS
    for capability, role in CAPABILITY_LOCAL_ADDRESS_SETTINGS.values():
        assert capability in HOST_CAPABILITIES
        assert role in PORTS


def test_the_board_config_asks_for_the_local_model_at_the_assigned_port() -> None:
    """The Host this was built for, read as an operator would deploy it."""

    from eidolon_ops.config import load_config
    from eidolon_ops.source_assets import PORTS

    config = load_config(Path("config/eidolon-rk3588.toml"))
    overlay = {
        (item.document, item.display): item.value for item in config.settings_overlay
    }

    assert "local_llm" in config.capabilities
    assert overlay[("agent.yaml", "llm.models[0].api_base")] == (
        f"http://127.0.0.1:{PORTS['llm_api']}/v1"
    )
    # The default has to be the entry that was retargeted, or the Host runs the
    # model and asks a provider anyway.
    assert overlay[("agent.yaml", "llm.default_model")] == (
        overlay[("agent.yaml", "llm.models[0].name")]
    )


def test_the_path_contract_check_reads_this_hosts_own_host_env(
    tmp_path, monkeypatch
) -> None:
    """It compared against the module constant, so every Host that declared a
    capability reported its own path contract as broken.

    A false alarm rather than a wrong Host: host.env carries the capability
    line the agent itself writes, and the baseline constant does not. It said
    nothing true about the Host and hid whatever it would have said.
    """

    from eidolon_ops.hostagent import contract as agent, lifecycle

    declared = frozenset({"local_asr", "local_llm"})
    host_env = tmp_path / "host.env"
    host_env.write_text(agent.host_env_value(declared), encoding="utf-8")
    monkeypatch.setattr(agent, "HOST_ENV_PATH", host_env)

    payload = {
        "units": list(agent.expected_units(declared)),
        "capabilities": sorted(declared),
        "data": {name: str(path) for name, path in agent.FIXED_DATA.items()},
        "remote_uv": "/usr/local/bin/uv",
        "ports": "ports: {}\n",
    }

    checks = lifecycle.doctor_host(payload)["checks"]

    assert checks["host_path_contract"] is True

    # And a Host whose file really has drifted still fails.
    host_env.write_text(agent.host_env_value(frozenset()), encoding="utf-8")
    assert lifecycle.doctor_host(payload)["checks"]["host_path_contract"] is False


def test_the_agent_knows_the_same_service_groups_ops_does() -> None:
    """The seventh copy of a capability table, held like the other six."""

    assert agent_contract.CAPABILITY_SERVICE_GROUPS == ops_config.CAPABILITY_SERVICE_GROUPS


def test_every_service_group_belongs_to_a_capability_that_exists() -> None:
    for capability in ops_config.CAPABILITY_SERVICE_GROUPS:
        assert capability in HOST_CAPABILITIES


def test_a_capability_needing_hardware_is_refused_when_the_group_is_absent() -> None:
    """Rather than skipped. Without the group the service starts, loads
    nothing, and answers 503 forever — which reads like a slow board.

    This is what a real deployment did: `local_tts` came up as `eidolon`, the
    RKNN runtime could not open /dev/dri/card1, and the release timed out on
    readiness three times before the cause was read off an strace.
    """

    import inspect

    from eidolon_ops.hostagent import install

    from eidolon_ops.hostagent import contract as agent

    with pytest.raises(Exception, match="no such group exists"):
        agent.ensure_capability_service_groups(
            frozenset({"local_tts"}),
            group_exists=lambda _group: False,
            add_membership=lambda *_a: pytest.fail("membership attempted anyway"),
            member_of=lambda _user: frozenset(),
        )

    # And a Host that already has it is left alone rather than usermod'ed on
    # every release.
    assert (
        agent.ensure_capability_service_groups(
            frozenset({"local_tts"}),
            group_exists=lambda _group: True,
            add_membership=lambda *_a: pytest.fail("membership added twice"),
            member_of=lambda _user: frozenset({"video"}),
        )
        == ()
    )
    assert (
        agent.ensure_capability_service_groups(
            frozenset({"local_tts"}),
            group_exists=lambda _group: True,
            add_membership=lambda *_a: None,
            member_of=lambda _user: frozenset(),
        )
        == ("video",)
    )


def test_the_groups_are_not_created_by_this_agent() -> None:
    """`video` is the distribution's, with a number the device nodes already
    use. A group created here would be a different one with the same name."""

    import inspect

    from eidolon_ops.hostagent import install

    source = inspect.getsource(install)
    start = source.index("def _ensure_capability_service_groups")
    end = source.index("def _ensure_group_membership")
    assert "groupadd" not in source[start:end]


def test_the_release_path_applies_them_too_not_only_a_first_install() -> None:
    """A Host's capabilities change between releases. Applying this only on a
    first install is how one that gained local synthesis would keep failing
    readiness until someone re-installed it."""

    import inspect

    from eidolon_ops.hostagent import host_application

    assert "ensure_capability_service_groups" in inspect.getsource(host_application)
