from pathlib import Path

import pytest
from test_install_inputs import _config_for_init, _settings_reader

from eidolon_ops.errors import InstallInputError
from eidolon_ops.host_identity import derive_host_lan_identity
from eidolon_ops.identity_replacement import replacement_inputs
from eidolon_ops.install_inputs import initialize_install_inputs, target_directory
from eidolon_ops.owner_domain_assets import ensure_owner_domain_assets


def snapshot(root):
    return {str(p.relative_to(root)): p.read_bytes() for p in root.rglob("*") if p.is_file()}


def established(config, tmp_path):
    configured = _config_for_init(config, tmp_path)
    initialize_install_inputs(configured, _settings_reader)
    target = target_directory(configured)
    identity = derive_host_lan_identity((target / "host_identity.ed25519").read_bytes())
    owner_root = target.parent / "owner-domain"
    owner = ensure_owner_domain_assets(owner_root, identity, 8443)
    return configured, target, owner_root, identity, owner


def test_preparation_failure_preserves_old_identity(config, tmp_path):
    configured, target, owner_root, _, _ = established(config, tmp_path)
    before = snapshot(target.parent)

    def broken_settings(*_args):
        raise InstallInputError("exact settings unavailable")

    with (
        pytest.raises(InstallInputError, match="exact settings unavailable"),
        replacement_inputs(configured, broken_settings, owner_root=owner_root, port=8443),
    ):
        pytest.fail("must not reach the destructive operation")
    assert snapshot(target.parent) == before


def test_failed_host_reset_discards_staged_identity(config, tmp_path):
    configured, target, owner_root, _, _ = established(config, tmp_path)
    before = snapshot(target.parent)
    with (
        pytest.raises(RuntimeError, match="Host reset refused"),
        replacement_inputs(configured, _settings_reader, owner_root=owner_root, port=8443),
    ):
        raise RuntimeError("Host reset refused")
    assert snapshot(target.parent) == before


def test_factory_reset_replaces_both_identities_and_keeps_retired_material(config, tmp_path):
    configured, target, owner_root, old_host, old_owner = established(config, tmp_path)
    old_inputs, old_material = snapshot(target), snapshot(owner_root)
    with replacement_inputs(
        configured, _settings_reader, owner_root=owner_root, port=8443
    ) as staged:
        assert snapshot(target) == old_inputs
        result = staged.commit()
    new_host = derive_host_lan_identity((target / "host_identity.ed25519").read_bytes())
    new_owner = ensure_owner_domain_assets(owner_root, new_host, 8443)
    assert new_host.host_id != old_host.host_id
    assert new_owner.owner_domain_id != old_owner.owner_domain_id
    assert new_owner.owner_domain_generation == 1
    assert (target.parent / "host_identity.ed25519").read_bytes() == (
        target / "host_identity.ed25519"
    ).read_bytes()
    retired = Path(result["retired_inputs"])
    assert snapshot(retired / "inputs") == old_inputs
    assert snapshot(retired / "owner-domain") == old_material
    assert retired.stat().st_mode & 0o777 == 0o700


def test_publish_failure_rolls_back_local_identity(config, tmp_path, monkeypatch):
    configured, target, owner_root, _, _ = established(config, tmp_path)
    before = snapshot(target.parent)
    rename = Path.rename
    with replacement_inputs(
        configured, _settings_reader, owner_root=owner_root, port=8443
    ) as staged:

        def fail_owner_publish(source, destination):
            if source == staged.staged_owner:
                raise OSError("publish failed")
            return rename(source, destination)

        monkeypatch.setattr(Path, "rename", fail_owner_publish)
        with pytest.raises(OSError, match="publish failed"):
            staged.commit()
    assert snapshot(target.parent) == before
