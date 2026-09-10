"""The one definition of what "this Host can serve the App" means.

``app-ready`` used to be two independent check lists — one the workstation ran
against a macOS source run, one the injected agent ran on the product Host —
that had drifted into asserting different things under the same verdict. This
module holds the check set itself: a closed vocabulary of facts, and a table of
which kind of Host is expected to attest each one. The probes stay where they
have to run; the contract does not.

Fail closed is the point. An evaluator that produces a fact the contract does
not name, or omits one it is expected to attest, is refused rather than scored
— a readiness gate that silently drops a check is worse than one that fails,
because it is the gate a release rollback is decided by.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum


class ReadinessError(ValueError):
    """A readiness report does not match the declared check set."""


#: The agent name the Channel worker registers under and the product dispatches
#: to. A component fact Ops should be reading from the Channel contract rather
#: than holding; until that section exists this is the one place it is, and
#: the readiness payload carries it to the Host instead of the Host guessing.
CHANNEL_AGENT_NAME = "eidolon"
#: Two entries from the Ops-owned port registry. The registry travels to a Host
#: as text and the Host cannot parse YAML, so the readiness payload carries
#: these as values; a drift test pins them to ``config/ports.yaml``.
CHANNEL_WORKER_PORT = 8766
LIVEKIT_SIGNALLING_PORT = 7880
#: How long a Channel probe is given to settle. Sized on a worker reconnecting
#: to LiveKit, because this report decides whether a release is rolled back.
#: A worker that is reconnecting to LiveKit is briefly unable to serve, and an
#: activation restarts LiveKit under it. How long that is allowed to take is the
#: Host's readiness budget — the same one that exists because this board spends
#: sixteen seconds loading models before it can answer at all. Owning a second,
#: smaller number here is how a healthy release gets rolled back.
DEFAULT_CHANNEL_SETTLE_SECONDS = 240

#: What the rest of an app-ready costs beyond the waiting: unit status for
#: every product unit, two HTTPS calls to the Hub, mDNS browsing, file checks.
#: Seconds on a healthy board, and the allowance is generous because being
#: wrong here turns a degraded report into a timed-out connection.
_READINESS_FIXED_OVERHEAD_SECONDS = 60

#: How long an indeterminate setup answer is retried before it is called
#: ``unknown``, which is the only answer worth asking again — ``absent`` and
#: ``ready`` are settled facts about a Host. What this absorbs is the narrow
#: case where Data is up and the one hop to it failed.
SETUP_READINESS_SETTLE_SECONDS = 10

#: Derived, so raising either half cannot leave the operator's transport
#: giving up before the Host has finished answering.
READINESS_OVERHEAD_SECONDS = (
    _READINESS_FIXED_OVERHEAD_SECONDS + SETUP_READINESS_SETTLE_SECONDS
)

#: The deadline the operator's side holds. Derived rather than written down, so
#: raising the budget cannot leave a transport that gives up before the Host
#: has finished answering — which is exactly how a Host with one unhealthy
#: worker came back as a 300-second timeout that said nothing.
READINESS_TRANSPORT_TIMEOUT_SECONDS = (
    DEFAULT_CHANNEL_SETTLE_SECONDS + READINESS_OVERHEAD_SECONDS
)


class HostKind(StrEnum):
    """Which kind of Host is attesting."""

    #: A macOS workstation running the pinned sources under supervisord.
    SOURCE = "source"
    #: A product Host running the sealed release under systemd.
    PRODUCT = "product"


class ReadinessFact(StrEnum):
    """A fact a device's first conversation depends on."""

    BACKEND_HEALTHY = "backend_healthy"
    LAN_ADDRESS_OBSERVED = "lan_address_observed"
    LAN_NAME_RESOLVES = "lan_name_resolves"
    HOST_IDENTITY_MATERIAL = "host_identity_material"
    BOOTSTRAP_PREFLIGHT = "bootstrap_preflight"
    BOOTSTRAP_CONTROL_SOCKET = "bootstrap_control_socket"
    FOUNDATION_SERVICES = "foundation_services"
    HUB_TLS_IDENTITY = "hub_tls_identity"
    HUB_SETTINGS_BOUND = "hub_settings_bound"
    HUB_LAN_REACHABLE = "hub_lan_reachable"
    HUB_DESCRIPTOR_PUBLISHED = "hub_descriptor_published"
    HUB_ADMITS_DEVICES = "hub_admits_devices"
    HUB_MDNS_SERVICE = "hub_mdns_service"
    LOCAL_API_REACHABLE = "local_api_reachable"
    LOCAL_API_TARGETS_HUB = "local_api_targets_hub"
    HOST_SETUP_COMPLETABLE = "host_setup_completable"
    LOCAL_API_MDNS_SERVICE = "local_api_mdns_service"
    DEVICE_REMOVAL_AVAILABLE = "device_removal_available"
    LIVEKIT_CLIENT_ORIGIN = "livekit_client_origin"
    LIVEKIT_LAN_REACHABLE = "livekit_lan_reachable"
    LIVEKIT_NETWORK_CURRENT = "livekit_network_current"
    CHANNEL_WORKER_HEALTHY = "channel_worker_healthy"
    CHANNEL_WORKER_DISPATCH_IDENTITY = "channel_worker_dispatch_identity"
    CHANNEL_WORKER_LIVEKIT_LINK = "channel_worker_livekit_link"


