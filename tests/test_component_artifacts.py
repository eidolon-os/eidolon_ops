"""What a Host holds that no release contains, and how it gets there.

Every pin here is read from the component that declared it rather than
restated: the point of the contract is that there is one place to look.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from eidolon_ops import component_artifacts
from eidolon_ops.capabilities import HOST_CAPABILITIES
from eidolon_ops.component_artifacts import (
    CarriedArtifact,
    CarriedFile,
    carried_artifacts,
    ensure_workstation_artifact,
    host_artifact_root,
    workstation_artifact_root,
)
from eidolon_ops.component_contract import read_component_contracts
from eidolon_ops.errors import OperationsError
from eidolon_ops.hostagent import contract, staging
from eidolon_ops.hostagent.primitives import TargetError

pytestmark = pytest.mark.unit

_WEIGHTS = b"onnx-weights"
_TOKENIZER = b"tokenizer"


def _artifact(model_id: str = "test-encoder") -> CarriedArtifact:
    return CarriedArtifact(
        component_id="eidolon_test",
        artifact_id="test-encoder",
        kind="model",
        install_root=contract.HOST_MODEL_ROOT / model_id,
        files=(
            CarriedFile(
                path="onnx/model_quantized.onnx",
                sha256=hashlib.sha256(_WEIGHTS).hexdigest(),
                url="https://example.invalid/model_quantized.onnx",
            ),
            CarriedFile(
                path="tokenizer.json",
                sha256=hashlib.sha256(_TOKENIZER).hexdigest(),
                url="https://example.invalid/tokenizer.json",
            ),
        ),
    )


#: The sibling checkouts a workstation has. A release operates whatever the
#: matrix pins, which may predate any of this, so an absent sibling skips
#: rather than fails.
_CHECKOUT_ROOT = Path(__file__).resolve().parents[2]


def _declared(capabilities: frozenset[str]) -> tuple[CarriedArtifact, ...]:
    from eidolon_ops.config import expected_sources

    sources = {
        source_id: _CHECKOUT_ROOT / source_id
        for source_id in sorted(expected_sources(capabilities))
    }
    missing = [source_id for source_id, path in sources.items() if not (path / "ops").is_dir()]
    if missing:
        pytest.skip(f"no sibling checkout with a contract for: {', '.join(missing)}")
    return carried_artifacts(read_component_contracts(sources, capabilities))


@pytest.fixture(scope="module")
def declared() -> tuple[CarriedArtifact, ...]:
    """Every artifact any Host can be asked to hold.

    Read with every capability, not none: a conditional artifact read with none
    simply is not there, and each check below would pass by not seeing it.
    """

    return _declared(HOST_CAPABILITIES)


def test_the_encoder_is_the_memory_512_dimension_identity(
    declared: tuple[CarriedArtifact, ...]
) -> None:
    encoder = next(item for item in declared if item.artifact_id == "bge-small-zh")
    assert encoder.component_id == "eidolon_memory"
    assert encoder.install_root == contract.HOST_MODEL_ROOT / "bge-small-zh"
    assert {item.path for item in encoder.files} == {
        "onnx/model_quantized.onnx",
        "tokenizer.json",
    }


def test_the_chat_weights_are_declared_by_the_component_that_serves_them(
    declared: tuple[CarriedArtifact, ...]
) -> None:
    """And only reach a Host that declared the capability — see below."""

    model = next(item for item in declared if item.artifact_id == "qwen3-1.7b-q4_0")
    assert model.component_id == "eidolon_models"
    assert model.install_root == contract.HOST_MODEL_ROOT / "qwen3-1.7b"
    assert [item.path for item in model.files] == ["Qwen3-1.7B-Q4_0.gguf"]


def test_an_artifact_a_host_has_no_capability_for_is_not_carried() -> None:
    """Dropped where every other conditional entry is dropped, not skipped here.

    A carry that filtered on its own would be a second place capabilities are
    interpreted, and the two would eventually disagree.
    """

    without = _declared(frozenset())

    assert "qwen3-1.7b-q4_0" not in {item.artifact_id for item in without}
    # The encoder is unconditional: every Host remembers.
    assert "bge-small-zh" in {item.artifact_id for item in without}


def test_every_pinned_file_names_an_immutable_source(
    declared: tuple[CarriedArtifact, ...]
) -> None:
    """A branch can be moved under a digest, and then the pin fails rather than
    the file being found wrong. Both hubs in use spell a commit in the path."""

    assert declared
    for artifact in declared:
        for item in artifact.files:
            assert item.url.startswith("https://"), item
            assert "/resolve/main/" not in item.url, item
            assert "/refs/heads/" not in item.url, item


def test_the_set_digest_changes_when_any_file_does() -> None:
    """One digest over the whole set, so a partial copy is not a copy."""

    first = _artifact()
    changed = CarriedArtifact(
        component_id=first.component_id,
        artifact_id=first.artifact_id,
        kind=first.kind,
        install_root=first.install_root,
        files=(first.files[0], CarriedFile("tokenizer.json", "0" * 64, first.files[1].url)),
    )

    assert changed.digest != first.digest
    # And not on the order the files happen to be declared in.
    reordered = CarriedArtifact(
        component_id=first.component_id,
        artifact_id=first.artifact_id,
        kind=first.kind,
        install_root=first.install_root,
        files=tuple(reversed(first.files)),
    )
    assert reordered.digest == first.digest


@pytest.fixture
def offline(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    fetched: list[str] = []

    def _download(url: str, destination: Path) -> None:
        fetched.append(url)
        payload = _WEIGHTS if url.endswith(".onnx") else _TOKENIZER
        destination.write_bytes(payload)

    monkeypatch.setattr(component_artifacts, "_download", _download)
    return fetched


def test_the_pin_is_carried_once_and_recognised_afterwards(
    tmp_path: Path, offline: list[str]
) -> None:
    artifact = _artifact()

    first = ensure_workstation_artifact(tmp_path, artifact)
    assert (first / "onnx" / "model_quantized.onnx").read_bytes() == _WEIGHTS
    assert (first / "tokenizer.json").read_bytes() == _TOKENIZER
    assert len(offline) == 2

    second = ensure_workstation_artifact(tmp_path, artifact)
    assert second == first
    # Recognised by the recorded digest, so nothing is fetched again.
    assert len(offline) == 2


def test_a_half_written_copy_is_fetched_again(tmp_path: Path, offline: list[str]) -> None:
    artifact = _artifact()
    root = ensure_workstation_artifact(tmp_path, artifact)
    (root / ".files-sha256").unlink()

    ensure_workstation_artifact(tmp_path, artifact)

    # The directory existed and had the right name. That is not the question.
    assert len(offline) == 4


def test_a_damaged_cached_file_is_refetched_despite_its_matching_record(tmp_path, offline):
    artifact = _artifact()
    root = ensure_workstation_artifact(tmp_path, artifact)
    (root / "tokenizer.json").write_bytes(b"damaged")
    ensure_workstation_artifact(tmp_path, artifact)
    assert (root / "tokenizer.json").read_bytes() == _TOKENIZER
    assert len(offline) == 4


def test_files_that_do_not_match_the_pin_are_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _tampered(url: str, destination: Path) -> None:
        destination.write_bytes(b"something else")

    monkeypatch.setattr(component_artifacts, "_download", _tampered)

    with pytest.raises(OperationsError) as error:
        ensure_workstation_artifact(tmp_path, _artifact())

    assert "does not match its pin" in str(error.value)


def test_the_directory_is_named_by_the_component_not_by_the_pin() -> None:
    """A palace is bound to the encoder's identity, which Chroma records on the
    collection — not to a particular build of its weights. And the Host env
    that points at this directory is a fixed string, so the path has to be one
    a fixed string can name."""

    first = _artifact()
    newer = CarriedArtifact(
        component_id=first.component_id,
        artifact_id=first.artifact_id,
        kind=first.kind,
        install_root=first.install_root,
        files=(CarriedFile("onnx/model_quantized.onnx", "1" * 64, first.files[0].url),),
    )

    assert host_artifact_root(newer) == host_artifact_root(first)
    assert host_artifact_root(first) == contract.HOST_MODEL_ROOT / "test-encoder"
    assert workstation_artifact_root(Path("/toolchain"), first) == (
        Path("/toolchain/models/test-encoder")
    )


def test_a_contract_asking_for_a_destination_outside_the_model_root_is_refused() -> None:
    """Refused by the tool reading the contract as well as by the Host.

    An operator finds out from the thing that read the file, rather than from a
    Host halfway through a carry.
    """

    stray = CarriedArtifact(
        component_id="eidolon_test",
        artifact_id="stray",
        kind="model",
        install_root=Path("/etc/eidolon/somewhere"),
        files=_artifact().files,
    )

    with pytest.raises(OperationsError) as error:
        host_artifact_root(stray)

    assert "outside" in str(error.value)


def _manifest(root: Path) -> tuple[dict[str, str], str]:
    files = {"onnx/model.onnx": hashlib.sha256(b"weights").hexdigest()}
    digest = hashlib.sha256("\n".join(f"{name} {value}" for name, value in sorted(files.items())).encode()).hexdigest()
    (root / "onnx").mkdir(parents=True)
    (root / "onnx/model.onnx").write_bytes(b"weights")
    (root / contract.EMBEDDING_DIGEST_RECORD).write_text(digest)
    return files, digest


def test_the_host_rehashes_content_even_when_the_digest_record_matches(tmp_path, monkeypatch):
    monkeypatch.setattr(contract, "HOST_MODEL_ROOT", tmp_path)
    destination = tmp_path / "encoder"
    files, digest = _manifest(destination)
    payload = {"destination": str(destination), "files": files}
    assert staging.component_artifact_state(payload)["digest"] == digest
    (destination / "onnx/model.onnx").write_bytes(b"damaged")
    assert staging.component_artifact_state(payload)["status"] == "absent"


def test_an_install_outside_the_model_root_is_refused(tmp_path):
    staged = tmp_path / "staged"
    files, _ = _manifest(staged)
    with pytest.raises(TargetError, match="model root"):
        staging.install_component_artifact({"staging": str(staged), "destination": str(tmp_path / "elsewhere"), "files": files})


@pytest.mark.parametrize("fault", ["corrupt", "symlink", "extra", "occupied"])
def test_bad_or_conflicting_models_leave_existing_content_untouched(tmp_path, monkeypatch, fault):
    monkeypatch.setattr(contract, "HOST_MODEL_ROOT", tmp_path / "models")
    monkeypatch.setattr(contract, "VAR_TMP", tmp_path)
    staged = tmp_path / "eidolon-artifact-test"
    files, _ = _manifest(staged)
    destination = tmp_path / "models/encoder"
    _manifest(destination)
    if fault == "corrupt":
        (staged / "onnx/model.onnx").write_bytes(b"bad")
    elif fault == "symlink":
        (staged / "onnx/model.onnx").unlink()
        (staged / "onnx/model.onnx").symlink_to(destination / "onnx/model.onnx")
    elif fault == "extra":
        (staged / "extra").write_bytes(b"unexpected")
    else:
        (destination / "onnx/model.onnx").write_bytes(b"old pin")
    before = (destination / "onnx/model.onnx").read_bytes()
    with pytest.raises(TargetError):
        staging.install_component_artifact({"staging": str(staged), "destination": str(destination), "files": files})
    assert (destination / "onnx/model.onnx").read_bytes() == before


def test_the_declared_encoder_is_the_one_the_host_env_selects(
    declared: tuple[CarriedArtifact, ...]
) -> None:
    # The env names an encoder and the contract carries its weights. Two
    # places, one decision — and the failure when they disagree is silent: the
    # embedder falls back to the model hub, which a Host may not be able to
    # reach.
    encoder = next(item for item in declared if item.artifact_id == "bge-small-zh")
    assert f"EIDOLON_MEMORY_EMBEDDING_MODEL={encoder.install_root.name}\n" in (
        contract.HOST_ENV_VALUE
    )
    assert f"EIDOLON_MEMORY_EMBEDDING_MODEL_DIR={encoder.install_root}\n" in (
        contract.HOST_ENV_VALUE
    )


def test_the_declared_chat_weights_are_the_file_the_unit_launches(
    declared: tuple[CarriedArtifact, ...]
) -> None:
    """Same pairing, for the model the Agent talks to.

    The unit names an absolute path and the contract says what lands there. A
    disagreement here is not silent — the launcher refuses to start with no
    model — but it is only found on a board, which is the wrong place.
    """

    unit_path = _CHECKOUT_ROOT / "eidolon_models" / "deploy/systemd/eidolon-llm.service"
    if not unit_path.is_file():
        pytest.skip("no sibling eidolon_models checkout")
    unit = unit_path.read_text(encoding="utf-8")
    model = next(item for item in declared if item.artifact_id == "qwen3-1.7b-q4_0")
    expected = model.install_root / model.files[0].path

    assert f"EIDOLON_LLM_MODEL={expected}\n" in unit


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

    monkeypatch.setattr(contract, "HOST_MODEL_ROOT", tmp_path / "models")
    monkeypatch.setattr(contract, "VAR_TMP", tmp_path)
    monkeypatch.setattr(staging.os, "chown", lambda *args, **kwargs: None)
    staged = tmp_path / "eidolon-artifact-abc"
    files, digest = _manifest(staged)
    destination = tmp_path / "models/bge-small-zh"
    result = staging.install_component_artifact({"staging": str(staged), "destination": str(destination), "files": files})
    assert result["status"] == "installed" and result["digest"] == digest
    assert not staged.exists()
    assert (destination / "onnx/model.onnx").read_bytes() == b"weights"
    assert staging.component_artifact_state({"destination": str(destination), "files": files})["digest"] == digest


def test_both_sides_spell_the_digest_record_the_same_way() -> None:
    """The agent ships to the Host alone, so it cannot import the name.

    Two copies of one string, and the failure when they drift is not loud: the
    operator writes a record the Host then refuses to find, and every carried
    model is rejected as having no digest.
    """

    assert contract.EMBEDDING_DIGEST_RECORD == component_artifacts.DIGEST_RECORD
