"""What a Host must hold that no release contains, carried once.

A release is exact Git commits, so a gigabyte of weights is in neither. Every
component that needs such a thing says so in its own ``ops/component.toml`` —
which files, what they hash to, and where the pinned bytes come from — and this
module is the whole of how they arrive.

Fetched on the workstation and carried over the transport a release already
uses, never pulled by the Host: a board may have no route to a model hub, which
is the case the declaration exists to remove rather than relocate. Carried
beside a release rather than inside one, because these bytes outlive any single
release and a gigabyte that did not change should not be paid for again on
every update.
"""

from __future__ import annotations

import hashlib
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from urllib.request import urlopen

from eidolon_ops.errors import OperationsError
from eidolon_ops.hostagent.contract import HOST_MODEL_ROOT

__all__ = [
    "DIGEST_RECORD",
    "CarriedArtifact",
    "CarriedFile",
    "carried_artifacts",
    "ensure_workstation_artifact",
    "host_artifact_root",
    "workstation_artifact_root",
]

#: Written beside the files and read back from the Host to answer "already
#: held". One digest over the whole set, so a partial copy is not a copy.
DIGEST_RECORD = ".files-sha256"

_DOWNLOAD_TIMEOUT_SECONDS = 600


@dataclass(frozen=True, slots=True)
class CarriedFile:
    """One pinned file, exactly as the component declared it."""

    path: str
    sha256: str
    url: str


@dataclass(frozen=True, slots=True)
class CarriedArtifact:
    """One component's declaration, read rather than restated.

    Nothing here is Ops's own knowledge: the component owns the pin, and this
    is the shape the carry needs it in.
    """

    component_id: str
    artifact_id: str
    kind: str
    install_root: Path
    files: tuple[CarriedFile, ...]

    @property
    def digest(self) -> str:
        """One digest over the set, so a partial match is not a match."""

        joined = "\n".join(
            f"{item.path} {item.sha256}" for item in sorted(self.files, key=lambda f: f.path)
        )
        return hashlib.sha256(joined.encode("utf-8")).hexdigest()


def carried_artifacts(topology: object) -> tuple[CarriedArtifact, ...]:
    """Every artifact the contracts declare, in a fixed order.

    Read from a topology already selected for this Host's capabilities, so an
    artifact a Host has no capability for is not here to be skipped later — it
    was dropped where every other conditional entry is dropped.
    """

    carried: list[CarriedArtifact] = []
    for contract in getattr(topology, "contracts", ()):
        for entry in contract.artifacts:
            files = tuple(
                CarriedFile(path=item["path"], sha256=item["sha256"], url=item["url"])
                for item in entry.get("files", ())
            )
            if not files:
                # A declaration with no files names nothing to carry. The
                # schema allows it so a component can state a dependency it has
                # not pinned yet; carrying it would be carrying nothing.
                continue
            carried.append(
                CarriedArtifact(
                    component_id=contract.component_id,
                    artifact_id=entry["id"],
                    kind=entry["kind"],
                    install_root=Path(entry["install_root"]),
                    files=files,
                )
            )
    return tuple(sorted(carried, key=lambda item: (item.component_id, item.artifact_id)))


def host_artifact_root(artifact: CarriedArtifact) -> Path:
    """Where this lands on the Host — the component's own choice, checked.

    The Host agent refuses a destination outside its model root, and refusing
    here as well means an operator is told by the tool that is reading the
    contract rather than by a Host halfway through a carry.
    """

    root = artifact.install_root
    if root.parent != HOST_MODEL_ROOT or not root.name:
        raise OperationsError(
            f"{artifact.component_id} artifact {artifact.artifact_id} declares an "
            f"install root outside {HOST_MODEL_ROOT}: {root}"
        )
    return root


def workstation_artifact_root(toolchain_root: Path, artifact: CarriedArtifact) -> Path:
    """Named by where it will land, so two pins cannot share a directory."""

    return toolchain_root / "models" / host_artifact_root(artifact).name


def ensure_workstation_artifact(toolchain_root: Path, artifact: CarriedArtifact) -> Path:
    """Return the pinned files on this workstation, fetching them if absent.

    The record and every file must match the pinned manifest. A matching
    record cannot hide a damaged or partial download.
    """

    root = workstation_artifact_root(toolchain_root, artifact)
    if (
        not root.is_symlink() and _recorded_digest(root) == artifact.digest
        and not any(path.is_symlink() for path in root.rglob("*"))
        and {path.relative_to(root).as_posix() for path in root.rglob("*") if path.is_file()}
        == {DIGEST_RECORD, *(item.path for item in artifact.files)}
        and all((root / item.path).is_file() and not (root / item.path).is_symlink()
                and _digest_of(root / item.path) == item.sha256 for item in artifact.files)
    ):
        return root
    return _materialize(root, artifact)


def _recorded_digest(root: Path) -> str:
    record = root / DIGEST_RECORD
    return record.read_text(encoding="utf-8").strip() if record.is_file() else ""


def _materialize(root: Path, artifact: CarriedArtifact) -> Path:
    with tempfile.TemporaryDirectory(prefix="eidolon-artifact-") as scratch:
        staged = Path(scratch)
        for item in artifact.files:
            destination = staged / item.path
            destination.parent.mkdir(parents=True, exist_ok=True)
            _download(item.url, destination)
            actual = _digest_of(destination)
            if actual != item.sha256:
                raise OperationsError(
                    f"{artifact.component_id} {artifact.artifact_id} {item.path} does "
                    f"not match its pin: expected {item.sha256}, got {actual}"
                )
        (staged / DIGEST_RECORD).write_text(artifact.digest, encoding="utf-8")
        root.parent.mkdir(parents=True, exist_ok=True)
        if root.exists():
            shutil.rmtree(root)
        shutil.copytree(staged, root)
    return root


def _download(url: str, destination: Path) -> None:
    try:
        with (
            urlopen(url, timeout=_DOWNLOAD_TIMEOUT_SECONDS) as response,
            destination.open("wb") as handle,
        ):
            shutil.copyfileobj(response, handle)
    except OSError as error:
        raise OperationsError(f"could not fetch {url}: {error}") from error


def _digest_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
