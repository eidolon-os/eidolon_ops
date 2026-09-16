"""Fail-closed initialization of the 14 private first-install inputs."""

from __future__ import annotations

import secrets
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

from eidolon_ops.config import INSTALL_FILE_NAMES, OperationsConfig
from eidolon_ops.errors import InstallInputError
from eidolon_ops.private_inputs import (
    INSTALL_DESTINATION_NAMES,
    PROVIDER_DESTINATIONS,
    refresh_derived_settings,
    refresh_provider_credentials,
    require_safe_input_directory,
    write_private_directory,
    write_private_file,
)
from eidolon_ops.product_settings import product_settings
from eidolon_ops.provider_inputs import (
    EXTERNAL_KEYS as _EXTERNAL_KEYS,
)
from eidolon_ops.provider_inputs import (
    OPTIONAL_CHANNEL_KEYS as _OPTIONAL_CHANNEL_KEYS,
)
from eidolon_ops.provider_inputs import (
    parse_provider_env,
    serialize_env,
    usable_secret,
)

__all__ = [
    "INSTALL_DESTINATION_NAMES",
    "add_missing_install_credentials",
    "declared_credential_classes",
    "declared_credential_relationships",
    "declared_secret_env_keys",
    "host_rendered_fields",
    "initialize_install_inputs",
    "validate_install_input_contract",
    "withdraw_rendered_fields",
]


