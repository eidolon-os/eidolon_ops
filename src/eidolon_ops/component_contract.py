"""Read what each component says about how it is operated, and check it adds up.

Ops carries several tables of other people's business, and it carries some of
them twice: ``PRODUCT_UNITS`` exists in :mod:`eidolon_ops.config` and again in
:mod:`eidolon_ops.hostagent.contract`; the install-file destination map exists
in :mod:`eidolon_ops.host_layer` and again, byte for byte, in
:mod:`eidolon_ops.private_inputs`. Every entry is a fact the owning repository
already knows and this one has to be told again — by hand, in review, if
anyone remembers.

The contract inverts that. A component publishes ``ops/component.toml``; Ops
validates the set and derives a topology from it. Ops keeps only what is
genuinely its own: that the dependency graph has no cycle, that two components
did not claim the same port role or the same unit, that nobody is silent about
something a destructive operation depends on.

Two rules keep this from becoming a second bureaucracy beside the first:

* **Fail closed on the set, not on each file.** A missing contract is not a
  component with no operational needs; it is a component nobody asked. An
  operation that must be complete to be safe refuses rather than proceeding
  with a partial picture.
* **The built-in tables become a test, not a fallback path.** Ops still knows
  the old tables, but only in its test suite, where they assert the contracts
  did not drift. A fallback that runs in production is just the old source of
  truth wearing a different name.
"""

from __future__ import annotations

import json
import tomllib
from dataclasses import dataclass, field, replace
from functools import lru_cache
from pathlib import Path
from typing import Any

from eidolon_ops.capabilities import require_known_capability
from eidolon_ops.errors import OperationsError

__all__ = [
    "COMPONENT_CONTRACT_PATH",
    "PLATFORM_COMPONENT_ID",
    "AuthorityState",
    "ComponentContract",
    "ContractTopology",
    "InstallInput",
    "component_contract_schema",
    "load_component_contract",
    "load_platform_contract",
    "read_component_contracts",
]

#: Where a component publishes it, relative to that repository's root.
COMPONENT_CONTRACT_PATH = Path("ops/component.toml")

#: What the platform's own contract calls itself. Ops holds that one, because
#: Ops installs those units; everything else about it — schema, loader,
#: checks — is the same as a component's.
PLATFORM_COMPONENT_ID = "eidolon_platform"

#: Ordering targets systemd provides. A unit may sit behind these without any
#: component owning them.
_SYSTEM_TARGETS = frozenset(
    {
        "local-fs.target",
        "network.target",
        "network-online.target",
        "dbus.service",
        "bluetooth.service",
        "NetworkManager.service",
    }
)

#: Inside the package rather than at the repository root, so it is found the
#: same way whether Ops is run from a checkout or from an installed wheel.
_CONTRACTS = Path(__file__).with_name("contracts")
_SCHEMA_PATH = _CONTRACTS / "component-ops" / "v1.schema.json"
_PLATFORM_PATH = _CONTRACTS / "platform" / "component.toml"


@dataclass(frozen=True, slots=True)
class InstallInput:
    """A file Ops must install before this component's units can start."""

    name: str
    install_path: Path
    owner: str
    group: str
    mode: int
    kind: str
    #: "operator" — an install refuses to start without it; "derived" — Ops
    #: renders it from this component's own template on the way to the Host.
    source: str
    component_id: str
    template: str | None = None
    required_by: tuple[str, ...] = ()

    @property
    def is_operator_supplied(self) -> bool:
        return self.source == "operator"

    @property
    def install_as(self) -> str:
        """The filename Ops stages it under, which is the one it lands as."""

        return self.install_path.name


