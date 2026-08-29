from __future__ import annotations

import stat
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from types import MappingProxyType

import pytest

from eidolon_ops.config import OperationsConfig
from eidolon_ops.install_inputs import (
    add_missing_install_credentials,
    INSTALL_DESTINATION_NAMES,
    InstallInputError,
    initialize_install_inputs,
    validate_install_input_contract,
)

pytestmark = pytest.mark.component


def _config_for_init(
    config: OperationsConfig, tmp_path: Path, *, directory: str = "install"
) -> OperationsConfig:
    target = tmp_path / "operator-private/pi5" / directory
    files = {key: target / name for key, name in INSTALL_DESTINATION_NAMES.items()}
    provider_values = {
        "eidolon_agent": {"EIDOLON_AGENT_LLM_API_KEY": '"agent-provider-key"'},
        "eidolon_channel": {
            "OPENAI_LLM_API_KEY": "channel-llm-key",
            "BAILIAN_STT_API_KEY": "channel-stt-key",
            "BAILIAN_TTS_API_KEY": "channel-tts-key",
            "SENSETIME_STT_API_KEY": "optional-stt-key",
        },
        "eidolon_memory": {"EIDOLON_MEMORY_LLM_API_KEY": "memory-provider-key"},
    }
    for source_id, values in provider_values.items():
        source = config.sources[source_id].path / "config/.env"
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_text(
            "# provider credentials\n\n"
            + "".join(f"{key}={value}\n" for key, value in values.items()),
            encoding="utf-8",
        )
    return replace(config, install_files=MappingProxyType(files))


def _settings_reader(_source_id: str, _revision: str, path: str) -> str:
    if path != "config/settings.yaml":
        raise AssertionError(path)
    if _source_id == "eidolon_agent":
        return """\
env: dev
log_level: DEBUG
uds_path: $EIDOLON_RUNTIME_ROOT/agent/eidolon-agent.sock
log_a: $EIDOLON_LOG_ROOT/agent
log_b: $EIDOLON_LOG_ROOT/agent
run_dir: $EIDOLON_RUNTIME_ROOT/agent
debug_dir: $EIDOLON_CACHE_ROOT/debug/agent
sqlite_path: $EIDOLON_STATE_ROOT/agent/eidolon-agent.sqlite3
mcp_url: http://127.0.0.1:8030/mcp
discovery_token_env: ''
models:
  - name: openai/deepseek-v4-flash
    thinking: disabled
"""
    if _source_id == "eidolon_channel":
        return """\
avatar:
  enabled: true
root: $EIDOLON_STATE_ROOT/voiceprints
timeline_debug_path: "$EIDOLON_LOG_ROOT/channel/turn-timeline.jsonl"
stt:
  dump_wav: true
  dump_dir: "$EIDOLON_CACHE_ROOT/debug/channel"
"""
    if _source_id == "eidolon_memory":
        return """\
palaces_root: ~/eidolon/memory/mempalaces
log_dir: ''
run_dir: ''
model: bge-large-zh
"""
    raise AssertionError(_source_id)


def _env(path: Path) -> dict[str, str]:
    return dict(line.split("=", 1) for line in path.read_text(encoding="utf-8").splitlines())


def _write_env(path: Path, values: dict[str, str]) -> None:
    path.write_text(
        "".join(f"{key}={values[key]}\n" for key in sorted(values)),
        encoding="utf-8",
    )
    path.chmod(0o600)


