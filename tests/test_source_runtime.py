"""Source deployment uses the component's complete service/identity contract."""

import configparser
from dataclasses import replace
from pathlib import Path

import pytest
from test_local_product import _product

from eidolon_ops.component_contract import load_component_contract
from eidolon_ops.errors import OperationsError
from eidolon_ops.source_runtime import render_supervisor, source_services

ROOT = Path(__file__).resolve().parents[1]
ADMIN = ROOT.parent / "eidolon_admin"


def test_channel_processes_share_the_declared_host_settings_on_source_and_systemd(tmp_path):
    from eidolon_ops.source_runtime import service_values

    channel = ROOT.parent / "eidolon_channel"
    declaration = load_component_contract(channel, "eidolon_channel")
    profile = _product(tmp_path, foundation_mode="external").profile
    voice_input = next(i for i in declaration.inputs if i.name == "channel_settings")
    assert set(voice_input.required_by) == {"eidolon-channel", "eidolon-channel-provider"}
    # Channel still uses the source driver's worker declarations, unlike the
    # control services above. Check the actual rendered result, not an empty
    # source_services() collection that would make this test vacuously pass.
    parser = configparser.ConfigParser(defaults={
        "ENV_EIDOLON_PRODUCT_SETTINGS_ROOT": str(profile.paths.config_root / "settings"),
    })
    parser.read_string(render_supervisor(
        (ROOT / "deploy/supervisor/product-source.conf").read_text(), (), "test-operator",
        contracts=(declaration,),
    ))
    for unit_id, program in (
        ("eidolon-channel-provider", "channel-provider"),
        ("eidolon-channel", "channel-worker"),
    ):
        expected = str(profile.paths.config_root / "settings/channel.yaml")
        assert f'EIDOLON_CHANNEL_SETTINGS_YAML="{expected}"' in parser[f"program:{program}"]["environment"]
        provider_settings = str(profile.paths.config_root / "settings/channel-provider.yaml")
        assert f'EIDOLON_CHANNEL_PROVIDER_SETTINGS_YAML="{provider_settings}"' in parser[f"program:{program}"]["environment"]
        unit = ROOT.parent / "eidolon_kernel/deploy/systemd" / f"{unit_id}.service"
        assert "EIDOLON_CHANNEL_SETTINGS_YAML=/etc/eidolon/channel.yaml" in service_values(
            unit.read_text()
        )["Environment"]


def _services(tmp_path):
    declaration = load_component_contract(ADMIN, "eidolon_admin")
    return declaration, source_services(
        declaration, _product(tmp_path, foundation_mode="external").profile, ADMIN
    )


def test_every_source_control_service_runs_as_the_operator_with_native_peer_auth(tmp_path):
    declaration, services = _services(tmp_path)
    parser = configparser.ConfigParser(interpolation=None)
    parser.read_string(
        render_supervisor(
            (ROOT / "deploy/supervisor/product-source.conf").read_text(), services, services[0].user
        )
    )
    assert {s.unit_id for s in services} == set(declaration.unit_ids)
    for service in services:
        section = parser[f"program:{service.program}"]
        assert section["user"] == service.user
        assert ".venv/bin/" in section["command"]
    workflow = next(s for s in services if s.unit_id == "eidolon-lifecycle-workflow")
    local = next(s for s in services if s.unit_id == "eidolon-local-api")
    admin = next(s for s in services if s.unit_id == "eidolon-admin")
    assert parser[f"group:{workflow.group}"].getint("priority") < parser[
        f"group:{local.group}"
    ].getint("priority")
    assert parser[f"group:{admin.group}"].getint("priority") < parser[
        f"group:{workflow.group}"
    ].getint("priority")
    assert (
        workflow.environment["EIDOLON_LIFECYCLE_REMOVAL_CAPABILITY_SOCKET"]
        == admin.environment["EIDOLON_ADMIN_REMOVAL_CAPABILITY_SOCKET"]
    )
    assert parser["program:hub-api"]["user"] == services[0].user
    assert {s.declared_user for s in services} == {u["user"] for u in declaration.units}
    assert local.environment["EIDOLON_BOOTSTRAP_MODE"] == "development"
    assert workflow.environment["EIDOLON_LIFECYCLE_ALLOWED_LOCAL_API_USER"] == local.user
    assert admin.environment["EIDOLON_ADMIN_REMOVAL_CAPABILITY_WORKFLOW_USER"] == workflow.user
    assert parser["program:local-api"]["environment"].find('HOME="/var/empty"') == -1
    assert admin.environment["EIDOLON_PORTS_FILE"] == str(
        tmp_path / "product/config/settings/ports.yaml"
    )


def test_a_new_unbound_required_service_refuses_source_deployment(tmp_path):
    declaration, _ = _services(tmp_path)
    document = dict(declaration.document)
    document["units"] = [
        *document["units"],
        {
            "id": "eidolon-new-authority",
            "kind": "service",
            "exec": ".venv/bin/new",
            "user": "eidolon",
        },
    ]
    with pytest.raises(OperationsError, match="no source-host execution binding"):
        source_services(
            replace(declaration, document=document),
            _product(tmp_path / "new", foundation_mode="external").profile,
            ADMIN,
        )


