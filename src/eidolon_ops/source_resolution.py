"""Decide, once per run, which commit of each source repository is shipped.

A release used to be defined by a list of commits written into the operator
profile beside the repository paths. That made every source fact exist twice —
in the repository and in a file a person had to remember to edit — and two
hand-maintained copies of one fact are only ever accidentally equal. They were
not: a cross-repository change was written and tested on every HEAD, ``deploy``
reported success, and the board went on running the commits in the file. The
feature was never live, and the three deploys that followed failed for the same
reason, reporting only ``readiness timeout: hub, kernel, local-api``.

The fix is not to require the two copies to agree — that keeps the duplicate and
turns it into a prompt to edit a file, which is a tax with no information in it.
The fix is that there is one copy: the repository. This module reads it.

A written ``revision`` (or ``--revision``) therefore no longer declares the
release; it *departs* from it, for the one case where departing is meaningful —
reproducing a combination that already shipped. That is announced rather than
assumed, because an operator who does not know they are reproducing something is
in exactly the state the incident above was invisible in.

What is refused here is the one thing that really is an invariant about a single
copy: a dirty worktree. A release is built from commits, so a working tree with
uncommitted changes means the thing that was tested is not the thing that ships.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from types import MappingProxyType

from eidolon_ops.config import SOURCE_IDS, OperationsConfig
from eidolon_ops.errors import OperationsError
from eidolon_ops.process import ProcessRunner, checked

_COMMIT = re.compile(r"^[0-9a-f]{40}$")

#: How much of a dirty worktree is worth naming in a refusal. Enough to
#: recognize what was forgotten, bounded so a large refactor cannot turn one
#: error line into a screenful.
_DIRTY_SAMPLE = 3


@dataclass(frozen=True, slots=True)
class ResolvedSource:
    """One source repository, as this run found it."""

    source_id: str
    path: Path
    #: The commit that ships.
    revision: str
    #: The commit the repository is on, which is normally the same thing.
    head: str
    #: ``None`` for a detached HEAD. Recorded, never required: which branch a
    #: commit was reached from does not change what the commit contains.
    branch: str | None
    pinned: bool
    dirty: bool
    dirty_paths: int = 0
    dirty_sample: tuple[str, ...] = ()
    tag: str | None = None
    #: Commits reachable from HEAD but not from the shipped revision, and the
    #: reverse. Both zero unless a pin was used.
    behind_head: int = 0
    ahead_of_head: int = 0

    @property
    def provenance(self) -> dict[str, object]:
        """The five facts every later question about this release asks.

        Written into Host-side evidence, so "which commits were in the release
        from three weeks ago" is answerable at all. It was not: the only
        artifact that carried commits lived in the release directory, which the
        commit phase deletes.
        """

        return {
            "revision": self.revision,
            "head": self.head,
            "branch": self.branch,
            "pinned": self.pinned,
            "dirty": self.dirty,
        }


class SourceResolver:
    """The one place a source's shipped commit is decided.

    Answers are memoized for the life of the object, which is the life of one
    operation: a commit landing in a sibling repository while a bundle is being
    sealed must not change what the bundle contains half way through.
    """

    def __init__(
        self,
        config: OperationsConfig,
        runner: ProcessRunner,
        *,
        git: str = "git",
        allow_dirty: bool = False,
    ) -> None:
        self.config = config
        self.runner = runner
        self.git = git
        self.allow_dirty = allow_dirty
        self._resolved: Mapping[str, ResolvedSource] | None = None
        self._resolved_config: OperationsConfig | None = None

    # -- resolution ----------------------------------------------------------

    def resolve(self) -> Mapping[str, ResolvedSource]:
        if self._resolved is None:
            self._resolved = MappingProxyType(
                {source_id: self._resolve_one(source_id) for source_id in self.source_ids}
            )
        return self._resolved

    @property
    def source_ids(self) -> tuple[str, ...]:
        """The sources this configuration actually declares.

        Not ``SOURCE_IDS``: the macOS source-run path composes a narrower set,
        and resolution has no reason to insist on a topology it is only reading.
        """

        declared = set(self.config.sources)
        return tuple(source_id for source_id in SOURCE_IDS if source_id in declared) + tuple(
            sorted(declared.difference(SOURCE_IDS))
        )

    def revision(self, source_id: str) -> str:
        return self.resolve()[source_id].revision

    def revisions(self) -> dict[str, str]:
        return {source_id: item.revision for source_id, item in self.resolve().items()}

    def provenance(self) -> dict[str, dict[str, object]]:
        return {source_id: item.provenance for source_id, item in self.resolve().items()}

    def resolved_config(self) -> OperationsConfig:
        """The same configuration with every shipped commit filled in.

        For the modules that legitimately want a configuration rather than a
        resolver — the settings overlay, the install-input contract — and would
        otherwise have to learn where a revision comes from. They already take
        the release's commits from ``config.sources``; this is that same
        configuration, no longer half-declared.
        """

        if self._resolved_config is None:
            resolved = self.resolve()
            self._resolved_config = replace(
                self.config,
                sources=MappingProxyType(
                    {
                        source_id: replace(source, revision=resolved[source_id].revision)
                        for source_id, source in self.config.sources.items()
                    }
                ),
            )
        return self._resolved_config

    def selection(self) -> dict[str, object]:
        """Say out loud when this run is not shipping what the repositories hold."""

        reproduced = {
            source_id: {
                "revision": item.revision,
                "head": item.head,
                "behind_head": item.behind_head,
                "ahead_of_head": item.ahead_of_head,
            }
            for source_id, item in self.resolve().items()
            if item.pinned
        }
        if not reproduced:
            return {"mode": "repository_head"}
        detail = ", ".join(
            f"{source_id} ships {values['revision'][:12]}, "
            f"{values['behind_head']} behind HEAD"
            for source_id, values in sorted(reproduced.items())
        )
        return {
            "mode": "reproduction",
            "reproduction": reproduced,
            "note": (
                "reproduction deployment: an explicit commit was given for "
                f"{len(reproduced)} of {len(self.resolve())} sources, so this release is "
                f"not what the repositories currently hold ({detail}). Nothing was "
                "written back to any file; drop the pin to ship HEAD again."
            ),
        }

    # -- the one invariant worth refusing ------------------------------------

    def require_clean(self) -> None:
        """Refuse to ship out of a worktree that has uncommitted changes.

        This is not the old "HEAD must equal the pin" check wearing new clothes.
        That one compared two copies of a declaration; this one is a property of
        the single copy: a release is sealed with ``git archive``, so whatever is
        uncommitted is not in it, and the tree that was tested is not the tree
        that ships.
        """

        dirty = [item for item in self.resolve().values() if item.dirty]
        if not dirty or self.allow_dirty:
            return
        detail = "; ".join(
            f"{item.source_id} ({item.dirty_paths} "
            + ("path" if item.dirty_paths == 1 else "paths")
            + ": "
            + ", ".join(item.dirty_sample)
            + (", ..." if item.dirty_paths > len(item.dirty_sample) else "")
            + ")"
            for item in dirty
        )
        raise OperationsError(
            "a release is sealed from commits, and these worktrees hold changes that "
            f"would not be in it: {detail}. Commit or stash them, or pass --allow-dirty "
            "to ship the committed HEAD anyway and record the dirty state in the Host's "
            "release evidence"
        )

    def require_worktree_is_the_selection(self) -> None:
        """For a run that executes the worktree instead of sealing an archive.

        The macOS source run starts processes out of the checkouts themselves,
        so a pin that is not the worktree's HEAD does not describe what is
        running — it contradicts it. This is the only place a HEAD/pin equality
        check carries information rather than being a duplicate to reconcile.
        """

        for item in self.resolve().values():
            if item.pinned and item.revision != item.head:
                raise OperationsError(
                    f"a source run executes the worktree, so a pinned release commit must "
                    f"be its HEAD: {item.source_id} is on {item.head[:12]} but "
                    f"{item.revision[:12]} is pinned"
                )

    # -- diagnosis -----------------------------------------------------------

    def advance_from(self, previous: Mapping[str, object]) -> dict[str, int]:
        """How far each source moved since some earlier release's provenance.

        Prevents nothing. A combination of commits that is not self-consistent
        cannot be detected before it runs, so the useful thing to change is what
        the failure says: "these three repositories moved, one of them is the
        suspect" instead of a readiness timeout and no starting point.
        """

        resolved = self.resolve()
        advance: dict[str, int] = {}
        for source_id, item in resolved.items():
            record = previous.get(source_id)
            before = record.get("revision") if isinstance(record, Mapping) else record
            if not isinstance(before, str) or _COMMIT.fullmatch(before) is None:
                continue
            if before == item.revision:
                continue
            _only_before, only_now = self._distance(item.path, before, item.revision)
            if only_now:
                advance[source_id] = only_now
        return advance

    # -- git -----------------------------------------------------------------

    def _resolve_one(self, source_id: str) -> ResolvedSource:
        source = self.config.sources[source_id]
        path = Path(source.path)
        if not path.is_dir() or not (path / ".git").exists():
            raise OperationsError(f"source worktree is missing: {source_id}: {path}")
        head = self._read(source_id, path, ("rev-parse", "HEAD"))
        if _COMMIT.fullmatch(head) is None:
            raise OperationsError(
                f"source HEAD is not a full lowercase 40-hex commit: {source_id}: {head!r}"
            )
        branch = self._read(source_id, path, ("branch", "--show-current")) or None
        status = self._raw(source_id, path, ("status", "--porcelain"))
        entries = tuple(line.strip() for line in status.splitlines() if line.strip())
        revision = source.revision if source.revision is not None else head
        self._require_exact_commit(source_id, path, revision)
        item = ResolvedSource(
            source_id=source_id,
            path=path,
            revision=revision,
            head=head,
            branch=branch,
            pinned=source.revision is not None,
            dirty=bool(entries),
            dirty_paths=len(entries),
            dirty_sample=entries[:_DIRTY_SAMPLE],
            tag=source.tag,
        )
        if item.pinned and revision != head:
            only_pinned, only_head = self._distance(path, revision, head)
            item = replace(item, behind_head=only_head, ahead_of_head=only_pinned)
        self._require_tag_resolves(item)
        return item

    def _require_exact_commit(self, source_id: str, path: Path, revision: str) -> None:
        resolved = self._read(source_id, path, ("rev-parse", "--verify", f"{revision}^{{commit}}"))
        if resolved != revision:
            raise OperationsError(
                f"source revision is not the exact commit object: {source_id}"
            )

    def _require_tag_resolves(self, item: ResolvedSource) -> None:
        """Prove an annotated tag still names the commit this release ships.

        A tag is a movable reference, so it cannot define a release. It can
        still make one reviewable — as long as moving it is reported instead of
        silently followed.
        """

        if item.tag is None:
            return
        resolved = self._read(
            item.source_id, item.path, ("rev-parse", "--verify", f"{item.tag}^{{commit}}")
        )
        if resolved != item.revision:
            raise OperationsError(
                f"tag no longer names the resolved commit: {item.source_id} "
                f"{item.tag} -> {resolved[:12]}, expected {item.revision[:12]}"
            )

    def _distance(self, path: Path, left: str, right: str) -> tuple[int, int]:
        """Commits exclusive to each side of ``left...right``, or zeros if unanswerable.

        A commit that is not in this checkout at all — a pin from a branch that
        was pruned, a provenance record from a Host older than the clone — is a
        thing to report as unknown, not a reason to fail an operation that had
        nothing to do with counting.
        """

        counted = self.runner.run(
            (
                self.git,
                "-C",
                str(path),
                "rev-list",
                "--left-right",
                "--count",
                f"{left}...{right}",
            )
        )
        if counted.returncode != 0:
            return (0, 0)
        parts = counted.stdout.split()
        if len(parts) != 2 or not all(part.isdigit() for part in parts):
            return (0, 0)
        return (int(parts[0]), int(parts[1]))

    def _read(self, source_id: str, path: Path, arguments: tuple[str, ...]) -> str:
        return self._raw(source_id, path, arguments).strip()

    def _raw(self, source_id: str, path: Path, arguments: tuple[str, ...]) -> str:
        return checked(
            f"source resolution for {source_id} ({' '.join(arguments)})",
            self.runner.run((self.git, "-C", str(path), *arguments)),
        ).stdout