@dataclass(frozen=True, slots=True)
class ReadinessCheck:
    fact: ReadinessFact
    description: str
    attested_by: frozenset[HostKind]


def _both(fact: ReadinessFact, description: str) -> ReadinessCheck:
    return ReadinessCheck(fact, description, frozenset(HostKind))


def _only(kind: HostKind, fact: ReadinessFact, description: str) -> ReadinessCheck:
    return ReadinessCheck(fact, description, frozenset({kind}))


#: Ports and endpoints answer whether a process is listening. These are the
#: facts a device actually needs, which is a different question — the Channel
#: entries exist because a worker with an open port, an active unit and a green
#: gate had already stopped being able to accept a job.
READINESS_CONTRACT: tuple[ReadinessCheck, ...] = (
    _both(
        ReadinessFact.BACKEND_HEALTHY,
        "every product service answers its own health surface",
    ),
    _both(
        ReadinessFact.LAN_ADDRESS_OBSERVED,
        "the Host holds the LAN address it publishes",
    ),
    _both(
        ReadinessFact.LAN_NAME_RESOLVES,
        "the Host-bound name resolves to that address in a live mDNS query",
    ),
    _both(
        ReadinessFact.HOST_IDENTITY_MATERIAL,
        "the Host identity material is present and private",
    ),
    _both(
        ReadinessFact.HUB_TLS_IDENTITY,
        "the Hub TLS pair matches the Host identity and is in date",
    ),
    _both(
        ReadinessFact.HUB_SETTINGS_BOUND,
        "the rendered Hub settings name this Host's Hub id and origin",
    ),
    _both(
        ReadinessFact.HUB_LAN_REACHABLE,
        "the Hub answers over the LAN on its published TLS port",
    ),
    _both(
        ReadinessFact.LOCAL_API_REACHABLE,
        "the Local API answers its health surface and publishes its descriptor",
    ),
    _both(
        ReadinessFact.LOCAL_API_TARGETS_HUB,
        "the Local API is pointed at this Host's Hub and its certificate",
    ),
    # Every other Local API fact here is about a process being up and pointed
    # at the right things, which a unit's ActiveState answers. This one asks
    # the question setup actually depends on, of the component that owns the
    # answer. All the others were green on a Host no phone could finish
    # setting up: services answering is not the same fact as a person being
    # able to finish, and only the second one is readiness.
    _both(
        ReadinessFact.HOST_SETUP_COMPLETABLE,
        "a phone that claims this Host can finish setup: its Workspace "
        "authority answers for it",
    ),
    _both(
        ReadinessFact.LIVEKIT_CLIENT_ORIGIN,
        "Channel is configured with the LiveKit origin devices are given",
    ),
    _both(
        ReadinessFact.LIVEKIT_LAN_REACHABLE,
        "the LiveKit signalling port answers over the LAN",
    ),
    _both(
        ReadinessFact.CHANNEL_WORKER_HEALTHY,
        "the Channel worker's own health surface reports it able to take jobs",
    ),
    _both(
        ReadinessFact.CHANNEL_WORKER_DISPATCH_IDENTITY,
        "that worker serves the agent name the product dispatches to",
    ),
    _only(
        HostKind.PRODUCT,
        ReadinessFact.CHANNEL_WORKER_LIVEKIT_LINK,
        "the worker holds a live registration link to LiveKit",
    ),
    _only(
        HostKind.PRODUCT,
        ReadinessFact.BOOTSTRAP_PREFLIGHT,
        "Bootstrap's own commissioning preflight passes",
    ),
    _only(
        HostKind.PRODUCT,
        ReadinessFact.BOOTSTRAP_CONTROL_SOCKET,
        "the Bootstrap control socket is present",
    ),
    _only(
        HostKind.PRODUCT,
        ReadinessFact.FOUNDATION_SERVICES,
        "Bluetooth, NetworkManager and Avahi are active",
    ),
    _only(
        HostKind.PRODUCT,
        ReadinessFact.HUB_DESCRIPTOR_PUBLISHED,
        "the Hub publishes the onboarding descriptor devices fetch",
    ),
    _only(
        HostKind.PRODUCT,
        ReadinessFact.HUB_ADMITS_DEVICES,
        "the Hub can verify a device's commissioning proof, so a device can be added",
    ),
    _only(
        HostKind.PRODUCT,
        ReadinessFact.DEVICE_REMOVAL_AVAILABLE,
        "the lifecycle workflow is listening, so a device can be removed",
    ),
    _only(
        HostKind.PRODUCT,
        ReadinessFact.HUB_MDNS_SERVICE,
        "the Hub service record advertises this Host's name, address and port",
    ),
    _only(
        HostKind.PRODUCT,
        ReadinessFact.LOCAL_API_MDNS_SERVICE,
        "the Local API service record is published",
    ),
    _only(
        HostKind.SOURCE,
        ReadinessFact.LIVEKIT_NETWORK_CURRENT,
        "eidolond reports LiveKit running on the network it is configured for",
    ),
)


