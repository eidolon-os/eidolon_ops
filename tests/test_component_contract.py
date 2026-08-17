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