def test_driver_template_cannot_redefine_a_component_process(tmp_path):
    _, services = _services(tmp_path)
    with pytest.raises(OperationsError, match="duplicate source runtime binding"):
        render_supervisor("[program:bootstrapd]\ncommand=wrong\n", services, "operator")



def test_source_composition_refuses_root(tmp_path, monkeypatch):
    from eidolon_ops import source_runtime

    monkeypatch.setattr(source_runtime.os, "geteuid", lambda: 0)
    with pytest.raises(OperationsError, match="non-root workspace owner"):
        _services(tmp_path)

def test_component_private_roots_stay_inside_backup_and_reset_roles(tmp_path):
    from eidolon_ops.source_assets import translate_fhs

    profile = _product(tmp_path, foundation_mode="external").profile
    assert translate_fhs(profile, "/var/lib/eidolon-lifecycle/lifecycle-workflows.sqlite3") == str(
        profile.paths.state_root / "lifecycle/lifecycle-workflows.sqlite3"
    )
    assert translate_fhs(profile, "/run/eidolon-removal-capability/broker.sock") == str(
        profile.paths.runtime_root / "removal-capability/broker.sock"
    )
    assert translate_fhs(profile, "/var/lib/eidolon-bootstrap/host_identity.ed25519") == str(
        profile.paths.bootstrap_state_root / "host_identity.ed25519"
    )



def test_source_roots_need_no_accounts_or_permission_commands(tmp_path, monkeypatch):
    import os
    import subprocess

    from eidolon_ops.source_state import prepare_roots

    profile = _product(tmp_path, foundation_mode="external").profile
    declaration = load_component_contract(ADMIN, "eidolon_admin")
    services = source_services(declaration, profile, ADMIN)
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: pytest.fail("source roots must use no subprocess"))
    monkeypatch.setattr(os, "chown", lambda *a, **k: pytest.fail("source roots must never chown"))
    assert prepare_roots(services, profile) == []
    assert prepare_roots(services, profile) == []
    for service in services:
        assert service.user != service.declared_user
        assert all(path.stat().st_uid == os.getuid() for path in (*service.state_paths, *service.runtime_paths))


def test_source_root_rejects_symlinks_and_unrelated_paths_before_mutation(tmp_path):
    from eidolon_ops.source_state import prepare_roots

    profile = _product(tmp_path, foundation_mode="external").profile
    declaration = load_component_contract(ADMIN, "eidolon_admin")
    services = source_services(declaration, profile, ADMIN)
    outside = tmp_path / "unrelated"
    outside.mkdir()
    escaped = replace(services[0], state_paths=(outside,))
    with pytest.raises(OperationsError, match="outside declared Host paths"):
        prepare_roots((escaped,), profile)
    linked = profile.paths.state_root / "linked"
    linked.parent.mkdir(parents=True)
    linked.symlink_to(outside, target_is_directory=True)
    escaped = replace(services[0], state_paths=(linked,))
    with pytest.raises(OperationsError, match="symlink"):
        prepare_roots((escaped,), profile)
    assert list(outside.iterdir()) == []

def test_shutdown_budget_covers_supervisors_ordered_group_timeouts(tmp_path):
    from eidolon_ops.source_runtime import shutdown_timeout

    profile = _product(tmp_path, foundation_mode="external").profile
    profile.paths.config_root.mkdir(parents=True)
    (profile.paths.config_root / "supervisor.conf").write_text(
        "[program:channel]\nstopwaitsecs=40\n[program:agent]\nstopwaitsecs=35\n"
    )
    assert shutdown_timeout(profile) > 40 + 35


def test_missing_dependency_and_cycle_refuse_before_supervisor_start(tmp_path):
    _, services = _services(tmp_path)
    template = (ROOT / "deploy/supervisor/product-source.conf").read_text()
    workflow = next(s for s in services if s.unit_id == "eidolon-lifecycle-workflow")
    with pytest.raises(OperationsError, match="no source-host binding for required dependency"):
        render_supervisor(
            template,
            tuple(
                replace(s, requires=("missing-authority",)) if s == workflow else s
                for s in services
            ),
            "operator",
        )
    with pytest.raises(OperationsError, match="dependency cycle"):
        render_supervisor(
            template,
            tuple(replace(s, requires=(s.unit_id,)) if s == workflow else s for s in services),
            "operator",
        )


def test_privileged_path_expansion_keeps_the_configuration_owner(tmp_path, monkeypatch):
    import pwd

    from eidolon_ops import operator_paths

    monkeypatch.setattr(operator_paths.os, "geteuid", lambda: 0)
    owner_home = Path(pwd.getpwuid(tmp_path.stat().st_uid).pw_dir)
    assert (
        operator_paths.expand_operator_path("~/host/state", tmp_path) == owner_home / "host/state"
    )