def test_initializer_creates_one_private_consistent_input_set(config, tmp_path: Path) -> None:
    configured = _config_for_init(config, tmp_path)

    result = initialize_install_inputs(configured, _settings_reader)

    target = next(iter(configured.install_files.values())).parent
    assert result["status"] == "initialized"
    assert set(path.name for path in target.iterdir()) == set(INSTALL_DESTINATION_NAMES.values())
    assert stat.S_IMODE(target.stat().st_mode) == 0o700
    assert all(stat.S_IMODE(path.stat().st_mode) == 0o600 for path in target.iterdir())
    assert (target / "host_identity.ed25519").stat().st_size == 32

    data = _env(target / "data.env")
    kernel = _env(target / "kernel.env")
    admin = _env(target / "admin.env")
    local_api = _env(target / "local-api.env")
    agent = _env(target / "agent.env")
    channel = _env(target / "channel.env")
    memory = _env(target / "memory.env")
    assert (
        memory["EIDOLON_DATA_MEMORY_RUNTIME_ROSTER_TOKEN"]
        == data["EIDOLON_DATA_MEMORY_RUNTIME_ROSTER_TOKEN"]
    )
    livekit = _env(target / "livekit.env")
    hub = _env(target / "hub.env")
    assert (
        kernel["EIDOLON_KERNEL_COMPANION_AUTHORITY_TOKEN"]
        == data["EIDOLON_DATA_COMPANION_AUTHORITY_TOKEN"]
    )
    assert (
        agent["EIDOLON_DATA_COMPANION_AUTHORITY_TOKEN"]
        == data["EIDOLON_DATA_COMPANION_AUTHORITY_TOKEN"]
    )
    assert (
        channel["EIDOLON_DATA_COMPANION_AUTHORITY_TOKEN"]
        == data["EIDOLON_DATA_COMPANION_AUTHORITY_TOKEN"]
    )
    assert (
        admin["EIDOLON_ADMIN_DATA_WORKSPACE_AUTHORITY_TOKEN"]
        == data["EIDOLON_DATA_WORKSPACE_AUTHORITY_TOKEN"]
    )
    assert (
        admin["EIDOLON_ADMIN_LOCAL_API_SERVICE_TOKEN"]
        == local_api["EIDOLON_LOCAL_API_ADMIN_SERVICE_TOKEN"]
    )
    assert (
        kernel["EIDOLON_KERNEL_HUB_MANAGEMENT_TOKEN"]
        == hub["EIDOLON_HUB_DEVICE_REGISTRY_READER_TOKEN"]
    )
    assert agent["PAIRING_JWT_SECRET"] == channel["PAIRING_JWT_SECRET"]
    assert agent["EIDOLON_AGENT_LLM_API_KEY"] == "agent-provider-key"
    assert agent["EIDOLON_MEMORY_MCP_TOKEN"] == memory["EIDOLON_MEMORY_MCP_TOKEN"]
    # The two Owner-facing authority surfaces that grew a credential. Each one is
    # shared by exactly two files, and a Host missing either has a management
    # surface that answers 503 rather than a management surface that is open.
    assert admin["EIDOLON_ADMIN_MEMORY_API_SERVICE_TOKEN"] == memory["EIDOLON_MEMORY_API_TOKEN"]
    assert admin["EIDOLON_AGENT_ADMIN_API_TOKEN"] == agent["EIDOLON_AGENT_ADMIN_API_TOKEN"]
    # Distinct secrets: memory's MCP tool surface and its HTTP authority surface
    # are different boundaries with different callers.
    assert memory["EIDOLON_MEMORY_API_TOKEN"] != memory["EIDOLON_MEMORY_MCP_TOKEN"]
    assert channel["LIVEKIT_API_KEY"] == livekit["LIVEKIT_API_KEY"]
    assert channel["LIVEKIT_API_SECRET"] == livekit["LIVEKIT_API_SECRET"]
    assert channel["OPENAI_LLM_API_KEY"] == "channel-llm-key"
    assert channel["SENSETIME_STT_API_KEY"] == "optional-stt-key"
    assert "SENSETIME_TTS_API_KEY" not in channel
    assert (target / "bootstrap.env").read_bytes() == b""
    agent_settings = (target / "agent.yaml").read_text(encoding="utf-8")
    assert "~/eidolon" not in agent_settings
    assert agent_settings.startswith("env: prod\n")
    # A product Host is not the machine the Agent is being debugged on: at DEBUG
    # this component logs every sqlite cursor close, which measured 4.5 GB/day
    # on an idle Host and lands on a Pi's SD card.
    assert "log_level: INFO" in agent_settings
    assert "DEBUG" not in agent_settings
    assert "uds_path: $EIDOLON_RUNTIME_ROOT/agent/eidolon-agent.sock" in agent_settings
    assert "sqlite_path: $EIDOLON_STATE_ROOT/agent/eidolon-agent.sqlite3" in agent_settings
    assert "thinking: disabled" in agent_settings
    channel_settings = (target / "channel.yaml").read_text(encoding="utf-8")
    assert "avatar:\n  enabled: false" in channel_settings
    assert "  dump_wav: false" in channel_settings
    assert "dump_wav: true" not in channel_settings
    # Memory's settings ship unmodified: the Host expresses its encoder through
    # the environment, so improving that file cannot break an install.
    memory_settings = (target / "memory.yaml").read_text(encoding="utf-8")
    assert memory_settings == _settings_reader("eidolon_memory", "", "config/settings.yaml")
    # The encoder is Host configuration, not a credential: it belongs in
    # host.env, where changing it does not mean reissuing every secret.
    assert "EIDOLON_MEMORY_EMBEDDING_MODEL" not in _env(target / "memory.env")

    # Credentials are never reissued, but the derived settings follow the pinned
    # commits: a component changing a default must not require an operator to
    # reissue every secret on the Host.
    repeated = initialize_install_inputs(configured, _settings_reader)
    assert repeated["status"] == "already_initialized"
    assert repeated["refreshed_settings"] == []

    (target / "memory.yaml").write_bytes(b"stale: true\n")
    refreshed = initialize_install_inputs(configured, _settings_reader)
    assert refreshed["refreshed_settings"] == ["memory.yaml"]
    assert (target / "memory.yaml").read_text(encoding="utf-8") == _settings_reader(
        "eidolon_memory", "", "config/settings.yaml"
    )
    assert stat.S_IMODE((target / "memory.yaml").stat().st_mode) == 0o600
    validated = validate_install_input_contract(configured, _settings_reader)
    assert validated["status"] == "compatible"
    assert validated["contract"] == "pi-private-inputs-v1"


