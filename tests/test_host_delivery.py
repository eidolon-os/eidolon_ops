import json
from pathlib import Path

import pytest

from eidolon_ops.host_delivery import bind_delivery
from eidolon_ops.hostagent.hardware import (
    BINDING_FILE,
    HostHardwareError,
    binding_for,
    observe_hardware,
    verify_binding,
)

HOST = 'ehost-' + 'a' * 20
A = {'kind': 'device-tree:raspberrypi,5-model-b', 'fingerprint': 'sha256:' + 'a' * 64}
B = {**A, 'fingerprint': 'sha256:' + 'b' * 64}


def test_delivery_retries_preserve_identity_and_other_board_cannot_reuse_it(tmp_path):
    original = bind_delivery(tmp_path, HOST, A)
    assert bind_delivery(tmp_path, HOST, A) == original
    with pytest.raises(HostHardwareError, match='another board'):
        bind_delivery(tmp_path, HOST, B)
    assert (tmp_path / BINDING_FILE).read_bytes() == original


def test_replacing_one_key_alone_does_not_rebind_delivery(tmp_path):
    bind_delivery(tmp_path, HOST, A)
    with pytest.raises(HostHardwareError):
        bind_delivery(tmp_path, 'ehost-' + 'b' * 20, A)


def test_board_identity_does_not_use_network_or_machine_id(tmp_path):
    tree = tmp_path / 'proc/device-tree'
    tree.mkdir(parents=True)
    (tree / 'serial-number').write_bytes(b'1000000012345678\0')
    (tree / 'compatible').write_bytes(b'raspberrypi,5-model-b\0brcm,bcm2712\0')
    before = observe_hardware(root=tmp_path, system='Linux')
    (tmp_path / 'etc').mkdir()
    (tmp_path / 'etc/machine-id').write_text('new os instance')
    assert observe_hardware(root=tmp_path, system='Linux') == before
    (tree / 'serial-number').write_bytes(b'1000000098765432\0')
    assert observe_hardware(root=tmp_path, system='Linux') != before


def test_no_hardware_falls_back_to_neither_os_id_nor_mac(tmp_path):
    with pytest.raises(HostHardwareError, match='permanent hardware'):
        observe_hardware(root=tmp_path, system='Linux')


def test_binding_checks_both_host_and_hardware():
    raw = json.dumps(binding_for(HOST, A)).encode()
    verify_binding(raw, HOST, A)
    with pytest.raises(HostHardwareError):
        verify_binding(raw, HOST, B)


def test_injected_agent_contains_the_same_hardware_observer():
    import base64

    from eidolon_ops.hostagent import hardware
    from eidolon_ops.hostagent_delivery import injected_script
    assert base64.b64encode(Path(hardware.__file__).read_bytes()) in injected_script()


def test_mac_hardware_uses_platform_uuid_not_network_interface():
    import plistlib
    from types import SimpleNamespace
    def run(command, **kwargs):
        assert 'IOPlatformExpertDevice' in command
        return SimpleNamespace(stdout=plistlib.dumps([{'IOPlatformUUID': 'AAAAAAAA-BBBB-CCCC-DDDD-EEEEEEEEEEEE'}]))
    result = observe_hardware(system='Darwin', run=run)
    assert result['kind'] == 'apple-platform-uuid'


@pytest.mark.parametrize('serial', [b'', b'0000000000000000\0', b'ffffffffffffffff\0'])
def test_invalid_board_serial_is_not_silently_accepted(tmp_path, serial):
    tree = tmp_path / 'proc/device-tree'
    tree.mkdir(parents=True)
    (tree / 'serial-number').write_bytes(serial)
    with pytest.raises(HostHardwareError):
        observe_hardware(root=tmp_path, system='Linux')


def test_used_legacy_identity_is_not_silently_bound_to_current_board(tmp_path):
    from eidolon_ops.errors import OperationsError
    with pytest.raises(OperationsError, match="no hardware delivery evidence"):
        bind_delivery(tmp_path, HOST, A, allow_create=False)
    assert not (tmp_path / BINDING_FILE).exists()


def test_consumed_bound_delivery_can_resume_on_same_board(tmp_path):
    original = bind_delivery(tmp_path, HOST, A)
    assert bind_delivery(tmp_path, HOST, A, allow_create=False) == original
