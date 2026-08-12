"""Fail-closed initialization of the 14 private first-install inputs."""

from __future__ import annotations

import secrets
from collections.abc import Callable, Mapping
from pathlib import Path

from eidolon_ops.config import INSTALL_FILE_NAMES, OperationsConfig
from eidolon_ops.errors import InstallInputError
from eidolon_ops.private_inputs import (
    INSTALL_DESTINATION_NAMES,
    refresh_derived_settings,
    require_safe_input_directory,
    write_private_directory,
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
    "initialize_install_inputs",
    "validate_install_input_contract",
]


def initialize_install_inputs(
    config: OperationsConfig,
    read_exact_file: Callable[[str, str, str], str],
) -> dict[str, object]:
    """Create one private, internally consistent product input set."""

    target = _target_directory(config)
    if target.exists() or target.is_symlink():
        return _validate_existing(target, config, read_exact_file)

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
            "EIDOLON_ADMIN_SYSTEM_DIRECTORY_UDS": "/run/eidolon/system.sock",
        },
        "local-api.env": {
            "EIDOLON_LOCAL_API_ADMIN_BASE_URL": "http://127.0.0.1:9000",
            "EIDOLON_LOCAL_API_ADMIN_SERVICE_TOKEN": local_api_token,
        },
        "bootstrap.env": {},
        "agent.env": {
            "EIDOLON_AGENT_LLM_API_KEY": providers["eidolon_agent"]["EIDOLON_AGENT_LLM_API_KEY"],
            "EIDOLON_DATA_COMPANION_AUTHORITY_TOKEN": data_token,
            "EIDOLON_MEMORY_MCP_TOKEN": memory_token,
            "PAIRING_JWT_SECRET": pairing_token,
        },
        "channel.env": {
            **channel_external,
            "LIVEKIT_API_KEY": livekit_key,
            "LIVEKIT_API_SECRET": livekit_secret,
            "EIDOLON_CHANNEL_PROVIDER_TOKEN": hub_provider_token,
            "EIDOLON_LIVEKIT_CLIENT_URL": "ws://127.0.0.1:7880",
            "PAIRING_JWT_SECRET": pairing_token,
            "EIDOLON_DATA_COMPANION_AUTHORITY_TOKEN": data_token,
        },
        "memory.env": {
            "EIDOLON_DATA_MEMORY_RUNTIME_ROSTER_TOKEN": memory_roster_token,
            "EIDOLON_MEMORY_LLM_API_KEY": providers["eidolon_memory"]["EIDOLON_MEMORY_LLM_API_KEY"],
            "EIDOLON_MEMORY_MCP_TOKEN": memory_token,
        },
        "livekit.env": {
            "LIVEKIT_API_KEY": livekit_key,
            "LIVEKIT_API_SECRET": livekit_secret,
        },
    }
    settings = product_settings(config, read_exact_file)
    files: dict[str, bytes] = {
        **{name: serialize_env(values) for name, values in env_documents.items()},
        "host_identity.ed25519": secrets.token_bytes(32),
        **{name: value.encode("utf-8") for name, value in settings.items()},
    }
    if set(files) != set(INSTALL_DESTINATION_NAMES.values()):
        raise InstallInputError("generated install input set is incomplete")
    write_private_directory(target, files)
    return {
        "status": "initialized",
        "directory": str(target),
        "files": sorted(files),
        "provider_sources": {
            source_id: str(config.sources[source_id].path / "config/.env")
            for source_id in _EXTERNAL_KEYS
        },
        "redaction": "credential values and generated key material are never returned",
    }


