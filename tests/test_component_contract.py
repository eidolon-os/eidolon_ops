"""What the contract loader refuses, and why each refusal earns its place.

Every check here exists because the alternative failure is expensive and late:
a duplicate port that only shows up as a service that will not bind, a stale
dependency that shows up as a unit that never starts, a backup that quietly
omits a database. The loader is worth having exactly to the extent that it
turns those into a message on a workstation.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from eidolon_ops.component_contract import (
    load_component_contract,
    read_component_contracts,
)
from eidolon_ops.errors import OperationsError

pytestmark = pytest.mark.unit


def _publish(root: Path, component_id: str, body: str) -> Path:
    repository = root / component_id
    (repository / "ops").mkdir(parents=True, exist_ok=True)
    (repository / "ops/component.toml").write_text(
        textwrap.dedent(
            f"""
            schema_version = 1
            component_id = "{component_id}"
            contract_version = "1"
            """
        ).lstrip()
        + textwrap.dedent(body),
        encoding="utf-8",
    )
    return repository


_HUB = """
    [[units]]
    id = "eidolon-hub"
    kind = "service"
    exec = ".venv/bin/uvicorn"
    user = "eidolon"
    serves = ["hub"]

    [ports.hub]
    default = 8082
    bind = "loopback"

    [[state.authority]]
    path = "/var/lib/eidolon/hub/eidolon-hub.sqlite3"
    owner = "eidolon"
    backup = "sqlite-online"

    [reset]
    factory = ["/var/lib/eidolon/hub"]