def test_initializer_fails_before_writing_when_provider_key_is_missing(
    config, tmp_path: Path
) -> None:
    configured = _config_for_init(config, tmp_path)
    channel_source = configured.sources["eidolon_channel"].path / "config/.env"
    channel_source.write_text("OPENAI_LLM_API_KEY=only-one-key\n", encoding="utf-8")

    with pytest.raises(InstallInputError, match="BAILIAN_STT_API_KEY"):
        initialize_install_inputs(configured, _settings_reader)

    assert not next(iter(configured.install_files.values())).parent.exists()


def test_validator_rejects_required_provider_drift_from_mac(config, tmp_path: Path) -> None:
    configured = _config_for_init(config, tmp_path)
    initialize_install_inputs(configured, _settings_reader)
    source = configured.sources["eidolon_agent"].path / "config/.env"
    source.write_text("EIDOLON_AGENT_LLM_API_KEY=replaced-on-mac\n", encoding="utf-8")

    with pytest.raises(InstallInputError, match="credential drifted from Mac"):
        validate_install_input_contract(configured, _settings_reader)


def test_validator_rejects_optional_provider_drift_from_mac(config, tmp_path: Path) -> None:
    configured = _config_for_init(config, tmp_path)
    initialize_install_inputs(configured, _settings_reader)
    source = configured.sources["eidolon_channel"].path / "config/.env"
    source.write_text(
        "OPENAI_LLM_API_KEY=channel-llm-key\n"
        "BAILIAN_STT_API_KEY=channel-stt-key\n"
        "BAILIAN_TTS_API_KEY=channel-tts-key\n",
        encoding="utf-8",
    )

    with pytest.raises(InstallInputError, match="SENSETIME_STT_API_KEY"):
        validate_install_input_contract(configured, _settings_reader)


def test_initializer_fails_closed_on_exact_settings_drift(config, tmp_path: Path) -> None:
    configured = _config_for_init(config, tmp_path)

    with pytest.raises(InstallInputError, match="template drifted"):
        initialize_install_inputs(configured, lambda *_args: "unexpected")

    assert not next(iter(configured.install_files.values())).parent.exists()


