"""A Host runs one forward-moving set of commits from main (M1, M3, M4, M5, M8).

Every case here is one way the Mac ran something nobody had chosen on
2026-10-30: commits on no branch, a selection that dropped another task's
work, processes importing two SDKs, and a profile edited by hand.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest

from eidolon_ops import release_set
from eidolon_ops.errors import OperationsError
from eidolon_ops.process import ProcessResult, SubprocessRunner
from eidolon_ops.source_resolution import SourceResolver

pytestmark = pytest.mark.unit

_IDENTITY = ("-c", "user.name=Test", "-c", "user.email=test@example.invalid")


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


def _repo(path: Path, *commits: str) -> list[str]:
    path.mkdir(parents=True)
    _git(path, "init", "-q", "-b", "main")
    made = []
    for name in commits:
        (path / f"{name}.txt").write_text(name)
        _git(path, "add", ".")
        _git(path, *_IDENTITY, "commit", "-qm", name)
        made.append(_git(path, "rev-parse", "HEAD"))
    return made


def _branch_commit(repo: Path, name: str) -> str:
    _git(repo, "checkout", "-q", "-b", f"feature/{name}")
    (repo / f"{name}.txt").write_text(name)
    _git(repo, "add", ".")
    _git(repo, *_IDENTITY, "commit", "-qm", name)
    commit = _git(repo, "rev-parse", "HEAD")
    _git(repo, "checkout", "-q", "main")
    return commit


def _resolver(config, repo: Path, revision: str, **arguments) -> SourceResolver:
    source = replace(config.sources["eidolon_hub"], path=repo, revision=revision)
    _git(repo, "checkout", "-q", "--detach", revision)
    return SourceResolver(
        replace(config, sources={"eidolon_hub": source}), SubprocessRunner(), **arguments
    )


# -- M1 ----------------------------------------------------------------------


def test_a_commit_on_main_may_run(config, tmp_path: Path) -> None:
    repo = tmp_path / "hub"
    _first, tip = _repo(repo, "a", "b")
    _resolver(config, repo, tip).require_on_trunk()


def test_a_commit_only_on_a_branch_is_refused(config, tmp_path: Path) -> None:
    """The recognition policy lived only on codex/conversation-person."""

    repo = tmp_path / "hub"
    _repo(repo, "a")
    feature = _branch_commit(repo, "policy")
    with pytest.raises(OperationsError, match=r"only commits on main.*eidolon_hub"):
        _resolver(config, repo, feature).require_on_trunk()


# -- M4 ----------------------------------------------------------------------


def test_moving_forward_from_the_previous_selection_is_allowed(config, tmp_path: Path) -> None:
    repo = tmp_path / "hub"
    before, after = _repo(repo, "a", "b")
    result = _resolver(config, repo, after).require_no_regression(
        {"eidolon_hub": before}, since="release r1"
    )
    assert result == {"status": "forward", "since": "release r1"}


def test_dropping_what_the_previous_selection_ran_is_refused(config, tmp_path: Path) -> None:
    """A selection made for one task removed another task's commits."""

    repo = tmp_path / "hub"
    older, newer = _repo(repo, "a", "b")
    with pytest.raises(OperationsError, match=r"drops commits that release r1 ran \(eidolon_hub -1\)"):
        _resolver(config, repo, older).require_no_regression(
            {"eidolon_hub": {"revision": newer}}, since="release r1"
        )


def test_a_sideways_selection_is_a_regression_too(config, tmp_path: Path) -> None:
    """c76777b's lineage held commits main did not, and main held commits it did not."""

    repo = tmp_path / "hub"
    _repo(repo, "a")
    one = _branch_commit(repo, "one")
    other = _branch_commit(repo, "other")
    with pytest.raises(OperationsError, match="eidolon_hub -1"):
        _resolver(config, repo, other).require_no_regression(
            {"eidolon_hub": one}, since="release r1"
        )


def test_going_back_on_purpose_is_recorded_with_its_reason(config, tmp_path: Path) -> None:
    repo = tmp_path / "hub"
    older, newer = _repo(repo, "a", "b")
    result = _resolver(
        config, repo, older, allow_regression="bisecting the 10-31 stutter"
    ).require_no_regression({"eidolon_hub": newer}, since="release r1")
    assert result == {
        "status": "regression_allowed",
        "since": "release r1",
        "dropped": {"eidolon_hub": 1},
        "reason": "bisecting the 10-31 stutter",
    }


# -- M3 ----------------------------------------------------------------------


