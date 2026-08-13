from __future__ import annotations

import stat
from dataclasses import replace
from pathlib import Path
from types import MappingProxyType

import pytest

from eidolon_ops.config import OperationsConfig
from eidolon_ops.install_inputs import (
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
uds_path: ~/eidolon/run/eidolon-agent.sock
log_a: ~/eidolon/logs/agent
log_b: ~/eidolon/logs/agent
run_dir: ~/eidolon/run
debug_dir: ~/eidolon/debug
sqlite_path: ~/eidolon/data/eidolon-agent.sqlite3
mcp_url: http://127.0.0.1:8030/mcp
discovery_token_env: ''
"""
    if _source_id == "eidolon_channel":
        return """\
avatar:
  enabled: true
root: ~/eidolon/voiceprints
timeline_debug_path: "~/eidolon/logs/channel/turn-timeline.jsonl"
dump_dir: "~/eidolon/debug"
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
    assert channel["LIVEKIT_API_KEY"] == livekit["LIVEKIT_API_KEY"]
    assert channel["LIVEKIT_API_SECRET"] == livekit["LIVEKIT_API_SECRET"]
    assert channel["OPENAI_LLM_API_KEY"] == "channel-llm-key"
    assert channel["SENSETIME_STT_API_KEY"] == "optional-stt-key"
    assert "SENSETIME_TTS_API_KEY" not in channel
    assert (target / "bootstrap.env").read_bytes() == b""
    assert "~/eidolon" not in (target / "agent.yaml").read_text(encoding="utf-8")
    assert (target / "agent.yaml").read_text(encoding="utf-8").startswith("env: prod\n")
    assert "avatar:\n  enabled: false" in (target / "channel.yaml").read_text(encoding="utf-8")
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
        _config_for_init(config, tmp_path, directory="install-v3"),
        _settings_reader,
    )
    assert following["host_identity"] == "adopted"
    assert (
        tmp_path / "operator-private/pi5/install-v3/host_identity.ed25519"
    ).read_bytes() == minted