def test_initializer_refuses_partial_existing_directory(config, tmp_path: Path) -> None:
    configured = _config_for_init(config, tmp_path)
    target = next(iter(configured.install_files.values())).parent
    target.mkdir(parents=True)
    target.chmod(0o700)
    (target / "data.env").write_text("partial", encoding="utf-8")

    with pytest.raises(InstallInputError, match="partial"):
        initialize_install_inputs(configured, _settings_reader)


def test_initializer_refuses_insecure_existing_directory(config, tmp_path: Path) -> None:
    configured = _config_for_init(config, tmp_path)
    target = next(iter(configured.install_files.values())).parent
    target.mkdir(parents=True)
    target.chmod(0o755)

    with pytest.raises(InstallInputError, match="directory is unsafe"):
        initialize_install_inputs(configured, _settings_reader)


def test_initializer_refuses_insecure_existing_file(config, tmp_path: Path) -> None:
    configured = _config_for_init(config, tmp_path)
    initialize_install_inputs(configured, _settings_reader)
    target = next(iter(configured.install_files.values())).parent
    (target / "agent.env").chmod(0o644)

    with pytest.raises(InstallInputError, match=r"agent\.env"):
        initialize_install_inputs(configured, _settings_reader)


@pytest.mark.parametrize(
    ("content", "message"),
    [
        ("NOT_AN_ENV_LINE\n", "syntax is invalid"),
        ("EIDOLON_AGENT_LLM_API_KEY=has whitespace\n", "unsupported whitespace"),
    ],
)
def test_initializer_rejects_ambiguous_provider_env(
    config, tmp_path: Path, content: str, message: str
) -> None:
    configured = _config_for_init(config, tmp_path)
    source = configured.sources["eidolon_agent"].path / "config/.env"
    source.write_text(content, encoding="utf-8")

    with pytest.raises(InstallInputError, match=message):
        initialize_install_inputs(configured, _settings_reader)


def test_initializer_refuses_wrong_fixed_filename(config, tmp_path: Path) -> None:
    configured = _config_for_init(config, tmp_path)
    files = dict(configured.install_files)
    files["data_env"] = files["data_env"].with_name("wrong.env")
    configured = replace(configured, install_files=MappingProxyType(files))

    with pytest.raises(InstallInputError, match="fixed filenames"):
        initialize_install_inputs(configured, _settings_reader)


def test_validator_rejects_cross_service_token_drift(config, tmp_path: Path) -> None:
    configured = _config_for_init(config, tmp_path)
    initialize_install_inputs(configured, _settings_reader)
    target = next(iter(configured.install_files.values())).parent
    kernel = _env(target / "kernel.env")
    kernel["EIDOLON_KERNEL_COMPANION_AUTHORITY_TOKEN"] = "different-token-value-long-enough"
    _write_env(target / "kernel.env", kernel)

    with pytest.raises(InstallInputError, match="Data/Kernel"):
        validate_install_input_contract(configured, _settings_reader)


def test_validator_rejects_identity_and_settings_drift(config, tmp_path: Path) -> None:
    configured = _config_for_init(config, tmp_path)
    initialize_install_inputs(configured, _settings_reader)
    target = next(iter(configured.install_files.values())).parent
    (target / "host_identity.ed25519").write_bytes(b"too-short")
    (target / "host_identity.ed25519").chmod(0o600)

    with pytest.raises(InstallInputError, match="32 raw Ed25519"):
        validate_install_input_contract(configured, _settings_reader)

    (target / "host_identity.ed25519").write_bytes(b"x" * 32)
    (target / "host_identity.ed25519").chmod(0o600)
    (target / "agent.yaml").write_text("drifted\n", encoding="utf-8")
    (target / "agent.yaml").chmod(0o600)
    with pytest.raises(InstallInputError, match="exact Git objects"):
        validate_install_input_contract(configured, _settings_reader)