def initialize_install_inputs(
    config: OperationsConfig,
    read_exact_file: Callable[[str, str, str], str],
    *,
    new_identity: bool = False,
) -> dict[str, object]:
    """Create one private, internally consistent product input set."""

    target = target_directory(config)
    if target.exists() or target.is_symlink():
        if new_identity:
            raise InstallInputError("new identity requires the Host identity replacement workflow")
        return _validate_existing(target, config, read_exact_file)
    identity, identity_origin = _host_identity(target, new_identity=new_identity)

    providers = {
        source_id: parse_provider_env(config.sources[source_id].path / "config/.env")
        for source_id in _EXTERNAL_KEYS
    }
    missing = [
        f"{source_id}:{key}"
        for source_id, keys in _EXTERNAL_KEYS.items()
        for key in keys
        if not usable_secret(providers[source_id].get(key), key=key)
    ]
    if missing:
        raise InstallInputError(
            "required provider credentials are missing or placeholders: " + ", ".join(missing)
        )

    data_token = secrets.token_urlsafe(32)
    memory_roster_token = secrets.token_urlsafe(32)
    workspace_token = secrets.token_urlsafe(32)
    local_api_token = secrets.token_urlsafe(32)
    hub_reader_token = secrets.token_urlsafe(32)
    hub_jwt_secret = secrets.token_urlsafe(48)
    hub_provider_token = secrets.token_urlsafe(32)
    pairing_token = secrets.token_urlsafe(48)
    memory_token = secrets.token_urlsafe(32)
    # Two credentials for the two Owner-facing authority surfaces that grew one.
    # Both were unauthenticated until 2026-08-25 and both hold the most personal
    # state on a Host: what an Eidolon remembers, and what was said to it.
    memory_api_token = secrets.token_urlsafe(32)
    agent_admin_token = secrets.token_urlsafe(32)
    livekit_key = secrets.token_urlsafe(18)
    livekit_secret = secrets.token_urlsafe(48)

    channel_external = {
        key: value
        for key in (*_EXTERNAL_KEYS["eidolon_channel"], *_OPTIONAL_CHANNEL_KEYS)
        if usable_secret(
            value := providers["eidolon_channel"].get(key),
            key=key,
        )
    }
    env_documents: dict[str, Mapping[str, str]] = {
        "data.env": {
            "EIDOLON_DATA_COMPANION_AUTHORITY_TOKEN": data_token,
            "EIDOLON_DATA_MEMORY_RUNTIME_ROSTER_TOKEN": memory_roster_token,
            "EIDOLON_DATA_WORKSPACE_AUTHORITY_TOKEN": workspace_token,
            "EIDOLON_DATA_SQLITE_PATH": "/var/lib/eidolon/eidolon-system.sqlite3",
            "EIDOLON_DATA_DATABASE_URL": (
                "sqlite+aiosqlite:////var/lib/eidolon/eidolon-system.sqlite3"
            ),
            "EIDOLON_DATA_OBJECT_STORE_PATH": "/var/lib/eidolon/objects",
            "EIDOLON_DATA_AUDIT_NATS_URL": "nats://127.0.0.1:4222",
        },
        "hub.env": {
            "EIDOLON_HUB_MANAGEMENT_JWT_SECRET": hub_jwt_secret,
            "EIDOLON_HUB_DEVICE_REGISTRY_READER_TOKEN": hub_reader_token,
            "EIDOLON_HUB_CHANNEL_PROVIDER_TOKEN": hub_provider_token,
        },
        "kernel.env": {
            "EIDOLON_KERNEL_HUB_MANAGEMENT_TOKEN": hub_reader_token,
            "EIDOLON_KERNEL_COMPANION_AUTHORITY_TOKEN": data_token,
        },
        "admin.env": {
            "EIDOLON_ADMIN_DATA_AUTHORITY_TOKEN": data_token,
            "EIDOLON_ADMIN_DATA_WORKSPACE_AUTHORITY_TOKEN": workspace_token,
            "EIDOLON_ADMIN_HUB_MANAGEMENT_JWT_SECRET": hub_jwt_secret,
            "EIDOLON_ADMIN_LOCAL_API_SERVICE_TOKEN": local_api_token,
            # The per-Realm memory surface Admin reads the Owner's memory
            # through. Without it every memory management page answers 503 with
            # "credential is not configured" — which is what a Host installed
            # before this line did.
            "EIDOLON_ADMIN_MEMORY_API_SERVICE_TOKEN": memory_api_token,
            # Unprefixed on purpose: the service registry names this exact
            # variable (``config/services.yaml``, agent entry), and two names for
            # one secret is how they come to disagree.
            "EIDOLON_AGENT_ADMIN_API_TOKEN": agent_admin_token,
            # The channel provider's credential, unprefixed for the same reason
            # as the Agent's above: the service registry names this exact
            # variable. Admin reads one route on that surface — which bodies are
            # on their channel — because nothing else on this Host observes
            # presence: Hub publishes existence and refuses liveness by
            # contract, and the runtime blackboard's reader was withdrawn.
            "EIDOLON_CHANNEL_PROVIDER_TOKEN": hub_provider_token,
            "EIDOLON_ADMIN_SYSTEM_DIRECTORY_UDS": "/run/eidolon/system.sock",
            "EIDOLON_ADMIN_AUDIT_NATS_URL": "nats://127.0.0.1:4222",
        },
        "local-api.env": {
            "EIDOLON_LOCAL_API_ADMIN_BASE_URL": "http://127.0.0.1:9000",
            "EIDOLON_LOCAL_API_ADMIN_SERVICE_TOKEN": local_api_token,
            "EIDOLON_LOCAL_API_LIFECYCLE_WORKFLOW_SOCKET": (
                "/run/eidolon-lifecycle/workflow.sock"
            ),
        },
        "bootstrap.env": {},
        "agent.env": {
            "EIDOLON_AGENT_LLM_API_KEY": providers["eidolon_agent"]["EIDOLON_AGENT_LLM_API_KEY"],
            "EIDOLON_DATA_COMPANION_AUTHORITY_TOKEN": data_token,
            "EIDOLON_MEMORY_MCP_TOKEN": memory_token,
            "EIDOLON_AGENT_ADMIN_API_TOKEN": agent_admin_token,
            "PAIRING_JWT_SECRET": pairing_token,
        },
        "channel.env": {
            **channel_external,
            "LIVEKIT_API_KEY": livekit_key,
            "LIVEKIT_API_SECRET": livekit_secret,
            "EIDOLON_CHANNEL_PROVIDER_TOKEN": hub_provider_token,
            "PAIRING_JWT_SECRET": pairing_token,
            "EIDOLON_DATA_COMPANION_AUTHORITY_TOKEN": data_token,
        },
        "memory.env": {
            "EIDOLON_DATA_MEMORY_RUNTIME_ROSTER_TOKEN": memory_roster_token,
            "EIDOLON_MEMORY_LLM_API_KEY": providers["eidolon_memory"]["EIDOLON_MEMORY_LLM_API_KEY"],
            "EIDOLON_MEMORY_MCP_TOKEN": memory_token,
            "EIDOLON_MEMORY_API_TOKEN": memory_api_token,
        },
        "livekit.env": {
            "LIVEKIT_API_KEY": livekit_key,
            "LIVEKIT_API_SECRET": livekit_secret,
        },
    }
    settings = product_settings(config, read_exact_file)
    files: dict[str, bytes] = {
        **{name: serialize_env(values) for name, values in env_documents.items()},
        "host_identity.ed25519": identity,
        **{name: value.encode("utf-8") for name, value in settings.items()},
    }
    if set(files) != set(INSTALL_DESTINATION_NAMES.values()):
        raise InstallInputError("generated install input set is incomplete")
    write_private_directory(target, files)
    _anchor_host_identity(target, identity)
    return {
        "status": "initialized",
        "directory": str(target),
        "host_identity": identity_origin,
        "files": sorted(files),
        "provider_sources": {
            source_id: str(config.sources[source_id].path / "config/.env")
            for source_id in _EXTERNAL_KEYS
        },
        "redaction": "credential values and generated key material are never returned",
    }


