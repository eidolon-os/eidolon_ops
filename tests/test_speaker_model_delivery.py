from pathlib import Path

from eidolon_ops.capabilities import require_known_capability
from eidolon_ops.component_artifacts import carried_artifacts
from eidolon_ops.component_contract import read_component_contracts
from eidolon_ops.config import expected_sources, expected_units

ROOT = Path(__file__).resolve().parents[2]


def test_speaker_assets_select_models_without_a_new_service():
    capability = 'local_speaker_verification'
    require_known_capability(capability, label='test')
    assert 'eidolon_models' in expected_sources(frozenset({capability}))
    assert expected_units(frozenset({capability})) == expected_units(frozenset())
    sources = {'eidolon_models': ROOT / 'eidolon_models'}
    absent = carried_artifacts(read_component_contracts(sources, frozenset()))
    selected = carried_artifacts(read_component_contracts(sources, frozenset({capability})))
    assert not any(x.artifact_id.startswith('campplus') for x in absent)
    assert [x.artifact_id for x in selected] == ['campplus-zh-16k-624dcb0f']