@pytest.mark.parametrize(
    ("filename", "mutation", "message"),
    [
        ("data.env", ("EXTRA_KEY", "extra-value"), "key set drifted"),
        ("channel.env", ("BAILIAN_STT_API_KEY", None), "channel.env"),
        (
            "data.env",
            ("EIDOLON_DATA_SQLITE_PATH", "/tmp/wrong.sqlite3"),
            "Data authority paths",
        ),
        (
            "admin.env",
            ("EIDOLON_ADMIN_SYSTEM_DIRECTORY_UDS", "/tmp/wrong.sock"),
            "Admin system directory",
        ),
        (
            "agent.env",
            ("EIDOLON_AGENT_LLM_API_KEY", "EIDOLON_AGENT_LLM_API_KEY"),
            "provider credential",
        ),
        # The two credentials that make the Owner-facing authority surfaces
        # answer at all. A Host where either side drifts has a management page
        # that reports the wrong thing: 401 rather than "not configured".
        (
            "memory.env",
            ("EIDOLON_MEMORY_API_TOKEN", "drifted-memory-api-token-value"),
            "Admin/Memory API service token",
        ),
        (
            "agent.env",
            ("EIDOLON_AGENT_ADMIN_API_TOKEN", "drifted-agent-admin-token-value"),
            "Admin/Agent admin API token",
        ),
    ],
)
def test_validator_rejects_env_contract_drift(
    config,
    tmp_path: Path,
    filename: str,
    mutation: tuple[str, str | None],
    message: str,
) -> None:
    configured = _config_for_init(config, tmp_path)
    initialize_install_inputs(configured, _settings_reader)
    target = next(iter(configured.install_files.values())).parent
    values = _env(target / filename)
    key, value = mutation
    if value is None:
        values.pop(key)
    else:
        values[key] = value
    _write_env(target / filename, values)

    with pytest.raises(InstallInputError, match=message):
        validate_install_input_contract(configured, _settings_reader)


def test_initializer_refuses_install_files_spread_across_directories(
    config, tmp_path: Path
) -> None:
    configured = _config_for_init(config, tmp_path)
    files = dict(configured.install_files)
    files["data_env"] = tmp_path / "other/data.env"
    configured = replace(configured, install_files=MappingProxyType(files))

    with pytest.raises(InstallInputError, match="one directory"):
        initialize_install_inputs(configured, _settings_reader)


def test_installing_the_same_machine_again_keeps_the_host_it_already_is(
    config, tmp_path: Path
) -> None:
    """Reinstalling is not a new Host, and must not read as one.

    host_id, hub_id and the Hub's hostname all come from this key. Minting a
    fresh one every time an input set is created makes each reinstall a
    different Host, and every phone that claimed the old one keeps an entry
    that can never answer again — the person owns one machine and is shown
    several, with nothing in the product able to tell them apart.
    """

    first = initialize_install_inputs(_config_for_init(config, tmp_path), _settings_reader)
    identity = (tmp_path / "operator-private/pi5/install/host_identity.ed25519").read_bytes()

    again = initialize_install_inputs(
        _config_for_init(config, tmp_path, directory="install-v2"),
        _settings_reader,
    )
    adopted = (tmp_path / "operator-private/pi5/install-v2/host_identity.ed25519").read_bytes()

    assert first["host_identity"] == "minted"
    assert again["host_identity"] == "adopted"
    assert adopted == identity
    # Everything else is reissued: the identity is what the machine is, not
    # what any one delivery contains.
    assert _env(tmp_path / "operator-private/pi5/install/hub.env") != _env(
        tmp_path / "operator-private/pi5/install-v2/hub.env"
    )


