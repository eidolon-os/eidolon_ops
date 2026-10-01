"""The same source lifecycle and identity binding on every supported Unix Host."""

import configparser
from dataclasses import replace
from pathlib import Path

import pytest
from test_host_controller import Runner, _profile
from test_paths import _write_mac_profile

from eidolon_ops.component_contract import load_component_contract
from eidolon_ops.errors import OperationsError
from eidolon_ops.execution import ExecutionIdentity
from eidolon_ops.host import build_adapter
from eidolon_ops.paths import HostPlatform, HostProfileError, load_host_profile
from eidolon_ops.source_runtime import render_supervisor, source_services

EXECUTION = '''[execution]
transport = "local"
supervisor = "supervisord"
packages = "none"
identity = "current-user"
'''


@pytest.mark.parametrize("platform", list(HostPlatform))
def test_source_identity_paths_and_control_plane_are_independent_of_platform(tmp_path, platform):
    path = _write_mac_profile(tmp_path, script=tmp_path / "run.sh")
    path.write_text(path.read_text().replace('platform = "macos"', f'platform = "{platform}"')
                    .replace('driver = "local-supervisord"\n', '')
                    .replace('[paths]', EXECUTION + '\n[paths]'))
    profile = load_host_profile(path)
    assert profile.source_run
    assert profile.execution.identity is ExecutionIdentity.CURRENT_USER
    assert profile.paths.state_root == tmp_path / "state"
    root = Path(__file__).parents[2] / "eidolon_admin"
    services = source_services(load_component_contract(root, "eidolon_admin"), profile, root)
    workflow = next(service for service in services if service.unit_id == "eidolon-lifecycle-workflow")
    local = next(service for service in services if service.unit_id == "eidolon-local-api")
    assert workflow.environment["EIDOLON_LIFECYCLE_ALLOWED_LOCAL_API_USER"] == local.user
    assert len({service.user for service in services}) == 1
    adapter = build_adapter(profile, Runner())
    assert adapter.describe()["execution"] == profile.execution.describe()
    assert adapter.release is None


@pytest.mark.parametrize("replacement", [
    ('identity = "current-user"', 'identity = "service-accounts"'),
    ('transport = "local"', 'transport = "ssh"'),
    ('packages = "none"', 'packages = "apt"'),
])
def test_unimplemented_composition_fails_before_running_commands(tmp_path, replacement):
    path = _write_mac_profile(tmp_path, script=tmp_path / "run.sh")
    path.write_text(path.read_text().replace('driver = "local-supervisord"\n', '')
                    .replace('[paths]', EXECUTION.replace(*replacement) + '\n[paths]'))
    with pytest.raises(HostProfileError, match="no implemented lifecycle"):
        load_host_profile(path)


def test_legacy_alias_cannot_override_explicit_identity_policy(tmp_path):
    path = _write_mac_profile(tmp_path, script=tmp_path / "run.sh")
    path.write_text(path.read_text().replace('[paths]', EXECUTION + '\n[paths]')
                    .replace('driver = "local-supervisord"', 'driver = "ssh-systemd"'))
    with pytest.raises(HostProfileError, match="disagrees with execution"):
        load_host_profile(path)


@pytest.mark.parametrize("platform", [HostPlatform.MACOS, HostPlatform.LINUX])
@pytest.mark.parametrize("capabilities", [frozenset(), frozenset({"local_laya"})])
def test_source_model_and_discovery_binding_follow_capabilities_and_os(tmp_path, platform, capabilities):
    root = Path(__file__).parents[1]
    profile = replace(_profile(tmp_path), platform=platform)
    admin = root.parent / "eidolon_admin"
    controls = source_services(load_component_contract(admin, "eidolon_admin"), profile, admin)
    models = load_component_contract(root.parent / "eidolon_models", "eidolon_models")
    parser = configparser.ConfigParser(interpolation=None)
    parser.read_string(render_supervisor(
        (root / "deploy/supervisor/product-source.conf").read_text(), controls,
        controls[0].user, platform=platform, contracts=(models.select(capabilities),),
    ))
    assert ("program:laya" in parser) == ("local_laya" in capabilities)
    assert "program:hub-mdns" not in parser  # Hub owns its portable publisher.
    command = parser["program:local-api-mdns"]["command"]
    assert ("dns-sd" in command) == (platform is HostPlatform.MACOS)
    assert ("avahi-publish-service" in command) == (platform is HostPlatform.LINUX)


def test_source_refuses_selected_models_without_execution_bindings(tmp_path):
    root = Path(__file__).parents[1]
    models = load_component_contract(root.parent / "eidolon_models", "eidolon_models")
    with pytest.raises(OperationsError, match="execution binding for selected unit: eidolon-asr"):
        render_supervisor(
            (root / "deploy/supervisor/product-source.conf").read_text(), (), "operator",
            contracts=(models.select(frozenset({"local_asr"})),),
        )
