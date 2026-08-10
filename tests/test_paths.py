from __future__ import annotations

from pathlib import Path

import pytest

from eidolon_ops.paths import (
    HostProfileError,
    load_host_profile,
    merged_environment,
)

REPOSITORY_ROOT = Path(__file__).parents[1]


def _write_mac_profile(tmp_path: Path, *, script: Path, overrides: str = "") -> Path:
    root = tmp_path / "workspace"
    config = tmp_path / "config"
    state = tmp_path / "state"
    runtime = tmp_path / "run"
    logs = tmp_path / "logs"
    cache = tmp_path / "cache"
    bootstrap = tmp_path / "bootstrap"
    bootstrap_runtime = tmp_path / "bootstrap-run"
    path = tmp_path / "mac.toml"
    path.write_text(
        f"""schema_version = 1
[host]
id = "mac-test"
platform = "macos"
driver = "local-supervisord"
[paths]
install_root = "{root}"
current_root = "{root}"
config_root = "{config}"
state_root = "{state}"
runtime_root = "{runtime}"
log_root = "{logs}"
cache_root = "{cache}"
bootstrap_state_root = "{bootstrap}"
bootstrap_runtime_root = "{bootstrap_runtime}"
[adapter]
lifecycle_script = "{script}"
{overrides}
""",
        encoding="utf-8",
    )
    return path


def test_mac_profile_exports_one_host_path_contract(tmp_path: Path) -> None:
    script = tmp_path / "run.sh"
    script.write_text("#!/bin/sh\n", encoding="utf-8")
    profile = load_host_profile(_write_mac_profile(tmp_path, script=script))

    assert profile.platform == "macos"
    assert profile.lifecycle_script == script
    environment = profile.environment()
    assert environment["EIDOLON_ROOT"] == environment["EIDOLON_WORKSPACE_ROOT"]
    assert environment["EIDOLON_STATE_ROOT"] == str(tmp_path / "state")
    assert environment["EIDOLON_BOOTSTRAP_STATE_ROOT"] == str(tmp_path / "bootstrap")
    assert environment["EIDOLON_BOOTSTRAP_RUNTIME_DIR"] == str(tmp_path / "bootstrap-run")
    assert merged_environment(profile)["EIDOLON_HOST_DRIVER"] == "local-supervisord"


def test_mac_example_uses_the_ops_owned_source_lifecycle() -> None:
    profile = load_host_profile(REPOSITORY_ROOT / "config/hosts/mac.example.toml")

    assert profile.lifecycle_script == (REPOSITORY_ROOT / "deploy/dev/run_all.sh").resolve()
    product_root = Path.home() / "ai/eidolon/.eidolon/mac-product"
    assert profile.paths.config_root == product_root / "config"
    assert profile.paths.state_root == product_root / "state"
    assert profile.paths.runtime_root == product_root / "run"
    assert profile.paths.log_root == product_root / "logs"
    assert profile.paths.cache_root == product_root / "cache"
    assert profile.paths.bootstrap_state_root == product_root / "bootstrap/state"
    assert profile.paths.bootstrap_runtime_root == product_root / "bootstrap/run"


def test_pi_profile_requires_reviewed_fhs_paths(tmp_path: Path) -> None:
    operations = tmp_path / "pi.toml"
    operations.write_text("placeholder", encoding="utf-8")
    profile_file = tmp_path / "host.toml"
    profile_file.write_text(
        f"""schema_version = 1
[host]
id = "pi5"
platform = "raspberry-pi"
driver = "ssh-systemd"
[paths]
install_root = "/opt/eidolon"
current_root = "/opt/eidolon/current"
config_root = "/etc/eidolon"
state_root = "/var/lib/eidolon"
runtime_root = "/run/eidolon"
log_root = "/var/log/eidolon"
cache_root = "/var/cache/eidolon"
bootstrap_state_root = "/var/lib/eidolon-bootstrap"
bootstrap_runtime_root = "/run/eidolon-bootstrap"
[adapter]
operations_config = "{operations}"
""",
        encoding="utf-8",
    )

    profile = load_host_profile(profile_file)
    assert profile.operations_config == operations
    assert profile.paths.current_root == Path("/opt/eidolon/current")

    profile_file.write_text(
        profile_file.read_text(encoding="utf-8").replace(
            'state_root = "/var/lib/eidolon"', 'state_root = "/srv/eidolon"'
        ),
        encoding="utf-8",
    )
    with pytest.raises(HostProfileError, match=r"Pi paths\.state_root"):
        load_host_profile(profile_file)


