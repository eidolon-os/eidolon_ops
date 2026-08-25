"""What decides which commit of each source repository a run ships."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from eidolon_ops.config import SOURCE_IDS, OperationsConfig
from eidolon_ops.errors import OperationsError
from eidolon_ops.process import ProcessResult
from eidolon_ops.source_resolution import SourceResolver

pytestmark = pytest.mark.unit

HEADS = {source_id: f"{index:040x}" for index, source_id in enumerate(SOURCE_IDS, start=1)}


class FakeGit:
    """A git that answers only the questions resolution is allowed to ask."""

    def __init__(
        self,
        *,
        heads: dict[str, str] | None = None,
        dirty: dict[str, str] | None = None,
        branches: dict[str, str] | None = None,
        aliases: dict[str, str] | None = None,
        counts: dict[str, str] | None = None,
    ) -> None:
        self.heads = dict(heads or HEADS)
        self.dirty = dict(dirty or {})
        self.branches = dict(branches or {})
        self.aliases = dict(aliases or {})
        self.counts = dict(counts or {})
        self.calls: list[tuple[str, ...]] = []

    def source(self, command: tuple[str, ...]) -> str:
        return Path(command[command.index("-C") + 1]).name

    def run(self, command, **_kwargs):
        command = tuple(command)
        self.calls.append(command)
        source = self.source(command)
        if command[-2:] == ("rev-parse", "HEAD"):
            return ProcessResult(0, self.heads[source] + "\n", "")
        if command[-2:] == ("branch", "--show-current"):
            return ProcessResult(0, self.branches.get(source, "main") + "\n", "")
        if command[-2:] == ("status", "--porcelain"):
            return ProcessResult(0, self.dirty.get(source, ""), "")
        if "rev-list" in command:
            return ProcessResult(0, self.counts.get(source, "0\t0") + "\n", "")
        if "rev-parse" in command:
            requested = command[-1].removesuffix("^{commit}")
            return ProcessResult(0, self.aliases.get(requested, requested) + "\n", "")
        raise AssertionError(f"resolution asked git something unexpected: {command}")


def _resolver(config: OperationsConfig, git: FakeGit, **arguments) -> SourceResolver:
    return SourceResolver(config, git, **arguments)


def _pin(config: OperationsConfig, source_id: str, revision: str) -> OperationsConfig:
    return replace(
        config,
        sources={
            **config.sources,
            source_id: replace(config.sources[source_id], revision=revision),
        },
    )


def test_an_unpinned_source_ships_its_repository_head(config) -> None:
    git = FakeGit()

    resolved = _resolver(config, git).resolve()

    assert {source_id: item.revision for source_id, item in resolved.items()} == HEADS
    assert all(item.pinned is False for item in resolved.values())
    assert all(item.head == item.revision for item in resolved.values())
    # The commit HEAD resolved to is put through the same exact-commit-object
    # proof a written pin was: a release input is a commit, whatever named it.
    assert any(command[-1].endswith("^{commit}") for command in git.calls)


def test_head_that_is_not_a_full_commit_object_is_refused(config) -> None:
    git = FakeGit(heads={**HEADS, "eidolon_hub": "not-a-commit"})

    with pytest.raises(OperationsError, match="40-hex commit"):
        _resolver(config, git).resolve()


def test_head_that_is_not_the_exact_commit_object_is_refused(config) -> None:
    """`rev-parse HEAD` output must survive `^{commit}` verification too."""

    git = FakeGit(aliases={HEADS["eidolon_hub"]: "f" * 40})

    with pytest.raises(OperationsError, match="exact commit"):
        _resolver(config, git).resolve()


def test_a_written_revision_is_an_intentional_reproduction_pin(config) -> None:
    pinned = _pin(config, "eidolon_hub", "d" * 40)
    git = FakeGit(counts={"eidolon_hub": "0\t6"})

    resolver = _resolver(pinned, git)
    resolved = resolver.resolve()

    assert resolved["eidolon_hub"].revision == "d" * 40
    assert resolved["eidolon_hub"].head == HEADS["eidolon_hub"]
    assert resolved["eidolon_hub"].pinned is True
    assert resolved["eidolon_data"].pinned is False
    selection = resolver.selection()
    assert selection["mode"] == "reproduction"
    assert selection["reproduction"]["eidolon_hub"]["behind_head"] == 6
    # An operator who did not mean to reproduce anything has to be able to see
    # that they are: this is the state the release incident was invisible in.
    assert "reproduction" in str(selection["note"])
    assert "eidolon_hub" in str(selection["note"])


def test_head_selection_is_not_announced_as_a_reproduction(config) -> None:
    resolver = _resolver(config, FakeGit())

    assert resolver.selection() == {"mode": "repository_head"}


def test_a_dirty_worktree_is_refused_because_a_release_ships_commits(config) -> None:
    git = FakeGit(dirty={"eidolon_sdk": " M eidolon_sdk/session.py\n?? scratch.py\n"})
    resolver = _resolver(config, git)

    # Resolution itself still answers: a diagnosis must be able to report a
    # dirty repository rather than refuse to run because of one.
    assert resolver.resolve()["eidolon_sdk"].dirty is True
    with pytest.raises(OperationsError, match="eidolon_sdk") as failure:
        resolver.require_clean()
    assert "--allow-dirty" in str(failure.value)


def test_allow_dirty_ships_the_committed_head_and_records_the_dirt(config) -> None:
    git = FakeGit(dirty={"eidolon_sdk": " M eidolon_sdk/session.py\n"})
    resolver = _resolver(config, git, allow_dirty=True)

    resolver.require_clean()

    provenance = resolver.provenance()
    assert provenance["eidolon_sdk"]["dirty"] is True
    assert provenance["eidolon_sdk"]["revision"] == HEADS["eidolon_sdk"]
    assert set(provenance["eidolon_sdk"]) == {
        "revision",
        "head",
        "branch",
        "pinned",
        "dirty",
    }
    assert provenance["eidolon_hub"]["dirty"] is False
    assert provenance["eidolon_hub"]["branch"] == "main"


def test_a_detached_head_is_recorded_rather_than_refused(config) -> None:
    git = FakeGit(branches={"eidolon_hub": ""})

    assert _resolver(config, git).provenance()["eidolon_hub"]["branch"] is None


def test_resolution_answers_once_so_a_mid_run_commit_cannot_change_it(config) -> None:
    git = FakeGit()
    resolver = _resolver(config, git)

    first = resolver.revision("eidolon_hub")
    git.heads["eidolon_hub"] = "c" * 40
    second = resolver.revision("eidolon_hub")

    assert first == second == HEADS["eidolon_hub"]


def test_a_tag_must_name_the_commit_that_was_resolved(config) -> None:
    tagged = replace(
        config,
        sources={
            **config.sources,
            "eidolon_kernel": replace(config.sources["eidolon_kernel"], tag="kernel/v1"),
        },
    )
    resolving = FakeGit(aliases={"kernel/v1": HEADS["eidolon_kernel"]})
    moved = FakeGit(aliases={"kernel/v1": "e" * 40})

    assert _resolver(tagged, resolving).resolve()["eidolon_kernel"].tag == "kernel/v1"
    with pytest.raises(OperationsError, match="tag no longer names"):
        _resolver(tagged, moved).resolve()


def test_advance_from_a_previous_release_is_counted_per_source(config) -> None:
    git = FakeGit(counts={"eidolon_hub": "0\t6", "eidolon_sdk": "0\t1"})
    resolver = _resolver(config, git)
    previous = {
        "eidolon_hub": {"revision": "d" * 40},
        "eidolon_sdk": {"revision": "e" * 40},
        "eidolon_data": {"revision": HEADS["eidolon_data"]},
    }

    advance = resolver.advance_from(previous)

    assert advance == {"eidolon_hub": 6, "eidolon_sdk": 1}


def test_advance_from_nothing_comparable_is_absent_rather_than_invented(config) -> None:
    resolver = _resolver(config, FakeGit())

    assert resolver.advance_from({}) == {}
    assert resolver.advance_from({"eidolon_hub": {"revision": "nonsense"}}) == {}


def test_the_workstation_and_the_host_agree_on_what_provenance_is(config) -> None:
    """Two vocabularies for one record is how a record stops being comparable.

    The workstation writes these five facts and the Host validates and stores
    them; if the two sets ever drift, an audit is reading one shape while a
    deploy wrote another.
    """

    from eidolon_ops.hostagent.contract import SOURCE_PROVENANCE_KEYS

    resolved = _resolver(config, FakeGit()).provenance()

    assert all(set(record) == set(SOURCE_PROVENANCE_KEYS) for record in resolved.values())


def test_the_resolved_configuration_has_no_half_declared_source_left(config) -> None:
    """What the settings overlay and the input contract are handed.

    They read a release's commits off `config.sources`, and are entitled to a
    configuration where that is answered rather than optional.
    """

    resolved = _resolver(config, FakeGit()).resolved_config()

    assert {source_id: source.revision for source_id, source in resolved.sources.items()} == HEADS
    # The configuration it was derived from is left alone.
    assert all(source.revision is None for source in config.sources.values())


def test_the_refusal_separates_a_modified_file_from_an_unadded_one(config) -> None:
    """One line of advice fitted neither case.

    "Commit or stash them" is right for a tracked file whose changed version is
    not what ``git archive`` will take, and wrong for a file nobody added: that
    one is *missing* from the archive, and stashing it makes the omission
    permanent instead of visible.
    """

    git = FakeGit(
        dirty={"eidolon_sdk": " M eidolon_sdk/session.py\n?? eidolon_sdk/new_module.py\n"}
    )
    resolver = _resolver(config, git)

    item = resolver.resolve()["eidolon_sdk"]
    assert (item.modified_paths, item.untracked_paths) == (1, 1)

    with pytest.raises(OperationsError) as failure:
        resolver.require_clean()
    message = str(failure.value)
    assert "1 modified, 1 untracked" in message
    assert "commit or stash the modified files" in message
    assert "missing from the archive" in message


def test_only_modified_files_do_not_mention_adding_anything(config) -> None:
    git = FakeGit(dirty={"eidolon_sdk": " M eidolon_sdk/session.py\n"})
    resolver = _resolver(config, git)

    with pytest.raises(OperationsError) as failure:
        resolver.require_clean()
    message = str(failure.value)
    assert "1 modified" in message
    assert "untracked" not in message


def test_the_refusal_does_not_restate_the_activator_guard(config) -> None:
    """The repository holding the sealing tool is somebody else's check.

    ``ReleasePreflight._require_activator_checkout`` refuses any uncommitted
    change under ``eidolon_deploy`` on its own -- before this runs, and with no
    ``--allow-dirty`` to get past it -- and the digest beside it proves the
    activator being run is byte-identical to the one the shipped commit
    contains. A second, vaguer sentence here would be one more copy of a fact
    already held somewhere sharper, which is the thing this module exists to
    stop doing.
    """

    resolver = _resolver(
        config, FakeGit(dirty={"eidolon_kernel": " M eidolon_deploy/bundle.py\n"})
    )

    with pytest.raises(OperationsError) as failure:
        resolver.require_clean()
    message = str(failure.value)
    assert "eidolon_kernel (1 modified" in message
    assert "seal the release" not in message
