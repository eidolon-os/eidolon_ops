"""The encoder a Host reads memories with, and how it gets there."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from eidolon_ops import embedding_model
from eidolon_ops.embedding_model import (
    PINNED_EMBEDDING_MODEL,
    EmbeddingModelArtifact,
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


def test_the_shipped_encoder_is_the_memory_512_dimension_identity() -> None:
    assert PINNED_EMBEDDING_MODEL.model_id == "bge-small-zh"
    assert PINNED_EMBEDDING_MODEL.repo == "Xenova/bge-small-zh-v1.5"
    assert PINNED_EMBEDDING_MODEL.revision != "main"
    assert PINNED_EMBEDDING_MODEL.files == {
        "onnx/model_quantized.onnx": (
            "15b717c382bcb518ba457b93ea6850ede7f4f1cd8937454aa06972366cd19bcc"
        ),
        "tokenizer.json": (
            "48cea5d44424912a6fd1ea647bf4fe50b55ab8b1e5879c3275f80e339e8fae26"
        ),
    }


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
    destination = tmp_path / "bge-small-zh-abc"

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


def test_a_host_learns_a_path_that_did_not_exist_when_it_was_installed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """host.env is derived, so it is rewritten rather than defended.

    It used to be refused when it differed, which reads like safety and is
    not: a Host installed before a path existed could never be told about it.
    That is how this Host ended up holding an encoder it had been given and
    could not find — the line naming the directory was added to the contract
    and had nowhere to land.
    """

    root = tmp_path
    (root / "etc" / "eidolon").mkdir(parents=True)
    host_env = root / "etc" / "eidolon" / "host.env"
    host_env.write_text("EIDOLON_STATE_ROOT=/var/lib/eidolon\n", encoding="utf-8")

    contract.ensure_host_path_contract(root, lambda *_a: None, "ports: {}\n")

    assert host_env.read_text(encoding="utf-8") == contract.host_env_value(frozenset())
    assert "EIDOLON_MEMORY_EMBEDDING_MODEL_DIR" in host_env.read_text(encoding="utf-8")


def test_carrying_a_model_in_reports_the_digest_of_what_is_now_held(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The success path, which nothing exercised until a Host was wiped.

    The only test this function had went through the refusal branch — a
    destination outside the model root — and returned before anything was
    written. So the line after the move was never run, and it read the digest
    record through a path into the staging directory that the move had just
    taken away. A first install on a Host with no encoder yet therefore failed
    with FileNotFoundError, having already put the weights correctly in place.

    ``os.chown`` is stubbed because the agent runs as root on the Host and this
    test does not; the ownership call is not what is under test, and leaving it
    real would only mean this path stays untested for another reason.
    """

    monkeypatch.setattr(contract, "HOST_EMBEDDING_MODEL_ROOT", tmp_path / "models")
    monkeypatch.setattr(staging.os, "chown", lambda *args, **kwargs: None)

    staged = tmp_path / "eidolon-encoder-abc"
    (staged / "onnx").mkdir(parents=True)
    (staged / "onnx" / "model.onnx").write_bytes(b"weights")
    (staged / contract.EMBEDDING_DIGEST_RECORD).write_text("d1\n", encoding="utf-8")
    destination = tmp_path / "models" / "bge-small-zh"

    result = staging.install_embedding_model(
        {"staging": str(staged), "destination": str(destination)}
    )

    assert result == {
        "status": "installed",
        "destination": str(destination),
        "digest": "d1",
    }
    # Moved, not copied: a staging directory left behind is what the next
    # install would find half-written.
    assert not staged.exists()
    assert (destination / "onnx" / "model.onnx").read_bytes() == b"weights"
    # And the Host now answers the same digest when asked what it holds.
    assert staging.embedding_model_state({"destination": str(destination)}) == {
        "status": "held",
        "destination": str(destination),
        "digest": "d1",
    }


def test_both_sides_spell_the_digest_record_the_same_way() -> None:
    """The agent ships to the Host alone, so it cannot import the name.

    Two copies of one string, and the failure when they drift is not loud: the
    operator writes a record the Host then refuses to find, and every carried
    model is rejected as having no digest.
    """

    from eidolon_ops.embedding_model import EMBEDDING_DIGEST_RECORD

    assert contract.EMBEDDING_DIGEST_RECORD == EMBEDDING_DIGEST_RECORD
