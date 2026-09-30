"""Exercise the inventory against real repositories, including orphan worktrees."""

from __future__ import annotations

import importlib.util
import json
import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "repo_status.py"
spec = importlib.util.spec_from_file_location("repo_status", SCRIPT)
assert spec and spec.loader
status = importlib.util.module_from_spec(spec)
spec.loader.exec_module(status)


def git(path: Path, *args: str) -> str:
    return subprocess.check_output(["git", "-C", str(path), *args], text=True).strip()


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "empty-gitconfig"))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_AUTHOR_NAME", "Inventory test")
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", "inventory@example.invalid")
    monkeypatch.setenv("GIT_COMMITTER_NAME", "Inventory test")
    monkeypatch.setenv("GIT_COMMITTER_EMAIL", "inventory@example.invalid")
    path = tmp_path / "projects" / "sample"
    path.mkdir(parents=True)
    git(path, "init", "-b", "main")
    (path / "tracked.txt").write_text("base\n")
    git(path, "add", "tracked.txt")
    git(path, "commit", "-m", "base")
    return path


def test_discovery_includes_vendor_but_excludes_dependency_repos(repo):
    vendor = repo.parent / "vendor" / "sdk"
    vendor.mkdir(parents=True)
    git(vendor, "init", "-b", "main")
    nested = repo / "dependency"
    nested.mkdir()
    git(nested, "init", "-b", "main")
    cache = repo.parent / ".cache" / "repo"
    cache.mkdir(parents=True)
    git(cache, "init", "-b", "main")
    (repo.parent / "linked").symlink_to(repo, target_is_directory=True)
    assert status.discover(repo.parent) == [repo, vendor]


def test_staged_rename_newline_and_untracked_files_are_not_lost(repo):
    git(repo, "mv", "tracked.txt", "renamed 中文.txt")
    (repo / "line\nbreak.txt").write_text("pending")
    result = status.audit(repo, repo.parent)
    wt = result["worktrees"][0]
    assert wt["changes"] == [
        {"status": "R ", "path": "renamed 中文.txt", "original_path": "tracked.txt"},
        {"status": "??", "path": "line\nbreak.txt"},
    ]
    assert wt["ahead"] == wt["behind"] == 0
    report = {"root": str(repo.parent), "checked_at": "now", "repositories": [result]}
    assert r"line\nbreak.txt" in status.render(report, details=True)


def test_dirty_unmerged_and_missing_worktrees_remain_distinct(repo):
    other = repo.parent.parent / "outside-worktree"
    git(repo, "worktree", "add", "-b", "feature", str(other))
    (other / "tracked.txt").write_text("feature\n")
    git(other, "commit", "-am", "feature")
    (other / "pending.txt").write_text("uncommitted")
    wt = status.audit(repo, repo.parent)["worktrees"][1]
    assert wt["ahead"] == 1 and wt["behind"] == 0
    assert len(wt["changes"]) == 1 and not wt["stale"]
    shutil.rmtree(other)
    stale = status.audit(repo, repo.parent)["worktrees"][1]
    assert stale["stale"] and stale["changes"] is None
    assert stale["ahead"] == 1
    assert stale["preserved_refs"] == ["feature"]


def test_patch_equivalence_does_not_claim_reverted_code_is_merged(repo):
    git(repo, "checkout", "-b", "feature")
    (repo / "tracked.txt").write_text("feature\n")
    git(repo, "commit", "-am", "feature")
    feature = git(repo, "rev-parse", "HEAD")
    git(repo, "checkout", "main")
    (repo / "another.txt").write_text("main\n")
    git(repo, "add", "another.txt")
    git(repo, "commit", "-m", "main moved")
    git(repo, "cherry-pick", feature)
    git(repo, "revert", "--no-edit", "HEAD")
    git(repo, "checkout", "feature")
    result = status.audit(repo, repo.parent)
    wt = result["worktrees"][0]
    assert wt["ahead"] == 1 and wt["equivalent_patches"] == 1
    assert status.needs_attention(result)
    assert "未合入 1" in status.relation(wt, "main")


def test_detached_head_and_annotated_tag(repo):
    git(repo, "tag", "-a", "ed_v0.2.1", "-m", "release")
    git(repo, "checkout", "--detach")
    wt = status.audit(repo, repo.parent, tag="ed_v0.2.1")["worktrees"][0]
    assert wt["branch"] == "DETACHED" and wt["tag_matches"] is True
    assert wt["ahead"] == 0
    (repo / "tracked.txt").write_text("later\n")
    git(repo, "commit", "-am", "later")
    wt = status.audit(repo, repo.parent, tag="ed_v0.2.1")["worktrees"][0]
    assert wt["tag_matches"] is False and wt["ahead"] == 1


def test_missing_base_still_reports_dirty_files(repo):
    git(repo, "branch", "-m", "development")
    (repo / "new.txt").write_text("pending")
    result = status.audit(repo, repo.parent)
    assert result["errors"]
    assert len(result["worktrees"][0]["changes"]) == 1


def test_cli_json_check_exit_codes_and_read_only_refs(repo, capsys):
    refs = git(repo, "show-ref")
    assert status.main(["--root", str(repo.parent), "--json", "--check"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["schema_version"] == 1
    assert report["repositories"][0]["worktrees"][0]["changes"] == []
    (repo / "new.txt").write_text("pending")
    assert status.main(["--root", str(repo.parent), "--check"]) == 1
    capsys.readouterr()
    assert status.main(["--root", str(repo.parent), "--tag", "missing"]) == 2
    assert git(repo, "show-ref") == refs


def test_git_failure_is_reported_instead_of_a_clean_result(repo):
    broken = repo.parent / "broken"
    broken.mkdir()
    (broken / ".git").write_text("not a gitdir")
    result = status.audit(broken, repo.parent)
    assert result["errors"] and status.needs_attention(result)
