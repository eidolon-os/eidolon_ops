from __future__ import annotations

import ast
import configparser
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from eidolon_ops.hostagent.contract import (
    HOST_APPLICATION_INPUTS,
    INSTALL_INPUTS,
    LEGACY_SYSTEM_ASSETS,
    MANAGED_SYSTEM_ASSETS,
    PRODUCT_UNITS,
    SECRET_INPUTS,
)

KERNEL_ROOT = Path(__file__).resolve().parents[2] / "eidolon_kernel"

#: The Kernel commit whose release contract this repository has been reviewed
#: against.
#:
#: Deliberately a constant here and not a release input. It used to be read out
#: of ``config/eidolon-pi.example.toml``, which worked only while that file
#: declared the commits a release shipped; it no longer does, and following the
#: Kernel's HEAD instead would aim these assertions at a moving target — every
#: Kernel commit could turn this repository red for a change nobody here made.
#:
#: What this anchor buys is the opposite: a Kernel change that alters the unit,
#: secret or system-asset contract turns *one* test red, on purpose, when
#: somebody moves this line after reading the diff. Move it in the same commit
#: that adapts ``hostagent/contract.py`` to whatever changed, and never to
#: silence a failure without reading what moved.
REVIEWED_KERNEL_CONTRACT_COMMIT = "568684caf49520097eacd36561ec7f38b4428018"

pytestmark = pytest.mark.contract


@pytest.fixture(scope="module")
def kernel_contract() -> SimpleNamespace:
    if not (KERNEL_ROOT / ".git").exists():
        pytest.skip("sibling eidolon_kernel checkout is unavailable")
    revision = REVIEWED_KERNEL_CONTRACT_COMMIT
    result = subprocess.run(
        ["git", "-C", str(KERNEL_ROOT), "show", f"{revision}:eidolon_deploy/manifest.py"],
        check=True,
        capture_output=True,
        text=True,
    )
    assignments = {
        node.targets[0].id: node.value
        for node in ast.parse(result.stdout).body
        if isinstance(node, ast.Assign)
        and len(node.targets) == 1
        and isinstance(node.targets[0], ast.Name)
    }

    def exact(name: str):
        return eval(
            compile(ast.Expression(assignments[name]), "<pinned-manifest>", "eval"), {"Path": Path}
        )

    return SimpleNamespace(
        affected_units=exact("V2_AFFECTED_UNITS"),
        readiness=exact("V2_READINESS"),
        required_secrets=exact("V2_REQUIRED_SECRETS"),
        system_assets=exact("V2_SYSTEM_ASSETS"),
        revision=revision,
    )


def test_operator_topology_is_kernel_release_topology_plus_manager(kernel_contract) -> None:
    assert set(PRODUCT_UNITS) == set(kernel_contract.affected_units) | {"eidolond.service"}


def test_first_install_prerequisites_match_kernel_descriptor(kernel_contract) -> None:
    private = {
        destination for destination, _user, _group, mode in SECRET_INPUTS.values() if mode == 0o600
    }
    assert private == set(kernel_contract.required_secrets)
    assert {
        destination for destination, _user, _group, mode in SECRET_INPUTS.values() if mode == 0o640
    } == {
        Path("/etc/eidolon/agent.yaml"),
        Path("/etc/eidolon/channel.yaml"),
        Path("/etc/eidolon/memory.yaml"),
    }


def test_current_release_contract_counts_are_not_stale_document_counts(
    kernel_contract,
) -> None:
    assert len(kernel_contract.system_assets) == 25
    assert len(kernel_contract.required_secrets) == 11
    assert len(kernel_contract.affected_units) == 18
    assert len(kernel_contract.readiness) == 15


def test_reset_system_asset_allowlist_matches_kernel_release_contract(kernel_contract) -> None:
    # Stated as a union with the legacy set so the assertion holds across the
    # revision bump that drops a path from the release: what a reset must take
    # away is what any release this deployer can operate has ever installed.
    assert set(MANAGED_SYSTEM_ASSETS) == {
        *kernel_contract.system_assets,
        *(value[0] for value in HOST_APPLICATION_INPUTS.values()),
        *LEGACY_SYSTEM_ASSETS,
    }


def test_the_reviewed_kernel_contract_commit_exists_in_this_checkout() -> None:
    """The anchor has to name a real commit, or every assertion above is skipped.

    A constant that silently stops resolving — a rebase, a pruned branch — would
    turn this whole contract suite into an error nobody reads as a contract
    failure.
    """

    if not (KERNEL_ROOT / ".git").exists():
        pytest.skip("sibling eidolon_kernel checkout is unavailable")
    resolved = subprocess.run(
        [
            "git",
            "-C",
            str(KERNEL_ROOT),
            "rev-parse",
            "--verify",
            f"{REVIEWED_KERNEL_CONTRACT_COMMIT}^{{commit}}",
        ],
        capture_output=True,
        text=True,
    )
    assert resolved.returncode == 0, (
        "REVIEWED_KERNEL_CONTRACT_COMMIT does not exist in eidolon_kernel; point it at "
        "the commit whose release contract this repository has actually been read against"
    )
    assert resolved.stdout.strip() == REVIEWED_KERNEL_CONTRACT_COMMIT