#: Every secret two files have to agree on, and what to call the disagreement.
#:
#: Declared once because two things read it: the contract check, which proves the
#: two sides still match, and the repair below, which gives an already-installed
#: Host a credential the product grew after it was installed. Those two used to
#: be the same table written twice — and the second copy is how a new credential
#: gets validated on a Host that has no way to receive it.
SHARED_CREDENTIALS: tuple[tuple[str, str, str, str, str], ...] = (
    ("data.env", "EIDOLON_DATA_MEMORY_RUNTIME_ROSTER_TOKEN", "memory.env", "EIDOLON_DATA_MEMORY_RUNTIME_ROSTER_TOKEN", "Data/Memory runtime roster token"),
    ("data.env", "EIDOLON_DATA_COMPANION_AUTHORITY_TOKEN", "kernel.env", "EIDOLON_KERNEL_COMPANION_AUTHORITY_TOKEN", "Data/Kernel companion authority token"),
    ("data.env", "EIDOLON_DATA_COMPANION_AUTHORITY_TOKEN", "admin.env", "EIDOLON_ADMIN_DATA_AUTHORITY_TOKEN", "Data/Admin authority token"),
    ("data.env", "EIDOLON_DATA_COMPANION_AUTHORITY_TOKEN", "agent.env", "EIDOLON_DATA_COMPANION_AUTHORITY_TOKEN", "Data/Agent companion authority token"),
    ("data.env", "EIDOLON_DATA_COMPANION_AUTHORITY_TOKEN", "channel.env", "EIDOLON_DATA_COMPANION_AUTHORITY_TOKEN", "Data/Channel companion authority token"),
    ("data.env", "EIDOLON_DATA_WORKSPACE_AUTHORITY_TOKEN", "admin.env", "EIDOLON_ADMIN_DATA_WORKSPACE_AUTHORITY_TOKEN", "Data/Admin Workspace authority token"),
    ("hub.env", "EIDOLON_HUB_DEVICE_REGISTRY_READER_TOKEN", "kernel.env", "EIDOLON_KERNEL_HUB_MANAGEMENT_TOKEN", "Hub/Kernel management token"),
    ("hub.env", "EIDOLON_HUB_MANAGEMENT_JWT_SECRET", "admin.env", "EIDOLON_ADMIN_HUB_MANAGEMENT_JWT_SECRET", "Hub/Admin management JWT secret"),
    ("hub.env", "EIDOLON_HUB_CHANNEL_PROVIDER_TOKEN", "channel.env", "EIDOLON_CHANNEL_PROVIDER_TOKEN", "Hub/Channel Provider token"),
    ("admin.env", "EIDOLON_ADMIN_LOCAL_API_SERVICE_TOKEN", "local-api.env", "EIDOLON_LOCAL_API_ADMIN_SERVICE_TOKEN", "Admin/Local API service token"),
    ("agent.env", "PAIRING_JWT_SECRET", "channel.env", "PAIRING_JWT_SECRET", "Agent/Channel JWT"),
    ("agent.env", "EIDOLON_MEMORY_MCP_TOKEN", "memory.env", "EIDOLON_MEMORY_MCP_TOKEN", "Agent/Memory MCP token"),
    ("admin.env", "EIDOLON_ADMIN_MEMORY_API_SERVICE_TOKEN", "memory.env", "EIDOLON_MEMORY_API_TOKEN", "Admin/Memory API service token"),
    ("admin.env", "EIDOLON_AGENT_ADMIN_API_TOKEN", "agent.env", "EIDOLON_AGENT_ADMIN_API_TOKEN", "Admin/Agent admin API token"),
    ("admin.env", "EIDOLON_CHANNEL_PROVIDER_TOKEN", "channel.env", "EIDOLON_CHANNEL_PROVIDER_TOKEN", "Admin/Channel Provider token"),
    ("channel.env", "LIVEKIT_API_KEY", "livekit.env", "LIVEKIT_API_KEY", "Channel/LiveKit key"),
    ("channel.env", "LIVEKIT_API_SECRET", "livekit.env", "LIVEKIT_API_SECRET", "Channel/LiveKit secret"),
)

@dataclass(frozen=True, slots=True)
class EnvFileKeys:
    """What one generated environment file holds, and who writes each key.

    Three categories rather than one set, because ``channel.env`` has all three
    and a set could only state one. So that file was pushed out of the
    accounting entirely, and four separate places grew their own way of saying
    "except channel.env" -- ``declared_secret_env_keys``, the repair below, the
    ``not_repairable`` field it reported, and a second required-key set written
    out by hand inside the contract check. None of them agreed on why, and the
    reason they all gave -- that "missing" is undecidable there -- was not true:
    the required set was fully written down, 340 lines from this table.

    ``required``
        Every Host must hold it. The generator mints it or copies it from the
        file holding the other side of a shared secret, the contract check
        refuses a set without it, and the convergence payload asks a Host for
        exactly these.

    ``optional``
        The operator has this credential or does not, and the input set says
        the same either way. This is the one thing a plain set could not
        express, and the whole reason one file sat outside the table.

    ``rendered``
        Written onto this file by the Host layer on the way to a Host, from
        facts only a Host binding knows. **Never present in the input set
        itself.** ``local-api.env`` has always been this shape -- its five
        Owner-domain fields are rendered and none of them is in its seed --
        and stating it here is what makes the shape checkable rather than
        coincidental. ``channel.env`` was the one file that broke it: it
        carried ``EIDOLON_LIVEKIT_CLIENT_URL=ws://127.0.0.1:7880``, a value
        the render replaced on every path out of here, and which would have
        pointed every device at itself had it ever survived one.
    """

    required: frozenset[str] = frozenset()
    optional: frozenset[str] = frozenset()
    rendered: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        if self.required & self.optional or (self.required | self.optional) & self.rendered:
            raise InstallInputError("one key cannot be in two categories of one env file")