def test_a_machine_can_be_retired_but_only_by_saying_so(config, tmp_path: Path) -> None:
    # A machine changing hands does mean a new Host. That is a decision, not
    # something a reinstall performs on its own.
    initialize_install_inputs(_config_for_init(config, tmp_path), _settings_reader)
    identity = (tmp_path / "operator-private/pi5/install/host_identity.ed25519").read_bytes()

    retired = initialize_install_inputs(
        _config_for_init(config, tmp_path, directory="install-v2"),
        _settings_reader,
        new_identity=True,
    )
    minted = (tmp_path / "operator-private/pi5/install-v2/host_identity.ed25519").read_bytes()

    assert retired["host_identity"] == "minted"
    assert minted != identity
    # And the machine keeps the new one from then on.
    following = initialize_install_inputs(
        _config_for_init(config, tmp_path, directory="inputs"),
        _settings_reader,
    )
    assert following["host_identity"] == "adopted"
    assert (tmp_path / "operator-private/pi5/inputs/host_identity.ed25519").read_bytes() == minted


# --- 产品长出一个凭据，已装好的主机怎么拿到 ---------------------------------


def _strip_keys(target: Path, removals: dict[str, set[str]]) -> dict[str, dict[str, str]]:
    """Make an input set look like one generated before a credential existed."""

    before: dict[str, dict[str, str]] = {}
    for name, keys in removals.items():
        values = _env(target / name)
        before[name] = dict(values)
        for key in keys:
            values.pop(key)
        _write_env(target / name, values)
    return before


#: What a Host installed before 2026-08-25 has: the memory authority credential
#: and the Agent admin credential simply did not exist.
_OLDER_HOST = {
    "memory.env": {"EIDOLON_MEMORY_API_TOKEN"},
    "admin.env": {
        "EIDOLON_ADMIN_MEMORY_API_SERVICE_TOKEN",
        "EIDOLON_AGENT_ADMIN_API_TOKEN",
    },
    "agent.env": {"EIDOLON_AGENT_ADMIN_API_TOKEN"},
}


def test_an_older_input_set_is_refused_by_the_contract_check(config, tmp_path: Path) -> None:
    """The premise. Without a repair this is a Host the check condemns and
    nothing can fix short of reissuing every credential on it."""

    configured = _config_for_init(config, tmp_path)
    initialize_install_inputs(configured, _settings_reader)
    target = next(iter(configured.install_files.values())).parent
    _strip_keys(target, _OLDER_HOST)

    with pytest.raises(InstallInputError, match="key set drifted"):
        validate_install_input_contract(configured, _settings_reader)


def test_the_repair_adds_only_what_is_missing_and_says_so_first(config, tmp_path: Path) -> None:
    """Dry by default: the operator running this holds a Host that works."""

    configured = _config_for_init(config, tmp_path)
    initialize_install_inputs(configured, _settings_reader)
    target = next(iter(configured.install_files.values())).parent
    before = _strip_keys(target, _OLDER_HOST)

    planned = add_missing_install_credentials(configured)

    assert planned["status"] == "planned"
    assert planned["applied"] is False
    assert planned["added"] == {
        "admin.env": [
            "EIDOLON_ADMIN_MEMORY_API_SERVICE_TOKEN",
            "EIDOLON_AGENT_ADMIN_API_TOKEN",
        ],
        "agent.env": ["EIDOLON_AGENT_ADMIN_API_TOKEN"],
        "memory.env": ["EIDOLON_MEMORY_API_TOKEN"],
    }
    # Nothing written, and no value in the report to leak.
    assert "EIDOLON_MEMORY_API_TOKEN" not in _env(target / "memory.env")
    assert str(planned["added"]).count("=") == 0
    assert (
        _env(target / "admin.env").keys() == before["admin.env"].keys() - _OLDER_HOST["admin.env"]
    )


def test_applying_the_repair_leaves_every_existing_secret_untouched(config, tmp_path: Path) -> None:
    """The property that makes it safe to run on a working Host."""

    configured = _config_for_init(config, tmp_path)
    initialize_install_inputs(configured, _settings_reader)
    target = next(iter(configured.install_files.values())).parent
    before = _strip_keys(target, _OLDER_HOST)

    add_missing_install_credentials(configured, apply=True)

    for name, previous in before.items():
        current = _env(target / name)
        for key, value in previous.items():
            if key in _OLDER_HOST[name]:
                continue
            assert current[key] == value, f"{name}:{key} was rewritten"