@dataclass(frozen=True, slots=True)
class AuthorityState:
    """A path this component would be sorry to lose, and how it is carried."""

    path: Path
    owner: str
    group: str
    backup: str
    component_id: str
    quiesce_units: tuple[str, ...] = ()
    uncovered_reason: str | None = None
    #: Only for ``component-action``: the routes Ops calls to have a copy made
    #: and put back. Held here so a rename in the owning component fails a test
    #: instead of a backup.
    snapshot_action: str | None = None
    restore_action: str | None = None

    @property
    def is_covered(self) -> bool:
        return self.backup != "none"


@dataclass(frozen=True, slots=True)
class ComponentContract:
    """One component's operational self-description, already validated."""

    component_id: str
    contract_version: str
    document: dict[str, Any]
    source: Path

    @property
    def units(self) -> tuple[dict[str, Any], ...]:
        return tuple(self.document.get("units", ()))

    @property
    def unit_ids(self) -> tuple[str, ...]:
        return tuple(unit["id"] for unit in self.units)

    @property
    def ports(self) -> dict[str, dict[str, Any]]:
        return dict(self.document.get("ports", {}))

    @property
    def inputs(self) -> tuple[InstallInput, ...]:
        return tuple(
            InstallInput(
                name=entry["name"],
                install_path=Path(entry["install_path"]),
                owner=entry["owner"],
                group=entry["group"],
                mode=int(entry["mode"], 8),
                kind=entry["kind"],
                source=entry["source"],
                component_id=self.component_id,
                template=entry.get("template"),
                required_by=tuple(entry.get("required_by", ())),
            )
            for entry in self.document.get("inputs", ())
        )

    @property
    def authority(self) -> tuple[AuthorityState, ...]:
        return tuple(
            AuthorityState(
                path=Path(entry["path"]),
                owner=entry["owner"],
                group=entry.get("group", entry["owner"]),
                backup=entry["backup"],
                component_id=self.component_id,
                quiesce_units=tuple(entry.get("quiesce_units", ())),
                uncovered_reason=entry.get("uncovered_reason"),
                snapshot_action=entry.get("snapshot_action"),
                restore_action=entry.get("restore_action"),
            )
            for entry in self.document.get("state", {}).get("authority", ())
        )

    @property
    def runtime_paths(self) -> tuple[Path, ...]:
        return tuple(
            Path(item) for item in self.document.get("state", {}).get("runtime", ())
        )

    @property
    def factory_reset_paths(self) -> tuple[Path, ...]:
        return tuple(
            Path(item) for item in self.document.get("reset", {}).get("factory", ())
        )

    @property
    def schema_gate(self) -> str | None:
        return self.document.get("schema", {}).get("gate")

    @property
    def artifacts(self) -> tuple[dict[str, Any], ...]:
        return tuple(self.document.get("artifacts", ()))

    def select(self, capabilities: frozenset[str]) -> ComponentContract:
        """This contract as it applies to a Host offering ``capabilities``.

        Entries asking for something the Host does not have are dropped here
        and nowhere else, so every later check — port claims, dependencies,
        cycles — sees exactly what this Host installs. A unit kept while
        something it requires was dropped is therefore caught by the ordinary
        dependency check rather than by a special case.
        """

        document = dict(self.document)
        for section in ("units", "artifacts"):
            entries = document.get(section)
            if not entries:
                continue
            document[section] = [
                entry
                for entry in entries
                if _selected(entry, capabilities, self.component_id, section)
            ]
        kept = {unit["id"] for unit in document.get("units", ())}
        if kept != set(self.unit_ids):
            document = _drop_orphans(document, self.document, kept)
        return replace(self, document=document)


