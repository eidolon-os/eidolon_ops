"""The encoder a Host reads memories with, and how it gets there."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from eidolon_ops import embedding_model
from eidolon_ops.embedding_model import (
    EmbeddingModelArtifact,
    PINNED_EMBEDDING_MODEL,
    embedding_model_digest,
    ensure_workstation_embedding_model,
    host_embedding_model_root,
)
from eidolon_ops.errors import OperationsError
from eidolon_ops.hostagent import contract, staging
from eidolon_ops.hostagent.primitives import TargetError

_WEIGHTS = b"onnx-weights"
_TOKENIZER = b"tokenizer"


def _artifact() -> EmbeddingModelArtifact:
    return EmbeddingModelArtifact(
        model_id="test-encoder",
        repo="Example/test-encoder",
        revision="main",
        files={
            "onnx/model_quantized.onnx": hashlib.sha256(_WEIGHTS).hexdigest(),
            "tokenizer.json": hashlib.sha256(_TOKENIZER).hexdigest(),
        },
    )


@pytest.fixture
def offline(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    fetched: list[str] = []

    def _download(url: str, destination: Path) -> None:
        fetched.append(url)
        payload = _WEIGHTS if url.endswith(".onnx") else _TOKENIZER
        destination.write_bytes(payload)

    monkeypatch.setattr(embedding_model, "_download", _download)
    return fetched


def test_the_pin_is_carried_once_and_recognised_afterwards(
    tmp_path: Path, offline: list[str]
) -> None:
    artifact = _artifact()

    first = ensure_workstation_embedding_model(tmp_path, artifact)
    assert (first / "onnx" / "model_quantized.onnx").read_bytes() == _WEIGHTS
    assert (first / "tokenizer.json").read_bytes() == _TOKENIZER
    assert len(offline) == 2

    second = ensure_workstation_embedding_model(tmp_path, artifact)
    assert second == first
    # Recognised by the recorded digest, so nothing is fetched again.
    assert len(offline) == 2


def test_a_half_written_copy_is_fetched_again(
    tmp_path: Path, offline: list[str]
) -> None:
    artifact = _artifact()
    root = ensure_workstation_embedding_model(tmp_path, artifact)
    (root / ".files-sha256").unlink()

    ensure_workstation_embedding_model(tmp_path, artifact)

    # The directory existed and had the right name. That is not the question.
    assert len(offline) == 4


def test_weights_that_do_not_match_the_pin_are_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _tampered(url: str, destination: Path) -> None:
        destination.write_bytes(b"something else")

    monkeypatch.setattr(embedding_model, "_download", _tampered)

    with pytest.raises(OperationsError) as error:
        ensure_workstation_embedding_model(tmp_path, _artifact())

    assert "does not match its pin" in str(error.value)


def test_the_directory_is_named_after_the_encoder_not_its_build() -> None:
    first_root = host_embedding_model_root(_artifact())
    other = EmbeddingModelArtifact(
        model_id="test-encoder",
        repo="Example/test-encoder",
        revision="main",
        files={
            "onnx/model_quantized.onnx": hashlib.sha256(b"newer").hexdigest(),
            "tokenizer.json": hashlib.sha256(_TOKENIZER).hexdigest(),
        },
    )

    # A palace is bound to the encoder's identity, which Chroma records on the
    # collection — not to a particular build of its weights. And the Host env
    # that points at this directory is a fixed string, so the path has to be
    # one a fixed string can name.
    other_root = host_embedding_model_root(other)
    assert other_root == first_root
    assert first_root == contract.HOST_EMBEDDING_MODEL_ROOT / "test-encoder"


def test_the_host_reports_what_it_holds_by_digest(tmp_path: Path) -> None:
    destination = tmp_path / "bge-base-zh-abc"

    absent = staging.embedding_model_state({"destination": str(destination)})
    assert absent["status"] == "absent"

    destination.mkdir()
    (destination / ".files-sha256").write_text("d1", encoding="utf-8")
    held = staging.embedding_model_state({"destination": str(destination)})
    assert held == {
        "status": "held",
        "destination": str(destination),
        "digest": "d1",
    }


def test_an_install_outside_the_model_root_is_refused(tmp_path: Path) -> None:
    staged = tmp_path / "staged"
    (staged / "onnx").mkdir(parents=True)
    (staged / ".files-sha256").write_text("d1", encoding="utf-8")

    with pytest.raises(TargetError) as error:
        staging.install_embedding_model(
            {"staging": str(staged), "destination": str(tmp_path / "elsewhere")}
        )

    assert "outside the model root" in str(error.value) or "model root" in str(
        error.value
    )


def test_the_shipped_pin_is_the_encoder_the_host_env_selects() -> None:
    # The env names an encoder and this carries its weights. Two places, one
    # decision — and the failure when they disagree is silent: the embedder
    # falls back to the model hub, which a Host may not be able to reach.
    assert f"EIDOLON_MEMORY_EMBEDDING_MODEL={PINNED_EMBEDDING_MODEL.model_id}\n" in (
        contract.HOST_ENV_VALUE
    )
    assert (
        f"EIDOLON_MEMORY_EMBEDDING_MODEL_DIR={contract.HOST_EMBEDDING_MODEL_ROOT}"
        in contract.HOST_ENV_VALUE
    )
    # And the env names the directory the carrier actually creates.
    assert f"EIDOLON_MEMORY_EMBEDDING_MODEL_DIR={host_embedding_model_root()}\n" in (
        contract.HOST_ENV_VALUE
    )
