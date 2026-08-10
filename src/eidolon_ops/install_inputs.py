"""Fail-closed initialization of the 14 private first-install inputs."""

from __future__ import annotations

import os
import re
import secrets
import shutil
import stat
import tempfile
from collections.abc import Callable, Mapping
from pathlib import Path

from eidolon_ops.config import INSTALL_FILE_NAMES, OperationsConfig


class InstallInputError(ValueError):
    """Install inputs cannot be created without ambiguity or overwrite."""


INSTALL_DESTINATION_NAMES = {
    "data_env": "data.env",
    "hub_env": "hub.env",
    "kernel_env": "kernel.env",
    "admin_env": "admin.env",
    "local_api_env": "local-api.env",
    "bootstrap_env": "bootstrap.env",
    "host_identity": "host_identity.ed25519",
    "agent_env": "agent.env",
    "channel_env": "channel.env",
    "memory_env": "memory.env",
    "livekit_env": "livekit.env",
    "agent_settings": "agent.yaml",
    "channel_settings": "channel.yaml",
    "memory_settings": "memory.yaml",
}

_ENV_KEY = re.compile(r"^[A-Z][A-Z0-9_]*$")
_EXTERNAL_KEYS = {
    "eidolon_agent": ("EIDOLON_AGENT_LLM_API_KEY",),
    "eidolon_channel": (
        "OPENAI_LLM_API_KEY",
        "BAILIAN_STT_API_KEY",
        "BAILIAN_TTS_API_KEY",
    ),
    "eidolon_memory": ("EIDOLON_MEMORY_LLM_API_KEY",),
}
_OPTIONAL_CHANNEL_KEYS = ("SENSETIME_STT_API_KEY", "SENSETIME_TTS_API_KEY")