#: Which answers from the Local API's setup surface mean a phone could finish.
#:
#: ``absent`` is a Host nobody has set up yet and ``ready`` is one somebody
#: has; both are Hosts a person can walk up to and use. ``unknown`` is a Host
#: that could not be asked, which is not the same as a Host that answered
#: well. Fail closed, as everywhere else here: a check that could not be made
#: has not passed.
#:
#: There was an ``orphaned`` state, for a Host whose Bootstrap held an Owner
#: its Data plane had no Workspace for. It was reachable only because two
#: stores held one fact; one does now, so nothing can disagree.
HOST_SETUP_COMPLETABLE_STATES = frozenset({"absent", "ready"})


def setup_is_completable(report: object) -> bool:
    """Grade one Local API setup answer, for either kind of Host.

    Here rather than in each probe because the two probes grading the same
    document differently is the exact drift this module was extracted to end.
    """

    return (
        isinstance(report, Mapping)
        and report.get("contract_version") == "1"
        and report.get("state") in HOST_SETUP_COMPLETABLE_STATES
    )


def setup_answer_is_settled(report: Mapping[str, object]) -> bool:
    """Whether re-asking could change this answer.

    The predicate both probes settle on. ``absent`` and ``ready`` are settled
    facts about a Host, so a retry window spent on either is a slower report
    and nothing else. Only ``unknown`` — the answer that means the question
    could not be put — is worth asking again.
    """

    return report.get("state") != "unknown"


