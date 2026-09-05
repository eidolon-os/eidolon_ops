from __future__ import annotations

from pathlib import Path

import pytest

from eidolon_ops.config import (
    PRODUCT_UNITS,
    ConfigurationError,
    SourceConfig,
    load_config,
    validate_private_local_file,
    validate_release_id,
)
from eidolon_ops.foundation import FOUNDATION_PROFILE
from eidolon_ops.workstation_toolchain import workstation_uv_path

pytestmark = pytest.mark.unit


def test_loads_strict_config(config_path: Path) -> None:
    config = load_config(config_path)

    assert config.host.target == "pi@pi.example"
    assert config.foundation_profile == FOUNDATION_PROFILE
    assert config.host.port == 2222
    assert config.host.require_wired_release_upload is False
    assert config.workspace.python_index_url == "https://pypi.org/simple"
    assert config.workspace.python_http_timeout_seconds == 120
    assert config.workspace.python_http_retries == 8
    assert config.workspace.python_concurrent_downloads == 4
    assert config.units == PRODUCT_UNITS
    assert set(config.sources) == {
        "eidolon_kernel",
        "eidolon_data",
        "eidolon_hub",
        "eidolon_admin",
        "eidolon_agent",
        "eidolon_channel",
        "eidolon_memory",
        "eidolon_sdk",
    }
    assert len(config.install_files) == 14


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


def test_rejects_unreviewed_foundation_profile(config_path: Path) -> None:
    _replace(
        config_path,
        'profile = "raspberry-pi-os-debian-arm64-v2"',
        'profile = "operator-controlled"',
    )

    with pytest.raises(ConfigurationError, match="reviewed profile"):
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


@pytest.mark.parametrize(
    ("old", "new", "message"),
    [
        (
            'python_index_url = "https://pypi.org/simple"',
            'python_index_url = "http://pypi.org/simple"',
            "HTTPS",
        ),
        (
            'python_index_url = "https://pypi.org/simple"',
            'python_index_url = "https://user:secret@pypi.org/simple"',
            "without credentials",
        ),
        (
            'python_index_url = "https://pypi.org/simple"',
            'python_index_url = "https://pypi.org:invalid/simple"',
            "valid HTTPS",
        ),
        (
            'python_index_url = "https://pypi.org/simple"',
            'python_index_url = "https://pypi.org/simple?channel=unstable"',
            "without credentials",
        ),
        (
            'python_index_url = "https://pypi.org/simple"',
            'python_index_url = "https://pypi.org"',
            "without credentials",
        ),
        (
            'python_index_url = "https://pypi.org/simple"',
            'python_index_url = "https://pypi.org/simple path"',
            "bounded HTTPS",
        ),
        (
            "python_http_timeout_seconds = 120",
            "python_http_timeout_seconds = 9",
            "between",
        ),
        ("python_http_retries = 8", "python_http_retries = 21", "between"),
        (
            "python_concurrent_downloads = 4",
            "python_concurrent_downloads = 17",
            "between",
        ),
    ],
)
def test_rejects_unsafe_python_resolver_fields(
    config_path: Path, old: str, new: str, message: str
) -> None:
    _replace(config_path, old, new)

    with pytest.raises(ConfigurationError, match=message):
        load_config(config_path)


def test_a_source_needs_no_revision_at_all(config) -> None:
    """The declaration is gone; the repository is the fact.

    Two hand-maintained copies of one commit shipped a release nobody meant to
    ship. What is left here is a path.
    """

    assert all(source.revision is None for source in config.sources.values())


def test_an_explicit_revision_is_still_a_full_commit(pinned_config, config_path: Path) -> None:
    """Writing one down is allowed, and still means exactly one commit."""

    assert pinned_config.sources["eidolon_data"].revision == f"{2:040x}"
    _replace(config_path, f'revision = "{2:040x}"', 'revision = "abc"')

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


