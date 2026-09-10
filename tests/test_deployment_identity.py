import json
import os
import sqlite3
import subprocess
from pathlib import Path

import pytest

from eidolon_ops.hostagent import (
    authority_reset,
    contract,
    deployment_identity,
    host_application,
    primitives,
)
from eidolon_ops.hostagent.primitives import TargetError


def test_configuration_gate_loads_the_staged_yaml_in_the_real_hub_interpreter(tmp_path, monkeypatch):
    hub = Path(__file__).resolve().parents[2] / "eidolon_hub"
    interpreter = hub / ".venv/bin/python"
    if not interpreter.is_file():
        pytest.skip("cross-repository Hub interpreter is not installed")
    settings = (hub / "config/settings.yaml").read_text()
    owner = "owner-" + "a" * 20
    settings = settings.replace("owner_domain_id: owner-local", "owner_domain_id: " + owner)
    settings = settings.replace("owner_domain_generation: 1", "owner_domain_generation: 8")
    staged = tmp_path / "hub.generated.yaml"
    staged.write_text(settings)
    calls = []
    def execute(_label, command, **kwargs):
        actual = [str(interpreter) if str(arg).endswith("/.venv/bin/python") else str(arg) for arg in command]
        calls.append(actual)
        subprocess.run(actual, check=True, capture_output=True, timeout=30)
    monkeypatch.setattr(primitives, "checked", execute)
    authority = {"owner_domain_id": owner, "owner_domain_generation": 8}
    host_application._validate_hub_settings_compatibility(tmp_path, "r1", authority)
    assert len(calls) == 2
    staged.write_text(settings.replace("owner_domain_generation: 8", "owner_domain_generation: 9"))
    with pytest.raises(subprocess.CalledProcessError):
        host_application._validate_hub_settings_compatibility(tmp_path, "r1", authority)


@pytest.fixture
def installed(tmp_path):
    lineage = {"contract_version": 1, "owner_domain_id": "owner-" + "a" * 20,
               "owner_domain_generation": 8, "state_id": "authority-state_original"}
    for name in deployment_identity.PRESERVED_INPUTS:
        path = tmp_path / contract.INSTALL_INPUTS[name][0].relative_to("/")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("original " + name)
    db = tmp_path / authority_reset.HUB_DATABASE.relative_to("/")
    db.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db) as connection:
        connection.execute("CREATE TABLE hub_authority_state(singleton_id INTEGER, owner_domain_id TEXT, owner_domain_generation INTEGER, state_id TEXT)")
        connection.execute("INSERT INTO hub_authority_state VALUES (1, ?, 8, ?)", (lineage["owner_domain_id"], lineage["state_id"]))
    (tmp_path / authority_reset.AUTHORITY_ANCHOR.relative_to("/")).write_text(json.dumps(lineage))
    (tmp_path / authority_reset.OWNER_DESCRIPTOR.relative_to("/")).write_text(json.dumps({
        **lineage, "descriptor_uri": "https://eidolon-hub-test.local:9443/api/device-onboarding/v1/descriptor"}))
    return tmp_path


def test_observation_requires_descriptor_database_and_anchor_to_agree(installed):
    observed = deployment_identity.observe({}, root=installed)
    assert observed["authority"]["owner_domain_generation"] == 8
    path = installed / authority_reset.OWNER_DESCRIPTOR.relative_to("/")
    descriptor = json.loads(path.read_text())
    descriptor["owner_domain_generation"] = 9
    path.write_text(json.dumps(descriptor))
    with pytest.raises(TargetError, match="different authorities"):
        deployment_identity.observe({}, root=installed)


def test_identity_drift_is_rejected_before_configuration_write(installed, monkeypatch):
    original = deployment_identity.observe({}, root=installed)
    path = installed / contract.INSTALL_INPUTS["hub.key"][0].relative_to("/")
    path.write_text("changed key")
    observed = deployment_identity.observe({}, root=installed)
    monkeypatch.setattr(deployment_identity, "observe", lambda _: observed)
    with pytest.raises(TargetError, match="identity changed"):
        host_application.refresh_release_configuration({"deployment_identity": original})


def test_release_updates_business_configuration_but_preserves_identity_and_secrets(installed, monkeypatch):
    observed = deployment_identity.observe({}, root=installed)
    monkeypatch.setattr(deployment_identity, "observe", lambda _: observed)
    stage_root = installed / "stage"
    stage = stage_root / "eidolon-secrets-r1"
    stage.mkdir(parents=True)
    for name in contract.RELEASE_CONFIGURATION_INPUTS:
        (stage / name).write_text("new " + name)
    # Even an extra staged identity is never selected by the release path.
    (stage / "hub.key").write_text("must not be installed")
    before = {name: (installed / contract.INSTALL_INPUTS[name][0].relative_to("/")).read_bytes()
              for name in deployment_identity.PRESERVED_INPUTS}
    monkeypatch.setattr(contract, "VAR_TMP", stage_root)
    monkeypatch.setattr(contract, "fixed_units", lambda _: ())
    monkeypatch.setattr(contract, "fixed_port_registry", lambda _: {})
    monkeypatch.setattr(contract, "declared_capabilities", lambda _: frozenset())
    monkeypatch.setattr(contract, "ensure_host_path_contract", lambda *_: None)
    monkeypatch.setattr(contract, "ensure_capability_service_groups", lambda *_a, **_k: None)
    monkeypatch.setattr(primitives, "host_path", lambda _root, path: installed / path.relative_to("/"))
    monkeypatch.setattr(primitives, "chown_path", lambda *_: None)
    monkeypatch.setattr(primitives, "checked", lambda *_a, **_k: None)
    monkeypatch.setattr(host_application, "_validate_hub_settings_compatibility", lambda *_: None)
    monkeypatch.setattr(host_application, "_validate_product_settings_compatibility", lambda *_: None)
    monkeypatch.setattr(host_application, "withdraw_hub_hostname", lambda: [])
    monkeypatch.setattr(host_application, "remove_legacy_system_assets", lambda: [])
    monkeypatch.setattr(host_application, "_expected_ids", lambda *_: (os.getuid(), os.getgid()))
    result = host_application.refresh_release_configuration({"release_id": "r1", "deployment_identity": observed})
    assert result["status"] == "refreshed"
    assert len(result["changed"]) == len(contract.RELEASE_CONFIGURATION_INPUTS)
    assert all((installed / contract.INSTALL_INPUTS[name][0].relative_to("/")).read_bytes() == data
               for name, data in before.items())
