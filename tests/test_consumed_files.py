"""A component's reader declares what it opens; a release must carry it (M6)."""

from __future__ import annotations

import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from eidolon_ops.component_artifacts import require_consumed_files
from eidolon_ops.errors import OperationsError

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]
CAPABILITY = "local_speaker_verification"


def _release(models_contract: str, *, capabilities=frozenset({CAPABILITY}), sources=None):
    sources = sources or ("eidolon_channel", "eidolon_models")
    config = SimpleNamespace(
        capabilities=capabilities,
        sources={
            name: SimpleNamespace(path=ROOT / name, revision="r" * 40) for name in sources
        },
    )
    contracts = {
        "eidolon_channel": (ROOT / "eidolon_channel/ops/component.toml").read_text(),
        "eidolon_models": models_contract,
    }
    return config, contracts.get, (lambda name, _rev, _path: contracts[name])


def _models() -> str:
    return (ROOT / "eidolon_models/ops/component.toml").read_text()


def test_the_speaker_artifact_carries_everything_channel_opens() -> None:
    confirmed = require_consumed_files(*_release(_models()))
    files = confirmed[f"eidolon_channel<-eidolon_models:{CAPABILITY}"]
    assert "recognition-controlled-trial.json" in files
    assert "campplus_cn_common.bin" in files


def test_an_artifact_without_the_recognition_policy_is_refused() -> None:
    """What c76777b did to the declaration, and nothing refused."""

    dropped = re.sub(
        r'\[\[artifacts\.files\]\]\npath = "recognition-controlled-trial\.json"\n[^\[]*',
        "",
        _models(),
    )
    assert "recognition-controlled-trial.json" not in dropped
    with pytest.raises(
        OperationsError,
        match=r"eidolon_channel reads recognition-controlled-trial\.json from campplus-zh-16k-",
    ):
        require_consumed_files(*_release(dropped))


def test_a_capability_this_host_does_not_offer_asks_nothing() -> None:
    assert require_consumed_files(*_release("", capabilities=frozenset())) == {}


def test_reading_from_a_component_the_release_lacks_is_refused() -> None:
    with pytest.raises(OperationsError, match="does not include"):
        require_consumed_files(*_release(_models(), sources=("eidolon_channel",)))
