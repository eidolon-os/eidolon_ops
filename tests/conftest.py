from __future__ import annotations

from pathlib import Path

import pytest

from eidolon_ops.config import SOURCE_IDS, load_config


@pytest.fixture
def config_path(tmp_path: Path) -> Path:
    identity = tmp_path / "id_ed25519"
    identity.write_text("private-test-placeholder", encoding="utf-8")
    identity.chmod(0o600)
    known_hosts = tmp_path / "known_hosts"
    known_hosts.write_text("host ssh-ed25519 AAAATEST\n", encoding="utf-8")
    known_hosts.chmod(0o644)
    install_dir = tmp_path / "private"
    install_dir.mkdir()
    install_dir.chmod(0o700)
    install_names = {
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
    for name in install_names.values():
        path = install_dir / name
        path.write_text(f"test-{name}\n", encoding="utf-8")
        path.chmod(0o600)
    revisions: dict[str, str] = {}
    for index, source_id in enumerate(SOURCE_IDS, start=1):
        repository = tmp_path / source_id
        repository.mkdir()
        (repository / ".git").mkdir()
        revisions[source_id] = f"{index:040x}"
    release_cli = tmp_path / "eidolon_kernel/.venv/bin/eidolon-release"
    release_cli.parent.mkdir(parents=True)
    release_cli.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    release_cli.chmod(0o755)
    path = tmp_path / "eidolon-pi.toml"
    path.write_text(
        f"""\
schema_version = 1

[foundation]
profile = "raspberry-pi-os-debian-arm64-v2"

[host]
user = "pi"
hostname = "pi.example"
port = 2222
identity_file = "{identity}"
known_hosts_file = "{known_hosts}"
connect_timeout_seconds = 7
remote_uv = "/usr/local/bin/uv"

[workspace]
bundle_root = "{tmp_path / "bundles"}"
release_cli = "{release_cli}"

{_source_tables(tmp_path, revisions)}
[services]
units = [
  "eidolon-bootstrapd.service",
  "eidolond.service",
  "eidolon-data.service",
  "eidolon-data-workspace.service",
  "eidolon-hub.service",
  "eidolon-kernel.service",
  "eidolon-local-api.service",
  "eidolon-admin.service",
  "eidolon-nats.service",
  "eidolon-livekit.service",
  "eidolon-memory-supervisor.service",
  "eidolon-memory-discovery.service",
  "eidolon-agent.service",
  "eidolon-channel-provider.service",
  "eidolon-channel.service",
]

[data]
system_database = "/var/lib/eidolon/eidolon-system.sqlite3"
object_store = "/var/lib/eidolon/objects"
bootstrap_database = "/var/lib/eidolon-bootstrap/bootstrap.sqlite3"
deployment_evidence = "/var/lib/eidolon/deployments"

[install.files]
data_env = "{install_dir / "data.env"}"
hub_env = "{install_dir / "hub.env"}"
kernel_env = "{install_dir / "kernel.env"}"
admin_env = "{install_dir / "admin.env"}"
local_api_env = "{install_dir / "local-api.env"}"
bootstrap_env = "{install_dir / "bootstrap.env"}"
host_identity = "{install_dir / "host_identity.ed25519"}"
agent_env = "{install_dir / "agent.env"}"
channel_env = "{install_dir / "channel.env"}"
memory_env = "{install_dir / "memory.env"}"
livekit_env = "{install_dir / "livekit.env"}"
agent_settings = "{install_dir / "agent.yaml"}"
channel_settings = "{install_dir / "channel.yaml"}"
memory_settings = "{install_dir / "memory.yaml"}"
""",
        encoding="utf-8",
    )
    return path


@pytest.fixture
def config(config_path: Path):
    return load_config(config_path)


def _source_tables(root: Path, revisions: dict[str, str]) -> str:
    return "".join(
        f'[sources.{source_id}]\npath = "{root / source_id}"\nrevision = "{revision}"\n\n'
        for source_id, revision in revisions.items()
    )