def test_the_repair_restores_the_relationships_the_check_requires(config, tmp_path: Path) -> None:
    """Minting one side of a shared secret is how a pair breaks, so a value the
    other side already has is copied rather than re-made."""

    configured = _config_for_init(config, tmp_path)
    initialize_install_inputs(configured, _settings_reader)
    target = next(iter(configured.install_files.values())).parent
    # Only one side of each pair is missing here, which is the ordinary case: a
    # Host whose admin.env was hand-patched but whose memory.env was not.
    _strip_keys(
        target,
        {
            "admin.env": {"EIDOLON_ADMIN_MEMORY_API_SERVICE_TOKEN"},
            "agent.env": {"EIDOLON_AGENT_ADMIN_API_TOKEN"},
        },
    )
    memory_before = _env(target / "memory.env")["EIDOLON_MEMORY_API_TOKEN"]
    admin_before = _env(target / "admin.env")["EIDOLON_AGENT_ADMIN_API_TOKEN"]

    add_missing_install_credentials(configured, apply=True)

    assert _env(target / "admin.env")["EIDOLON_ADMIN_MEMORY_API_SERVICE_TOKEN"] == memory_before
    assert _env(target / "agent.env")["EIDOLON_AGENT_ADMIN_API_TOKEN"] == admin_before
    # And the whole set passes the check it used to fail.
    validate_install_input_contract(configured, _settings_reader)


def test_a_credential_nobody_has_yet_is_minted_once_for_both_sides(config, tmp_path: Path) -> None:
    configured = _config_for_init(config, tmp_path)
    initialize_install_inputs(configured, _settings_reader)
    target = next(iter(configured.install_files.values())).parent
    _strip_keys(target, _OLDER_HOST)

    add_missing_install_credentials(configured, apply=True)

    memory = _env(target / "memory.env")
    admin = _env(target / "admin.env")
    agent = _env(target / "agent.env")
    assert admin["EIDOLON_ADMIN_MEMORY_API_SERVICE_TOKEN"] == memory["EIDOLON_MEMORY_API_TOKEN"]
    assert admin["EIDOLON_AGENT_ADMIN_API_TOKEN"] == agent["EIDOLON_AGENT_ADMIN_API_TOKEN"]
    assert len(memory["EIDOLON_MEMORY_API_TOKEN"]) >= 24
    validate_install_input_contract(configured, _settings_reader)


def test_running_it_again_changes_nothing(config, tmp_path: Path) -> None:
    """Idempotent, because an operator who is unsure will run it twice."""

    configured = _config_for_init(config, tmp_path)
    initialize_install_inputs(configured, _settings_reader)
    target = next(iter(configured.install_files.values())).parent
    _strip_keys(target, _OLDER_HOST)
    add_missing_install_credentials(configured, apply=True)
    after = {name: _env(target / name) for name in _OLDER_HOST}

    second = add_missing_install_credentials(configured, apply=True)

    assert second["added"] == {}
    assert second["applied"] is False
    assert {name: _env(target / name) for name in _OLDER_HOST} == after


def test_a_missing_provider_credential_is_refused_rather_than_invented(
    config, tmp_path: Path
) -> None:
    """An LLM key this process made up would authenticate to nothing, and the
    Host would then fail somewhere far from here."""

    configured = _config_for_init(config, tmp_path)
    initialize_install_inputs(configured, _settings_reader)
    target = next(iter(configured.install_files.values())).parent
    _strip_keys(target, {"memory.env": {"EIDOLON_MEMORY_LLM_API_KEY"}})

    with pytest.raises(InstallInputError, match="provider credentials"):
        add_missing_install_credentials(configured, apply=True)


# --- 派生的副本跟随它的来源，而不是被拿来跟它比对 -----------------------------


def _drifting_reader(debug_dir: str) -> Callable[[str, str, str], str]:
    """The Agent template as a later commit carries it.

    A default the product overlay does not touch, because the point is a
    component changing its own mind — not this repository changing the overlay.
    """

    def read(source_id: str, revision: str, path: str) -> str:
        text = _settings_reader(source_id, revision, path)
        if source_id == "eidolon_agent":
            return text.replace("debug_dir: $EIDOLON_CACHE_ROOT/debug/agent", debug_dir)
        return text

    return read


