"""What a source-run Host runs, as one set rather than one choice per repository.

On 2026-10-30 the Mac ran nine repositories out of five task directories. Each
task had written its own ``chore(mac): run X``, so the combination running had
never run together anywhere else; two of the nine commits were on no branch's
history; Agent, Hub and Kernel imported one SDK while Channel and Admin imported
another; Data ran out of its development checkout; and a Models commit chosen
for one task dropped the recognition policy another task had shipped. Nothing
refused any of it, and recognition was off for a day.

This module holds what a source run must be instead, and the one command that
moves it:

- **one root**: every source, and the Ops checkout whose lifecycle script runs
  them, is a worktree directly under one directory, so editable dependencies
  resolve inside that directory (``require_one_root``);
- **one library set**: every environment in that root imports the SDK, the
  Memory contracts and Data from the root's own checkouts
  (``require_one_library_set``);
- **committed**: the profile that names the root is committed, so what runs is
  what the repository says (``require_committed_profile``);
- **forward only**: a new set contains the last one this Host prepared, which
  ``SourceResolver.require_no_regression`` checks against the record kept here;
- **one writer**: ``ReleaseSetSelection`` builds the next root from ``main``
  and rewrites the profile, so nobody edits paths by hand.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from types import MappingProxyType

from eidolon_ops.errors import OperationsError
from eidolon_ops.private_files import atomic_private_file
from eidolon_ops.process import ProcessRunner, checked
from eidolon_ops.source_resolution import TRUNK

#: Libraries more than one process imports, and the checkout each must come
#: from: (import name, owning source, path inside that source).
SHARED_LIBRARIES = (
    ("eidolon_sdk", "eidolon_sdk", ""),
    ("eidolon_memory_contracts", "eidolon_memory", "contracts"),
    ("eidolon_data", "eidolon_data", ""),
)

#: How each checkout's environments are built for a source run: the directory
#: inside the checkout and the ``uv sync --frozen`` arguments. Read off the
#: environments the Mac actually ran on 2026-10-31 (``uv sync --dry-run``
#: reporting no change), not chosen.
SOURCE_ENVIRONMENTS: Mapping[str, tuple[tuple[str, tuple[str, ...]], ...]] = {
    "eidolon_sdk": ((".", ("--all-extras",)),),
    "eidolon_data": ((".", ("--all-extras",)),),
    "eidolon_admin": ((".", ("--all-extras",)),),
    "eidolon_agent": ((".", ("--all-extras",)),),
    "eidolon_hub": ((".", ()),),
    "eidolon_kernel": ((".", ()),),
    "eidolon_memory": ((".", ()),),
    "eidolon_channel": ((".", ("--extra", "dev")),),
    "eidolon_models": ((".", ()), ("laya", ("--extra", "torch"))),
    "eidolon_ops": ((".", ("--extra", "dev")),),
}

OPS_SOURCE = "eidolon_ops"
#: Checkouts whose large files reach a Host only as artifacts, read from git's
#: own store (`cat-file --filters`), so their worktree can hold LFS pointers:
#: 11 GB less per root. Only Models: Channel loads its VAD and turn-detector
#: ONNX straight from its tree, and pointers there failed both on 2026-10-31.
LFS_POINTERS_SUFFICE = frozenset({"eidolon_models"})
#: Where Host profiles live in the Ops repository. Committing a selection
#: advances Ops ``main`` without changing any code a Host runs, so a commit that
#: touches only these is not a reason to build a new root.
PROFILE_PATHS = ("config/hosts/",)
_RECORD = "release-set.json"
_SET_NAME = re.compile(r"^main-\d{8}-[0-9a-f]{8}$")

_IMPORT_PROBE = (
    "import importlib.util, json;"
    "print(json.dumps({n: getattr(importlib.util.find_spec(n), 'origin', None) "
    "for n in %r}))"
)


def require_one_root(sources: Mapping[str, Path], workspace: Path) -> Path:
    """The single directory every source and the Ops workspace sit directly in."""

    places = {source_id: Path(path) for source_id, path in sources.items()}
    places[OPS_SOURCE] = Path(workspace)
    roots = {path.parent for path in places.values()}
    if len(roots) != 1:
        detail = ", ".join(
            f"{source_id}={path.parent}" for source_id, path in sorted(places.items())
        )
        raise OperationsError(
            "a source run executes one release set, so every source and the Ops "
            f"lifecycle checkout must sit in one root; this profile spreads them: {detail}. "
            "Select a set with the release-set command instead of editing paths"
        )
    return roots.pop()


def require_committed_profile(runner: ProcessRunner, git: str, profile: Path) -> None:
    """What runs is what the repository says, not what a working copy says."""

    directory, name = str(profile.parent), profile.name
    tracked = runner.run((git, "-C", directory, "ls-files", "--error-unmatch", "--", name))
    if tracked.returncode != 0:
        raise OperationsError(f"the Host profile is not committed to a repository: {profile}")
    status = checked(
        "Host profile status",
        runner.run((git, "-C", directory, "status", "--porcelain", "--", name)),
    ).stdout.strip()
    if status:
        raise OperationsError(
            f"the Host profile has uncommitted changes: {profile}. A Host runs the "
            "selection its repository records; commit it (the release-set command does)"
        )


def require_workspace_on_trunk(runner: ProcessRunner, git: str, workspace: Path) -> str:
    """The Ops checkout running the lifecycle is held to the same rule as sources."""

    head = checked(
        "Ops workspace HEAD", runner.run((git, "-C", str(workspace), "rev-parse", "HEAD"))
    ).stdout.strip()
    answer = runner.run(
        (git, "-C", str(workspace), "merge-base", "--is-ancestor", "HEAD", f"refs/heads/{TRUNK}")
    )
    if answer.returncode == 1:
        raise OperationsError(
            f"the Ops lifecycle checkout {workspace} is on {head[:12]}, which is not on {TRUNK}"
        )
    if answer.returncode != 0:
        raise OperationsError(f"cannot tell whether the Ops lifecycle checkout is on {TRUNK}")
    return head


def ops_dropped(
    runner: ProcessRunner, git: str, workspace: Path, *, before: object, now: str
) -> int:
    """Commits the previous Ops lifecycle checkout had that this one lacks."""

    if not isinstance(before, str) or before == now:
        return 0
    counted = runner.run((git, "-C", str(workspace), "rev-list", "--count", f"{now}..{before}"))
    text = counted.stdout.strip()
    return int(text) if counted.returncode == 0 and text.isdigit() else 0


def environments(source_id: str, path: Path) -> tuple[Path, ...]:
    """The Python environments a source run starts processes from, in one checkout."""

    if not path.is_dir():
        return ()
    found = [path / ".venv"]
    found.extend(sorted(child / ".venv" for child in path.iterdir() if child.is_dir()))
    return tuple(env for env in found if (env / "bin/python").is_file())


def require_one_library_set(
    runner: ProcessRunner, sources: Mapping[str, Path], workspace: Path
) -> dict[str, dict[str, str]]:
    """Every environment imports the shared libraries from this set's checkouts.

    Asked of each interpreter rather than read from lock files: the 10-30 split
    was invisible in every file and obvious the moment a process was asked
    where its ``eidolon_sdk`` came from.
    """

    expected = {
        name: (Path(sources[owner]) / inner).resolve()
        for name, owner, inner in SHARED_LIBRARIES
        if owner in sources
    }
    places = {**{k: Path(v) for k, v in sources.items()}, OPS_SOURCE: Path(workspace)}
    report: dict[str, dict[str, str]] = {}
    wrong: list[str] = []
    for source_id, path in sorted(places.items()):
        for env in environments(source_id, path):
            answer = checked(
                f"shared library probe for {env}",
                runner.run((str(env / "bin/python"), "-I", "-c", _IMPORT_PROBE % (tuple(expected),))),
            ).stdout
            try:
                origins = json.loads(answer.strip().splitlines()[-1])
            except (ValueError, IndexError) as exc:
                raise OperationsError(f"shared library probe gave no answer: {env}") from exc
            label = str(env.relative_to(path.parent))
            report[label] = {}
            for name, root in expected.items():
                origin = origins.get(name)
                if origin is None:
                    continue
                report[label][name] = origin
                if not Path(origin).resolve().is_relative_to(root):
                    wrong.append(f"{label} imports {name} from {origin}")
    if wrong:
        raise OperationsError(
            "processes in one release set must import one copy of each shared "
            "library, and these do not: " + "; ".join(wrong)
        )
    return report


def record_path(state_root: Path) -> Path:
    return state_root / "ops" / _RECORD


def read_record(state_root: Path) -> dict[str, object] | None:
    path = record_path(state_root)
    if not path.is_file():
        return None
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise OperationsError(f"the release-set record is unreadable: {path}") from exc
    if not isinstance(document, dict) or not isinstance(document.get("sources"), dict):
        raise OperationsError(f"the release-set record is malformed: {path}")
    return document


def write_record(
    state_root: Path,
    *,
    root: Path,
    sources: Mapping[str, str],
    workspace: str,
    regression: Mapping[str, object],
) -> None:
    path = record_path(state_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    document = {
        "root": str(root),
        "sources": dict(sorted(sources.items())),
        OPS_SOURCE: workspace,
        "regression": dict(regression),
        "recorded_at": datetime.now(UTC).isoformat(timespec="seconds"),
    }
    atomic_private_file(path, (json.dumps(document, indent=2, sort_keys=True) + "\n").encode())


# -- the one writer -----------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ReleaseSetSelection:
    """Build the next release set from ``main`` and make the profile name it.

    ``repositories`` maps each source id (and ``eidolon_ops``) to its main
    checkout, which is where worktrees are added from and where ``main`` is
    read. ``sources_root`` is the directory release roots are created in.
    """

    profile: Path
    repositories: Mapping[str, Path]
    sources_root: Path
    runner: ProcessRunner
    git: str = "git"

    def plan(
        self,
        pins: Mapping[str, str] = MappingProxyType({}),
        *,
        current: Mapping[str, str] = MappingProxyType({}),
        previous: Mapping[str, object] | None = None,
        allow_regression: str | None = None,
    ) -> dict[str, object]:
        unknown = set(pins).difference(self.repositories)
        if unknown:
            raise OperationsError("unknown source in release-set pin: " + ", ".join(sorted(unknown)))
        revisions = {
            source_id: pins.get(source_id) or self._main(path)
            for source_id, path in sorted(self.repositories.items())
        }
        held = current.get(OPS_SOURCE)
        if OPS_SOURCE not in pins and held and held != revisions[OPS_SOURCE]:
            if self._only_profiles_changed(held, revisions[OPS_SOURCE]):
                revisions[OPS_SOURCE] = held
        for source_id, revision in revisions.items():
            self._require_on_main(source_id, revision)
        regression = self._regression(revisions, previous, allow_regression)
        digest = set_digest(revisions)
        existing = sorted(self.sources_root.glob(f"main-*-{digest}"))
        name = existing[0].name if existing else f"main-{datetime.now(UTC):%Y%m%d}-{digest}"
        return {
            "name": name,
            "root": str(self.sources_root / name),
            "revisions": revisions,
            "exists": bool(existing),
            "regression": regression,
        }

    def apply(self, plan: Mapping[str, object]) -> dict[str, object]:
        root = Path(str(plan["root"]))
        revisions = plan["revisions"]
        if not isinstance(revisions, Mapping) or _SET_NAME.fullmatch(root.name) is None:
            raise OperationsError("release-set plan is malformed")
        root.mkdir(parents=True, exist_ok=True)
        created: list[str] = []
        for source_id, revision in revisions.items():
            target = root / source_id
            if target.exists():
                head = checked(
                    f"release-set worktree {source_id}",
                    self.runner.run((self.git, "-C", str(target), "rev-parse", "HEAD")),
                ).stdout.strip()
                if head != revision:
                    raise OperationsError(
                        f"{target} exists on {head[:12]}, not {str(revision)[:12]}; "
                        "a release root is never moved, select a new one"
                    )
            else:
                checked(
                    f"release-set worktree {source_id}",
                    self.runner.run(
                        (
                            self.git, "-C", str(self.repositories[source_id]),
                            "worktree", "add", "--detach", str(target), str(revision),
                        ),
                        env={**os.environ, "GIT_LFS_SKIP_SMUDGE": "1"}
                        if source_id in LFS_POINTERS_SUFFICE else None,
                    ),
                )
                created.append(source_id)
        # Second pass: an environment resolves editable siblings (Admin needs
        # Memory's contracts), so every checkout has to exist before any sync.
        for source_id in revisions:
            target = root / source_id
            for inner, arguments in SOURCE_ENVIRONMENTS.get(source_id, ((".", ()),)):
                directory = target / inner
                checked(
                    f"release-set environment {source_id}/{inner}",
                    self.runner.run(
                        ("uv", "sync", "--frozen", *arguments),
                        cwd=directory,
                        # Ops itself runs under `uv run`; its environment is not this one.
                        env={k: v for k, v in os.environ.items() if k != "VIRTUAL_ENV"},
                        timeout=900,
                    ),
                )
        return {"root": str(root), "created": created}

    def write_profile(self, plan: Mapping[str, object]) -> str:
        """Point the profile at the root, and commit only that file."""

        root = Path(str(plan["root"]))
        revisions = plan["revisions"]
        assert isinstance(revisions, Mapping)
        text = self.profile.read_text(encoding="utf-8")
        relative = Path(_relative(root, self.profile.parent))
        for source_id, revision in revisions.items():
            if source_id == OPS_SOURCE:
                continue
            pattern = re.compile(
                r'(\[source_overrides\.%s\]\npath = )"[^"]*"\nrevision = "[0-9a-f]{40}"'
                % re.escape(source_id)
            )
            text, count = pattern.subn(
                lambda m: f'{m.group(1)}"{relative / source_id}"\nrevision = "{revision}"', text
            )
            if count != 1:
                raise OperationsError(
                    f"the profile has no single [source_overrides.{source_id}] path/revision pair"
                )
        text, count = re.subn(
            r'^lifecycle_script = "[^"]*"$',
            f'lifecycle_script = "{relative / OPS_SOURCE / "deploy/dev/run_all.sh"}"',
            text,
            flags=re.MULTILINE,
        )
        if count != 1:
            raise OperationsError("the profile has no single lifecycle_script line")
        self.profile.write_text(text, encoding="utf-8")
        directory = str(self.profile.parent)
        name = self.profile.name
        changed = self.runner.run((self.git, "-C", directory, "diff", "--quiet", "--", name))
        if changed.returncode == 0:
            return "unchanged"
        message = (
            f"chore({self.profile.stem}): run release set {root.name}\n\n"
            + "\n".join(f"{k} {v}" for k, v in sorted(revisions.items()))
            + "\n"
        )
        checked(
            "release-set profile commit",
            self.runner.run(
                (self.git, "-C", directory, "commit", "--only", "-m", message, "--", name)
            ),
        )
        return "committed"

    def _regression(
        self,
        revisions: Mapping[str, str],
        previous: Mapping[str, object] | None,
        allow_regression: str | None,
    ) -> dict[str, object]:
        """Refused before anything is created, by the same rule ``prepare`` applies."""

        if previous is None:
            return {"status": "no_previous_set"}
        before = {
            **dict(previous.get("sources", {})),  # type: ignore[arg-type]
            OPS_SOURCE: previous.get(OPS_SOURCE),
        }
        dropped: dict[str, int] = {}
        for source_id, revision in revisions.items():
            old = before.get(source_id)
            if not isinstance(old, str) or old == revision:
                continue
            counted = self.runner.run((
                self.git, "-C", str(self.repositories[source_id]),
                "rev-list", "--count", f"{revision}..{old}",
            ))
            text = counted.stdout.strip()
            if counted.returncode == 0 and text.isdigit() and int(text):
                dropped[source_id] = int(text)
        since = f"the release set this Host last prepared ({previous.get('root')})"
        if not dropped:
            return {"status": "forward", "since": since}
        if allow_regression:
            return {"status": "regression_allowed", "since": since,
                    "dropped": dict(sorted(dropped.items())), "reason": allow_regression}
        detail = ", ".join(f"{k} -{v}" for k, v in sorted(dropped.items()))
        raise OperationsError(
            f"this release set drops commits that {since} ran ({detail}); pin commits "
            "that contain them, or pass --allow-regression REASON"
        )

    def _only_profiles_changed(self, held: str, tip: str) -> bool:
        ancestor = self.runner.run((
            self.git, "-C", str(self.repositories[OPS_SOURCE]),
            "merge-base", "--is-ancestor", held, tip,
        ))
        if ancestor.returncode != 0:
            return False
        changed = checked(
            "Ops changes since the held release root",
            self.runner.run((
                self.git, "-C", str(self.repositories[OPS_SOURCE]),
                "diff", "--name-only", held, tip,
            )),
        ).stdout.split()
        return all(path.startswith(PROFILE_PATHS) for path in changed)

    def _main(self, path: Path) -> str:
        return checked(
            f"main of {path}",
            self.runner.run((self.git, "-C", str(path), "rev-parse", f"refs/heads/{TRUNK}")),
        ).stdout.strip()

    def _require_on_main(self, source_id: str, revision: str) -> None:
        answer = self.runner.run((
            self.git, "-C", str(self.repositories[source_id]), "merge-base",
            "--is-ancestor", revision, f"refs/heads/{TRUNK}",
        ))
        if answer.returncode != 0:
            raise OperationsError(f"{source_id} {revision[:12]} is not on {TRUNK}")


def set_digest(revisions: Mapping[str, str]) -> str:
    """Named by its content, so selecting the same set twice finds one root."""

    return hashlib.sha256(
        "\n".join(f"{k}={v}" for k, v in sorted(revisions.items())).encode()
    ).hexdigest()[:8]


def _relative(target: Path, start: Path) -> str:
    return os.path.relpath(target, start)