#: Exactly which keys each generated env file holds, and who writes each.
#:
#: Module level, and read by four callers for the same reason
#: ``SHARED_CREDENTIALS`` is: the contract check proves a set has not drifted,
#: the repair below works out what an older Host is missing, the convergence
#: payload tells a Host what the product requires, and the Host layer proves
#: it renders exactly the fields declared here. A second copy would let a
#: credential be required by one and unknown to the other.
DECLARED_ENV_KEYS: dict[str, EnvFileKeys] = {
    "data.env": EnvFileKeys(
        required=frozenset({
            "EIDOLON_DATA_COMPANION_AUTHORITY_TOKEN",
            "EIDOLON_DATA_MEMORY_RUNTIME_ROSTER_TOKEN",
            "EIDOLON_DATA_WORKSPACE_AUTHORITY_TOKEN",
            "EIDOLON_DATA_SQLITE_PATH",
            "EIDOLON_DATA_DATABASE_URL",
            "EIDOLON_DATA_OBJECT_STORE_PATH",
            "EIDOLON_DATA_AUDIT_NATS_URL",
        }),
    ),
    "hub.env": EnvFileKeys(
        required=frozenset({
            "EIDOLON_HUB_MANAGEMENT_JWT_SECRET",
            "EIDOLON_HUB_DEVICE_REGISTRY_READER_TOKEN",
            "EIDOLON_HUB_CHANNEL_PROVIDER_TOKEN",
        }),
    ),
    "kernel.env": EnvFileKeys(
        required=frozenset({
            "EIDOLON_KERNEL_HUB_MANAGEMENT_TOKEN",
            "EIDOLON_KERNEL_COMPANION_AUTHORITY_TOKEN",
        }),
    ),
    "admin.env": EnvFileKeys(
        required=frozenset({
            "EIDOLON_ADMIN_DATA_AUTHORITY_TOKEN",
            "EIDOLON_ADMIN_DATA_WORKSPACE_AUTHORITY_TOKEN",
            "EIDOLON_ADMIN_HUB_MANAGEMENT_JWT_SECRET",
            "EIDOLON_ADMIN_LOCAL_API_SERVICE_TOKEN",
            "EIDOLON_ADMIN_MEMORY_API_SERVICE_TOKEN",
            "EIDOLON_AGENT_ADMIN_API_TOKEN",
            "EIDOLON_CHANNEL_PROVIDER_TOKEN",
            "EIDOLON_ADMIN_SYSTEM_DIRECTORY_UDS",
            "EIDOLON_ADMIN_AUDIT_NATS_URL",
        }),
    ),
    "local-api.env": EnvFileKeys(
        required=frozenset({
            "EIDOLON_LOCAL_API_ADMIN_BASE_URL",
            "EIDOLON_LOCAL_API_ADMIN_SERVICE_TOKEN",
            "EIDOLON_LOCAL_API_LIFECYCLE_WORKFLOW_SOCKET",
        }),
        # Where this Host's Owner domain is, which is a fact about this Host
        # and not a credential. Rendered on the way out, never held here.
        rendered=frozenset({
            "EIDOLON_LOCAL_API_OWNER_DOMAIN_ID",
            "EIDOLON_LOCAL_API_OWNER_DOMAIN_DESCRIPTOR_URI",
            "EIDOLON_LOCAL_API_OWNER_DOMAIN_DESCRIPTOR",
            "EIDOLON_LOCAL_API_OWNER_ROOT_CERTIFICATE",
            "EIDOLON_LOCAL_API_AUTHORITY_SIGNING_CERTIFICATE",
        }),
    ),
    "bootstrap.env": EnvFileKeys(),
    "agent.env": EnvFileKeys(
        required=frozenset({
            "EIDOLON_AGENT_LLM_API_KEY",
            "EIDOLON_AGENT_ADMIN_API_TOKEN",
            "EIDOLON_DATA_COMPANION_AUTHORITY_TOKEN",
            "EIDOLON_MEMORY_MCP_TOKEN",
            "PAIRING_JWT_SECRET",
        }),
    ),
    "channel.env": EnvFileKeys(
        required=frozenset({
            "OPENAI_LLM_API_KEY",
            "BAILIAN_STT_API_KEY",
            "BAILIAN_TTS_API_KEY",
            "LIVEKIT_API_KEY",
            "LIVEKIT_API_SECRET",
            "EIDOLON_CHANNEL_PROVIDER_TOKEN",
            "PAIRING_JWT_SECRET",
            "EIDOLON_DATA_COMPANION_AUTHORITY_TOKEN",
        }),
        # A second synthesis vendor the operator may or may not have bought.
        # This pair is why the whole file used to sit outside this table.
        optional=frozenset(_OPTIONAL_CHANNEL_KEYS),
        # Where devices are told to reach LiveKit, and whether plain `ws://`
        # is allowed on this Host's LAN. Neither is answerable from here: the
        # first is answered per binding by the Channel provider, against the
        # address that binding is actually reachable on, and the second is one
        # profile's decision about one Host.
        rendered=frozenset({
            "EIDOLON_LIVEKIT_CLIENT_URL",
            "EIDOLON_CHANNEL_PROVIDER_ALLOW_INSECURE_LAN_CLIENT_URL",
        }),
    ),
    "memory.env": EnvFileKeys(
        required=frozenset({
            "EIDOLON_DATA_MEMORY_RUNTIME_ROSTER_TOKEN",
            "EIDOLON_MEMORY_LLM_API_KEY",
            "EIDOLON_MEMORY_MCP_TOKEN",
            "EIDOLON_MEMORY_API_TOKEN",
        }),
    ),
    "livekit.env": EnvFileKeys(
        required=frozenset({"LIVEKIT_API_KEY", "LIVEKIT_API_SECRET"}),
    ),
}

