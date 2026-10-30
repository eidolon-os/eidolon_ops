"""Model selection follows the active backend, not all files in a checkout."""
from pathlib import Path
from types import SimpleNamespace
import pytest
from eidolon_ops.component_contract import load_component_contract
from eidolon_ops.component_artifacts import carried_artifacts
from eidolon_ops.source_model_artifacts import runtime_model_artifacts
from eidolon_ops.errors import OperationsError

ROOT = Path(__file__).resolve().parents[2] / 'eidolon_models'


def declared():
    return load_component_contract(ROOT, 'eidolon_models'), (ROOT / 'laya/deploy/services.toml').read_text()


def test_source_backend_selects_only_needed_artifact():
    contract, services = declared()
    caps = frozenset({'local_laya', 'local_laya_participation'})
    selected = runtime_model_artifacts(contract, services, caps, participation_upstream_url='http://model/d3/v1/systemone')
    assert len(selected) == 1
    assert 'torch' in selected[0].artifact_id and 'smart-home' in selected[0].artifact_id
    assert not any('/npu/' in f.url or f.path.endswith('.rknn') for f in selected[0].files)
    local = runtime_model_artifacts(contract, services, caps)
    assert len(local) == 2 and all('torch' in a.artifact_id for a in local)
    npu = runtime_model_artifacts(contract, services, caps | {'rknpu2'})
    assert len(npu) == 2 and all('rknn' in a.artifact_id for a in npu)
    assert runtime_model_artifacts(contract, services, frozenset({'local_laya_participation'}), participation_upstream_url='http://model/d3/v1/systemone') == ()
    assert runtime_model_artifacts(contract, services, frozenset()) == ()
    delivered = carried_artifacts(SimpleNamespace(contracts=(contract.select(caps | {'rknpu2'}),)))
    assert not any('torch' in a.artifact_id for a in delivered)


def test_runtime_model_root_must_be_a_declared_pin():
    contract, services = declared()
    services = services.replace('/var/lib/eidolon/models/laya-smart-home-c10-torch-e8254243', '/tmp/untracked-model')
    with pytest.raises(OperationsError, match='exactly one pinned artifact'):
        runtime_model_artifacts(contract, services, frozenset({'local_laya'}))