def test_one_root_holds_every_source_and_the_ops_workspace(tmp_path: Path) -> None:
    root = tmp_path / "main-20261031-0123abcd"
    sources = {"eidolon_hub": root / "eidolon_hub", "eidolon_sdk": root / "eidolon_sdk"}
    assert release_set.require_one_root(sources, root / "eidolon_ops") == root


def test_sources_spread_over_task_directories_are_refused(tmp_path: Path) -> None:
    """Nine repositories out of five task directories, on 2026-10-30."""

    sources = {
        "eidolon_hub": tmp_path / "team-ux" / "eidolon_hub",
        "eidolon_sdk": tmp_path / "voice-selection" / "eidolon_sdk",
    }
    with pytest.raises(OperationsError, match="must sit in one root"):
        release_set.require_one_root(sources, tmp_path / "team-ux" / "eidolon_ops")


def test_data_from_a_development_checkout_is_not_in_the_root(tmp_path: Path) -> None:
    root = tmp_path / "sources" / "main-20261031-0123abcd"
    sources = {"eidolon_hub": root / "eidolon_hub", "eidolon_data": tmp_path / "eidolon_data"}
    with pytest.raises(OperationsError, match="eidolon_data="):
        release_set.require_one_root(sources, root / "eidolon_ops")


# -- M8 ----------------------------------------------------------------------


def test_the_profile_must_be_committed(tmp_path: Path) -> None:
    repo = tmp_path / "ops"
    _repo(repo, "a")
    profile = repo / "mac.toml"
    profile.write_text("x = 1\n")
    runner = SubprocessRunner()
    with pytest.raises(OperationsError, match="not committed"):
        release_set.require_committed_profile(runner, "git", profile)
    _git(repo, "add", "mac.toml")
    _git(repo, *_IDENTITY, "commit", "-qm", "profile")
    release_set.require_committed_profile(runner, "git", profile)
    profile.write_text("x = 2\n")
    with pytest.raises(OperationsError, match="uncommitted changes"):
        release_set.require_committed_profile(runner, "git", profile)


def test_the_ops_workspace_is_held_to_main_too(tmp_path: Path) -> None:
    repo = tmp_path / "eidolon_ops"
    _first, tip = _repo(repo, "a", "b")
    assert release_set.require_workspace_on_trunk(SubprocessRunner(), "git", repo) == tip
    feature = _branch_commit(repo, "lifecycle")
    _git(repo, "checkout", "-q", "--detach", feature)
    with pytest.raises(OperationsError, match="not on main"):
        release_set.require_workspace_on_trunk(SubprocessRunner(), "git", repo)


# -- M5 ----------------------------------------------------------------------


class _Interpreters:
    """Answers the import probe as each environment's interpreter would."""

    def __init__(self, answers: dict[str, dict[str, str | None]]) -> None:
        self.answers = answers

    def run(self, command, **_kwargs):
        python = str(command[0])
        for marker, origins in self.answers.items():
            if marker in python:
                return ProcessResult(0, json.dumps(origins) + "\n", "")
        raise AssertionError(f"unexpected interpreter {python}")


def _env(path: Path) -> None:
    python = path / ".venv/bin/python"
    python.parent.mkdir(parents=True)
    python.write_text("")


def test_every_environment_importing_the_roots_libraries_passes(tmp_path: Path) -> None:
    root = tmp_path / "main-20261031-0123abcd"
    sources = {name: root / name for name in ("eidolon_sdk", "eidolon_memory", "eidolon_data", "eidolon_agent")}
    for path in sources.values():
        _env(path)
    _env(root / "eidolon_ops")
    sdk = str(root / "eidolon_sdk/eidolon_sdk/__init__.py")
    runner = _Interpreters({
        "eidolon_agent": {
            "eidolon_sdk": sdk,
            "eidolon_memory_contracts": str(root / "eidolon_memory/contracts/eidolon_memory_contracts/__init__.py"),
            "eidolon_data": str(root / "eidolon_data/eidolon_data/__init__.py"),
        },
        "": {"eidolon_sdk": sdk, "eidolon_memory_contracts": None, "eidolon_data": None},
    })
    report = release_set.require_one_library_set(runner, sources, root / "eidolon_ops")
    assert report["eidolon_agent/.venv"]["eidolon_sdk"] == sdk
    assert set(report) == {f"{name}/.venv" for name in (*sources, "eidolon_ops")}