def test_no_path_this_deployer_installs_is_also_declared_legacy() -> None:
    """A legacy path is removed on every refresh, so it must be nobody's input.

    Declaring a live path legacy would delete it on the same pass that just
    wrote it.
    """

    assert not set(LEGACY_SYSTEM_ASSETS) & {
        destination for destination, _user, _group, _mode in INSTALL_INPUTS.values()
    }


def test_data_v2_paths_are_fixed_in_systemd_assets(kernel_contract) -> None:
    text = subprocess.run(
        [
            "git",
            "-C",
            str(KERNEL_ROOT),
            "show",
            f"{kernel_contract.revision}:deploy/systemd/eidolon-data.service",
        ],
        check=True,
        capture_output=True,
        text=True,
    ).stdout

    assert "EIDOLON_DATA_SQLITE_PATH=/var/lib/eidolon/eidolon-system.sqlite3" in text
    assert "eidolon.sqlite3" not in text


@pytest.fixture(scope="module")
def pinned_system_services(kernel_contract) -> dict[str, str | None]:
    """Each service in Kernel's manifest and its supervisord target, as pinned.

    Read by hand rather than with a YAML parser: Ops runs on the Host with
    three dependencies and this test is not a reason to make it four. The shape
    is narrow and Kernel's own suite validates the file against its schema.
    """

    text = subprocess.run(
        [
            "git",
            "-C",
            str(KERNEL_ROOT),
            "show",
            f"{kernel_contract.revision}:config/system-services.yaml",
        ],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    services: dict[str, str | None] = {}
    current: str | None = None
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("- service_id:"):
            current = stripped.split(":", 1)[1].strip()
            services[current] = None
        elif stripped.startswith("supervisord:") and current is not None:
            services[current] = stripped.split(":", 1)[1].strip()
    assert services, "pinned manifest declared no services; the hand parse is wrong"
    return services


def _product_source_profile() -> tuple[dict[str, set[str]], dict[str, bool]]:
    parser = configparser.ConfigParser(interpolation=None, strict=False)
    parser.optionxform = str
    parser.read(Path(__file__).resolve().parents[1] / "deploy/supervisor/product-source.conf")
    groups = {
        section.split(":", 1)[1]: {
            name.strip() for name in parser[section]["programs"].split(",") if name.strip()
        }
        for section in parser.sections()
        if section.startswith("group:")
    }
    autostart = {
        section.split(":", 1)[1]: parser[section].getboolean("autostart", fallback=True)
        for section in parser.sections()
        if section.startswith("program:")
    }
    return groups, autostart


def _managed_programs(services: dict[str, str | None]) -> set[str]:
    return {
        target.split(":", 1)[1]
        for target in services.values()
        if target not in (None, "external")
    }


def test_kernel_supervisord_targets_name_programs_this_profile_actually_defines(
    pinned_system_services,
) -> None:
    groups, _autostart = _product_source_profile()

    for service_id, target in pinned_system_services.items():
        assert target is not None, (
            f"{service_id} has no supervisord target, so a macOS source run would "
            "not have that service at all. Say `external` if that is meant."
        )
        if target == "external":
            continue
        group, separator, program = target.partition(":")
        assert separator, f"{service_id} supervisord target must be group:program, got {target!r}"
        assert program in groups.get(group, set()), (
            f"{service_id} points at {target}, which product-source.conf does not define. "
            "Kernel names the target; this profile is what has to have it."
        )


def test_services_eidolond_manages_are_not_auto_started_behind_its_back(
    pinned_system_services,
) -> None:
    _groups, autostart = _product_source_profile()

    # supervisord is the executor here, not the authority: a program eidolond
    # reconciles must wait for eidolond to ask for it, or supervisord's own
    # autorestart quietly overrides a desired state of "off". The four
    # authorities have always been wired this way; the check exists so the rest
    # cannot arrive in the manifest without arriving here too.
    managed = _managed_programs(pinned_system_services)
    still_auto_started = sorted(name for name in managed if autostart.get(name, True))
    assert not still_auto_started, (
        f"eidolond reconciles {still_auto_started} but product-source.conf starts them "
        "itself. Set autostart=false so the manifest is the only thing that decides."
    )
    assert managed <= set(autostart), sorted(managed - set(autostart))


def test_programs_outside_the_service_manifest_are_named_rather_than_assumed(
    pinned_system_services,
) -> None:
    _groups, autostart = _product_source_profile()

    managed = _managed_programs(pinned_system_services)
    # Everything supervisord runs that eidolond does not: either it is not a
    # system service at all, or it is one eidolond has not been given yet. Both
    # are fine; being neither written down nor noticed is not.
    assert set(autostart) - managed == {
        "admin-api",
        "admin-web",
        "bootstrapd",
        "eidolond",
        "hub-ingress",
        "local-api",
        "local-api-mdns",
    }