#: Which fields the Host layer writes onto an input file on the way to a Host.
#:
#: Derived from the table above rather than restated, and read by both
#: renderers -- the Pi's and the Mac's -- so a field added to one of them and
#: not to this table fails rather than arriving undeclared. That is exactly how
#: ``EIDOLON_CHANNEL_PROVIDER_ALLOW_INSECURE_LAN_CLIENT_URL`` came to exist on
#: every Host while appearing in no declaration anywhere in Ops.
HOST_RENDERED_ENV_KEYS: dict[str, frozenset[str]] = {
    name: keys.rendered for name, keys in DECLARED_ENV_KEYS.items() if keys.rendered
}

#: Entries that are product topology rather than secrets: the same on every
#: Host, so a repair can write them and a check can prove they were not edited.
FIXED_ENV_VALUES: dict[str, str] = {
    "EIDOLON_DATA_SQLITE_PATH": "/var/lib/eidolon/eidolon-system.sqlite3",
    "EIDOLON_DATA_DATABASE_URL": (
        "sqlite+aiosqlite:////var/lib/eidolon/eidolon-system.sqlite3"
    ),
    "EIDOLON_DATA_OBJECT_STORE_PATH": "/var/lib/eidolon/objects",
    # Where the Data authority publishes governance facts for the global audit
    # stream. A fixed local address like the others here: the bus runs on this
    # Host and nothing about it is a secret.
    #
    # It matters that it is *declared*. Without it that authority publishes
    # nothing — and, by design, purges nothing either, so the Owner's history
    # stays readable and the failure is invisible. A silent failure that looks
    # like working is exactly what this table exists to catch.
    "EIDOLON_DATA_AUDIT_NATS_URL": "nats://127.0.0.1:4222",
    # The consuming end of the same stream. Admin keeps the index, so the loop
    # that fills it runs there — one address declared for both ends, because a
    # producer pointed at a bus its consumer is not on looks exactly like
    # working.
    "EIDOLON_ADMIN_AUDIT_NATS_URL": "nats://127.0.0.1:4222",
    "EIDOLON_ADMIN_SYSTEM_DIRECTORY_UDS": "/run/eidolon/system.sock",
    "EIDOLON_LOCAL_API_ADMIN_BASE_URL": "http://127.0.0.1:9000",
    "EIDOLON_LOCAL_API_LIFECYCLE_WORKFLOW_SOCKET": "/run/eidolon-lifecycle/workflow.sock",
}

_DATA_PATH_KEYS = (
    "EIDOLON_DATA_SQLITE_PATH",
    "EIDOLON_DATA_DATABASE_URL",
    "EIDOLON_DATA_OBJECT_STORE_PATH",
)

#: Credentials only an operator can supply. A repair refuses when one is missing
#: rather than inventing a value that would authenticate to nothing.
#:
#: Derived from the two tables that already state this, rather than written out
#: a third time. The hand-written copy this replaces was byte-identical to a
#: fourth copy inside the contract check below, which is the arrangement every
#: comment in this file warns against.
PROVIDER_ENV_KEYS: tuple[tuple[str, str], ...] = tuple(
    (PROVIDER_DESTINATIONS[source_id], key)
    for source_id, keys in _EXTERNAL_KEYS.items()
    for key in keys
)

def declared_credential_classes() -> list[dict[str, object]]:
    """Every place one shared credential lives, grouped by the value they share.

    :data:`SHARED_CREDENTIALS` states pairs, and a repair cannot work in pairs.
    The companion authority token lives in five files joined by four pairs; a
    Host holding ``Y`` in four of them and ``X`` in the fifth has exactly one
    mismatched *pair*, and correcting that pair alone would move ``data.env``
    and leave the three files that agreed with it behind — the same Host, broken
    a different way.

    So the unit is the credential: the connected component of the pair graph,
    every slot that must hold one value. Thirteen of them today, from seventeen
    pairs.

    Derived here rather than on the Host, like every other declaration in the
    payload, and ordered so two runs produce the same document.
    """

    parent: dict[tuple[str, str], tuple[str, str]] = {}

    def find(slot: tuple[str, str]) -> tuple[str, str]:
        parent.setdefault(slot, slot)
        while parent[slot] != slot:
            parent[slot] = parent[parent[slot]]
            slot = parent[slot]
        return slot

    for left_file, left_key, right_file, right_key, _label in SHARED_CREDENTIALS:
        left, right = find((left_file, left_key)), find((right_file, right_key))
        if left != right:
            parent[left] = right

    grouped: dict[tuple[str, str], list[tuple[str, str]]] = {}
    for slot in parent:
        grouped.setdefault(find(slot), []).append(slot)
    return [
        {"slots": [{"file": file, "key": key} for file, key in sorted(slots)]}
        for _root, slots in sorted(grouped.items())
    ]


def declared_credential_relationships() -> list[dict[str, str]]:
    """Which two credentials a Host must hold as one value, for the agent to prove.

    Derived from :data:`SHARED_CREDENTIALS` for the same reason
    :func:`declared_secret_env_keys` is derived from the table beside it: the
    workstation owns what the product requires, and this travels to the Host
    rather than being known there.

    It is the same table the install contract proves on this machine. The
    difference is where it is proven — and until this existed, nowhere was the
    Host. The contract check runs on ``install --apply`` and on no other path,
    so after the day a Host is installed its two copies of a shared secret were
    never compared again by anything.
    """

    return [
        {
            "left_file": left_file,
            "left_key": left_key,
            "right_file": right_file,
            "right_key": right_key,
            "label": label,
        }
        for left_file, left_key, right_file, right_key, label in SHARED_CREDENTIALS
    ]