@pytest.mark.parametrize(
    ("old", "new", "message"),
    [
        ('id = "mac-test"', 'id = "bad id"', "host.id"),
        ('platform = "macos"', 'platform = "linux"', "host.platform"),
        (
            'driver = "local-supervisord"',
            'driver = "ssh-systemd"',
            "platform and driver",
        ),
    ],
)
def test_profile_rejects_ambiguous_identity_and_paths(
    tmp_path: Path, old: str, new: str, message: str
) -> None:
    script = tmp_path / "run.sh"
    script.write_text("", encoding="utf-8")
    path = _write_mac_profile(tmp_path, script=script)
    text = path.read_text(encoding="utf-8")
    text = text.replace(old, new)
    path.write_text(text, encoding="utf-8")
    with pytest.raises(HostProfileError, match=message):
        load_host_profile(path)


def test_profile_keeps_bootstrap_state_separate(tmp_path: Path) -> None:
    script = tmp_path / "run.sh"
    script.write_text("", encoding="utf-8")
    path = _write_mac_profile(tmp_path, script=script)
    text = path.read_text(encoding="utf-8").replace(
        f'bootstrap_state_root = "{tmp_path / "bootstrap"}"',
        f'bootstrap_state_root = "{tmp_path / "state"}"',
    )
    path.write_text(text, encoding="utf-8")
    with pytest.raises(HostProfileError, match="distinct ownership"):
        load_host_profile(path)


def test_profile_keeps_bootstrap_runtime_separate(tmp_path: Path) -> None:
    script = tmp_path / "run.sh"
    script.write_text("", encoding="utf-8")
    path = _write_mac_profile(tmp_path, script=script)
    text = path.read_text(encoding="utf-8").replace(
        f'bootstrap_runtime_root = "{tmp_path / "bootstrap-run"}"',
        f'bootstrap_runtime_root = "{tmp_path / "run"}"',
    )
    path.write_text(text, encoding="utf-8")
    with pytest.raises(HostProfileError, match="distinct ownership"):
        load_host_profile(path)


def test_profile_rejects_relative_and_duplicate_lifecycle_roots(tmp_path: Path) -> None:
    script = tmp_path / "run.sh"
    script.write_text("", encoding="utf-8")
    path = _write_mac_profile(tmp_path, script=script)
    text = path.read_text(encoding="utf-8").replace(
        f'cache_root = "{tmp_path / "cache"}"', 'cache_root = "relative/cache"'
    )
    path.write_text(text, encoding="utf-8")
    with pytest.raises(HostProfileError, match="safe absolute"):
        load_host_profile(path)

    path = _write_mac_profile(tmp_path, script=script)
    text = path.read_text(encoding="utf-8").replace(
        f'cache_root = "{tmp_path / "cache"}"',
        f'cache_root = "{tmp_path / "logs"}"',
    )
    path.write_text(text, encoding="utf-8")
    with pytest.raises(HostProfileError, match="must be distinct"):
        load_host_profile(path)


def test_profile_rejects_unreadable_and_extra_root_keys(tmp_path: Path) -> None:
    with pytest.raises(HostProfileError, match="unreadable"):
        load_host_profile(tmp_path / "missing.toml")
    script = tmp_path / "run.sh"
    script.write_text("", encoding="utf-8")
    path = _write_mac_profile(tmp_path, script=script)
    path.write_text(path.read_text(encoding="utf-8") + "\nextra = 1\n", encoding="utf-8")
    with pytest.raises(HostProfileError):
        load_host_profile(path)

    path = _write_mac_profile(tmp_path, script=script)
    path.write_text(
        path.read_text(encoding="utf-8").replace("schema_version = 1", "schema_version = 2"),
        encoding="utf-8",
    )
    with pytest.raises(HostProfileError, match="schema_version"):
        load_host_profile(path)