"""


def test_a_component_that_published_nothing_is_named_not_assumed_empty(
    tmp_path: Path,
) -> None:
    _publish(tmp_path, "eidolon_hub", _HUB)

    topology = read_component_contracts(
        {"eidolon_hub": tmp_path / "eidolon_hub", "eidolon_data": tmp_path / "absent"}
    )

    assert topology.silent == ("eidolon_data",)
    # Listing is allowed to work on a partial picture...
    assert topology.systemd_units[0] == "eidolon-hub.service"
    # ...but anything that must be complete to be safe is not.
    with pytest.raises(OperationsError) as error:
        topology.requires_every_component("factory reset")
    assert "eidolon_data" in str(error.value)


def test_a_contract_may_not_speak_for_a_repository_it_does_not_sit_in(
    tmp_path: Path,
) -> None:
    repository = _publish(tmp_path, "eidolon_hub", _HUB)

    with pytest.raises(OperationsError) as error:
        load_component_contract(repository, "eidolon_data")

    # The realistic mistake is a copied file, and a copy that is accepted makes
    # one component answer for another's units for as long as nobody notices.
    assert "declares component_id 'eidolon_hub'" in str(error.value)


def test_two_components_cannot_claim_one_port_role(tmp_path: Path) -> None:
    _publish(tmp_path, "eidolon_hub", _HUB)
    _publish(
        tmp_path,
        "eidolon_data",
        """
        [ports.hub]
        default = 8084
        """,
    )

    with pytest.raises(OperationsError) as error:
        read_component_contracts(
            {
                "eidolon_hub": tmp_path / "eidolon_hub",
                "eidolon_data": tmp_path / "eidolon_data",
            }
        )

    assert "port role 'hub' is claimed by both" in str(error.value)


def test_two_components_cannot_install_one_filename(tmp_path: Path) -> None:
    for component_id in ("eidolon_hub", "eidolon_data"):
        _publish(
            tmp_path,
            component_id,
            f"""
            [[inputs]]
            name = "{component_id}_env"
            install_path = "/etc/eidolon/shared.env"
            kind = "secret"
            source = "operator"
            owner = "root"
            group = "root"
            mode = "0600"
            """,
        )

    with pytest.raises(OperationsError) as error:
        read_component_contracts(
            {
                "eidolon_hub": tmp_path / "eidolon_hub",
                "eidolon_data": tmp_path / "eidolon_data",
            }
        )

    # Both would be written to /etc/eidolon/shared.env, and the surviving bytes
    # would be whichever component was staged last.
    assert "installed file '/etc/eidolon/shared.env'" in str(error.value)


def test_a_unit_may_not_depend_on_something_no_one_declares(tmp_path: Path) -> None:
    _publish(
        tmp_path,
        "eidolon_hub",
        """
        [[units]]
        id = "eidolon-hub"
        kind = "service"
        exec = ".venv/bin/uvicorn"
        requires = ["eidolon-channel-provider"]
        """,
    )

    with pytest.raises(OperationsError) as error:
        read_component_contracts({"eidolon_hub": tmp_path / "eidolon_hub"})

    assert "eidolon-channel-provider" in str(error.value)


def test_the_platform_units_may_be_depended_on_without_a_contract(
    tmp_path: Path,
) -> None:
    _publish(
        tmp_path,
        "eidolon_memory",
        """
        [[units]]
        id = "eidolon-memory-supervisor"
        kind = "service"
        exec = ".venv/bin/eidolon-memory-supervisor"
        requires = ["eidolon-nats"]
        after = ["local-fs.target"]
        """,
    )

    topology = read_component_contracts({"eidolon_memory": tmp_path / "eidolon_memory"})

    # NATS and LiveKit are upstream servers installed by the platform profile.
    # They have no repository of ours to publish a contract, and demanding one
    # would mean inventing a component that does not exist.
    assert "eidolon-nats.service" in topology.systemd_units


def test_a_dependency_cycle_is_named_rather_than_hung_on(tmp_path: Path) -> None:
    _publish(
        tmp_path,
        "eidolon_hub",
        """
        [[units]]
        id = "eidolon-hub"
        kind = "service"
        exec = ".venv/bin/uvicorn"
        requires = ["eidolon-data"]

        [[units]]
        id = "eidolon-data"
        kind = "service"
        exec = ".venv/bin/uvicorn"
        requires = ["eidolon-hub"]
        """,
    )

    with pytest.raises(OperationsError) as error:
        read_component_contracts({"eidolon_hub": tmp_path / "eidolon_hub"})

    message = str(error.value)
    assert "cycle" in message
    # systemd would resolve this by dropping an ordering edge of its choosing
    # and starting them in an order nobody designed.
    assert "eidolon-hub" in message and "eidolon-data" in message


def test_state_that_is_not_backed_up_has_to_say_what_is_missing(
    tmp_path: Path,
) -> None:
    repository = _publish(
        tmp_path,
        "eidolon_memory",
        """
        [[state.authority]]
        path = "/var/lib/eidolon/memory"
        owner = "eidolon"
        backup = "none"
        """,
    )

    with pytest.raises(OperationsError) as error:
        load_component_contract(repository, "eidolon_memory")

    # A backup that silently omits the memory palace restores into a Host that
    # looks whole and has forgotten everything.
    assert "uncovered_reason" in str(error.value)

    repository = _publish(
        tmp_path,
        "eidolon_memory",
        """
        [[state.authority]]
        path = "/var/lib/eidolon/memory"
        owner = "eidolon"
        backup = "none"
        uncovered_reason = "vector index and knowledge graph have no declared snapshot"

        [reset]
        factory = ["/var/lib/eidolon/memory"]
        """,
    )
    state = load_component_contract(repository, "eidolon_memory").authority[0]
    assert state.is_covered is False
    assert state.uncovered_reason


def test_quiesced_state_has_to_name_the_units_to_stop(tmp_path: Path) -> None:
    repository = _publish(
        tmp_path,
        "eidolon_data",
        """
        [[state.authority]]
        path = "/var/lib/eidolon/objects"
        owner = "eidolon"
        backup = "quiesce-copy"

        [reset]
        factory = ["/var/lib/eidolon/objects"]
        """,
    )

    with pytest.raises(OperationsError) as error:
        load_component_contract(repository, "eidolon_data")

    # "Stop it, copy it, start it" is not an instruction until it says what.
    assert "quiesce_units" in str(error.value)


def test_a_component_may_only_reference_its_own_units_and_ports(
    tmp_path: Path,
) -> None:
    repository = _publish(
        tmp_path,
        "eidolon_hub",
        """
        [[units]]
        id = "eidolon-hub"
        kind = "service"
        exec = ".venv/bin/uvicorn"
        serves = ["kernel"]

        [ports.hub]
        default = 8082
        """,
    )

    with pytest.raises(OperationsError) as error:
        load_component_contract(repository, "eidolon_hub")

    # Caught where the file is, so the message names something its author owns.
    assert "serves port role 'kernel'" in str(error.value)


def test_an_absolute_exec_is_refused_because_layout_is_not_the_components(
    tmp_path: Path,
) -> None:
    repository = _publish(
        tmp_path,
        "eidolon_hub",
        """
        [[units]]
        id = "eidolon-hub"
        kind = "service"
        exec = "/opt/eidolon/current/eidolon_hub/.venv/bin/uvicorn"
        """,
    )

    with pytest.raises(OperationsError) as error:
        load_component_contract(repository, "eidolon_hub")

    # /opt/eidolon/current is where this platform profile happens to unpack a
    # release. A component that writes it down has taken a position on
    # something it does not get to decide.
    assert "units/0/exec" in str(error.value)


def test_an_unknown_key_is_refused_rather_than_ignored(tmp_path: Path) -> None:
    repository = _publish(
        tmp_path,
        "eidolon_hub",
        """
        [[units]]
        id = "eidolon-hub"
        kind = "service"
        exec = ".venv/bin/uvicorn"
        restart_seconds = 3
        """,
    )

    with pytest.raises(OperationsError) as error:
        load_component_contract(repository, "eidolon_hub")

    # Ignoring it would let an author believe they had declared something.
    assert "restart_seconds" in str(error.value)


def test_a_factory_reset_may_not_leave_authority_behind(tmp_path: Path) -> None:
    repository = _publish(
        tmp_path,
        "eidolon_hub",
        """
        [[state.authority]]
        path = "/var/lib/eidolon/hub/eidolon-hub.sqlite3"
        owner = "eidolon"
        backup = "sqlite-online"

        [reset]
        factory = ["/var/lib/eidolon/hub-cache"]
        """,
    )

    with pytest.raises(OperationsError) as error:
        load_component_contract(repository, "eidolon_hub")

    # The failure this prevents is a Host handed to someone else that still
    # holds the last owner's device admissions, while presenting as new.
    message = str(error.value)
    assert "factory reset would leave" in message
    assert "eidolon-hub.sqlite3" in message


_MODELS = """
    [[units]]
    id = "eidolon-asr"
    kind = "service"
    exec = ".venv/bin/eidolon-asr"
    user = "eidolon"
    serves = ["asr_stream"]
    requires_capability = "local_asr"

    [ports.asr_stream]
    default = 8768
    bind = "loopback"

    [[artifacts]]
    id = "paraformer_zh_2pass"
    kind = "model"
    requires_capability = "local_asr"
    install_root = "/opt/eidolon/models/asr"

    [[artifacts]]
    id = "qwen3_1_7b_rkllm"
    kind = "model"
    requires_capability = "rknpu2"
    install_root = "/opt/eidolon/models/llm"
