"""The build tools a release is sealed with, carried rather than assumed.

The Host's tools are declared with a version and a digest and installed by
Ops. The workstation's were a path in a profile, and that path pointed into
the system temp directory — so "pinned uv 0.11.15" meant "0.11.15 for as long
as macOS has not swept /private/tmp". It swept it, and the whole release line
stopped: doctor failed outright and the next update would have failed in the
same place.

A pin is a promise that the same thing will be there next time. A path to
something somebody once built is not that promise; a version with a digest,
materialized where it will still be, is. So the workstation toolchain is
declared exactly like the Host's, and Ops puts it where it said it would be.
"""

from __future__ import annotations

import hashlib
import os
import platform
import shutil
import stat
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path
from urllib.error import URLError
from urllib.request import urlopen

from eidolon_ops.config import ConfigurationError
from eidolon_ops.errors import OperationsError

__all__ = [
    "WORKSTATION_UV",
    "WorkstationArtifact",
    "ensure_workstation_uv",
    "workstation_uv_path",
]

#: How long one artifact download may take before it is a failure rather than
#: a wait. Generous: this runs once per version, on a link Ops does not own.
_DOWNLOAD_TIMEOUT_SECONDS = 300

#: Written beside the extracted executable so a later run can tell whether what
#: is on disk came from the artifact currently pinned.
_DIGEST_RECORD = ".wheel-sha256"


@dataclass(frozen=True, slots=True)
class WorkstationArtifact:
    """One build tool, named by what it is rather than by where it sits."""

    artifact_id: str
    version: str
    #: Keyed by platform.machine(); a workstation this does not cover is told
    #: so, rather than silently falling back to whatever is on PATH.
    urls: dict[str, str]
    digests: dict[str, str]
    executable: str

    def source(self, machine: str) -> tuple[str, str]:
        url = self.urls.get(machine)
        digest = self.digests.get(machine)
        if url is None or digest is None:
            raise ConfigurationError(
                f"{self.artifact_id} {self.version} is not pinned for this "
                f"workstation architecture: {machine}"
            )
        return url, digest


#: The same version the Host runs and the same version the release contract
#: checks for. Both ends of a release are built by one tool on purpose.
WORKSTATION_UV = WorkstationArtifact(
    artifact_id="uv",
    version="0.11.15",
    urls={
        "arm64": (
            "https://files.pythonhosted.org/packages/a6/b8/"
            "48627f895a1569e576822e0a8416aa4797eb4a4551de21a4ad97b9b5819d/"
            "uv-0.11.15-py3-none-macosx_11_0_arm64.whl"
        ),
    },
    digests={
        "arm64": "9accae33619a9166e5c48531deb455d672cfb89f9357a00975e669c76b0bd49f",
    },
    executable="uv",
)


def workstation_uv_path(toolchain_root: Path) -> Path:
    """Where this version of uv lives once Ops has put it there.

    The version is in the path so a change of pin is a different file rather
    than a silent overwrite, and so two pins can coexist while one is reviewed.
    """

    return (
        toolchain_root
        / f"{WORKSTATION_UV.artifact_id}-{WORKSTATION_UV.version}"
        / WORKSTATION_UV.executable
    )


def ensure_workstation_uv(toolchain_root: Path) -> Path:
    """Return the pinned uv, materializing it if it is not already there.

    What decides "already there" is the digest recorded beside it, not the
    file's name: a half-written or replaced executable is materialized again
    rather than trusted for sitting in the right place.
    """

    machine = platform.machine()
    _url, digest = WORKSTATION_UV.source(machine)
    target = workstation_uv_path(toolchain_root)
    if target.is_file() and _recorded_digest(target) == digest:
        return target
    return _materialize(toolchain_root, machine)


def _recorded_digest(target: Path) -> str:
    """The digest of the artifact this executable was taken from."""

    record = target.parent / _DIGEST_RECORD
    return record.read_text(encoding="utf-8").strip() if record.is_file() else ""


def _materialize(toolchain_root: Path, machine: str) -> Path:
    url, digest = WORKSTATION_UV.source(machine)
    target = workstation_uv_path(toolchain_root)
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="eidolon-toolchain-") as scratch:
        wheel = Path(scratch) / "artifact.whl"
        _download(url, wheel)
        actual = hashlib.sha256(wheel.read_bytes()).hexdigest()
        if actual != digest:
            raise OperationsError(
                f"pinned {WORKSTATION_UV.artifact_id} {WORKSTATION_UV.version} "
                f"digest mismatch: expected {digest}, got {actual}"
            )
        extracted = _extract_executable(wheel, Path(scratch))
        os.chmod(extracted, 0o755)
        shutil.move(str(extracted), target)
    (target.parent / _DIGEST_RECORD).write_text(digest, encoding="utf-8")
    return target


def _download(url: str, destination: Path) -> None:
    try:
        with urlopen(url, timeout=_DOWNLOAD_TIMEOUT_SECONDS) as response:
            destination.write_bytes(response.read())
    except (URLError, OSError, ValueError) as exc:
        raise OperationsError(
            f"could not fetch the pinned {WORKSTATION_UV.artifact_id} "
            f"{WORKSTATION_UV.version}: {exc}"
        ) from exc


def _extract_executable(wheel: Path, scratch: Path) -> Path:
    """Take the one executable this artifact exists for, and nothing else."""

    wanted = f"/{WORKSTATION_UV.executable}"
    with zipfile.ZipFile(wheel) as archive:
        names = [
            name
            for name in archive.namelist()
            if name.endswith(wanted) and not name.endswith("/")
        ]
        if len(names) != 1:
            raise OperationsError(
                f"pinned {WORKSTATION_UV.artifact_id} wheel does not contain "
                f"exactly one {WORKSTATION_UV.executable}: {names}"
            )
        destination = scratch / WORKSTATION_UV.executable
        with archive.open(names[0]) as source, destination.open("wb") as sink:
            shutil.copyfileobj(source, sink)
    mode = destination.stat().st_mode
    destination.chmod(mode | stat.S_IXUSR)
    return destination
