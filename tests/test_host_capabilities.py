"""A Host profile names which model services that machine runs; the operations config bounds it.

Several Hosts share one operations config (the Pi product serves the Pi 5 boards and the Mac
source run). Whether a machine runs Laya is a property of the machine, not of the product: a
Pi 5 has no NPU, the Mac runs Laya on its GPU. A profile may narrow its operations config's
capabilities, never widen them, and everything derived from capabilities follows the narrower set.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from eidolon_ops.config import ConfigurationError, expected_units, load_config
from eidolon_ops.paths import HostProfileError, load_host_profile

_HOSTS = Path(__file__).resolve().parents[1] / "config" / "hosts"


def _with_laya(config_path: Path) -> Path:
    """The fixture config, as a product whose Hosts may run Laya."""

    text = config_path.read_text(encoding="utf-8")
    root = config_path.parent
    text = text.replace(
        "[services]",
        f'[sources.eidolon_models]\npath = "{root / "eidolon_models"}"\n\n[services]',
        1,
    )
    text = text.replace(
        '  "eidolon-channel.service",\n]',
        '  "eidolon-channel.service",\n  "eidolon-laya.service",\n]',
        1,
    )
    text = text.replace("[data]", '[capabilities]\nprovides = ["local_laya"]\n\n[data]', 1)
    path = root / "laya-product.toml"
    path.write_text(text, encoding="utf-8")
    return path


def test_without_a_host_declaration_the_operations_config_decides(config_path: Path) -> None:
    config = load_config(_with_laya(config_path))
    assert config.capabilities == frozenset({"local_laya"})
    assert "eidolon-laya.service" in config.units
    assert "eidolon_models" in config.sources


def test_a_host_without_models_installs_and_pins_none_of_them(config_path: Path) -> None:
    config = load_config(_with_laya(config_path), capabilities=frozenset())
    assert config.capabilities == frozenset()
    assert config.units == expected_units(frozenset())
    assert "eidolon-laya.service" not in config.units
    assert "eidolon_models" not in config.sources


def test_a_host_cannot_declare_more_than_its_product_reviews(config_path: Path) -> None:
    with pytest.raises(ConfigurationError, match="does not review: rknpu2"):
        load_config(_with_laya(config_path), capabilities=frozenset({"local_laya", "rknpu2"}))


def test_the_pi5_boards_run_no_local_model_and_the_mac_runs_laya() -> None:
    for name in ("pi5", "pi5-device-management-hil"):
        assert load_host_profile(_HOSTS / f"{name}.toml").capabilities == frozenset(), name
    mac = load_host_profile(_HOSTS / "mac.toml")
    assert mac.capabilities is None
    assert "local_laya" in load_config(mac.operations_config).capabilities


def _profile_with(tmp_path: Path, capabilities: str) -> Path:
    source = (_HOSTS / "pi5.toml").read_text(encoding="utf-8")
    head = source.split("\n[capabilities]")[0]
    path = tmp_path / "hosts" / "pi5.toml"
    path.parent.mkdir()
    path.write_text(head + "\n" + capabilities, encoding="utf-8")
    return path


@pytest.mark.parametrize(
    ("section", "message"),
    [
        ('[capabilities]\nprovides = ["local_laya", "local_laya"]\n', "repeats"),
        ('[capabilities]\nprovides = ["local_gpu"]\n', "local_gpu"),
        ('[capabilities]\nprovides = []\nextra = 1\n', "exactly provides"),
        ('[capabilities]\nprovides = "local_laya"\n', "array of strings"),
    ],
)
def test_a_malformed_host_declaration_is_refused(tmp_path: Path, section: str, message: str) -> None:
    with pytest.raises(HostProfileError, match=message):
        load_host_profile(_profile_with(tmp_path, section))


def _with_participation(config_path: Path) -> Path:
    """The fixture config, as a product whose Hosts may run the participation service."""

    text = _with_laya(config_path).read_text(encoding="utf-8")
    text = text.replace('"eidolon-laya.service",\n]', '"eidolon-laya-participation.service",\n]', 1)
    text = text.replace('provides = ["local_laya"]', 'provides = ["local_laya_participation"]', 1)
    path = config_path.parent / "participation-product.toml"
    path.write_text(text, encoding="utf-8")
    return path


_LOCAL = "http://127.0.0.1:8773/v1/participation/decide"


def _participation_url(config) -> list[str]:
    return [item.value for item in config.settings_overlay if item.display == "participation.url"]


def test_a_host_that_runs_the_model_calls_it_locally(config_path: Path) -> None:
    config = load_config(_with_participation(config_path))
    assert config.model_endpoints == {"laya_smart_home": "", "laya_participation": _LOCAL}
    assert _participation_url(config) == [_LOCAL]
    with pytest.raises(ConfigurationError, match="runs it"):
        load_config(_with_participation(config_path),
                    model_endpoints={"laya_participation": "https://laya.example/v1/participation/decide"})


def test_a_host_that_does_not_is_told_the_remote_or_nothing(config_path: Path) -> None:
    product = _with_participation(config_path)
    nothing = load_config(product, capabilities=frozenset())
    assert nothing.model_endpoints == {"laya_smart_home": "", "laya_participation": ""}
    assert _participation_url(nothing) == []  # the Agent's own default, "", disables teams
    remote = "https://laya.example/v1/participation/decide"
    config = load_config(product, capabilities=frozenset(), model_endpoints={"laya_participation": remote})
    assert _participation_url(config) == [remote]
    # A loopback placeholder (or forwarder) is the same service at its own port.
    assert load_config(product, capabilities=frozenset(),
                       model_endpoints={"laya_participation": _LOCAL}).model_endpoints == {
        "laya_smart_home": "", "laya_participation": _LOCAL}


@pytest.mark.parametrize(
    ("url", "message"),
    [
        ("http://127.0.0.1:8771/v1/participation/decide", "port 8773"),
        ("http://laya.example/v1/participation/decide", "must be https"),
        ("https://laya.example/v1/systemone", "ending in"),
        ("https://user:pw@laya.example/v1/participation/decide", "absolute URL"),
    ],
)
def test_a_remote_endpoint_must_name_the_same_service(config_path: Path, url: str, message: str) -> None:
    with pytest.raises(ConfigurationError, match=message):
        load_config(_with_participation(config_path), capabilities=frozenset(),
                    model_endpoints={"laya_participation": url})


def test_unknown_tasks_and_hand_written_endpoints_are_refused(config_path: Path) -> None:
    product = _with_participation(config_path)
    with pytest.raises(ConfigurationError, match="unknown model tasks: laya_vision"):
        load_config(product, model_endpoints={"laya_vision": "https://x.example/v1"})
    text = product.read_text(encoding="utf-8") + (
        '\n[[settings.overlay]]\ndocument = "agent.yaml"\npath = "participation.url"\n'
        f'value = "{_LOCAL}"\n'
    )
    product.write_text(text, encoding="utf-8")
    with pytest.raises(ConfigurationError, match="laya_participation endpoint"):
        load_config(product)


_HOME = "http://127.0.0.1:8771"


def _repository_host(name: str):
    profile = load_host_profile(_HOSTS / f"{name}.toml")
    return load_config(profile.operations_config, capabilities=profile.capabilities,
                       model_endpoints=profile.model_endpoints,
                       settings_overlay=profile.settings_overlay)


def test_the_repository_hosts_reach_their_models_where_they_should() -> None:
    expected = {
        "rk3588": {"laya_smart_home": _HOME, "laya_participation": _LOCAL},
        "mac": {"laya_smart_home": _HOME, "laya_participation": ""},
        "pi5": {"laya_smart_home": _HOME, "laya_participation": _LOCAL},  # placeholders
        "pi5-device-management-hil": {"laya_smart_home": _HOME, "laya_participation": _LOCAL},
    }
    for name, endpoints in expected.items():
        config = _repository_host(name)
        assert config.model_endpoints == endpoints, name
        assert ("local_laya_participation" in config.capabilities) == (name == "rk3588"), name


def test_each_host_keeps_its_own_home_interpretation() -> None:
    # The choice moved from each Host's agent environment into its profile, unchanged: the Mac
    # source run uses Laya and records it; opi5max has used Laya with c4 continuations since
    # 2026-09-30 (three EIDOLON_SMARTHOME_* lines in its agent.env); the Pis use the rules.
    def smarthome(name: str) -> dict[str, str]:
        return {item.display: item.value for item in _repository_host(name).settings_overlay
                if item.display.startswith("smarthome.") and item.display != "smarthome.laya.url"}

    assert smarthome("mac") == {
        "smarthome.interpreter": "laya",
        "smarthome.interpretation_record_path": "$EIDOLON_STATE_ROOT/agent/smarthome/interpretation.jsonl",
        # hub.yaml: the Mac assembles the 周边好生活 adapter for the demo.
        "smarthome.providers": "virtual,zhoubian,homeassistant",
    }
    assert smarthome("rk3588") == {
        "smarthome.interpreter": "laya",
        "smarthome.laya.continuation": "true",
    }
    for name in ("pi5", "pi5-device-management-hil"):
        assert smarthome(name) == {}, name  # the Agent's default: rules, nothing recorded


def test_a_setting_is_written_by_the_product_or_the_host_not_both(config_path: Path) -> None:
    from eidolon_ops.config import settings_overlay_of

    product = _with_laya(config_path)
    product.write_text(product.read_text(encoding="utf-8") + (
        '\n[[settings.overlay]]\ndocument = "agent.yaml"\npath = "smarthome.interpreter"\nvalue = "rules"\n'
    ), encoding="utf-8")
    host = settings_overlay_of({"overlay": [
        {"document": "agent.yaml", "path": "smarthome.interpreter", "value": "laya"}]})
    with pytest.raises(ConfigurationError, match="both the operations config and the Host profile"):
        load_config(product, settings_overlay=host)
