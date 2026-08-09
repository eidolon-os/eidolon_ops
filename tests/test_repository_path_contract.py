from __future__ import annotations

from pathlib import Path

import pytest

REPOSITORY = Path(__file__).resolve().parents[1]


def _files(root: Path, patterns: tuple[str, ...]) -> list[Path]:
    result: set[Path] = set()
    for pattern in patterns:
        result.update(path for path in root.glob(pattern) if path.is_file())
    return sorted(result)


def test_ops_owns_mac_host_lifecycle_assets() -> None:
    required = (
        "deploy/dev/run_all.sh",
        "deploy/dev/supervisord.conf",
        "deploy/dev/supervisord.profile.conf",
        "deploy/livekit/livekit.yaml",
        "config/default-enabled.txt",
        "config/ports.yaml",
    )
    assert all((REPOSITORY / item).is_file() for item in required)


def test_product_runtime_assets_do_not_reintroduce_legacy_host_paths() -> None:
    patterns = ("src/**/*.py", "deploy/**/*", "config/**/*")
    forbidden = ("/srv/eidolon", "/Users/manson", "%(ENV_HOME)s/eidolon")
    violations: list[str] = []
    for path in _files(REPOSITORY, patterns):
        text = path.read_text(encoding="utf-8", errors="replace")
        for value in forbidden:
            if value in text:
                if value == "/srv/eidolon" and path.name in {
                    "target_agent.py",
                    "controller.py",
                }:
                    continue
                if value == "/Users/manson" and path.name.endswith(".local.yaml"):
                    continue
                violations.append(f"{path.relative_to(REPOSITORY)}: {value}")
    assert violations == []


def test_legacy_srv_namespace_is_cutover_evidence_and_cleanup_target_only() -> None:
    target = REPOSITORY / "src/eidolon_ops/target_agent.py"
    lines = [
        line.strip()
        for line in target.read_text(encoding="utf-8").splitlines()
        if "/srv/eidolon" in line
    ]
    assert lines == [
        'component_id: Path("/srv/eidolon/current") / component_id for component_id in CURRENT_LINKS',
        '_LEGACY_RELEASES = Path("/srv/eidolon/releases")',
        '_LEGACY_ROOT = Path("/srv/eidolon")',
    ]


def test_data_and_hub_consume_the_shared_state_root_contract() -> None:
    repositories = {
        "data": REPOSITORY.parent / "eidolon_data",
        "hub": REPOSITORY.parent / "eidolon_hub",
    }
    if not all(root.is_dir() for root in repositories.values()):
        pytest.skip("cross-repository path contract requires an Eidolon workspace")
    required = {
        "data": "$EIDOLON_STATE_ROOT/eidolon-system.sqlite3",
        "hub": "$EIDOLON_STATE_ROOT/hub/eidolon-hub.sqlite3",
    }
    if not all((root / "config/settings.yaml").is_file() for root in repositories.values()):
        pytest.skip("sibling Data/Hub checkouts are unavailable")
    settings = {name: root / "config/settings.yaml" for name, root in repositories.items()}
    assert {
        name: marker in path.read_text(encoding="utf-8")
        for name, (path, marker) in {
            name: (settings[name], required[name]) for name in repositories
        }.items()
    } == {"data": True, "hub": True}

    violations: list[str] = []
    for name, root in repositories.items():
        source_patterns = (
            "eidolon_data/**/*.py" if name == "data" else "hub/**/*.py",
            "config/**/*",
        )
        for path in _files(root, source_patterns):
            text = path.read_text(encoding="utf-8", errors="replace")
            for forbidden in ("/Users/manson", "/srv/eidolon"):
                if forbidden in text:
                    violations.append(f"{name}/{path.relative_to(root)}: {forbidden}")
    assert violations == []


def test_active_product_repositories_have_no_host_specific_runtime_paths() -> None:
    repositories = {
        "kernel": (REPOSITORY.parent / "eidolon_kernel", ("src/**/*", "deploy/**/*", "config/**/*")),
        "data": (REPOSITORY.parent / "eidolon_data", ("eidolon_data/**/*", "config/**/*")),
        "hub": (REPOSITORY.parent / "eidolon_hub", ("hub/**/*", "config/**/*")),
        "admin": (
            REPOSITORY.parent / "eidolon_admin",
            ("server/eidolon_admin_server/**/*", "deploy/**/*", "config/**/*"),
        ),
        "agent": (REPOSITORY.parent / "eidolon_agent", ("eidolon_agent/**/*", "config/**/*")),
        "channel": (REPOSITORY.parent / "eidolon_channel", ("eidolon/**/*", "config/**/*")),
        "memory": (REPOSITORY.parent / "eidolon_memory", ("eidolon/**/*", "config/**/*")),
        "sdk": (REPOSITORY.parent / "eidolon_sdk", ("eidolon_sdk/**/*", "config/**/*")),
    }
    if not all(root.is_dir() for root, _patterns in repositories.values()):
        pytest.skip("cross-repository path contract requires an Eidolon workspace")

    violations: list[str] = []
    for name, (root, patterns) in repositories.items():
        for path in _files(root, patterns):
            if path.suffix not in {
                ".conf",
                ".py",
                ".service",
                ".sh",
                ".socket",
                ".toml",
                ".yaml",
                ".yml",
            } or ".local." in path.name:
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
            for forbidden in ("/Users/manson", "/srv/eidolon", "%(ENV_HOME)s/eidolon"):
                if forbidden in text:
                    violations.append(f"{name}/{path.relative_to(root)}: {forbidden}")
    assert violations == []