def test_source_overrides_replace_path_and_revision_immutably(config, tmp_path: Path) -> None:
    original = config.sources["eidolon_admin"]
    replacement = SourceConfig(path=tmp_path / "admin", revision="e" * 40)

    updated = config.with_source_overrides({"eidolon_admin": replacement})

    assert config.sources["eidolon_admin"] == original
    assert updated.sources["eidolon_admin"] == replacement
    with pytest.raises(ConfigurationError, match="unknown source"):
        config.with_source_overrides({"unknown": replacement})


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


def test_the_build_tool_is_pinned_not_derived_from_the_activator(config_path: Path):
    """uv is a workstation build tool, and it is pinned the way the Host's is.

    Naming a path was the old way to say "pinned", and it meant "pinned for as
    long as that path survives" — the profile pointed into the system temp
    directory, macOS swept it, and the release line stopped. Absence now means
    the pinned artifact, which Ops materializes from a version and a digest.
    What must never happen is either of them being derived from wherever the
    activator happens to live.
    """

    config = load_config(config_path)
    assert config.workspace.uv is not None
    assert config.workspace.uv.name == "uv"

    text = config_path.read_text(encoding="utf-8")
    config_path.write_text(text.replace(f'uv = "{config.workspace.uv}"\n', "", 1), encoding="utf-8")
    without_override = load_config(config_path)

    # No override means the pinned one, and it lives where Ops keeps its own
    # tools rather than beside the release CLI.
    assert without_override.workspace.uv is None
    toolchain_root = without_override.workspace.toolchain_root
    assert workstation_uv_path(toolchain_root).name == "uv"
    assert without_override.workspace.release_cli.parent != toolchain_root


def test_a_host_may_state_how_long_its_services_need(config_path: Path) -> None:
    """Platform property, not a constant compiled into the deployer: the
    default suits a Pi 5, and a slower board says so in its own profile."""

    assert load_config(config_path).host.readiness_timeout_seconds == 240

    _replace(
        config_path,
        "connect_timeout_seconds = 7",
        "connect_timeout_seconds = 7\nreadiness_timeout_seconds = 600",
    )

    assert load_config(config_path).host.readiness_timeout_seconds == 600


def test_a_host_may_require_release_uploads_to_use_a_wire(config_path: Path) -> None:
    _replace(
        config_path,
        "connect_timeout_seconds = 7",
        "connect_timeout_seconds = 7\nrequire_wired_release_upload = true",
    )

    assert load_config(config_path).host.require_wired_release_upload is True


def test_wired_release_policy_must_be_boolean(config_path: Path) -> None:
    _replace(
        config_path,
        "connect_timeout_seconds = 7",
        'connect_timeout_seconds = 7\nrequire_wired_release_upload = "yes"',
    )

    with pytest.raises(ConfigurationError, match="require_wired_release_upload must be a boolean"):
        load_config(config_path)


def test_a_readiness_deadline_outside_reason_is_refused(config_path: Path) -> None:
    _replace(
        config_path,
        "connect_timeout_seconds = 7",
        "connect_timeout_seconds = 7\nreadiness_timeout_seconds = 5",
    )

    with pytest.raises(ConfigurationError, match="readiness_timeout_seconds"):
        load_config(config_path)


@pytest.mark.parametrize(
    ("label", "old", "new"),
    [
        ("sources.eidolon_data.path", None, "/private/tmp/eidolon-data"),
        ("workspace.release_cli", None, "/tmp/eidolon-release"),
    ],
)
def test_a_release_input_may_not_be_pinned_where_the_system_sweeps(
    config_path: Path, label: str, old: str | None, new: str
) -> None:
    """Both of these were real, and both failed days later naming a file.

    A pinned uv sat in /private/tmp until macOS emptied it, and so did a pinned
    source repository. Neither said anything at the moment it became a lie; the
    release line simply stopped, and the error named a missing path rather than
    the reason it was missing.
    """

    key = label.rsplit(".", 1)[-1] if label.startswith("workspace") else "path"
    config = load_config(config_path)
    current = (
        config.workspace.release_cli
        if label.startswith("workspace")
        else config.sources["eidolon_data"].path
    )
    _replace(config_path, f'{key} = "{current}"', f'{key} = "{new}"')

    with pytest.raises(ConfigurationError, match="not pinned"):
        load_config(config_path)


