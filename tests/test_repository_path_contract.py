from __future__ import annotations

import re
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
        # Templates Ops reads at run time live inside the package, so they are
        # found the same way from a checkout and from an installed wheel.
        "src/eidolon_ops/assets/livekit.yaml",
        "src/eidolon_ops/assets/ports.yaml",
        "config/default-enabled.txt",
    )
    assert all((REPOSITORY / item).is_file() for item in required)


def test_product_runtime_assets_do_not_reintroduce_legacy_host_paths() -> None:
    patterns = ("src/**/*.py", "deploy/**/*", "config/**/*")
    forbidden = ("/srv/eidolon", "/Users/manson", "%(ENV_HOME)s/eidolon")
    violations: list[str] = []
    for path in _files(REPOSITORY, patterns):
        # Operator-local, and gitignored for that reason: `config/eidolon-pi.toml`
        # and the host profiles beside it describe one workstation's view of one
        # Host. Naming a local path is what they are for. Everything else here
        # ships, and a workstation path in it is the defect this test names.
        if path == REPOSITORY / "config/eidolon-pi.toml":
            continue
        if path.parent == REPOSITORY / "config/hosts" and path.suffix == ".toml":
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        for value in forbidden:
            if value in text:
                if path.name == "release_matrix.py":
                    continue
                if value == "/srv/eidolon" and path.name in {
                    "target_agent.py",
                    "controller.py",
                }:
                    continue
                if value == "/Users/manson" and path.name.endswith(".local.yaml"):
                    continue
                violations.append(f"{path.relative_to(REPOSITORY)}: {value}")
    assert violations == []


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
        "kernel": (
            REPOSITORY.parent / "eidolon_kernel",
            ("src/**/*", "deploy/**/*", "config/**/*"),
        ),
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
            if (
                path.suffix
                not in {
                    ".conf",
                    ".py",
                    ".service",
                    ".sh",
                    ".socket",
                    ".toml",
                    ".yaml",
                    ".yml",
                }
                or ".local." in path.name
            ):
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
            for forbidden in ("/Users/manson", "/srv/eidolon", "%(ENV_HOME)s/eidolon"):
                if forbidden in text:
                    violations.append(f"{name}/{path.relative_to(root)}: {forbidden}")
    assert violations == []


def test_no_module_finds_its_data_by_walking_up_to_a_repository_root() -> None:
    """Ops reads assets from beside its code, never from a repository above it.

    ``Path(__file__).resolve().parents[N]`` is only the checkout root while Ops
    is being run out of a checkout. Installed as a wheel it points into
    site-packages, and the file that was found in development is simply absent
    — which is discovered at the moment an operator most needs the tool to
    work. There were thirteen of these; this keeps the count at zero.

    Walking up from a *configured* path is a different thing and stays
    allowed: a profile that declares its lifecycle script has told Ops where
    its workspace is, and deriving the workspace from that is reading the
    configuration rather than guessing from installation layout.
    """

    pattern = re.compile(r"Path\(__file__\)[^\n]*parents\[")
    violations = [
        f"{path.relative_to(REPOSITORY)}:{number}"
        for path in _files(REPOSITORY, ("src/**/*.py",))
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
        if pattern.search(line)
    ]

    assert violations == []


def test_every_asset_ops_reads_at_run_time_travels_with_the_package() -> None:
    from eidolon_ops import source_assets
    from eidolon_ops.component_contract import COMPONENT_CONTRACT_PATH
    from eidolon_ops.host_layer import _PORT_REGISTRY

    package = REPOSITORY / "src/eidolon_ops"
    for asset in (
        _PORT_REGISTRY,
        source_assets.ASSETS / "livekit.yaml",
        package / "contracts/component-ops/v1.schema.json",
    ):
        assert asset.is_file(), f"{asset} is read at run time but not present"
        assert asset.is_relative_to(package), f"{asset} would not ship with the wheel"

    # Not an Ops asset: this one is read out of each component's own checkout,
    # which is the point of it.
    assert not COMPONENT_CONTRACT_PATH.is_absolute()
