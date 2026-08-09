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


def test_livekit_runtime_config_is_generated_without_source_credentials(
    tmp_path: Path,
) -> None:
    template = ROOT / "deploy/livekit/livekit.yaml"
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
            EIDOLON_LIVEKIT_NODE_IP="192.0.2.5",
            EIDOLON_LIVEKIT_GENERATE_ONLY="1",
            LIVEKIT_API_KEY=api_key,
            LIVEKIT_API_SECRET=api_secret,
        ),
    )

    rendered = generated.read_text(encoding="utf-8")
    assert result.returncode == 0
    assert template.read_bytes() == source
    assert "keys:" not in template.read_text(encoding="utf-8")
    assert "node_ip: 192.0.2.5" in rendered
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
