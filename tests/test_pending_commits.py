"""What this workstation holds that the Host is not running.

A release ships each checkout's HEAD as it stood when the run began, and several
people commit to these repositories all day. So a commit made during a deploy is
in the next release, and one made after it is on no Host at all — neither is
lost, and both look exactly like "my change is missing". Both halves of the
answer already existed: the Host records the commits every activation shipped,
and the checkouts know where they are now. Nothing subtracted them.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from eidolon_ops.controller import EidolonPiController
from eidolon_ops.process import ProcessResult

pytestmark = pytest.mark.unit

_SHIPPED = "a" * 40
_MOVED = "b" * 40


class _Git:
    """Answers only what the comparison asks: where HEAD is, and how far."""

    def __init__(
        self,
        heads: dict[str, str],
        counts: dict[str, str] | None = None,
        *,
        subjects: dict[str, list[str]] | None = None,
        dirty: dict[str, str] | None = None,
    ) -> None:
        self.heads = heads
        self.counts = counts or {}
        self.subjects = subjects or {}
        self.dirty = dirty or {}

    def run(self, command, **_kwargs):
        command = tuple(command)
        source = Path(command[command.index("-C") + 1]).name
        if command[-2:] == ("rev-parse", "HEAD"):
            head = self.heads.get(source)
            if head is None:
                return ProcessResult(1, "", "not a repository")
            return ProcessResult(0, head + "\n", "")
        if "rev-list" in command:
            answer = self.counts.get(source, "1")
            if answer is None:
                return ProcessResult(128, "", "bad revision")
            return ProcessResult(0, answer + "\n", "")
        if "log" in command:
            return ProcessResult(0, "\n".join(self.subjects.get(source, [])) + "\n", "")
        if "status" in command and "--porcelain" in command:
            return ProcessResult(0, self.dirty.get(source, ""), "")
        raise AssertionError(command)


def _controller(config, git: _Git) -> EidolonPiController:
    controller = object.__new__(EidolonPiController)
    controller.config = config
    controller.runner = git
    controller.git = "git"
    return controller


def _report(config, *, release_id: str, shipped: dict[str, str], links: str | None = None):
    linked = links or release_id
    return {
        "current_links": {
            source_id: f"/opt/eidolon/releases/{linked}/{source_id}"
            for source_id in config.sources
        },
        "release_sources": [
            {
                "release_id": release_id,
                "status": "activated",
                "sources": {
                    source_id: {"revision": revision, "branch": "main", "dirty": False}
                    for source_id, revision in shipped.items()
                },
            }
        ],
    }


def test_a_host_running_every_current_commit_has_nothing_pending(config) -> None:
    shipped = {source_id: _SHIPPED for source_id in config.sources}
    git = _Git({Path(source.path).name: _SHIPPED for source in config.sources.values()})

    answer = _controller(config, git)._pending_commits(
        _report(config, release_id="r1", shipped=shipped)
    )

    assert answer["active_release"] == "r1"
    assert answer["pending"] == {}
    assert answer["pending_commits"] == 0
    assert answer["uncommitted"] == {}
    assert "every source matches" in answer["detail"]


def test_a_source_committed_to_since_the_release_is_named_with_its_distance(
    config,
) -> None:
    shipped = {source_id: _SHIPPED for source_id in config.sources}
    heads = {Path(source.path).name: _SHIPPED for source in config.sources.values()}
    heads["eidolon_agent"] = _MOVED

    git = _Git(
        heads,
        {"eidolon_agent": "2"},
        subjects={"eidolon_agent": ["b08f1bc live turn handover", "0ebc134 floor from the host"]},
    )
    answer = _controller(config, git)._pending_commits(
        _report(config, release_id="r1", shipped=shipped)
    )

    assert set(answer["pending"]) == {"eidolon_agent"}
    assert answer["pending"]["eidolon_agent"] == {
        "shipped": _SHIPPED,
        "head": _MOVED,
        "commits": 2,
        # The count says how far; these say which. "2 commits behind" sends
        # someone to `git log`; naming them ends the question here.
        "subjects": ["b08f1bc live turn handover", "0ebc134 floor from the host"],
    }
    assert answer["pending_commits"] == 2
    assert answer["detail"] == "2 commit(s) in 1 source(s) are not on this Host"


def test_links_that_do_not_agree_are_reported_rather_than_guessed(config) -> None:
    """Mixed links mean a switch that did not complete, which is worth saying.

    Picking the majority would answer the question with the wrong release and
    then compare against it, which is how a diagnostic becomes the second bug.
    """

    report = _report(config, release_id="r1", shipped={})
    first, second = sorted(report["current_links"])[:2]
    report["current_links"][second] = report["current_links"][first].replace("/r1/", "/r2/")

    answer = _controller(config, _Git({}))._pending_commits(report)

    assert answer["active_release"] is None
    assert "nothing says what it runs" in answer["detail"]


def test_a_release_with_no_recorded_provenance_says_so(config) -> None:
    report = _report(config, release_id="r1", shipped={source: _SHIPPED for source in config.sources})
    report["release_sources"] = [{"release_id": "someone-else", "sources": {}}]

    answer = _controller(config, _Git({}))._pending_commits(report)

    assert answer["active_release"] == "r1"
    assert "no recorded provenance" in answer["detail"]
    assert "pending" not in answer


def test_a_shipped_commit_this_checkout_does_not_hold_is_unknown_not_zero(config) -> None:
    """The Host may be running a release built somewhere else.

    Reporting that as "nothing pending" would be a lie in the one direction that
    matters: it would say the board is current when nobody here can prove it.
    """

    shipped = {source_id: _SHIPPED for source_id in config.sources}
    heads = {Path(source.path).name: _MOVED for source in config.sources.values()}
    counts = {Path(source.path).name: None for source in config.sources.values()}

    answer = _controller(config, _Git(heads, counts))._pending_commits(
        _report(config, release_id="r1", shipped=shipped)
    )

    entry = answer["pending"]["eidolon_agent"]
    assert entry["commits"] is None
    assert entry["reason"] == "the shipped commit is not in this checkout"
    # An uncountable source contributes nothing to a total it cannot be part of.
    assert answer["pending_commits"] == 0


def test_a_source_the_release_never_carried_is_named_as_absent(config) -> None:
    heads = {Path(source.path).name: _SHIPPED for source in config.sources.values()}
    shipped = {source_id: _SHIPPED for source_id in config.sources}
    del shipped["eidolon_sdk"]

    answer = _controller(config, _Git(heads))._pending_commits(
        _report(config, release_id="r1", shipped=shipped)
    )

    assert answer["pending"]["eidolon_sdk"]["reason"] == "not in the release"


def test_uncommitted_work_is_a_separate_fact_from_being_behind(config) -> None:
    """Committed work is unsent; uncommitted work is unsendable.

    A release is sealed with `git archive` from a commit, so an edit nobody
    committed cannot travel at all — `deploy` refuses rather than shipping
    around it. Folding the two into one number would produce a count that means
    neither, which is the shape of mistake this whole command exists to undo.
    """

    shipped = {source_id: _SHIPPED for source_id in config.sources}
    heads = {Path(source.path).name: _SHIPPED for source in config.sources.values()}

    git = _Git(heads, dirty={"eidolon_hub": " M hub/composition/app.py\n?? scratch.py\n"})
    answer = _controller(config, git)._pending_commits(
        _report(config, release_id="r1", shipped=shipped)
    )

    # Not behind by a single commit, and still not reflecting what is here.
    assert answer["pending"] == {}
    assert answer["pending_commits"] == 0
    assert answer["uncommitted"] == {
        "eidolon_hub": {
            "paths": 2,
            "sample": ["M hub/composition/app.py", "?? scratch.py"],
        }
    }
    assert "no release can carry" in answer["detail"]


def test_a_long_list_is_elided_rather_than_allowed_to_bury_the_others(config) -> None:
    shipped = {source_id: _SHIPPED for source_id in config.sources}
    heads = {Path(source.path).name: _SHIPPED for source in config.sources.values()}
    heads["eidolon_admin"] = _MOVED
    many = [f"{index:07x} commit {index}" for index in range(9)]

    git = _Git(heads, {"eidolon_admin": "9"}, subjects={"eidolon_admin": many})
    answer = _controller(config, git)._pending_commits(
        _report(config, release_id="r1", shipped=shipped)
    )

    subjects = answer["pending"]["eidolon_admin"]["subjects"]
    assert len(subjects) == 6
    assert subjects[-1] == "…"
    assert answer["pending_commits"] == 9