def validate_install_input_contract(
    config: OperationsConfig,
    read_exact_file: Callable[[str, str, str], str],
    *,
    verify_provider_sources: bool = True,
) -> dict[str, object]:
    """Re-prove identities, token relationships and exact product settings."""

    target = _target_directory(config)
    require_safe_input_directory(target)
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
    exact_keys = {
        "data.env": {
            "EIDOLON_DATA_COMPANION_AUTHORITY_TOKEN",
            "EIDOLON_DATA_MEMORY_RUNTIME_ROSTER_TOKEN",
            "EIDOLON_DATA_WORKSPACE_AUTHORITY_TOKEN",
            "EIDOLON_DATA_SQLITE_PATH",
            "EIDOLON_DATA_DATABASE_URL",
            "EIDOLON_DATA_OBJECT_STORE_PATH",
        },
        "hub.env": {
            "EIDOLON_HUB_MANAGEMENT_JWT_SECRET",
            "EIDOLON_HUB_DEVICE_REGISTRY_READER_TOKEN",
            "EIDOLON_HUB_CHANNEL_PROVIDER_TOKEN",
        },
        "kernel.env": {
            "EIDOLON_KERNEL_HUB_MANAGEMENT_TOKEN",
            "EIDOLON_KERNEL_COMPANION_AUTHORITY_TOKEN",
        },
        "admin.env": {
            "EIDOLON_ADMIN_DATA_AUTHORITY_TOKEN",
            "EIDOLON_ADMIN_DATA_WORKSPACE_AUTHORITY_TOKEN",
            "EIDOLON_ADMIN_HUB_MANAGEMENT_JWT_SECRET",
            "EIDOLON_ADMIN_LOCAL_API_SERVICE_TOKEN",
            "EIDOLON_ADMIN_SYSTEM_DIRECTORY_UDS",
        },
        "local-api.env": {
            "EIDOLON_LOCAL_API_ADMIN_BASE_URL",
            "EIDOLON_LOCAL_API_ADMIN_SERVICE_TOKEN",
        },
        "bootstrap.env": set(),
        "agent.env": {
            "EIDOLON_AGENT_LLM_API_KEY",
            "EIDOLON_DATA_COMPANION_AUTHORITY_TOKEN",
            "EIDOLON_MEMORY_MCP_TOKEN",
            "PAIRING_JWT_SECRET",
        },
        "memory.env": {
            "EIDOLON_DATA_MEMORY_RUNTIME_ROSTER_TOKEN",
            "EIDOLON_MEMORY_LLM_API_KEY",
            "EIDOLON_MEMORY_MCP_TOKEN",
        },
        "livekit.env": {"LIVEKIT_API_KEY", "LIVEKIT_API_SECRET"},
    }
    channel_required = {
        "OPENAI_LLM_API_KEY",
        "BAILIAN_STT_API_KEY",
        "BAILIAN_TTS_API_KEY",
        "LIVEKIT_API_KEY",
        "LIVEKIT_API_SECRET",
        "EIDOLON_CHANNEL_PROVIDER_TOKEN",
        "EIDOLON_LIVEKIT_CLIENT_URL",
        "PAIRING_JWT_SECRET",
        "EIDOLON_DATA_COMPANION_AUTHORITY_TOKEN",
    }
    for name, keys in exact_keys.items():
        if set(envs[name]) != keys:
            raise InstallInputError(f"install input env key set drifted: {name}")
    channel_keys = set(envs["channel.env"])
    if not channel_required <= channel_keys or not channel_keys <= (
        channel_required | set(_OPTIONAL_CHANNEL_KEYS)
    ):
        raise InstallInputError("install input env key set drifted: channel.env")

    if verify_provider_sources:
        provider_destinations = {
            "eidolon_agent": "agent.env",
            "eidolon_channel": "channel.env",
            "eidolon_memory": "memory.env",
        }
        current_providers = {
            source_id: parse_provider_env(config.sources[source_id].path / "config/.env")
            for source_id in _EXTERNAL_KEYS
        }
        for source_id, keys in _EXTERNAL_KEYS.items():
            destination = envs[provider_destinations[source_id]]
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
    hub = envs["hub.env"]
    kernel = envs["kernel.env"]
    admin = envs["admin.env"]
    local_api = envs["local-api.env"]
    agent = envs["agent.env"]
    channel = envs["channel.env"]
    memory = envs["memory.env"]
    livekit = envs["livekit.env"]
    relationships = (
        (
            data["EIDOLON_DATA_MEMORY_RUNTIME_ROSTER_TOKEN"],
            memory["EIDOLON_DATA_MEMORY_RUNTIME_ROSTER_TOKEN"],
            "Data/Memory runtime roster token",
        ),
        (
            data["EIDOLON_DATA_COMPANION_AUTHORITY_TOKEN"],
            kernel["EIDOLON_KERNEL_COMPANION_AUTHORITY_TOKEN"],
            "Data/Kernel companion authority token",
        ),
        (
            data["EIDOLON_DATA_COMPANION_AUTHORITY_TOKEN"],
            admin["EIDOLON_ADMIN_DATA_AUTHORITY_TOKEN"],
            "Data/Admin authority token",
        ),
        (
            data["EIDOLON_DATA_COMPANION_AUTHORITY_TOKEN"],
            agent["EIDOLON_DATA_COMPANION_AUTHORITY_TOKEN"],
            "Data/Agent companion authority token",
        ),
        (
            data["EIDOLON_DATA_COMPANION_AUTHORITY_TOKEN"],
            channel["EIDOLON_DATA_COMPANION_AUTHORITY_TOKEN"],
            "Data/Channel companion authority token",
        ),
        (
            data["EIDOLON_DATA_WORKSPACE_AUTHORITY_TOKEN"],
            admin["EIDOLON_ADMIN_DATA_WORKSPACE_AUTHORITY_TOKEN"],
            "Data/Admin Workspace authority token",
        ),
        (
            hub["EIDOLON_HUB_DEVICE_REGISTRY_READER_TOKEN"],
            kernel["EIDOLON_KERNEL_HUB_MANAGEMENT_TOKEN"],
            "Hub/Kernel management token",
        ),
        (
            hub["EIDOLON_HUB_MANAGEMENT_JWT_SECRET"],
            admin["EIDOLON_ADMIN_HUB_MANAGEMENT_JWT_SECRET"],
            "Hub/Admin management JWT secret",
        ),
        (
            hub["EIDOLON_HUB_CHANNEL_PROVIDER_TOKEN"],
            channel["EIDOLON_CHANNEL_PROVIDER_TOKEN"],
            "Hub/Channel Provider token",
        ),
        (
            admin["EIDOLON_ADMIN_LOCAL_API_SERVICE_TOKEN"],
            local_api["EIDOLON_LOCAL_API_ADMIN_SERVICE_TOKEN"],
            "Admin/Local API service token",
        ),
        (agent["PAIRING_JWT_SECRET"], channel["PAIRING_JWT_SECRET"], "Agent/Channel JWT"),
        (
            agent["EIDOLON_MEMORY_MCP_TOKEN"],
            memory["EIDOLON_MEMORY_MCP_TOKEN"],
            "Agent/Memory MCP token",
        ),
        (channel["LIVEKIT_API_KEY"], livekit["LIVEKIT_API_KEY"], "Channel/LiveKit key"),
        (
            channel["LIVEKIT_API_SECRET"],
            livekit["LIVEKIT_API_SECRET"],
            "Channel/LiveKit secret",
        ),
    )
    for left, right, label in relationships:
        if left != right or len(left) < 24:
            raise InstallInputError(f"install input relationship drifted: {label}")
    fixed_values = {
        "EIDOLON_DATA_SQLITE_PATH": "/var/lib/eidolon/eidolon-system.sqlite3",
        "EIDOLON_DATA_DATABASE_URL": (
            "sqlite+aiosqlite:////var/lib/eidolon/eidolon-system.sqlite3"
        ),
        "EIDOLON_DATA_OBJECT_STORE_PATH": "/var/lib/eidolon/objects",
    }
    if any(data[key] != value for key, value in fixed_values.items()):
        raise InstallInputError("Data authority paths drifted from the product contract")
    if admin["EIDOLON_ADMIN_SYSTEM_DIRECTORY_UDS"] != "/run/eidolon/system.sock":
        raise InstallInputError("Admin system directory path drifted from the product contract")
    if channel["EIDOLON_LIVEKIT_CLIENT_URL"] != "ws://127.0.0.1:7880":
        raise InstallInputError("LiveKit client origin drifted from the backend-only contract")

    for name, key in (
        ("agent.env", "EIDOLON_AGENT_LLM_API_KEY"),
        ("channel.env", "OPENAI_LLM_API_KEY"),
        ("channel.env", "BAILIAN_STT_API_KEY"),
        ("channel.env", "BAILIAN_TTS_API_KEY"),
        ("memory.env", "EIDOLON_MEMORY_LLM_API_KEY"),
    ):
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
        "redaction": "input values and digests are not returned",
    }


def _target_directory(config: OperationsConfig) -> Path:
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


