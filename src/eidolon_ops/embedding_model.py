"""The encoder a Host reads memories with, carried rather than downloaded.

An Eidolon's memory is searched by meaning, and meaning here is an ONNX
sentence encoder. Until now nothing put that encoder on the Host: the memory
service asked the model hub for it at first use, which works on a laptop and
does not work on the thing this product actually is — a box in someone's home
that may have no route to the internet, and whose memory is supposed to never
leave it. What happened instead was a query that spent seventy seconds failing
to fetch a model and then answered, truthfully and uselessly, with nothing.

So the encoder is materialized like every other pinned artifact: fetched once
on the workstation against a recorded digest, carried to the Host, and left in
a durable place named after the encoder itself.

Named after the encoder and not after its digest, deliberately. What a palace
is bound to is the encoder's *identity* — Chroma records the model on the
collection and refuses to read it back with a different one, which is what
stops two vector spaces from being compared. A rebuilt copy of the same model
is the same encoder, and the Host env that selects it is a fixed string, so a
digest in the path would be a second name for one thing and nothing could
write it down. The digest still decides whether the files already on the Host
are the ones pinned now; it just does not decide where they live.
"""

from __future__ import annotations

import hashlib
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from urllib.request import urlopen

from eidolon_ops.errors import OperationsError
from eidolon_ops.hostagent.contract import (
    HOST_EMBEDDING_MODEL_ROOT as HOST_MODEL_ROOT,
)

__all__ = [
    "EMBEDDING_DIGEST_RECORD",
    "HOST_EMBEDDING_MODEL_ROOT",
    "PINNED_EMBEDDING_MODEL",
    "EmbeddingModelArtifact",
    "embedding_model_digest",
    "ensure_workstation_embedding_model",
    "host_embedding_model_root",
    "workstation_embedding_model_root",
]

#: Generous: this runs once per pin, over a link Ops does not own.
_DOWNLOAD_TIMEOUT_SECONDS = 900

#: Written beside the files so a later run can tell whether what is on disk is
#: what is pinned now, rather than trusting a directory for existing.
EMBEDDING_DIGEST_RECORD = ".files-sha256"
_DIGEST_RECORD = EMBEDDING_DIGEST_RECORD


@dataclass(frozen=True, slots=True)
class EmbeddingModelArtifact:
    """One encoder, named as the memory service names it."""

    #: The value of ``EIDOLON_MEMORY_EMBEDDING_MODEL`` this satisfies.
    model_id: str
    repo: str
    revision: str
    #: Paths relative to the model root, exactly as the embedder looks for
    #: them. Both must be present or the copy counts as absent: an incomplete
    #: directory sends the embedder back to the hub, which is the failure this
    #: exists to remove.
    files: dict[str, str]

    def url(self, relative: str) -> str:
        return (
            f"https://huggingface.co/{self.repo}/resolve/{self.revision}/{relative}"
        )


#: Chosen on measurement, not on size. On a Pi 5, bge-large costs 626 MB and
#: 104 ms/doc against base's 198 MB and 32 ms, for MRR 0.813 against 0.787 —
#: which is why the Host env pins base. This carries the files that choice
#: needs.
PINNED_EMBEDDING_MODEL = EmbeddingModelArtifact(
    model_id="bge-base-zh",
    repo="Xenova/bge-base-zh-v1.5",
    revision="main",
    files={
        "onnx/model_quantized.onnx": (
            "b665f3bba56c3119bc76ba131ebcc544d720a7408cb11581bdf354aaa0198d43"
        ),
        "tokenizer.json": (
            "7dfbf1966ebf99d471c3796e9b457329d2b2182b817e144f1e904b957745c839"
        ),
    },
)

#: Read from the Host path contract rather than restated here: the Host agent
#: is what creates and owns this directory, and a second copy of the path is a
#: second thing to keep true.
HOST_EMBEDDING_MODEL_ROOT = HOST_MODEL_ROOT


def host_embedding_model_root(
    artifact: EmbeddingModelArtifact = PINNED_EMBEDDING_MODEL,
) -> Path:
    return HOST_EMBEDDING_MODEL_ROOT / _directory_name(artifact)


def workstation_embedding_model_root(
    toolchain_root: Path,
    artifact: EmbeddingModelArtifact = PINNED_EMBEDDING_MODEL,
) -> Path:
    return toolchain_root / "models" / _directory_name(artifact)


def ensure_workstation_embedding_model(
    toolchain_root: Path,
    artifact: EmbeddingModelArtifact = PINNED_EMBEDDING_MODEL,
) -> Path:
    """Return the pinned encoder on this workstation, fetching it if absent.

    What decides "already here" is the recorded digest, not the presence of a
    file with the right name — a half-written download is fetched again rather
    than carried to a Host and trusted there.
    """

    root = workstation_embedding_model_root(toolchain_root, artifact)
    if _recorded_digest(root) == _expected_digest(artifact):
        return root
    return _materialize(root, artifact)


def _directory_name(artifact: EmbeddingModelArtifact) -> str:
    """Named by the encoder, which is the thing a palace is bound to."""

    return artifact.model_id


def embedding_model_digest(artifact: EmbeddingModelArtifact = PINNED_EMBEDDING_MODEL) -> str:
    return _expected_digest(artifact)


def _expected_digest(artifact: EmbeddingModelArtifact) -> str:
    """One digest over the whole set, so a partial match is not a match."""

    joined = "\n".join(
        f"{relative} {digest}" for relative, digest in sorted(artifact.files.items())
    )
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()


def _recorded_digest(root: Path) -> str:
    record = root / _DIGEST_RECORD
    return record.read_text(encoding="utf-8").strip() if record.is_file() else ""


def _materialize(root: Path, artifact: EmbeddingModelArtifact) -> Path:
    with tempfile.TemporaryDirectory(prefix="eidolon-encoder-") as scratch:
        staged = Path(scratch)
        for relative, digest in artifact.files.items():
            destination = staged / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            _download(artifact.url(relative), destination)
            actual = _digest_of(destination)
            if actual != digest:
                raise OperationsError(
                    f"{artifact.repo} {relative} does not match its pin: "
                    f"expected {digest}, got {actual}"
                )
        (staged / _DIGEST_RECORD).write_text(
            _expected_digest(artifact), encoding="utf-8"
        )
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
