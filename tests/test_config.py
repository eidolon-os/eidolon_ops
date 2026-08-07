from __future__ import annotations

from pathlib import Path

import pytest

from eidolon_ops.config import (
    PRODUCT_UNITS,
    ConfigurationError,
    load_config,
    validate_private_local_file,
    validate_release_id,
)

pytestmark = pytest.mark.unit


def test_loads_strict_config(config_path: Path) -> None:
    config = load_config(config_path)

    assert config.host.target == "pi@pi.example"
    assert config.host.port == 2222
    assert config.units == PRODUCT_UNITS
    assert set(config.sources) == {
        "eidolon_kernel",
        "eidolon_data",
        "eidolon_hub",
        "eidolon_admin",
        "eidolon_sdk",
    }
    assert len(config.install_files) == 7


@pytest.mark.parametrize("release_id", ["r1", "20260807-a.b_c-1", "A" * 64])
def test_release_id_accepts_safe_values(release_id: str) -> None:
    assert validate_release_id(release_id) == release_id


@pytest.mark.parametrize("release_id", ["", "/bad", "../bad", "has space", "A" * 65])
def test_release_id_rejects_unsafe_values(release_id: str) -> None:
    with pytest.raises(ConfigurationError, match="release id"):
        validate_release_id(release_id)


def test_rejects_extra_root_key(config_path: Path) -> None:
    _replace(config_path, "schema_version = 1", "schema_version = 1\nextra = true")

    with pytest.raises(ConfigurationError, match="keys are invalid"):
        load_config(config_path)


def test_rejects_wrong_schema(config_path: Path) -> None:
    _replace(config_path, "schema_version = 1", "schema_version = 2")

    with pytest.raises(ConfigurationError, match="schema_version"):
        load_config(config_path)


@pytest.mark.parametrize(
    ("old", "new", "message"),
    [
        ('user = "pi"', 'user = "PI!"', "unsafe"),
        ('user = "pi"', 'user = "root"', "unsafe"),
        ('hostname = "pi.example"', 'hostname = "bad host"', "unsafe"),
        ("port = 2222", "port = 0", "between"),
        ("connect_timeout_seconds = 7", "connect_timeout_seconds = 121", "between"),
        ('remote_uv = "/usr/local/bin/uv"', 'remote_uv = "uv"', "absolute"),
    ],
)
def test_rejects_unsafe_host_fields(config_path: Path, old: str, new: str, message: str) -> None:
    _replace(config_path, old, new)

    with pytest.raises(ConfigurationError, match=message):
        load_config(config_path)


def test_rejects_short_revision(config_path: Path) -> None:
    _replace(config_path, "0000000000000000000000000000000000000001", "abc")

    with pytest.raises(ConfigurationError, match="40-hex"):
        load_config(config_path)


def test_rejects_reused_install_file_path(config_path: Path) -> None:
    text = config_path.read_text(encoding="utf-8")
    data_line = next(line for line in text.splitlines() if line.startswith("data_env = "))
    hub_line = next(line for line in text.splitlines() if line.startswith("hub_env = "))
    config_path.write_text(
        text.replace(hub_line, data_line.replace("data_env", "hub_env")), encoding="utf-8"
    )

    with pytest.raises(ConfigurationError, match="unique"):
        load_config(config_path)


def test_rejects_service_topology_drift(config_path: Path) -> None:
    _replace(config_path, '  "eidolon-admin.service",\n', "")

    with pytest.raises(ConfigurationError, match="fixed reviewed"):
        load_config(config_path)


def test_rejects_data_path_drift(config_path: Path) -> None:
    _replace(
        config_path,
        "/var/lib/eidolon/eidolon-system.sqlite3",
        "/tmp/wrong.sqlite3",
    )

    with pytest.raises(ConfigurationError, match="reviewed system assets"):
        load_config(config_path)


def test_revision_overrides_are_immutable(config) -> None:
    original = config.sources["eidolon_data"].revision
    replacement = "f" * 40

    updated = config.with_revision_overrides((f"eidolon_data={replacement}",))

    assert config.sources["eidolon_data"].revision == original
    assert updated.sources["eidolon_data"].revision == replacement


@pytest.mark.parametrize(
    "values",
    [
        ("unknown=" + "f" * 40,),
        ("eidolon_data=short",),
        ("eidolon_data=" + "f" * 40, "eidolon_data=" + "e" * 40),
        ("missing-separator",),
    ],
)
def test_rejects_bad_revision_overrides(config, values: tuple[str, ...]) -> None:
    with pytest.raises(ConfigurationError):
        config.with_revision_overrides(values)


def test_private_file_requires_mode_0600_or_stricter(tmp_path: Path) -> None:
    path = tmp_path / "secret"
    path.write_text("value", encoding="utf-8")
    path.chmod(0o640)

    with pytest.raises(ConfigurationError, match="group/world"):
        validate_private_local_file(path, label="secret")

    path.chmod(0o600)
    validate_private_local_file(path, label="secret")


def test_private_file_rejects_symlink(tmp_path: Path) -> None:
    value = tmp_path / "value"
    value.write_text("x", encoding="utf-8")
    link = tmp_path / "link"
    link.symlink_to(value)

    with pytest.raises(ConfigurationError, match="regular"):
        validate_private_local_file(link, label="secret")


def _replace(path: Path, old: str, new: str) -> None:
    text = path.read_text(encoding="utf-8")
    assert old in text
    path.write_text(text.replace(old, new, 1), encoding="utf-8")
