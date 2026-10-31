from pathlib import Path

from eidolon_ops.capabilities import require_known_capability
from eidolon_ops.component_artifacts import carried_artifacts
from eidolon_ops.component_contract import read_component_contracts
from eidolon_ops.config import expected_sources, expected_units

ROOT = Path(__file__).resolve().parents[2]


def test_speaker_assets_select_models_without_a_new_service():
    capability = "local_speaker_verification"
    require_known_capability(capability, label="test")
    assert "eidolon_models" in expected_sources(frozenset({capability}))
    assert expected_units(frozenset({capability})) == expected_units(frozenset())
    sources = {"eidolon_models": ROOT / "eidolon_models"}
    absent = carried_artifacts(read_component_contracts(sources, frozenset()))
    selected = carried_artifacts(read_component_contracts(sources, frozenset({capability})))
    assert not any(x.artifact_id.startswith("campplus") for x in absent)
    assert len(selected) == 1
    assert selected[0].artifact_id.startswith("campplus-zh-16k-")


def test_selected_model_paths_follow_the_pinned_contract_not_worktree():
    from types import SimpleNamespace

    from eidolon_ops.component_artifacts import (
        capability_artifact,
        host_artifact_root,
        workstation_artifact_root,
    )

    contract = (ROOT / "eidolon_models/ops/component.toml").read_text()
    import tomllib

    original = next(
        a["id"] for a in tomllib.loads(contract)["artifacts"]
        if a.get("requires_capability") == "local_speaker_verification"
    )
    pinned = contract.replace(original, "review-future-model")
    cfg = SimpleNamespace(
        capabilities=frozenset({"local_speaker_verification"}),
        sources={
            "eidolon_models": SimpleNamespace(
                path=ROOT / "eidolon_models", revision="exact-revision"
            )
        },
    )
    calls = []

    def read(source, revision, path):
        calls.append((source, revision, path))
        return pinned

    artifact = capability_artifact(
        cfg, read, component_id="eidolon_models", capability="local_speaker_verification"
    )
    assert host_artifact_root(artifact) == Path("/var/lib/eidolon/models/review-future-model")
    assert workstation_artifact_root(Path("/source-host/state"), artifact) == Path(
        "/source-host/state/models/review-future-model"
    )
    assert calls == [("eidolon_models", "exact-revision", "ops/component.toml")]


def test_missing_or_ambiguous_model_declaration_is_not_a_guess():
    from types import SimpleNamespace

    import pytest

    from eidolon_ops.component_artifacts import capability_artifact
    from eidolon_ops.errors import OperationsError

    contract = (ROOT / "eidolon_models/ops/component.toml").read_text()
    start = contract.index("# Shared Channel inference;")
    cfg = SimpleNamespace(
        capabilities=frozenset({"local_speaker_verification"}),
        sources={"eidolon_models": SimpleNamespace(path=ROOT / "eidolon_models", revision="exact")},
    )
    for text in (
        contract[:start],
        contract
        + "\n"
        + contract[start:].replace(
            __import__("tomllib").loads(contract)["artifacts"][-1]["id"], "second-speaker-model"
        ),
    ):
        with pytest.raises(OperationsError, match="exactly one"):
            capability_artifact(
                cfg,
                lambda *_, text=text: text,
                component_id="eidolon_models",
                capability="local_speaker_verification",
            )
    cfg.capabilities = frozenset()
    assert (
        capability_artifact(
            cfg,
            lambda *_: pytest.fail("disabled capability read assets"),
            component_id="eidolon_models",
            capability="local_speaker_verification",
        )
        is None
    )


def test_generated_channel_settings_follow_model_pin_and_reserve_delivery_path():
    from types import SimpleNamespace

    import pytest

    from eidolon_ops.component_artifacts import capability_artifact
    from eidolon_ops.errors import InstallInputError
    from eidolon_ops.product_settings import product_settings
    from eidolon_ops.settings_overlay import OverlayAssignment, parse_path

    sources = {
        name: SimpleNamespace(path=ROOT / name, revision="pin-" + name)
        for name in ("eidolon_models", "eidolon_channel", "eidolon_agent", "eidolon_memory")
    }
    cfg = SimpleNamespace(
        sources=sources, capabilities=frozenset({"local_speaker_verification"}), settings_overlay=()
    )

    def read(name, revision, path):
        assert revision == "pin-" + name
        return (sources[name].path / path).read_text()

    artifact = capability_artifact(
        cfg, read, component_id="eidolon_models", capability="local_speaker_verification"
    )
    document = product_settings(cfg, read)["channel.yaml"]
    assert str(artifact.install_root) in document
    cfg.settings_overlay = (
        OverlayAssignment(
            "channel.yaml", parse_path("voiceprint.model_dir", label="test"), "/another/model"
        ),
    )
    with pytest.raises(InstallInputError, match="derived"):
        product_settings(cfg, read)

    cfg.settings_overlay = (
        OverlayAssignment(
            "channel.yaml",
            parse_path("voiceprint.calibration_path", label="test"),
            "not-delivered.json",
        ),
    )
    with pytest.raises(InstallInputError, match="voiceprint.calibration_path"):
        product_settings(cfg, read)
