from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
CREDENTIALS = ROOT / "deploy/supervisor/wrappers/livekit-credentials.sh"
LIVEKIT_WRAPPER = ROOT / "deploy/supervisor/wrappers/livekit-dev.sh"
WITH_ENV = ROOT / "deploy/supervisor/wrappers/with-env.sh"


def _clean_environment(**values: str) -> dict[str, str]:
    environment = os.environ.copy()
    environment.pop("LIVEKIT_API_KEY", None)
    environment.pop("LIVEKIT_API_SECRET", None)
    environment.update(values)
    return environment


def _generate_credentials(state_root: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            "/bin/bash",
            "-c",
            'source "$1"; eidolon_ensure_livekit_credentials && printf ready',
            "bash",
            str(CREDENTIALS),
        ],
        check=False,
        capture_output=True,
        text=True,
        env=_clean_environment(EIDOLON_STATE_ROOT=str(state_root)),
    )


def test_livekit_credentials_are_generated_once_with_private_modes(tmp_path: Path) -> None:
    state = tmp_path / "state"

    first = _generate_credentials(state)
    credentials = state / "livekit/credentials.env"
    first_bytes = credentials.read_bytes()
    second = _generate_credentials(state)

    assert first.returncode == second.returncode == 0
    assert first.stdout == second.stdout == "ready"
    assert first.stderr == second.stderr == ""
    assert credentials.read_bytes() == first_bytes
    assert stat.S_IMODE(credentials.stat().st_mode) == 0o600
    assert stat.S_IMODE(credentials.parent.stat().st_mode) == 0o700
    assert first_bytes.count(b"LIVEKIT_API_KEY=") == 1
    assert first_bytes.count(b"LIVEKIT_API_SECRET=") == 1


@pytest.mark.parametrize(
    "content",
    [
        "LIVEKIT_API_KEY=only_one_value\n",
        "LIVEKIT_API_KEY=abcdefghijklmnop\nLIVEKIT_API_KEY=abcdefghijklmnop\n"
        "LIVEKIT_API_SECRET=abcdefghijklmnopqrstuvwxyz_123456\n",
        "UNREVIEWED_KEY=value\n",
    ],
)
def test_livekit_credentials_reject_malformed_persistent_input(
    tmp_path: Path, content: str
) -> None:
    credentials = tmp_path / "state/livekit/credentials.env"
    credentials.parent.mkdir(parents=True)
    credentials.write_text(content, encoding="utf-8")

    result = _generate_credentials(tmp_path / "state")

    assert result.returncode != 0
    assert result.stdout == ""
    assert "abcdefghijklmnop" not in result.stderr


@pytest.mark.parametrize("override", ["", "192.0.2.5"])
def test_livekit_runtime_config_is_generated_without_source_credentials(
    tmp_path: Path, override: str,
) -> None:
    template = ROOT / "src/eidolon_ops/assets/livekit.yaml"
    generated = tmp_path / "livekit.generated.yaml"
    api_key = "test_runtime_key_1234"
    api_secret = "test_runtime_secret_abcdefghijklmnopqrstuvwxyz_123456"
    source = template.read_bytes()

    result = subprocess.run(
        [str(LIVEKIT_WRAPPER)],
        check=False,
        capture_output=True,
        text=True,
        env=_clean_environment(
            EIDOLON_RUNTIME_ROOT=str(tmp_path / "run"),
            EIDOLON_LIVEKIT_TEMPLATE_CONFIG=str(template),
            EIDOLON_LIVEKIT_GENERATED_CONFIG=str(generated),
            EIDOLON_LIVEKIT_NODE_IP=override,
            EIDOLON_LIVEKIT_GENERATE_ONLY="1",
            LIVEKIT_API_KEY=api_key,
            LIVEKIT_API_SECRET=api_secret,
        ),
    )

    rendered = generated.read_text(encoding="utf-8")
    assert result.returncode == 0
    assert template.read_bytes() == source
    assert "keys:" not in template.read_text(encoding="utf-8")
    assert ("node_ip:" in rendered) == bool(override)
    if override:
        assert f"node_ip: {override}" in rendered
    assert f"{api_key}: {api_secret}" in rendered
    assert stat.S_IMODE(generated.stat().st_mode) == 0o600
    assert api_key not in result.stdout + result.stderr
    assert api_secret not in result.stdout + result.stderr


