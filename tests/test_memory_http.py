"""Exercise the published Memory document and transport, not a mocked request."""

import json
import threading
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from test_target_agent import _FakeMemory

from eidolon_ops import probes
from eidolon_ops.hostagent import memory_realms
from eidolon_ops.hostagent.primitives import TargetError


def test_component_snapshot_and_restore_bypass_system_proxies(tmp_path, monkeypatch):
    component = _FakeMemory()
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_GET(self):
            self.reply(None)

        def do_POST(self):
            self.reply(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))

        def reply(self, body):
            requests.append((self.command, self.path))
            answer = component("", self.path, method=self.command, body=body, timeout=1)
            raw = json.dumps(answer).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

    # Models both environment and macOS proxy settings. If either caller uses
    # the default opener, it attempts an unavailable proxy rather than Memory.
    monkeypatch.setattr(urllib.request, "getproxies", lambda: {"http": "http://127.0.0.1:1"})
    monkeypatch.setattr(urllib.request, "proxy_bypass", lambda _host: False)
    with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        url = f"http://127.0.0.1:{server.server_port}"
        root = tmp_path / "memory"
        bindings = {"own": lambda *_: None, "hand_to_operator": lambda *_: None}
        try:
            assert probes.http_health(url + "/api/admin/realms")["healthy"]
            assert probes.http_json(url + "/api/admin/realms")["memory_available"]
            captured = memory_realms.capture(url, root, **bindings)
            assert captured[0]["memory_space_id"] == "r_owner_one"
            restored = memory_realms.put_back(url, root, captured, **bindings)
            assert restored[0]["memory_space_id"] == "r_owner_one"
            assert ("POST", "/api/admin/realms/r_owner_one/snapshot") in requests
            assert ("POST", "/api/admin/realms/r_owner_one/restore") in requests
        finally:
            server.shutdown()
            thread.join(timeout=5)


@pytest.mark.parametrize("document", [
    [], {}, {"realms": None}, {"realms": [{}]},
    {"realms": [{"spec": {"memory_realm_id": "../escape"}}]},
    {"realms": [{"spec": {"memory_realm_id": "r_one"}}] * 2},
])
def test_invalid_roster_document_is_refused(document, monkeypatch):
    monkeypatch.setattr(memory_realms, "_request", lambda *_a, **_kw: document)
    with pytest.raises(TargetError):
        memory_realms.realm_ids("http://127.0.0.1:8019")


def test_admin_requests_cannot_leave_this_host():
    with pytest.raises(TargetError, match="must be on this Host"):
        memory_realms.realm_ids("http://remote-host:8019")
