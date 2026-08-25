from __future__ import annotations

import re
from pathlib import Path
from types import SimpleNamespace

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


def test_a_source_run_translates_every_role_in_the_product_layout() -> None:
    """The product layout and its translation are one table, not two.

    They were two. ``translate_fhs`` restated the layout as a list of literals
    and left out ``config_root``, so every Hub settings file a Mac source run
    generated kept pointing at ``/etc/eidolon/owner-domain`` — a directory that
    does not exist on a Mac and that Ops had already written the real material
    into somewhere else. Generation succeeded; Hub failed at startup on every
    single run. A role that a future template starts using must not be able to
    reintroduce that by being absent from a hand-written list.
    """

    from eidolon_ops import source_assets
    from eidolon_ops.paths import PRODUCT_PATHS, HostPaths

    host = HostPaths(
        install_root=Path("/w/install"),
        current_root=Path("/w/current"),
        config_root=Path("/w/config"),
        state_root=Path("/w/state"),
        runtime_root=Path("/w/runtime"),
        log_root=Path("/w/log"),
        cache_root=Path("/w/cache"),
        bootstrap_state_root=Path("/w/bootstrap-state"),
        bootstrap_runtime_root=Path("/w/bootstrap-runtime"),
    )
    profile = SimpleNamespace(paths=host)
    for role, product_path in PRODUCT_PATHS.items():
        expected = str(getattr(host, role))
        for written in (str(product_path), f"${source_assets._ENVIRONMENT_NAMES[role]}"):
            translated = source_assets.translate_fhs(profile, f"path: {written}/thing\n")
            assert translated == f"path: {expected}/thing\n", f"{role} via {written}"

    # Every role the layout names is a role a template may write, so the
    # variable table has to answer for all of them too.
    assert set(source_assets._ENVIRONMENT_NAMES) == set(PRODUCT_PATHS)


def test_a_nested_product_root_is_translated_before_its_parent() -> None:
    """``/var/lib/eidolon-bootstrap`` is not ``/var/lib/eidolon`` plus a suffix.

    Bootstrap keeps a separate state domain precisely so its ownership boundary
    survives a reset, and three of the nine roots are prefixes of another one.
    Replacing the parent first would silently relocate the child under it.
    """

    from eidolon_ops import source_assets
    from eidolon_ops.paths import HostPaths

    host = HostPaths(
        install_root=Path("/w/install"),
        current_root=Path("/w/install/current"),
        config_root=Path("/w/config"),
        state_root=Path("/w/state"),
        runtime_root=Path("/w/runtime"),
        log_root=Path("/w/log"),
        cache_root=Path("/w/cache"),
        bootstrap_state_root=Path("/w/bootstrap-state"),
        bootstrap_runtime_root=Path("/w/bootstrap-runtime"),
    )
    profile = SimpleNamespace(paths=host)
    translated = source_assets.translate_fhs(
        profile,
        "a: /var/lib/eidolon-bootstrap/b.sqlite3\n"
        "b: /run/eidolon-bootstrap/c.lock\n"
        "c: /opt/eidolon/current/d\n",
    )
    assert translated == (
        "a: /w/bootstrap-state/b.sqlite3\n"
        "b: /w/bootstrap-runtime/c.lock\n"
        "c: /w/install/current/d\n"
    )


def test_the_kept_dependency_cache_does_not_live_in_an_output_directory() -> None:
    """A cache is bound to the absolute path it was built at.

    ``bundle_root`` holds outputs and is allowed to be a temp directory the
    system sweeps. The kept uv cache is not an output — it exists to still be
    there next time — and every entry inside it points at the root it was built
    under, so moving it produces a cache full of dangling links rather than an
    empty one. That happened: a cache copied to a new bundle root carried 266
    links to the old absolute path, and the first symptom was a packaging error
    four hundred lines away. It belongs with the toolchain, which the profile is
    already required to place somewhere durable.
    """

    from eidolon_ops import release_bundle

    source = (REPOSITORY / "src/eidolon_ops/release_bundle.py").read_text(encoding="utf-8")
    assert "toolchain_root / _KEPT_DEPENDENCY_CACHE" in source
    assert "bundle_root / _KEPT_DEPENDENCY_CACHE" not in source
    # And it is not a dot entry any more: it no longer shares a directory with
    # release ids, so hiding it from a listing buys nothing.
    assert not release_bundle._KEPT_DEPENDENCY_CACHE.startswith(".")


def test_the_livekit_port_contract_is_written_once_per_host_and_they_agree() -> None:
    """Two hand-written copies of the same three ports, and nothing compared them.

    A source run renders LiveKit's config from this repository's template; a
    product Host renders it at each start from a launcher the Kernel release
    ships. Both spell out the signalling port, the TURN port and the RTC range,
    and a Host whose media ports disagree with the port registry is reachable
    for signalling and silent for audio -- which is exactly what a stale config
    on 17880 did.
    """

    from eidolon_ops import source_assets

    launcher = REPOSITORY.parent / "eidolon_kernel/deploy/systemd/eidolon-livekit-launch"
    if not launcher.is_file():
        pytest.skip("no sibling eidolon_kernel checkout to compare against")
    template = (source_assets.ASSETS / "livekit.yaml").read_text(encoding="utf-8")
    shipped = launcher.read_text(encoding="utf-8")
    for label, line in (
        ("signalling", f"port: {source_assets.PORTS['livekit']}"),
        ("turn", "udp_port: 3478"),
    ):
        assert line in template, f"{label} missing from the source-run template"
        assert line in shipped, f"{label} missing from the product launcher"
