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


def _services(tmp_path):
    declaration = load_component_contract(ADMIN, "eidolon_admin")
    return declaration, source_services(
        declaration, _product(tmp_path, foundation_mode="external").profile, ADMIN
    )


def test_every_control_service_is_generated_with_its_native_identity(tmp_path):
    declaration, services = _services(tmp_path)
    parser = configparser.ConfigParser(interpolation=None)
    parser.read_string(
        render_supervisor(
            (ROOT / "deploy/supervisor/product-source.conf").read_text(), services, "operator"
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
    assert parser["program:hub-api"]["user"] == "operator"
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


def test_unprivileged_provisioning_fails_before_any_host_mutation(tmp_path, monkeypatch):
    from eidolon_ops import source_runtime

    _, services = _services(tmp_path)
    monkeypatch.setattr(source_runtime.os, "geteuid", lambda: 501)
    with pytest.raises(OperationsError, match="administrator privileges"):
        source_runtime.provision_source_identities(services)


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


def test_filesystem_adapter_preserves_declared_owners_and_runtime_modes(tmp_path, monkeypatch):
    import grp
    import pwd
    import subprocess
    from types import SimpleNamespace

    from eidolon_ops import source_runtime

    profile = _product(tmp_path, foundation_mode="external").profile
    declaration = load_component_contract(ADMIN, "eidolon_admin")
    services = source_services(declaration, profile, ADMIN)
    for root in (
        profile.paths.config_root,
        profile.paths.state_root,
        profile.paths.runtime_root,
        profile.paths.log_root,
        profile.paths.cache_root,
    ):
        root.mkdir(parents=True, exist_ok=True)
    profile.paths.config_root.chmod(0o700)
    inputs = profile.paths.config_root / "env"
    inputs.mkdir()
    for name in ("admin.env", "local-api.env", "bootstrap.env"):
        (inputs / name).write_text("")
    ports = profile.paths.config_root / "settings/ports.yaml"
    ports.parent.mkdir()
    ports.write_text("ports: {}\n")
    ports.chmod(0o600)
    owners = {
        user: index + 1000 for index, user in enumerate(("operator", *(s.user for s in services)))
    }
    owners["operator"] = source_runtime.os.getuid()
    monkeypatch.setattr(source_runtime.os, "geteuid", lambda: 0)
    monkeypatch.setattr(
        pwd, "getpwnam", lambda name: SimpleNamespace(pw_uid=owners[name], pw_gid=3000)
    )
    monkeypatch.setattr(grp, "getgrnam", lambda name: SimpleNamespace(gr_gid=3000))
    monkeypatch.setattr(source_runtime.os, "getgrouplist", lambda name, gid: [gid])
    ownership = []
    grants = []
    monkeypatch.setattr(
        source_runtime.os, "chown", lambda path, uid, gid: ownership.append((path, uid))
    )
    monkeypatch.setattr(subprocess, "run", lambda args, **kwargs: grants.append(args))
    for path in inputs.iterdir():
        path.chmod(0o600)
    source_runtime.provision_source_files(services, profile, "operator", initialize=True)
    workflow = next(s for s in services if s.unit_id == "eidolon-lifecycle-workflow")
    for path in workflow.runtime_paths:
        assert path.stat().st_mode & 0o777 == 0o750
        assert (path, owners[workflow.user]) in ownership
    # Local API may read its own input; it receives no read grant on Admin's issuer input.
    assert any(
        str(inputs / "local-api.env") == args[-1]
        and "user:eidolon-local-api allow read" in args[-2]
        for args in grants
    )
    assert not any(
        str(inputs / "admin.env") == args[-1] and "user:eidolon-local-api allow read" in args[-2]
        for args in grants
    )
    assert any(str(ports) == args[-1] and "user:eidolon allow read" in args[-2]
               for args in grants)
    search_grants = [args for args in grants if args[-2].endswith(" allow search")]
    assert search_grants
    assert all(not Path(args[-1]).stat().st_mode & 0o001 for args in search_grants)
    # Protected public system ancestors need no ACL mutation; private config
    # ancestors still receive the narrow traversal permission.
    assert not any(args[-1] == "/Users" for args in search_grants)
    assert any(args[-1] == str(profile.paths.config_root) for args in search_grants)
    # A normal input refresh never transfers ownership of existing state.
    before = list(ownership)
    source_runtime.provision_source_files(
        tuple(replace(s, state_paths=(), runtime_paths=()) for s in services), profile, "operator"
    )
    assert ownership == before
    # macOS ls can expose UUID principals rather than account names. Existing
    # equivalent grants must not grow on every normal input refresh.
    refresh_grants = []

    def existing_acl(args, **kwargs):
        if args[0] == "/usr/bin/dsmemberutil":
            return SimpleNamespace(stdout=f"UUID-{args[-1]}\n")
        if args[0] == "/bin/ls":
            return SimpleNamespace(stdout="\n".join(
                f" {index}: UUID-{s.user} allow search,read,readattr,readextattr,readsecurity"
                for index, s in enumerate(services)
            ))
        refresh_grants.append(args)
        return SimpleNamespace(stdout="")

    monkeypatch.setattr(subprocess, "run", existing_acl)
    source_runtime.provision_source_files(
        tuple(replace(s, state_paths=(), runtime_paths=()) for s in services), profile, "operator"
    )
    assert refresh_grants == []
    # Paths merely mentioned by an input cannot authorize unrelated private files.
    outside = tmp_path / "unrelated-private"
    outside.write_text("private")
    outside.chmod(0o600)
    escaped = replace(services[0], environment={**services[0].environment,
                       "EIDOLON_UNDECLARED_INPUT": str(outside)})
    before_grants = len(grants)
    with pytest.raises(OperationsError, match="outside declared Host roots"):
        source_runtime.provision_source_files((escaped,), profile, "operator", initialize=True)
    assert ownership == before
    assert len(grants) == before_grants


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