def declared_secret_env_keys() -> dict[str, list[str]]:
    """What every environment file on a Host must hold, for the agent to apply.

    Derived from :data:`DECLARED_ENV_KEYS` rather than restated, so "what the
    product requires" has one author. It is sent to the Host in the convergence
    payload instead of being known there: the agent is a mechanism, and an agent
    carrying its own copy of the requirement would be a second opinion that
    drifts — which is the whole shape of the failure this exists to close.

    ``required`` only. An optional credential is legitimately absent, so asking
    a Host for one could only ever report a Host that is fine; and a rendered
    field is not in the input set this converges from, so there would be nothing
    to deliver. Files with nothing required are left out, because a declaration
    of nothing is not a thing to converge to — which today is ``bootstrap.env``
    and nothing else. ``channel.env`` used to be left out too, on the stated
    grounds that "missing" was undecidable there; it was decidable, and it is in
    now, so a Host short a Channel credential is caught by the same gate as
    every other file.
    """

    return {
        name: sorted(keys.required)
        for name, keys in sorted(DECLARED_ENV_KEYS.items())
        if keys.required
    }


def add_missing_install_credentials(
    config: OperationsConfig,
    *,
    apply: bool = False,
) -> dict[str, object]:
    """Give an already-installed Host the credentials the product grew since.

    The gap this closes: a component adds a credential, the generator learns to
    mint it, the contract check learns to require it — and every Host installed
    before that day has an input set the check now refuses and no supported way
    to fix. The alternatives were hand-editing secrets or reinitialising, and
    reinitialising rotates every credential on the Host and re-anchors its
    identity. Neither is a thing to ask of someone whose Host is working.

    What it will do: add keys this file set is *missing*, and only those.
    - a secret shared with another file that already has it is **copied**, never
      re-minted, because minting one side of a pair is how the pair breaks;
    - a secret nobody has yet is minted once and written to both sides;
    - product topology (paths, loopback URLs) is written from the contract.

    What it will not do: touch a value that is already there, rotate anything,
    re-anchor the Host identity, or invent a provider credential — an LLM key
    this process made up would authenticate to nothing, so a missing one is
    reported and the repair refuses.

    Dry by default. ``apply=False`` reports what it would add and writes nothing,
    because the operator running this is holding a Host that currently works.
    """

    target = target_directory(config)
    require_safe_input_directory(target)
    envs = {name: parse_provider_env(target / name) for name in DECLARED_ENV_KEYS}

    missing_provider = [
        f"{name}:{key}"
        for name, key in PROVIDER_ENV_KEYS
        if not usable_secret(envs[name].get(key), key=key)
    ]
    if missing_provider:
        raise InstallInputError(
            "provider credentials must be supplied by the operator, not generated: "
            + ", ".join(missing_provider)
        )

    #: Where each shared secret can be copied from, keyed by (file, key).
    partners: dict[tuple[str, str], tuple[str, str]] = {}
    for left_file, left_key, right_file, right_key, _label in SHARED_CREDENTIALS:
        partners.setdefault((left_file, left_key), (right_file, right_key))
        partners.setdefault((right_file, right_key), (left_file, left_key))

    added: dict[str, list[str]] = {}
    minted: dict[tuple[str, str], str] = {}
    for name, declared in DECLARED_ENV_KEYS.items():
        for key in sorted(declared.required - set(envs[name])):
            value = FIXED_ENV_VALUES.get(key)
            if value is None:
                partner = partners.get((name, key))
                if partner is not None and envs[partner[0]].get(partner[1]):
                    value = envs[partner[0]][partner[1]]
                elif partner is not None:
                    # Neither side has it: mint once for the pair, so both get
                    # the same value in one pass.
                    value = minted.setdefault(
                        min((name, key), partner), secrets.token_urlsafe(32)
                    )
                else:
                    value = secrets.token_urlsafe(32)
            envs[name][key] = value
            added.setdefault(name, []).append(key)

    if apply and added:
        for name in added:
            write_private_file(target / name, serialize_env(envs[name]))

    return {
        "status": "credentials_added" if apply and added else "planned",
        "directory": str(target),
        # No ``not_repairable`` any more. It named ``channel.env``, and it said
        # the opposite of the truth: that file is the one the Host layer can
        # replace whole, and it is now repaired like every other. A field
        # naming an exception outlives the exception; the accounting below
        # covers every file the table declares, so there is nothing to name.
        # Names only. A report that carried the values would put every new
        # secret in a terminal's scrollback.
        "added": {name: sorted(keys) for name, keys in sorted(added.items())},
        "unchanged": sorted(set(DECLARED_ENV_KEYS) - set(added)),
        "applied": bool(apply and added),
        "redaction": "credential values are never returned",
    }


def host_rendered_fields(name: str, values: Mapping[str, str]) -> dict[str, str]:
    """The fields a Host layer renderer writes onto one input file, proved.

    Both renderers — the Pi's and the Mac's — build their replacements and hand
    them here, so a field one of them writes is a field
    :data:`DECLARED_ENV_KEYS` names. Without this the table is a comment:
    ``EIDOLON_CHANNEL_PROVIDER_ALLOW_INSECURE_LAN_CLIENT_URL`` reached every
    Host this product has ever had while appearing in no declaration anywhere
    in Ops, and the only way to find out a Host had it was to read the file on
    the Host.
    """

    declared = HOST_RENDERED_ENV_KEYS.get(name, frozenset())
    if set(values) != declared:
        raise InstallInputError(
            f"Host layer renders fields {name} does not declare: "
            f"{', '.join(sorted(set(values) ^ declared))}"
        )
    return dict(values)