def test_an_environment_importing_another_trees_sdk_is_refused(tmp_path: Path) -> None:
    """Agent/Hub/Kernel imported 24bba53 from team-ux; Channel imported f40c19f."""

    root = tmp_path / "main-20261031-0123abcd"
    sources = {name: root / name for name in ("eidolon_sdk", "eidolon_agent")}
    for path in sources.values():
        _env(path)
    elsewhere = str(tmp_path / "team-ux-20261030/eidolon_sdk/eidolon_sdk/__init__.py")
    runner = _Interpreters({
        "eidolon_agent": {"eidolon_sdk": elsewhere},
        "": {"eidolon_sdk": str(root / "eidolon_sdk/eidolon_sdk/__init__.py")},
    })
    with pytest.raises(OperationsError, match=r"eidolon_agent/\.venv imports eidolon_sdk from .*team-ux"):
        release_set.require_one_library_set(runner, sources, root / "eidolon_ops")


def test_nested_environments_are_probed_as_well(tmp_path: Path) -> None:
    root = tmp_path / "main-20261031-0123abcd"
    models = root / "eidolon_models"
    _env(models)
    _env(models / "laya")
    assert release_set.environments("eidolon_models", models) == (
        models / ".venv", models / "laya" / ".venv",
    )


# -- the record and the one writer ---------------------------------------------


def test_the_record_round_trips(tmp_path: Path) -> None:
    assert release_set.read_record(tmp_path) is None
    release_set.write_record(
        tmp_path, root=tmp_path / "main-x", sources={"eidolon_hub": "a" * 40},
        workspace="b" * 40, regression={"status": "forward"},
    )
    record = release_set.read_record(tmp_path)
    assert record is not None
    assert record["sources"] == {"eidolon_hub": "a" * 40}
    assert record["eidolon_ops"] == "b" * 40
    assert (release_set.record_path(tmp_path).stat().st_mode & 0o777) == 0o600


class _GitButNoUv(SubprocessRunner):
    def __init__(self) -> None:
        self.synced: list[tuple[tuple[str, ...], Path | None]] = []

    def run(self, command, **kwargs):
        if tuple(command[:2]) == ("uv", "sync"):
            self.synced.append((tuple(command), kwargs.get("cwd")))
            return ProcessResult(0, "", "")
        return super().run(command, **kwargs)


def _selection(tmp_path: Path):
    repos = {}
    tips = {}
    for name in ("eidolon_hub", "eidolon_ops"):
        repos[name] = tmp_path / "checkouts" / name
        tips[name] = _repo(repos[name], "a", "b")
    ops = repos["eidolon_ops"]
    profile = ops / "config/hosts/mac.toml"
    profile.parent.mkdir(parents=True)
    profile.write_text(
        '[adapter]\nlifecycle_script = "old/eidolon_ops/deploy/dev/run_all.sh"\n\n'
        f'[source_overrides.eidolon_hub]\npath = "old/eidolon_hub"\nrevision = "{tips["eidolon_hub"][0]}"\n'
    )
    _git(ops, "add", ".")
    _git(ops, *_IDENTITY, "commit", "-qm", "profile")
    runner = _GitButNoUv()
    selection = release_set.ReleaseSetSelection(
        profile=profile, repositories=repos, sources_root=tmp_path / "sources", runner=runner,
    )
    return selection, repos, tips, runner


def test_a_release_set_is_main_built_once_and_committed(tmp_path: Path) -> None:
    selection, repos, tips, runner = _selection(tmp_path)
    plan = selection.plan()
    assert plan["revisions"] == {"eidolon_hub": tips["eidolon_hub"][1], "eidolon_ops": _git(repos["eidolon_ops"], "rev-parse", "main")}
    assert plan["regression"] == {"status": "no_previous_set"}
    built = selection.apply(plan)
    root = Path(str(plan["root"]))
    assert sorted(built["created"]) == ["eidolon_hub", "eidolon_ops"]
    assert _git(root / "eidolon_hub", "rev-parse", "HEAD") == tips["eidolon_hub"][1]
    assert ("uv", "sync", "--frozen", "--extra", "dev") in [c for c, _ in runner.synced]
    assert selection.write_profile(plan) == "committed"
    text = selection.profile.read_text()
    assert f'path = "../../../../sources/{root.name}/eidolon_hub"' in text
    assert f'revision = "{tips["eidolon_hub"][1]}"' in text
    assert f'lifecycle_script = "../../../../sources/{root.name}/eidolon_ops/deploy/dev/run_all.sh"' in text
    release_set.require_committed_profile(SubprocessRunner(), "git", selection.profile)
    # Committing the profile advanced Ops main, and that alone is not a new set:
    # selecting again finds the same root and changes nothing.
    assert _git(repos["eidolon_ops"], "rev-parse", "main") != plan["revisions"]["eidolon_ops"]
    again = selection.plan(current=plan["revisions"])
    assert again["root"] == plan["root"] and again["exists"] is True
    assert selection.apply(again)["created"] == []
    assert selection.write_profile(again) == "unchanged"