def test_a_component_changing_a_default_does_not_stop_every_operation(
    config, tmp_path: Path
) -> None:
    """The three settings inputs are derived, so they follow the pinned commits.

    They were materialized here and then required to be byte-equal to what they
    are derived from, with no verb on the shipping path to reconcile the two. So
    a component committing a new default in its own ``config/settings.yaml``
    stopped every operation on both Hosts with `product settings input drifted
    from exact Git objects`, and the only way out was a separate command. Now
    the copy follows, and the report says it moved.
    """

    configured = _config_for_init(config, tmp_path)
    initialize_install_inputs(configured, _settings_reader)
    target = next(iter(configured.install_files.values())).parent

    moved = _drifting_reader("debug_dir: $EIDOLON_CACHE_ROOT/agent-debug")

    # Without the flag it is still a refusal: a diagnosis must be able to report
    # a stale copy rather than quietly end it.
    with pytest.raises(InstallInputError, match="product settings input drifted"):
        validate_install_input_contract(configured, moved)

    report = validate_install_input_contract(configured, moved, refresh_derived=True)

    assert report["status"] == "compatible"
    assert report["refreshed"] == {"settings": ["agent.yaml"]}
    assert "$EIDOLON_CACHE_ROOT/agent-debug" in (target / "agent.yaml").read_text(encoding="utf-8")
    # And a second run has nothing to say, because nothing moved.
    assert (
        validate_install_input_contract(configured, moved, refresh_derived=True)["refreshed"] == {}
    )


def test_rotating_a_provider_key_where_it_is_typed_is_enough(config, tmp_path: Path) -> None:
    """An LLM key has one home: the component's own ``config/.env``.

    The input set holds a copy so a release can carry it to a Host. Both were
    required to be byte-equal and nothing reconciled them — ``converge-inputs``
    is additive by design and will not replace a value that is already there —
    so rotating the key in the file it is typed into stopped every operation
    with no way out but hand-editing a mode-0600 file.
    """

    configured = _config_for_init(config, tmp_path)
    initialize_install_inputs(configured, _settings_reader)
    target = next(iter(configured.install_files.values())).parent
    rotated = configured.sources["eidolon_agent"].path / "config/.env"
    rotated.write_text('EIDOLON_AGENT_LLM_API_KEY="rotated-agent-key"\n', encoding="utf-8")

    with pytest.raises(InstallInputError, match="provider credential drifted"):
        validate_install_input_contract(configured, _settings_reader)

    report = validate_install_input_contract(configured, _settings_reader, refresh_derived=True)

    assert report["refreshed"] == {"provider_credentials": ["agent.env"]}
    assert _env(target / "agent.env")["EIDOLON_AGENT_LLM_API_KEY"] == "rotated-agent-key"
    # The internal secrets in the same file are untouched: only the keys whose
    # home is the component's file follow it.
    assert (
        _env(target / "agent.env")["EIDOLON_MEMORY_MCP_TOKEN"]
        == _env(target / "memory.env")["EIDOLON_MEMORY_MCP_TOKEN"]
    )


def test_a_provider_key_that_is_gone_is_still_a_refusal(config, tmp_path: Path) -> None:
    """Following a source is not the same as inventing one.

    Nothing here can mint an LLM key, and a key that authenticates to nothing is
    worth stopping for. Only a *changed* value follows.
    """

    configured = _config_for_init(config, tmp_path)
    initialize_install_inputs(configured, _settings_reader)
    emptied = configured.sources["eidolon_agent"].path / "config/.env"
    emptied.write_text('EIDOLON_AGENT_LLM_API_KEY="your-key-here"\n', encoding="utf-8")

    with pytest.raises(InstallInputError, match="missing or a placeholder"):
        validate_install_input_contract(configured, _settings_reader, refresh_derived=True)
