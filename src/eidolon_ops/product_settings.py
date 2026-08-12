"""Instantiate the pinned component settings for a product Host.

Each substitution is matched an exact number of times: a component that changes
the line being matched breaks the build here, where it is visible, rather than
shipping a Host pointed at a workstation's home directory.
"""

from __future__ import annotations

from collections.abc import Callable

from eidolon_ops.config import OperationsConfig
from eidolon_ops.errors import InstallInputError


def product_settings(
    config: OperationsConfig,
    read_exact_file: Callable[[str, str, str], str],
) -> dict[str, str]:
    agent = read_exact_file(
        "eidolon_agent", config.sources["eidolon_agent"].revision, "config/settings.yaml"
    )
    agent = _replace(agent, "env: dev", "env: prod", expected=1)
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

    # Memory needs no overlay: its settings resolve paths from the Host path
    # contract this deployer already exports, and the Host's encoder is chosen
    # through EIDOLON_MEMORY_EMBEDDING_MODEL in memory.env. Rewriting a
    # component's shipped settings by string substitution breaks the moment
    # that component improves the line being matched.
    memory = read_exact_file(
        "eidolon_memory", config.sources["eidolon_memory"].revision, "config/settings.yaml"
    )
    return {"agent.yaml": agent, "channel.yaml": channel, "memory.yaml": memory}


def _replace(value: str, old: str, new: str, *, expected: int) -> str:
    if value.count(old) != expected:
        raise InstallInputError(f"pinned settings template drifted at product overlay: {old}")
    return value.replace(old, new)