def test_a_release_set_refuses_a_pin_off_main(tmp_path: Path) -> None:
    selection, repos, _tips, _runner = _selection(tmp_path)
    feature = _branch_commit(repos["eidolon_hub"], "unmerged")
    with pytest.raises(OperationsError, match="eidolon_hub .* is not on main"):
        selection.plan({"eidolon_hub": feature})


def test_a_release_set_refuses_to_drop_what_the_host_last_prepared(tmp_path: Path) -> None:
    selection, _repos, tips, _runner = _selection(tmp_path)
    previous = {"root": "earlier", "sources": {"eidolon_hub": tips["eidolon_hub"][1]}}
    with pytest.raises(OperationsError, match=r"drops commits .*eidolon_hub -1"):
        selection.plan({"eidolon_hub": tips["eidolon_hub"][0]}, previous=previous)
    allowed = selection.plan(
        {"eidolon_hub": tips["eidolon_hub"][0]}, previous=previous, allow_regression="bisect",
    )
    assert allowed["regression"]["status"] == "regression_allowed"


def test_a_release_root_is_never_moved(tmp_path: Path) -> None:
    selection, _repos, tips, _runner = _selection(tmp_path)
    plan = selection.plan()
    selection.apply(plan)
    moved = {**plan, "revisions": {**plan["revisions"], "eidolon_hub": tips["eidolon_hub"][0]}}
    with pytest.raises(OperationsError, match="never moved"):
        selection.apply(moved)


# -- a source run, end to end --------------------------------------------------


def _source_run(tmp_path: Path):
    """A Mac profile whose sources, Ops checkout and committed profile are real."""

    from test_local_product import _FakeConfig, _FakeSource, _product

    from eidolon_ops.local_product import LocalProductSource

    root = tmp_path / "sources" / "main-20261031-0123abcd"
    tips = {}
    for name in ("eidolon_hub", "eidolon_sdk"):
        tips[name] = _repo(root / name, "a", "b")
    ops = root / "eidolon_ops"
    _repo(ops, "a")
    script = ops / "deploy/dev/run_all.sh"
    script.parent.mkdir(parents=True)
    script.write_text("#!/bin/sh\n")
    profile_path = ops / "config/hosts/mac.toml"
    profile_path.parent.mkdir(parents=True)
    profile_path.write_text("[host]\nid = 'mac-test'\n")
    _git(ops, "add", ".")
    _git(ops, *_IDENTITY, "commit", "-qm", "profile")
    base = _product(tmp_path, foundation_mode="external").profile
    profile = replace(base, path=profile_path, lifecycle_script=script)
    config = _FakeConfig(sources={
        name: _FakeSource(path=root / name, revision=tips[name][1]) for name in tips
    })
    product = LocalProductSource(profile, config, SubprocessRunner())  # type: ignore[arg-type]
    return product, root, tips


def test_a_committed_single_root_from_main_is_a_release_set(tmp_path: Path) -> None:
    product, root, _tips = _source_run(tmp_path)
    release = product.require_release_set()
    assert release["root"] == str(root)
    assert release["eidolon_ops"] == _git(root / "eidolon_ops", "rev-parse", "HEAD")


def test_a_source_run_refuses_to_drop_what_it_last_prepared(tmp_path: Path) -> None:
    product, root, tips = _source_run(tmp_path)
    release_set.write_record(
        product.profile.paths.state_root, root=root,
        sources={"eidolon_hub": tips["eidolon_hub"][1], "eidolon_sdk": tips["eidolon_sdk"][1]},
        workspace=str(product.require_release_set()["eidolon_ops"]), regression={},
    )
    assert product._require_forward()["status"] == "forward"
    # Hub goes back one commit, as c76777b did to the speaker artifact.
    older = tips["eidolon_hub"][0]
    _git(root / "eidolon_hub", "checkout", "-q", "--detach", older)
    product.config = replace(product.config, sources={
        **product.config.sources,
        "eidolon_hub": replace(product.config.sources["eidolon_hub"], revision=older),
    })
    product._sources = None
    with pytest.raises(OperationsError, match="eidolon_hub -1"):
        product._require_forward()
    product.allow_regression = "reproduce 10-30"
    product._sources = None
    assert product._require_forward()["reason"] == "reproduce 10-30"


def test_an_uncommitted_profile_stops_a_source_run(tmp_path: Path) -> None:
    product, _root, _tips = _source_run(tmp_path)
    product.profile.path.write_text("[host]\nid = 'edited'\n")
    with pytest.raises(OperationsError, match="uncommitted changes"):
        product.require_release_set()