def withdraw_rendered_fields(target: Path) -> list[str]:
    """Drop fields the Host layer renders from the input set it renders onto.

    A copy of a fact is not a second opinion about it, and these are not even
    copies: the value an input set holds here was written before anything knew
    the answer. ``EIDOLON_LIVEKIT_CLIENT_URL`` is the one this was written for.
    It sat in ``channel.env`` as ``ws://127.0.0.1:7880``, replaced on every path
    out of this machine, pinned there by a contract check, and catastrophic on
    the one path that skips the render — a Host profile with no ``[app]``, which
    the target accepts — because the Channel provider hands a configured host
    straight to the device and loopback is the one host its "plain ws:// only on
    loopback" rule lets through. Every device would have been told to reach
    LiveKit at its own address.

    Withdrawn rather than refused, and on the same pass that makes the derived
    settings and the provider credentials follow their sources, because this is
    the same kind of thing: an input set that was written when Ops still thought
    it owned this field, and has no way to say so.
    """

    withdrawn: list[str] = []
    for name, rendered in HOST_RENDERED_ENV_KEYS.items():
        values = parse_provider_env(target / name)
        stale = sorted(set(values) & rendered)
        if not stale:
            continue
        for key in stale:
            del values[key]
        write_private_file(target / name, serialize_env(values))
        withdrawn.extend(f"{name}:{key}" for key in stale)
    return withdrawn


def validate_install_input_contract(
    config: OperationsConfig,
    read_exact_file: Callable[[str, str, str], str],
    *,
    verify_provider_sources: bool = True,
    refresh_derived: bool = False,
) -> dict[str, object]:
    """Re-prove identities, token relationships and exact product settings.

    Eleven of the fourteen inputs are material a person authored: credentials,
    the Host identity, the relationships between them. Those are compared and
    refused, which is what this function is for.

    The other three are not. ``agent.yaml``, ``channel.yaml`` and ``memory.yaml``
    are a pure function of the pinned commits plus this repository's overlay, and
    the provider keys inside three of the env files have their one home in a
    component's own ``config/.env``. Comparing a derived copy to what it is
    derived from can only ever report that a copy is stale, and this path had no
    verb to end it: a component committing a new default, or an operator rotating
    an LLM key in the file it is typed into, stopped every operation on both
    Hosts with a message naming the drift. ``refresh_derived`` makes those copies
    follow their source before the comparison, so the gate that remains is about
    the eleven files where a difference means something.

    Off by default: a diagnosis must be able to report a stale copy without
    quietly ending it, and ``doctor`` reaches the surrounding checks.
    """

    target = target_directory(config)
    require_safe_input_directory(target)
    refreshed: dict[str, list[str]] = {}
    if refresh_derived:
        settings = refresh_derived_settings(target, config, read_exact_file)
        if settings:
            refreshed["settings"] = settings
        # Before the key-set check below, which refuses what this removes. Not
        # gated on ``verify_provider_sources``: a rendered field has nothing to
        # do with a component's own credentials, and the Mac path — which skips
        # that verification — holds the same input set.
        withdrawn = withdraw_rendered_fields(target)
        if withdrawn:
            refreshed["withdrawn"] = withdrawn
        if verify_provider_sources:
            credentials = refresh_provider_credentials(target, config)
            if credentials:
                refreshed["provider_credentials"] = credentials
    envs = {
        name: parse_provider_env(target / name)
        for name in (
            "data.env",
            "hub.env",
            "kernel.env",
            "admin.env",
            "local-api.env",
            "bootstrap.env",
            "agent.env",
            "channel.env",
            "memory.env",
            "livekit.env",
        )
    }
    # One rule for all ten files. ``channel.env`` used to need its own, written
    # out by hand right here, because the table above could not say "optional";
    # with a file that declares none, the bounds below collapse to the exact
    # equality the other nine always had.
    for name, keys in DECLARED_ENV_KEYS.items():
        present = set(envs[name])
        rendered = present & keys.rendered
        if rendered:
            # Named separately from "drifted", because it is a different
            # mistake with a different fix: the Host layer owns these fields,
            # and an input set holding one is a copy that can only go stale.
            # ``refresh_derived`` above withdraws them, so reaching this means
            # the caller asked not to.
            raise InstallInputError(
                "install input holds fields the Host layer renders: "
                f"{name}:{', '.join(sorted(rendered))}"
            )
        if not keys.required <= present or not present <= (keys.required | keys.optional):
            raise InstallInputError(f"install input env key set drifted: {name}")

    if verify_provider_sources:
        current_providers = {
            source_id: parse_provider_env(config.sources[source_id].path / "config/.env")
            for source_id in _EXTERNAL_KEYS
        }
        for source_id, keys in _EXTERNAL_KEYS.items():
            destination = envs[PROVIDER_DESTINATIONS[source_id]]
            for key in keys:
                current = current_providers[source_id].get(key)
                if not usable_secret(current, key=key):
                    raise InstallInputError(
                        "current Mac provider credential is missing or a placeholder: "
                        f"{source_id}:{key}"
                    )
                if destination.get(key) != current:
                    raise InstallInputError(
                        f"install input provider credential drifted from Mac: {source_id}:{key}"
                    )
        for key in _OPTIONAL_CHANNEL_KEYS:
            current = current_providers["eidolon_channel"].get(key)
            installed = envs["channel.env"].get(key)
            if usable_secret(current, key=key):
                if installed != current:
                    raise InstallInputError(
                        f"install input provider credential drifted from Mac: eidolon_channel:{key}"
                    )
            elif installed is not None:
                raise InstallInputError(
                    f"install input provider credential drifted from Mac: eidolon_channel:{key}"
                )

    data = envs["data.env"]
    admin = envs["admin.env"]
    for left_file, left_key, right_file, right_key, label in SHARED_CREDENTIALS:
        left = envs[left_file][left_key]
        right = envs[right_file][right_key]
        if left != right or len(left) < 24:
            raise InstallInputError(f"install input relationship drifted: {label}")
    fixed_values = {key: FIXED_ENV_VALUES[key] for key in _DATA_PATH_KEYS}
    if any(data[key] != value for key, value in fixed_values.items()):
        raise InstallInputError("Data authority paths drifted from the product contract")
    if admin["EIDOLON_ADMIN_SYSTEM_DIRECTORY_UDS"] != "/run/eidolon/system.sock":
        raise InstallInputError("Admin system directory path drifted from the product contract")

    for name, key in PROVIDER_ENV_KEYS:
        if not usable_secret(envs[name].get(key), key=key):
            raise InstallInputError(
                f"provider credential is missing or a placeholder: {name}:{key}"
            )
    identity = target / "host_identity.ed25519"
    if identity.stat().st_size != 32:
        raise InstallInputError("Host identity must contain exactly 32 raw Ed25519 private bytes")
    expected_settings = product_settings(config, read_exact_file)
    for name, expected in expected_settings.items():
        try:
            actual = (target / name).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise InstallInputError(f"product settings input is unreadable: {name}") from exc
        if actual != expected:
            raise InstallInputError(
                f"product settings input drifted from exact Git objects: {name}"
            )
    return {
        "status": "compatible",
        "files": sorted(INSTALL_DESTINATION_NAMES.values()),
        "contract": "pi-private-inputs-v1",
        # Named, so following a source is visible after the fact rather than
        # silent. Empty on the overwhelmingly common run where nothing moved.
        "refreshed": refreshed,
        "redaction": "input values and digests are not returned",
    }