def test_an_output_may_live_in_a_temporary_directory(config_path: Path) -> None:
    # A bundle is rebuilt from its inputs, so losing one costs a build and
    # nothing else. Only what a release must be able to find again is refused.
    _replace(
        config_path,
        f'bundle_root = "{load_config(config_path).workspace.bundle_root}"',
        'bundle_root = "/private/tmp/eidolon-release-bundles"',
    )

    assert (
        load_config(config_path).workspace.bundle_root
        == Path("/private/tmp/eidolon-release-bundles").resolve()
    )


def _with_settings(config_path: Path, body: str) -> Path:
    config_path.write_text(config_path.read_text(encoding="utf-8") + body, encoding="utf-8")
    return config_path


def test_a_host_without_a_settings_section_overlays_nothing(config_path: Path) -> None:
    """The section is optional, because most Hosts take every default."""

    assert load_config(config_path).settings_overlay == ()


def test_settings_overlay_is_read_in_order(config_path: Path) -> None:
    config = load_config(
        _with_settings(
            config_path,
            """
[[settings.overlay]]
document = "channel.yaml"
path = "providers.stt_provider"
value = "eidolon_models"

[[settings.overlay]]
document = "channel.yaml"
path = "providers.tts_provider"
value = "eidolon_models"
""",
        )
    )

    assert [(a.document, a.display, a.value) for a in config.settings_overlay] == [
        ("channel.yaml", "providers.stt_provider", "eidolon_models"),
        ("channel.yaml", "providers.tts_provider", "eidolon_models"),
    ]


def test_an_overlay_path_is_parsed_while_reading_the_config(config_path: Path) -> None:
    """A typo should stop the operator here, not part-way through a release."""

    with pytest.raises(ConfigurationError, match="path segment is invalid"):
        load_config(
            _with_settings(
                config_path,
                """
[[settings.overlay]]
document = "channel.yaml"
path = "providers..stt_provider"
value = "x"
""",
            )
        )


def test_one_setting_may_not_be_assigned_twice(config_path: Path) -> None:
    """Both would apply and the last would win, silently."""

    with pytest.raises(ConfigurationError, match="twice"):
        load_config(
            _with_settings(
                config_path,
                """
[[settings.overlay]]
document = "channel.yaml"
path = "providers.stt_provider"
value = "a"

[[settings.overlay]]
document = "channel.yaml"
path = "providers.stt_provider"
value = "b"
""",
            )
        )


def test_a_non_string_value_is_refused_rather_than_rendered(config_path: Path) -> None:
    """TOML `false` and YAML `false` are not the same decision to make here."""

    with pytest.raises(ConfigurationError, match="value must be a string"):
        load_config(
            _with_settings(
                config_path,
                """
[[settings.overlay]]
document = "channel.yaml"
path = "avatar.enabled"
value = false
""",
            )
        )


def test_a_host_declares_no_capabilities_by_default(config_path: Path) -> None:
    assert load_config(config_path).capabilities == frozenset()


def test_capabilities_are_read(config_path: Path) -> None:
    config = load_config(
        _with_settings(
            config_path,
            '\n[capabilities]\nprovides = ["rknpu2", "local_asr", "local_tts"]\n',
        )
    )

    assert config.capabilities == frozenset({"rknpu2", "local_asr", "local_tts"})


def test_a_capability_nobody_defined_is_refused(config_path: Path) -> None:
    """A typo would provide nothing and drop units without saying why."""

    with pytest.raises(ConfigurationError, match="unknown Host capability 'rknpu'"):
        load_config(
            _with_settings(config_path, '\n[capabilities]\nprovides = ["rknpu"]\n')
        )


def test_a_repeated_capability_is_refused(config_path: Path) -> None:
    with pytest.raises(ConfigurationError, match="repeats 'rknpu2'"):
        load_config(
            _with_settings(
                config_path, '\n[capabilities]\nprovides = ["rknpu2", "rknpu2"]\n'
            )
        )