@dataclass(frozen=True, slots=True)
class ContractTopology:
    """What the whole set says, once checked together.

    The checks here are the ones no component can make about itself: it cannot
    know whether another claimed its port role, and it cannot see a dependency
    cycle it is only one arc of.
    """

    #: The release's components. Deliberately excludes the platform, so
    #: "nobody declared anything" stays answerable — a release from before the
    #: contracts existed has no component contracts, and Ops always has the
    #: platform's.
    contracts: tuple[ComponentContract, ...] = ()
    #: Ops's own, covering the units the host profile installs.
    platform: ComponentContract | None = None
    #: Sources that published nothing. Kept rather than raised, because whether
    #: silence is fatal depends on the operation: listing tolerates a partial
    #: picture, a factory reset does not.
    silent: tuple[str, ...] = ()

    unit_owner: dict[str, str] = field(default_factory=dict)
    port_roles: dict[str, int] = field(default_factory=dict)

    @property
    def declared(self) -> tuple[ComponentContract, ...]:
        """Everything that speaks for something on the Host, platform included."""

        return self.contracts + ((self.platform,) if self.platform else ())

    @property
    def systemd_units(self) -> tuple[str, ...]:
        """Every unit on a Host, as systemd names."""

        return tuple(f"{unit_id}.service" for unit_id in self.unit_owner)

    @property
    def install_inputs(self) -> tuple[InstallInput, ...]:
        return tuple(entry for contract in self.declared for entry in contract.inputs)

    @property
    def authority(self) -> tuple[AuthorityState, ...]:
        return tuple(entry for contract in self.declared for entry in contract.authority)

    def requires_every_component(self, operation: str) -> None:
        """Refuse an operation that cannot be complete while anyone is silent."""

        if self.silent:
            missing = ", ".join(sorted(self.silent))
            raise OperationsError(
                f"{operation} needs every component to declare an operations "
                f"contract, and these have not: {missing}. A component that "
                f"published nothing is not a component with nothing to declare."
            )


@lru_cache(maxsize=1)
def component_contract_schema() -> dict[str, Any]:
    schema = json.loads(_SCHEMA_PATH.read_text(encoding="utf-8"))
    _validator_class().check_schema(schema)
    return schema


def _validator_class():
    try:
        from jsonschema import Draft202012Validator
    except ModuleNotFoundError as error:  # pragma: no cover - packaging guard
        raise OperationsError(
            "reading component operations contracts needs jsonschema; "
            "install eidolon-ops with its runtime dependencies"
        ) from error
    return Draft202012Validator


