import errno
from pathlib import Path
import socket
import tempfile
from unittest.mock import patch

import pytest

from eidolon_ops.supervisor_socket import reclaim_abandoned_socket


@pytest.fixture
def endpoint():
    # macOS Unix socket paths are limited to 104 bytes.
    with tempfile.TemporaryDirectory(dir="/tmp", prefix="sv-") as root:
        yield Path(root) / "control.sock"


def test_missing_endpoint_is_idempotent(endpoint):
    assert not reclaim_abandoned_socket(endpoint)


def test_closed_listener_is_reclaimed_and_can_be_rebound(endpoint):
    with socket.socket(socket.AF_UNIX) as listener:
        listener.bind(str(endpoint))
        listener.listen()
    assert reclaim_abandoned_socket(endpoint)
    assert not reclaim_abandoned_socket(endpoint)
    with socket.socket(socket.AF_UNIX) as restarted:
        restarted.bind(str(endpoint))
        restarted.listen()


def test_live_listener_is_preserved(endpoint):
    with socket.socket(socket.AF_UNIX) as listener:
        listener.bind(str(endpoint))
        listener.listen()
        with pytest.raises(RuntimeError, match="has a listener"):
            reclaim_abandoned_socket(endpoint)
        assert endpoint.exists()


@pytest.mark.parametrize("kind", ["file", "symlink"])
def test_non_socket_is_preserved(endpoint, kind):
    if kind == "file":
        endpoint.write_text("unrelated")
    else:
        endpoint.symlink_to("missing")
    with pytest.raises(RuntimeError, match="non-socket"):
        reclaim_abandoned_socket(endpoint)
    assert endpoint.lstat()


@pytest.mark.parametrize("error", [PermissionError(errno.EACCES, "denied"), TimeoutError()])
def test_unknown_socket_state_is_preserved(endpoint, error):
    with socket.socket(socket.AF_UNIX) as listener:
        listener.bind(str(endpoint))
    with patch.object(socket.socket, "connect", side_effect=error):
        with pytest.raises(RuntimeError, match="cannot establish"):
            reclaim_abandoned_socket(endpoint)
    assert endpoint.exists()


def test_replaced_endpoint_is_preserved(endpoint):
    with socket.socket(socket.AF_UNIX) as listener:
        listener.bind(str(endpoint))
    def replace(_self, _path):
        endpoint.unlink()
        endpoint.write_text("replacement")
        raise ConnectionRefusedError(errno.ECONNREFUSED, "refused")
    with patch.object(socket.socket, "connect", replace):
        with pytest.raises(RuntimeError, match="changed during"):
            reclaim_abandoned_socket(endpoint)
    assert endpoint.read_text() == "replacement"
