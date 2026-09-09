"""Create and prove the private input directory, without reading a value."""

from __future__ import annotations

import os
import shutil
import stat
import tempfile
from collections.abc import Callable, Mapping
from pathlib import Path

from eidolon_ops.config import OperationsConfig
from eidolon_ops.errors import InstallInputError
from eidolon_ops.hostagent.contract import OPTIONAL_INSTALL_INPUTS
from eidolon_ops.product_settings import product_settings
from eidolon_ops.provider_inputs import (
    EXTERNAL_KEYS,
    OPTIONAL_CHANNEL_KEYS,
    parse_provider_env,
    serialize_env,
    usable_secret,
)

#: The one filename each install input is written under.
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


def write_private_directory(target: Path, files: Mapping[str, bytes]) -> None:
    ensure_private_parent(target.parent)
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


def write_private_file(target: Path, value: bytes) -> None:
    """Place one private file, replacing any previous content atomically."""

    ensure_private_parent(target.parent)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{target.name}.", dir=target.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(value)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, target)
    finally:
        if temporary.exists():
            temporary.unlink()


def ensure_private_parent(parent: Path) -> None:
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


def require_safe_input_directory(target: Path) -> set[str]:
    """Prove the private input set is complete and private. Reads no value."""

    if target.is_symlink() or not target.is_dir() or stat.S_IMODE(target.stat().st_mode) != 0o700:
        raise InstallInputError(f"install input directory is unsafe: {target}")
    expected = set(INSTALL_DESTINATION_NAMES.values())
    actual = {path.name for path in target.iterdir()}
    if not expected <= actual or not actual <= expected | OPTIONAL_INSTALL_INPUTS:
        raise InstallInputError("existing install input directory is partial or has extra files")
    for path in target.iterdir():
        if path.is_symlink() or not path.is_file() or stat.S_IMODE(path.stat().st_mode) != 0o600:
            raise InstallInputError(f"existing install input is unsafe: {path.name}")
    return actual

def refresh_derived_settings(
    target: Path,
    config: OperationsConfig,
    read_exact_file: Callable[[str, str, str], str],
) -> list[str]:
    """Rewrite the settings that follow the pinned commits; never a credential."""

    refreshed: list[str] = []
    for name, value in product_settings(config, read_exact_file).items():
        path = target / name
        payload = value.encode("utf-8")
        if path.read_bytes() == payload:
            continue
        temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)
        refreshed.append(name)
    return refreshed


def refresh_provider_credentials(target: Path, config: OperationsConfig) -> list[str]:
    """Carry the operator's current provider keys into the input set.

    These keys have one author and one home: the operator types them into a
    component's ``config/.env``, and the input set holds a copy so a release can
    carry them to a Host. Both copies were required to be byte-equal, and no verb
    reconciled them — ``converge-inputs`` is additive by design and will not
    replace a value that is already there. So rotating a key in the file it is
    typed into stopped every operation on both Hosts, with a message naming the
    drift and no way to end it but editing a mode-0600 file by hand.

    A copy of a fact is not a second opinion about it. This makes the component's
    file the one place the value lives and the input set follow it, the same way
    the derived settings beside it follow the pinned commits. What stays refused
    is a value that is *missing* or a placeholder: nothing here can invent an LLM
    key, and a key that authenticates to nothing is worth stopping for.
    """

    refreshed: list[str] = []
    for source_id, keys in EXTERNAL_KEYS.items():
        current = parse_provider_env(config.sources[source_id].path / "config/.env")
        name = _PROVIDER_DESTINATIONS[source_id]
        installed = parse_provider_env(target / name)
        wanted = dict(installed)
        for key in keys:
            value = current.get(key)
            if not usable_secret(value, key=key):
                raise InstallInputError(
                    "current provider credential is missing or a placeholder: "
                    f"{source_id}:{key}"
                )
            wanted[key] = str(value)
        if source_id == "eidolon_channel":
            # Optional by contract: present when the operator has one, absent
            # when they do not, and the input set must say the same either way.
            for key in OPTIONAL_CHANNEL_KEYS:
                value = current.get(key)
                if usable_secret(value, key=key):
                    wanted[key] = str(value)
                else:
                    wanted.pop(key, None)
        if wanted != installed:
            write_private_file(target / name, serialize_env(wanted))
            refreshed.append(name)
    return refreshed


#: Which input file holds each component's provider keys.
_PROVIDER_DESTINATIONS = {
    "eidolon_agent": "agent.env",
    "eidolon_channel": "channel.env",
    "eidolon_memory": "memory.env",
}
