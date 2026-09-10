import pytest

from eidolon_ops.controller import EidolonPiController
from eidolon_ops.errors import OperationsError
from eidolon_ops.hostagent import probe


@pytest.mark.parametrize("service,supported", [
    (None, False),
    ({"service_id": "livekit", "runtime_state": "ready"}, False),
    ({"service_id": "livekit", "network_current": None}, False),
    ({"service_id": "other", "network_current": True}, False),
    ({"service_id": "livekit", "network_current": "true"}, False),
    ({"service_id": "livekit", "network_current": False}, True),
    ({"service_id": "livekit", "network_current": True}, True),
])
def test_restore_checks_protocol_support_without_requiring_pre_restore_health(monkeypatch, service, supported):
    monkeypatch.setattr(probe.primitives, "unix_http_json", lambda *_args: service)
    actions = []

    class Transport:
        def run_agent(self, action, payload, **_kwargs):
            actions.append(action)
            assert action == "readiness-compatibility"
            return probe.readiness_compatibility(payload)

    controller = EidolonPiController.__new__(EidolonPiController)
    controller.transport = Transport()
    if supported:
        assert controller._require_authority_restore_readiness()["status"] == "compatible"
    else:
        with pytest.raises(OperationsError, match="AUTHORITY_RESTORE_READINESS_INCOMPATIBLE"):
            controller._require_authority_restore_readiness()
    assert actions == ["readiness-compatibility"]