def test_livekit_runtime_config_requires_credentials(tmp_path: Path) -> None:
    generated = tmp_path / "livekit.generated.yaml"

    result = subprocess.run(
        [str(LIVEKIT_WRAPPER)],
        check=False,
        capture_output=True,
        text=True,
        env=_clean_environment(
            EIDOLON_RUNTIME_ROOT=str(tmp_path / "run"),
            EIDOLON_LIVEKIT_GENERATED_CONFIG=str(generated),
            EIDOLON_LIVEKIT_NODE_IP="192.0.2.5",
            EIDOLON_LIVEKIT_GENERATE_ONLY="1",
        ),
    )

    assert result.returncode != 0
    assert not generated.exists()


def test_with_env_redacts_sensitive_parent_mismatch(tmp_path: Path) -> None:
    secret_from_parent = "parent-secret-value"
    secret_from_file = "file-secret-value"
    env_file = tmp_path / ".env"
    env_file.write_text(f"SERVICE_API_KEY={secret_from_file}\n", encoding="utf-8")

    result = subprocess.run(
        [str(WITH_ENV), str(tmp_path), str(env_file), "--", "/usr/bin/true"],
        check=False,
        capture_output=True,
        text=True,
        env={**os.environ, "SERVICE_API_KEY": secret_from_parent},
    )

    assert result.returncode == 0
    assert "SERVICE_API_KEY=<redacted>" in result.stderr
    assert secret_from_parent not in result.stderr
    assert secret_from_file not in result.stderr


def test_supervisor_does_not_enable_livekit_dev_credentials() -> None:
    config = (ROOT / "deploy/supervisor/available/livekit.conf").read_text(encoding="utf-8")
    run_all = (ROOT / "deploy/dev/run_all.sh").read_text(encoding="utf-8")

    assert "--dev" not in config
    assert "eidolon_ensure_livekit_credentials" in run_all


def test_admin_admits_the_web_client_as_a_browser_origin() -> None:
    """A generated copy of a component's registry must not narrow it.

    Admin's own registry allow-lists the web client's origin; this generated
    one listed only the operator console, so on a source run the browser
    refused every request the web client made before Admin ever saw it. The
    generated file exists to state this profile's topology, not to hold a
    smaller opinion about what Admin accepts.
    """

    from eidolon_ops import source_assets

    rendered = source_assets.admin_services_yaml()
    for origin in (
        f"http://127.0.0.1:{source_assets.CLIENT_WEB_PORT}",
        f"http://localhost:{source_assets.CLIENT_WEB_PORT}",
        f"http://127.0.0.1:{source_assets.PORTS['admin_web']}",
        f"http://localhost:{source_assets.PORTS['admin_web']}",
    ):
        assert f"    - {origin}\n" in rendered, origin


def test_the_product_source_profile_renders_livekit_before_running_it() -> None:
    """LiveKit's config is produced at start, not found on disk.

    Running the binary straight against a persisted path meant the server used
    whatever an earlier run had left: for months, a config on port 17880 with a
    stale rtc.node_ip, while every consumer of that server expected 7880. The
    product Host renders this file at each start; this profile now does too.
    """

    profile = (ROOT / "deploy/supervisor/product-source.conf").read_text(encoding="utf-8")
    block = profile.split("[program:livekit-server]", 1)[1].split("[program:", 1)[0]
    command = next(line for line in block.splitlines() if line.startswith("command="))
    assert "wrappers/livekit-dev.sh" in command
    assert "EIDOLON_LIVEKIT_GENERATED_CONFIG" not in command
    # The renderer needs the server credentials, which live in the profile's
    # own env root rather than in any settings file.
    assert "livekit.env" in command


def test_memory_38_source_profile_uses_a_fresh_storage_epoch() -> None:
    profile = (ROOT / "deploy/supervisor/product-source.conf").read_text(encoding="utf-8")
    launcher = (ROOT / "deploy/dev/run_all.sh").read_text(encoding="utf-8")

    expected = "memory/mempalaces-v3.8"
    assert f'EIDOLON_MEMORY_PALACES_ROOT="%(ENV_EIDOLON_STATE_ROOT)s/{expected}"' in profile
    assert f'"${{EIDOLON_STATE_ROOT}}/{expected}"' in launcher
