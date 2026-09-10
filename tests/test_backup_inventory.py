from pathlib import Path

from eidolon_ops.config import load_config
from eidolon_ops.console.hosts import discover
from eidolon_ops.paths import load_host_profile


def test_archived_opi_profile_is_not_live_inventory_and_selects_its_own_config():
    root = Path(__file__).resolve().parents[1]
    archived = root / "config/backups/20260910/opi5max/host.toml"
    profile = load_host_profile(archived)
    assert profile.operations_config == archived.with_name("operations.toml")
    operations = load_config(profile.operations_config)
    assert {"local_asr", "local_llm", "local_tts"} <= operations.capabilities
    profiles = discover(root / "config/hosts")
    assert archived not in profiles
    assert not any("backup" in path.name for path in profiles)
    host_ids = [load_host_profile(path).host_id for path in profiles]
    assert host_ids.count("eidolon-opi5max") == 1