def setup_readiness_evidence(report: object) -> dict[str, object]:
    """The graded answer, shaped so a failure says which failure it was.

    A bare ``None`` in the evidence is the difference between "this Host holds
    an Owner its Data plane lost" and "this Local API predates the route" —
    two facts an operator acts on completely differently, and the second is
    routine on a workstation, where the stack keeps running the build it
    started with until somebody restarts it. Both score the same, because a
    check that could not be made has not passed; only the report tells them
    apart, so the report has to.
    """

    if not isinstance(report, Mapping):
        return {
            "healthy": False,
            "state": "unknown",
            "error": (
                "the Local API did not answer /api/local/v1/setup/readiness; "
                "a build predating that route answers 404, and is restarted "
                "rather than repaired"
            ),
        }
    return {
        "healthy": setup_is_completable(report),
        "state": report.get("state"),
        "operation_id": report.get("operation_id"),
    }


def expected_facts(kind: HostKind) -> tuple[str, ...]:
    """The fact names one kind of Host must attest, in contract order."""

    return tuple(
        str(check.fact) for check in READINESS_CONTRACT if kind in check.attested_by
    )


def product_payload(*, settle_seconds: int = DEFAULT_CHANNEL_SETTLE_SECONDS) -> dict[str, object]:
    """What a product Host is told to attest, and what it needs to attest it.

    Sent rather than compiled into the injected agent, for the same reason the
    port registry is: this contract has one author, and a copy on the Host is
    precisely the one nobody would think to update.
    """

    return {
        "facts": list(expected_facts(HostKind.PRODUCT)),
        "channel_worker": {
            "port": CHANNEL_WORKER_PORT,
            "agent_name": CHANNEL_AGENT_NAME,
            "livekit_port": LIVEKIT_SIGNALLING_PORT,
            "settle_seconds": settle_seconds,
        },
    }


def describe(kind: HostKind) -> dict[str, str]:
    return {
        str(check.fact): check.description
        for check in READINESS_CONTRACT
        if kind in check.attested_by
    }


def require_complete(kind: HostKind, checks: Mapping[str, object]) -> None:
    """Refuse a readiness report that is not the declared check set."""

    expected = set(expected_facts(kind))
    actual = set(checks)
    missing = sorted(expected - actual)
    extra = sorted(actual - expected)
    if missing or extra:
        raise ReadinessError(
            "readiness report does not match the declared check set; "
            f"missing={missing or 'none'}, unexpected={extra or 'none'}"
        )


def is_ready(kind: HostKind, checks: Mapping[str, object]) -> bool:
    require_complete(kind, checks)
    return all(bool(value) for value in checks.values())


#: How a readiness report spells a verdict. Probes answer ``healthy``, check
#: lists answer ``ok``, and individual entries answer with a bare boolean.
_VERDICT_KEYS = ("healthy", "ok")


def describe_failures(report: object, path: str = "") -> str:
    """Name what a readiness report found, not merely that it was unhappy.

    A gate failure rolls the release back and the collected phases go with it,
    so "degraded" was the entire report an operator received for a decision
    that had just undone an install.

    This walks the report rather than naming the sections it expects, because
    a section added later would otherwise go unmentioned in exactly the report
    someone reads when they cannot see the Host.
    """

    reasons = _failures(report, path)
    if reasons:
        return "; ".join(reasons)
    status = report.get("status") if isinstance(report, dict) else None
    return f"status={status!r}"


def _failures(report: object, path: str) -> list[str]:
    if isinstance(report, list):
        return [
            reason
            for index, item in enumerate(report)
            for reason in _failures(item, f"{path}[{index}]")
        ]
    if not isinstance(report, dict):
        return []
    if any(report.get(key) is False for key in _VERDICT_KEYS):
        # The section already said it is unhealthy; anything below is why.
        below = [
            reason
            for key, value in report.items()
            if key not in _VERDICT_KEYS
            for reason in _failures(value, f"{path}.{key}" if path else str(key))
        ]
        return below or [path or "report"]
    reasons: list[str] = []
    for key, value in report.items():
        below = f"{path}.{key}" if path else str(key)
        if value is False:
            reasons.append(below)
        elif key == "error" and value:
            reasons.append(str(value))
        else:
            reasons.extend(_failures(value, below))
    return reasons