#: Where this machine's identity lives, beside the input sets rather than
#: inside any one of them.
_IDENTITY_ANCHOR = "host_identity.ed25519"


def _host_identity(target: Path, *, new_identity: bool) -> tuple[bytes, str]:
    """The identity of the machine these inputs are for.

    An input set is a delivery, and a machine may be delivered to more than
    once. Its identity is not part of that delivery: host_id, hub_id and the
    Hub's hostname are all derived from this key, so minting a fresh one turns
    a reinstall into a different Host — one that every phone holding the old
    one keeps forever as an entry that can never answer. The person looking at
    that list owns one machine and is shown three.

    So the key is kept beside the input sets, adopted by each new one, and only
    replaced when someone says to. That is not a convenience: a new key is the
    statement "this is no longer the same Host", which is a thing a factory
    reset means and a reinstall does not.
    """

    anchor = target.parent / _IDENTITY_ANCHOR
    if anchor.is_file() and not new_identity:
        identity = anchor.read_bytes()
        if len(identity) != 32:
            raise InstallInputError(
                f"anchored Host identity is not 32 raw Ed25519 private bytes: {anchor}"
            )
        return identity, "adopted"
    return secrets.token_bytes(32), "minted"


def _anchor_host_identity(target: Path, identity: bytes) -> None:
    anchor = target.parent / _IDENTITY_ANCHOR
    if anchor.is_file() and anchor.read_bytes() == identity:
        return
    write_private_file(anchor, identity)


def target_directory(config: OperationsConfig) -> Path:
    if set(config.install_files) != set(INSTALL_FILE_NAMES):
        raise InstallInputError("install.files must contain the fixed input set")
    parents = {path.parent for path in config.install_files.values()}
    if len(parents) != 1:
        raise InstallInputError("init-inputs requires all install files in one directory")
    target = next(iter(parents))
    expected = {key: target / filename for key, filename in INSTALL_DESTINATION_NAMES.items()}
    if dict(config.install_files) != expected or target == Path("/"):
        raise InstallInputError("install.files must use the fixed filenames in one safe directory")
    return target




def _validate_existing(
    target: Path,
    config: OperationsConfig,
    read_exact_file: Callable[[str, str, str], str],
) -> dict[str, object]:
    actual = require_safe_input_directory(target)
    # Settings are derived from the pinned commits, not generated here: they
    # carry no secret and are simply what those commits say. Refreshing them is
    # safe, and not refreshing them would mean a component cannot change a
    # default without an operator reissuing every credential on the Host.
    refreshed = refresh_derived_settings(target, config, read_exact_file)
    return {
        "status": "already_initialized",
        "directory": str(target),
        "files": sorted(actual),
        "refreshed_settings": refreshed,
        "redaction": "existing credential values were not read or returned",
    }


