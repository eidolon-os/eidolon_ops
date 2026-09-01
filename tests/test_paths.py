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
operations_config = "{tmp_path / "operations.toml"}"
foundation_mode = "external"
external_livekit_config = "{tmp_path / "livekit.yaml"}"
[app]
lan_ipv4 = "192.168.1.25"
hub_https_port = 8443
livekit_client_url = "ws://192.168.1.25:7880"
allow_insecure_livekit = true
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
    assert profile.operations_config == tmp_path / "operations.toml"
    assert profile.foundation_mode == "external"
    assert profile.app is not None
    assert str(profile.app.lan_ipv4) == "192.168.1.25"
    assert profile.app.livekit_client_url == "ws://192.168.1.25:7880"
    environment = profile.environment()
    assert environment["EIDOLON_ROOT"] == environment["EIDOLON_WORKSPACE_ROOT"]
    assert environment["EIDOLON_STATE_ROOT"] == str(tmp_path / "state")
    assert environment["EIDOLON_BOOTSTRAP_STATE_ROOT"] == str(tmp_path / "bootstrap")
    assert environment["EIDOLON_BOOTSTRAP_RUNTIME_DIR"] == str(tmp_path / "bootstrap-run")
    assert merged_environment(profile)["EIDOLON_HOST_DRIVER"] == "local-supervisord"


def test_mac_profile_accepts_exact_local_source_override(tmp_path: Path) -> None:
    script = tmp_path / "run.sh"
    script.write_text("#!/bin/sh\n", encoding="utf-8")
    admin = tmp_path / "admin-worktree"
    profile = load_host_profile(
        _write_mac_profile(
            tmp_path,
            script=script,
            overrides=(
                f'[source_overrides.eidolon_admin]\npath = "{admin}"\nrevision = "{"a" * 40}"\n'
            ),
        )
    )

    assert profile.source_overrides["eidolon_admin"].path == admin
    assert profile.source_overrides["eidolon_admin"].revision == "a" * 40


@pytest.mark.parametrize(
    ("body", "message"),
    [
        (
            '[source_overrides.unknown]\npath = "/tmp/source"\nrevision = "' + "a" * 40 + '"\n',
            "unknown source",
        ),
        (
            '[source_overrides.eidolon_admin]\npath = "/tmp/source"\nrevision = "short"\n',
            "40 lowercase hex",
        ),
    ],
)
def test_mac_profile_rejects_unsafe_source_override(
    tmp_path: Path, body: str, message: str
) -> None:
    script = tmp_path / "run.sh"
    script.write_text("#!/bin/sh\n", encoding="utf-8")

    with pytest.raises(HostProfileError, match=message):
        load_host_profile(_write_mac_profile(tmp_path, script=script, overrides=body))


def test_a_profile_may_pin_the_setup_code_it_already_knows(tmp_path: Path) -> None:
    """Pinning the value is the whole of what it does.

    Nothing else about commissioning changes: the Host still opens one ordinary
    session for it. What goes away is looking a code up — the command becomes
    one an operator fires without reading its output.
    """

    script = tmp_path / "run.sh"
    script.write_text("#!/bin/sh\n", encoding="utf-8")
    path = _write_mac_profile(
        tmp_path, script=script, overrides='setup_code = "99999990"'
    )

    profile = load_host_profile(path)

    assert profile.app is not None
    assert profile.app.setup_code == "99999990"


def test_a_profile_without_a_pinned_code_lets_the_host_draw_one(
    tmp_path: Path,
) -> None:
    script = tmp_path / "run.sh"
    script.write_text("#!/bin/sh\n", encoding="utf-8")

    profile = load_host_profile(_write_mac_profile(tmp_path, script=script))

    assert profile.app is not None
    assert profile.app.setup_code is None


@pytest.mark.parametrize(
    "code",
    [
        '"1234567"',  # too short
        '"999999900"',  # too long
        '"9999999a"',  # not digits
        '"11111111"',  # every digit the same
        '"01234567"',  # the plain run up
        '"76543210"',  # and down
        "99999990",  # a TOML integer, which would also lose the leading zeros
    ],
)
def test_a_pinned_code_is_refused_here_rather_than_three_hops_away(
    tmp_path: Path,
    code: str,
) -> None:
    """Caught while reading the file, not on the machine.

    The Host re-checks it and stays the authority; this only moves the error to
    where the value was written.
    """

    script = tmp_path / "run.sh"
    script.write_text("#!/bin/sh\n", encoding="utf-8")
    path = _write_mac_profile(
        tmp_path, script=script, overrides=f"setup_code = {code}"
    )

    with pytest.raises(HostProfileError, match="setup_code"):
        load_host_profile(path)


def test_the_pi_example_documents_the_pinned_code_without_pinning_one() -> None:
    """Read from the tracked example, never from an operator's own profile.

    ``config/hosts/*.toml`` is gitignored — those are local operator files, so a
    test that read one would pass here and fail on every other checkout. The
    example is what the repository actually promises, and what it promises is
    the shape: documented, and commented out, because a code in a tracked file
    is a code everybody has.
    """

    profile = load_host_profile(REPOSITORY_ROOT / "config/hosts/pi5.example.toml")

    assert profile.app is not None
    assert profile.app.setup_code is None
    text = (REPOSITORY_ROOT / "config/hosts/pi5.example.toml").read_text(
        encoding="utf-8"
    )
    assert "# setup_code = " in text