def initialize_install_inputs(
    config: OperationsConfig,
    read_exact_file: Callable[[str, str, str], str],
) -> dict[str, object]:
    """Create one private, internally consistent product input set."""

    target = _target_directory(config)
    if target.exists() or target.is_symlink():
        return _validate_existing(target)

    providers = {
        source_id: _parse_provider_env(config.sources[source_id].path / "config/.env")
        for source_id in _EXTERNAL_KEYS
    }
    missing = [
        f"{source_id}:{key}"
        for source_id, keys in _EXTERNAL_KEYS.items()
        for key in keys
        if not _usable_secret(providers[source_id].get(key), key=key)
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
        if _usable_secret(
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
    settings = _product_settings(config, read_exact_file)
    files: dict[str, bytes] = {
        **{name: _serialize_env(values) for name, values in env_documents.items()},
        "host_identity.ed25519": secrets.token_bytes(32),
        **{name: value.encode("utf-8") for name, value in settings.items()},
    }
    if set(files) != set(INSTALL_DESTINATION_NAMES.values()):
        raise InstallInputError("generated install input set is incomplete")
    _write_private_directory(target, files)
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
) -> dict[str, object]:
    """Re-prove identities, token relationships and exact product settings."""

    target = _target_directory(config)
    _validate_existing(target)
    envs = {
        name: _parse_provider_env(target / name)
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

    provider_destinations = {
        "eidolon_agent": "agent.env",
        "eidolon_channel": "channel.env",
        "eidolon_memory": "memory.env",
    }
    current_providers = {
        source_id: _parse_provider_env(config.sources[source_id].path / "config/.env")
        for source_id in _EXTERNAL_KEYS
    }
    for source_id, keys in _EXTERNAL_KEYS.items():
        destination = envs[provider_destinations[source_id]]
        for key in keys:
            current = current_providers[source_id].get(key)
            if not _usable_secret(current, key=key):
                raise InstallInputError(
                    f"current Mac provider credential is missing or a placeholder: {source_id}:{key}"
                )
            if destination.get(key) != current:
                raise InstallInputError(
                    f"install input provider credential drifted from Mac: {source_id}:{key}"
                )
    for key in _OPTIONAL_CHANNEL_KEYS:
        current = current_providers["eidolon_channel"].get(key)
        installed = envs["channel.env"].get(key)
        if _usable_secret(current, key=key):
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
        if not _usable_secret(envs[name].get(key), key=key):
            raise InstallInputError(
                f"provider credential is missing or a placeholder: {name}:{key}"
            )
    identity = target / "host_identity.ed25519"
    if identity.stat().st_size != 32:
        raise InstallInputError("Host identity must contain exactly 32 raw Ed25519 private bytes")
    expected_settings = _product_settings(config, read_exact_file)
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


def _parse_provider_env(path: Path) -> dict[str, str]:
    if not path.is_file() or path.is_symlink():
        raise InstallInputError(f"provider env source is missing or unsafe: {path}")
    values: dict[str, str] = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError) as exc:
        raise InstallInputError(f"provider env source is unreadable: {path}") from exc
    for line_number, raw in enumerate(lines, start=1):
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        key, separator, value = stripped.partition("=")
        if not separator or _ENV_KEY.fullmatch(key) is None or key in values:
            raise InstallInputError(f"provider env syntax is invalid: {path}:{line_number}")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        if any(character.isspace() or ord(character) < 32 for character in value):
            raise InstallInputError(f"provider env value has unsupported whitespace: {path}:{key}")
        values[key] = value
    return values


def _usable_secret(value: str | None, *, key: str) -> bool:
    if value is None or len(value) < 8:
        return False
    lowered = value.lower()
    return value != key and not lowered.startswith("your-") and "placeholder" not in lowered


def _serialize_env(values: Mapping[str, str]) -> bytes:
    for key, value in values.items():
        if (
            _ENV_KEY.fullmatch(key) is None
            or not value
            or any(character.isspace() or ord(character) < 32 for character in value)
        ):
            raise InstallInputError(f"generated env entry is unsafe: {key}")
    text = "".join(f"{key}={values[key]}\n" for key in sorted(values))
    return text.encode("utf-8")


def _product_settings(
    config: OperationsConfig,
    read_exact_file: Callable[[str, str, str], str],
) -> dict[str, str]:
    agent = read_exact_file(
        "eidolon_agent", config.sources["eidolon_agent"].revision, "config/settings.yaml"
    )
    agent = _replace(agent, "env: dev", "env: product", expected=1)
    agent = _replace(
        agent,
        "uds_path: ~/eidolon/run/eidolon-agent.sock",
        "uds_path: /run/eidolon/agent/eidolon-agent.sock",
        expected=1,
    )
    agent = _replace(agent, "~/eidolon/logs/agent", "/var/log/eidolon/agent", expected=2)
    agent = _replace(agent, "run_dir: ~/eidolon/run", "run_dir: /run/eidolon/agent", expected=1)
    agent = _replace(
        agent,
        "debug_dir: ~/eidolon/debug",
        "debug_dir: /var/cache/eidolon/agent/debug",
        expected=1,
    )
    agent = _replace(
        agent,
        "sqlite_path: ~/eidolon/data/eidolon-agent.sqlite3",
        "sqlite_path: /var/lib/eidolon/agent/eidolon-agent.sqlite3",
        expected=1,
    )
    agent = _replace(agent, "http://127.0.0.1:8030/mcp", "http://127.0.0.1:10030/mcp", expected=1)
    agent = _replace(
        agent,
        "discovery_token_env: ''",
        "discovery_token_env: EIDOLON_MEMORY_MCP_TOKEN",
        expected=1,
    )

    channel = read_exact_file(
        "eidolon_channel",
        config.sources["eidolon_channel"].revision,
        "config/settings.yaml",
    )
    channel = _replace(channel, "avatar:\n  enabled: true", "avatar:\n  enabled: false", expected=1)
    channel = _replace(
        channel, "root: ~/eidolon/voiceprints", "root: /var/lib/eidolon/voiceprints", expected=1
    )
    channel = _replace(
        channel,
        'timeline_debug_path: "~/eidolon/logs/channel/turn-timeline.jsonl"',
        'timeline_debug_path: "/var/log/eidolon/channel/turn-timeline.jsonl"',
        expected=1,
    )
    channel = _replace(
        channel,
        'dump_dir: "~/eidolon/debug"',
        'dump_dir: "/var/cache/eidolon/channel/debug"',
        expected=1,
    )

    memory = read_exact_file(
        "eidolon_memory", config.sources["eidolon_memory"].revision, "config/settings.yaml"
    )
    memory = _replace(
        memory,
        "palaces_root: ~/eidolon/memory/mempalaces",
        "palaces_root: /var/lib/eidolon/memory/mempalaces",
        expected=1,
    )
    memory = _replace(memory, "log_dir: ''", "log_dir: /var/log/eidolon/memory", expected=1)
    memory = _replace(memory, "run_dir: ''", "run_dir: /run/eidolon/memory", expected=1)
    memory = _replace(memory, "model: bge-large-zh", "model: bge-base-zh", expected=1)
    return {"agent.yaml": agent, "channel.yaml": channel, "memory.yaml": memory}


def _replace(value: str, old: str, new: str, *, expected: int) -> str:
    if value.count(old) != expected:
        raise InstallInputError(f"pinned settings template drifted at product overlay: {old}")
    return value.replace(old, new)


def _write_private_directory(target: Path, files: Mapping[str, bytes]) -> None:
    _ensure_private_parent(target.parent)
    temporary = Path(tempfile.mkdtemp(prefix=f".{target.name}.", dir=target.parent))
    os.chmod(temporary, 0o700)
    try:
        for name, value in files.items():
            path = temporary / name
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(value)
                stream.flush()
                os.fsync(stream.fileno())
        os.rename(temporary, target)
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)


def _ensure_private_parent(parent: Path) -> None:
    missing: list[Path] = []
    current = parent
    while not current.exists():
        missing.append(current)
        current = current.parent
    if current.is_symlink() or not current.is_dir():
        raise InstallInputError(f"install input parent is unsafe: {current}")
    for directory in reversed(missing):
        directory.mkdir(mode=0o700)
    if parent.is_symlink() or not parent.is_dir() or stat.S_IMODE(parent.stat().st_mode) & 0o077:
        raise InstallInputError(f"install input parent must be a private directory: {parent}")


def _validate_existing(target: Path) -> dict[str, object]:
    if target.is_symlink() or not target.is_dir() or stat.S_IMODE(target.stat().st_mode) != 0o700:
        raise InstallInputError(f"install input directory is unsafe: {target}")
    expected = set(INSTALL_DESTINATION_NAMES.values())
    actual = {path.name for path in target.iterdir()}
    if actual != expected:
        raise InstallInputError("existing install input directory is partial or has extra files")
    for path in target.iterdir():
        if path.is_symlink() or not path.is_file() or stat.S_IMODE(path.stat().st_mode) != 0o600:
            raise InstallInputError(f"existing install input is unsafe: {path.name}")
    return {
        "status": "already_initialized",
        "directory": str(target),
        "files": sorted(actual),
        "redaction": "existing credential values were not read or returned",
    }