def load_component_contract(
    repository: Path, component_id: str
) -> ComponentContract | None:
    """Read one component's contract, or None when it has not published one."""

    source = repository / COMPONENT_CONTRACT_PATH
    if not source.is_file():
        return None
    try:
        document = tomllib.loads(source.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as error:
        raise OperationsError(f"{source} is not readable TOML: {error}") from error

    contract = _validated(document, source, component_id)
    _refuse_internal_contradictions(contract)
    return contract


def _validated(
    document: dict[str, Any], source: Path, expected_id: str
) -> ComponentContract:
    errors = sorted(
        _validator_class()(component_contract_schema()).iter_errors(document),
        key=lambda item: list(item.path),
    )
    if errors:
        detail = "; ".join(
            f"{'/'.join(str(part) for part in error.path) or '<root>'}: {error.message}"
            for error in errors[:5]
        )
        raise OperationsError(f"{source} does not match component-ops/v1: {detail}")

    declared = document["component_id"]
    if declared != expected_id:
        # A contract speaks for the thing it was found for and no other.
        # Without this, a copied file quietly makes one answer for another.
        raise OperationsError(
            f"{source} declares component_id {declared!r} but sits in {expected_id!r}"
        )
    return ComponentContract(
        component_id=declared,
        contract_version=document["contract_version"],
        document=document,
        source=source,
    )


def _refuse_internal_contradictions(contract: ComponentContract) -> None:
    """Checks a component can make about itself, made where the file is read.

    Doing them here rather than in the aggregate means the error names one
    file an author can open, instead of surfacing later as a topology that
    does not add up.
    """

    own_units = set(contract.unit_ids)
    own_ports = set(contract.ports)

    for unit in contract.units:
        for role in unit.get("serves", ()):
            if role not in own_ports:
                raise OperationsError(
                    f"{contract.source}: unit {unit['id']!r} serves port role "
                    f"{role!r}, which this component does not declare"
                )
    for entry in contract.document.get("inputs", ()):
        for unit_id in entry.get("required_by", ()):
            if unit_id not in own_units:
                raise OperationsError(
                    f"{contract.source}: input {entry['name']!r} is required by "
                    f"unit {unit_id!r}, which this component does not own"
                )
    for state in contract.authority:
        for unit_id in state.quiesce_units:
            if unit_id not in own_units:
                raise OperationsError(
                    f"{contract.source}: {state.path} would be quiesced by unit "
                    f"{unit_id!r}, which this component does not own"
                )

    # A factory reset that leaves authority behind produces a Host that
    # presents itself as new and still holds the last owner's data. Checked
    # per component, because only this file knows both halves.
    reset_roots = contract.factory_reset_paths
    for state in contract.authority:
        if not any(state.path.is_relative_to(root) for root in reset_roots):
            raise OperationsError(
                f"{contract.source}: a factory reset would leave {state.path} "
                f"behind, because no reset.factory path covers it"
            )


@lru_cache(maxsize=1)
def load_platform_contract() -> ComponentContract:
    """Ops's own contract, for the units the host profile installs.

    Read through the same schema and the same checks as a component's. The
    only thing it does not do is sit in the repository it speaks for, because
    NATS and LiveKit have none — which is exactly why they were a list of
    exceptions before this file existed.
    """

    document = tomllib.loads(_PLATFORM_PATH.read_text(encoding="utf-8"))
    contract = _validated(document, _PLATFORM_PATH, PLATFORM_COMPONENT_ID)
    _refuse_internal_contradictions(contract)
    return contract


def read_component_contracts(
    sources: dict[str, Path], capabilities: frozenset[str] = frozenset()
) -> ContractTopology:
    """Load every contract and check what only the whole set can answer.

    ``capabilities`` is what the Host offers. Selection happens before any of
    the checks below, so what they see is this Host's installation rather than
    the union of every Host's — two components may both claim a port role on
    paper if no single Host installs both.
    """

    declared: list[ComponentContract] = []
    silent: list[str] = []
    for component_id, repository in sorted(sources.items()):
        contract = load_component_contract(repository, component_id)
        if contract is None:
            silent.append(component_id)
        else:
            declared.append(contract)
    declared.append(load_platform_contract())

    # Recorded before selection, because after it the unit is simply absent and
    # a dependency on it would read as a typo.
    withheld = {
        unit["id"]: unit["requires_capability"]
        for contract in declared
        for unit in contract.units
        if unit.get("requires_capability") not in (None, *capabilities)
    }
    selected = [contract.select(capabilities) for contract in declared]
    contracts, platform = selected[:-1], selected[-1]

    unit_owner: dict[str, str] = {}
    port_owner: dict[str, str] = {}
    port_roles: dict[str, int] = {}
    input_owner: dict[str, str] = {}
    installed_as: dict[str, str] = {}

    for contract in (*contracts, platform):
        for unit_id in contract.unit_ids:
            _claim(unit_owner, unit_id, contract.component_id, "unit")
        for role, port in contract.ports.items():
            _claim(port_owner, role, contract.component_id, "port role")
            port_roles[role] = int(port["default"])
        for entry in contract.inputs:
            _claim(input_owner, entry.name, contract.component_id, "install input")
            # One path, one writer. Two components staging different bytes to
            # one file is a race whose loser is whichever ran second, and it
            # would present as a configuration that reverts itself.
            _claim(
                installed_as,
                str(entry.install_path),
                contract.component_id,
                "installed file",
            )

    _refuse_unknown_dependencies([*contracts, platform], unit_owner, withheld)
    _refuse_cycles([*contracts, platform], unit_owner)

    return ContractTopology(
        contracts=tuple(contracts),
        platform=platform,
        silent=tuple(silent),
        unit_owner=unit_owner,
        port_roles=port_roles,
    )


def _drop_orphans(
    document: dict[str, Any], declared: dict[str, Any], kept: set[str]
) -> dict[str, Any]:
    """Withdraw what only the dropped units needed.

    A port role nothing listens on would still be reserved on the Host and
    still show in the port registry, and an install input nothing requires
    would still be demanded of the operator — for a service this Host is not
    running. Both are withdrawn only when a unit tied them here in the first
    place: an entry no unit ever referenced is not this selection's business.
    """

    served = {
        role
        for unit in document.get("units", ())
        for role in unit.get("serves", ())
    }
    was_served = {
        role
        for unit in declared.get("units", ())
        for role in unit.get("serves", ())
    }
    ports = document.get("ports")
    if ports:
        document["ports"] = {
            role: port
            for role, port in ports.items()
            if role in served or role not in was_served
        }
    inputs = document.get("inputs")
    if inputs:
        document["inputs"] = [
            entry
            for entry in inputs
            if not entry.get("required_by")
            or any(unit_id in kept for unit_id in entry["required_by"])
        ]
    return document


def _selected(
    entry: dict[str, Any],
    capabilities: frozenset[str],
    component_id: str,
    section: str,
) -> bool:
    wanted = entry.get("requires_capability")
    if wanted is None:
        return True
    require_known_capability(wanted, label=f"{component_id} {section} {entry['id']!r}")
    return wanted in capabilities


def _claim(register: dict[str, str], key: str, component_id: str, noun: str) -> None:
    held = register.get(key)
    if held is not None:
        raise OperationsError(
            f"{noun} {key!r} is claimed by both {held} and {component_id}"
        )
    register[key] = component_id


def _dependencies(unit: dict[str, Any]) -> tuple[str, ...]:
    return (*unit.get("requires", ()), *unit.get("after", ()))


def _refuse_unknown_dependencies(
    contracts: list[ComponentContract],
    unit_owner: dict[str, str],
    withheld: dict[str, str] | None = None,
) -> None:
    """A unit may only depend on something that exists.

    Anything unresolvable is a typo or a stale reference, and shipping it means
    finding out as a service that never comes up on a board in someone's home.

    ``withheld`` maps a unit this Host did not select to the capability it
    wanted. A dependency found there is not a typo — it is a Host that installs
    something whose requirement it declined — and saying which capability is
    missing is the difference between a fixable message and a puzzle.
    """

    known = set(unit_owner) | _SYSTEM_TARGETS
    for contract in contracts:
        for unit in contract.units:
            for dependency in _dependencies(unit):
                if dependency in known:
                    continue
                if withheld and dependency in withheld:
                    raise OperationsError(
                        f"{contract.component_id} unit {unit['id']!r} depends on "
                        f"{dependency!r}, which this Host did not select: it "
                        f"requires the capability {withheld[dependency]!r}. "
                        f"Either provide that capability or stop installing "
                        f"{unit['id']!r}."
                    )
                raise OperationsError(
                    f"{contract.component_id} unit {unit['id']!r} depends on "
                    f"{dependency!r}, which nothing declares"
                )


def _refuse_cycles(
    contracts: list[ComponentContract], unit_owner: dict[str, str]
) -> None:
    graph = {
        unit["id"]: tuple(
            dependency
            for dependency in _dependencies(unit)
            if dependency in unit_owner
        )
        for contract in contracts
        for unit in contract.units
    }

    visiting: list[str] = []
    settled: set[str] = set()

    def walk(unit_id: str) -> None:
        if unit_id in settled:
            return
        if unit_id in visiting:
            cycle = " → ".join((*visiting[visiting.index(unit_id) :], unit_id))
            raise OperationsError(f"unit dependencies form a cycle: {cycle}")
        visiting.append(unit_id)
        for dependency in graph.get(unit_id, ()):
            walk(dependency)
        visiting.pop()
        settled.add(unit_id)

    for unit_id in graph:
        walk(unit_id)