def test_mac_example_uses_the_ops_owned_source_lifecycle() -> None:
    profile = load_host_profile(REPOSITORY_ROOT / "config/hosts/mac.example.toml")

    assert profile.lifecycle_script == (REPOSITORY_ROOT / "deploy/dev/run_all.sh").resolve()
    assert profile.operations_config == (REPOSITORY_ROOT / "config/eidolon-pi.toml").resolve()
    assert profile.foundation_mode == "external"
    assert profile.app is not None
    assert profile.app.hub_https_port == 8443
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


@pytest.mark.parametrize(
    ("old", "new", "message"),
    [
        ("schema_version = 1", "schema_version = 1\nunexpected = true", "profile root"),
        ('id = "mac-test"', 'id = "mac-test"\nextra = true', "host must contain"),
        ('driver = "local-supervisord"', 'driver = "unknown"', "host.driver"),
        (
            'cache_root = "{cache}"',
            'cache_root = "{cache}"\nextra_path = "/tmp/extra"',
            "paths must contain",
        ),
        (
            'foundation_mode = "external"',
            'foundation_mode = "external"\nextra_adapter = true',
            "local adapter",
        ),
    ],
)
def test_profile_rejects_unknown_structural_fields(
    tmp_path: Path, old: str, new: str, message: str
) -> None:
    script = tmp_path / "run.sh"
    script.write_text("", encoding="utf-8")
    path = _write_mac_profile(tmp_path, script=script)
    old = old.format(cache=tmp_path / "cache")
    new = new.format(cache=tmp_path / "cache")
    path.write_text(path.read_text(encoding="utf-8").replace(old, new), encoding="utf-8")

    with pytest.raises(HostProfileError, match=message):
        load_host_profile(path)


@pytest.mark.parametrize(
    ("old", "new", "message"),
    [
        ('lan_ipv4 = "192.168.1.25"', 'lan_ipv4 = "127.0.0.1"', "private IPv4"),
        ('lan_ipv4 = "192.168.1.25"', 'lan_ipv4 = "not-an-ip"', "private IPv4"),
        ('lan_ipv4 = "192.168.1.25"', 'lan_ipv4 = "fd00::25"', "private IPv4"),
        ("hub_https_port = 8443", "hub_https_port = true", "valid TCP port"),
        ("hub_https_port = 8443", "hub_https_port = 0", "valid TCP port"),
        (
            'livekit_client_url = "ws://192.168.1.25:7880"',
            'livekit_client_url = "ws://user@192.168.1.25:7880/path?query=1"',
            "plain ws/wss origin",
        ),
        (
            'livekit_client_url = "ws://192.168.1.25:7880"',
            'livekit_client_url = "ws://192.168.1.99:7880"',
            "must use app.lan_ipv4",
        ),
        ("allow_insecure_livekit = true", 'allow_insecure_livekit = "yes"', "boolean"),
        ("allow_insecure_livekit = true", "allow_insecure_livekit = false", "opt-in"),
    ],
)
def test_profile_rejects_unsafe_app_access(
    tmp_path: Path, old: str, new: str, message: str
) -> None:
    script = tmp_path / "run.sh"
    script.write_text("#!/bin/sh\n", encoding="utf-8")
    path = _write_mac_profile(tmp_path, script=script)
    path.write_text(path.read_text(encoding="utf-8").replace(old, new), encoding="utf-8")

    with pytest.raises(HostProfileError, match=message):
        load_host_profile(path)


def test_profile_accepts_secure_livekit_origin_without_development_opt_in(tmp_path: Path) -> None:
    script = tmp_path / "run.sh"
    script.write_text("#!/bin/sh\n", encoding="utf-8")
    path = _write_mac_profile(tmp_path, script=script)
    text = path.read_text(encoding="utf-8").replace(
        'livekit_client_url = "ws://192.168.1.25:7880"',
        'livekit_client_url = "wss://livekit.example.test"',
    )
    path.write_text(
        text.replace("allow_insecure_livekit = true", "allow_insecure_livekit = false"),
        encoding="utf-8",
    )

    profile = load_host_profile(path)
    assert profile.app is not None
    assert profile.app.livekit_client_url == "wss://livekit.example.test"
    assert profile.app.allow_insecure_livekit is False


def test_a_host_may_leave_its_address_to_discovery(tmp_path: Path) -> None:
    """An address is observed state; a Host on DHCP has none to declare."""

    script = tmp_path / "run.sh"
    script.write_text("#!/bin/sh\n", encoding="utf-8")
    script.chmod(0o755)
    original = _write_mac_profile(tmp_path, script=script)
    text = original.read_text(encoding="utf-8")
    text = text.replace('lan_ipv4 = "192.168.1.25"\n', "")
    text = text.replace(
        'livekit_client_url = "ws://192.168.1.25:7880"',
        'livekit_client_url = "ws://eidolon-hub-0123456789abcdef0123.local:7880"',
    )
    discovered = tmp_path / "mac-discovered.toml"
    discovered.write_text(text, encoding="utf-8")

    profile = load_host_profile(discovered)

    assert profile.app is not None
    assert profile.app.lan_ipv4 is None


def test_a_discovered_address_forbids_a_literal_one_in_the_client_url(tmp_path: Path) -> None:
    """Otherwise the URL goes stale exactly the way the declaration did."""

    script = tmp_path / "run.sh"
    script.write_text("#!/bin/sh\n", encoding="utf-8")
    script.chmod(0o755)
    original = _write_mac_profile(tmp_path, script=script)
    text = original.read_text(encoding="utf-8").replace('lan_ipv4 = "192.168.1.25"\n', "")
    stale = tmp_path / "mac-stale-url.toml"
    stale.write_text(text, encoding="utf-8")

    with pytest.raises(HostProfileError, match="must not embed a literal address"):
        load_host_profile(stale)