"""


def _models_only(tmp_path: Path) -> dict[str, Path]:
    return {"eidolon_models": _publish(tmp_path, "eidolon_models", _MODELS)}


def test_a_host_that_provides_nothing_installs_nothing_conditional(tmp_path: Path) -> None:
    """The default, and what every Host looked like before capabilities."""

    topology = read_component_contracts(_models_only(tmp_path))

    assert "eidolon-asr" not in topology.unit_owner
    assert "asr_stream" not in topology.port_roles
    assert topology.contracts[0].artifacts == ()


def test_a_capability_selects_only_what_asked_for_it(tmp_path: Path) -> None:
    topology = read_component_contracts(_models_only(tmp_path), frozenset({"local_asr"}))

    assert topology.unit_owner["eidolon-asr"] == "eidolon_models"
    assert topology.port_roles["asr_stream"] == 8768
    # The NPU weights are two gigabytes this Host would never load.
    assert [entry["id"] for entry in topology.contracts[0].artifacts] == [
        "paraformer_zh_2pass"
    ]


def test_every_capability_selects_everything(tmp_path: Path) -> None:
    topology = read_component_contracts(
        _models_only(tmp_path), frozenset({"local_asr", "rknpu2"})
    )

    assert [entry["id"] for entry in topology.contracts[0].artifacts] == [
        "paraformer_zh_2pass",
        "qwen3_1_7b_rkllm",
    ]


def test_an_unconditional_unit_is_installed_on_every_host(tmp_path: Path) -> None:
    """Six components predate capabilities and must keep deploying."""

    topology = read_component_contracts(
        {"eidolon_hub": _publish(tmp_path, "eidolon_hub", _HUB)}, frozenset()
    )

    assert topology.unit_owner["eidolon-hub"] == "eidolon_hub"


def test_depending_on_a_unit_this_host_declined_names_the_capability(
    tmp_path: Path,
) -> None:
    """The one incoherent selection, refused rather than silently cascaded.

    Dropping the dependent too would leave a Host quietly missing a service it
    asked for; the operator has to choose which half they meant.
    """

    sources = {
        "eidolon_models": _publish(tmp_path, "eidolon_models", _MODELS),
        "eidolon_channel": _publish(
            tmp_path,
            "eidolon_channel",
            """
            [[units]]
            id = "eidolon-channel"
            kind = "service"
            exec = ".venv/bin/eidolon-channel"
            user = "eidolon"
            requires = ["eidolon-asr"]
            """,
        ),
    }

    with pytest.raises(OperationsError, match="requires the capability 'local_asr'"):
        read_component_contracts(sources, frozenset())


def test_a_capability_no_one_defined_is_refused_not_ignored(tmp_path: Path) -> None:
    """An open set would drop the unit on every Host and say nothing."""

    sources = {
        "eidolon_models": _publish(
            tmp_path,
            "eidolon_models",
            """
            [[units]]
            id = "eidolon-asr"
            kind = "service"
            exec = ".venv/bin/eidolon-asr"
            user = "eidolon"
            requires_capability = "locl_asr"
            """,
        )
    }

    with pytest.raises(OperationsError, match="unknown Host capability 'locl_asr'"):
        read_component_contracts(sources, frozenset({"local_asr"}))


def test_an_input_only_a_dropped_unit_needed_is_not_demanded(tmp_path: Path) -> None:
    """Asking an operator for a secret no service on this Host will read."""

    body = """
        [[units]]
        id = "eidolon-asr"
        kind = "service"
        exec = ".venv/bin/eidolon-asr"
        user = "eidolon"
        requires_capability = "local_asr"

        [[units]]
        id = "eidolon-models-admin"
        kind = "service"
        exec = ".venv/bin/eidolon-models-admin"
        user = "eidolon"

        [[inputs]]
        name = "asr_env"
        install_path = "/etc/eidolon/asr.env"
        kind = "secret"
        source = "operator"
        owner = "root"
        group = "root"
        mode = "0600"
        required_by = ["eidolon-asr"]

        [[inputs]]
        name = "models_env"
        install_path = "/etc/eidolon/models.env"
        kind = "secret"
        source = "operator"
        owner = "root"
        group = "root"
        mode = "0600"
        required_by = ["eidolon-models-admin"]
    """
    sources = {"eidolon_models": _publish(tmp_path, "eidolon_models", body)}

    def _models_inputs(topology) -> list[str]:
        # The platform declares inputs of its own; only this component's are
        # the selection's business.
        return sorted(
            entry.name
            for entry in topology.install_inputs
            if entry.component_id == "eidolon_models"
        )

    assert _models_inputs(read_component_contracts(sources, frozenset())) == [
        "models_env"
    ]
    assert _models_inputs(
        read_component_contracts(sources, frozenset({"local_asr"}))
    ) == ["asr_env", "models_env"]
