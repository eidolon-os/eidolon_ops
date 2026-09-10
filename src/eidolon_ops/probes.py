"""Ask a component how it is, from this workstation.

Everything here reads a surface the component publishes about itself. Nothing
here reconstructs a component's internal state from logs or from a library's
implementation — a probe that guesses is a gate that lies.
"""

from __future__ import annotations

import http.client
import json
import re
import socket
import ssl
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from pathlib import Path

_HTTP_TIMEOUT = 1.5
_STATUS_LINE = re.compile(rb"HTTP/\d(?:\.\d)? (\d{3})(?: |\r)")


def http_health(url: str) -> dict[str, object]:
    """Whether an HTTP(S) endpoint answers 200 right now."""

    context = ssl._create_unverified_context() if url.startswith("https://") else None
    handlers: list[urllib.request.BaseHandler] = [urllib.request.ProxyHandler({})]
    if context is not None:
        handlers.append(urllib.request.HTTPSHandler(context=context))
    opener = urllib.request.build_opener(*handlers)
    try:
        with opener.open(url, timeout=_HTTP_TIMEOUT) as response:
            status = response.status
    except (OSError, urllib.error.URLError):
        return {"healthy": False, "http_status": None}
    return {"healthy": status == 200, "http_status": status}


def http_json(url: str) -> dict[str, object] | None:
    """The JSON body of a 200 answer, or nothing at all."""

    context = ssl._create_unverified_context() if url.startswith("https://") else None
    handlers: list[urllib.request.BaseHandler] = [urllib.request.ProxyHandler({})]
    if context is not None:
        handlers.append(urllib.request.HTTPSHandler(context=context))
    opener = urllib.request.build_opener(*handlers)
    try:
        with opener.open(url, timeout=_HTTP_TIMEOUT) as response:
            if response.status != 200:
                return None
            document = json.loads(response.read(1024 * 1024))
    except (OSError, ValueError, urllib.error.URLError):
        return None
    return document if isinstance(document, dict) else None


def unix_http_health(path: Path) -> dict[str, object]:
    request = b"GET /health HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n"
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(_HTTP_TIMEOUT)
            client.connect(str(path))
            client.sendall(request)
            response = client.recv(128)
    except OSError:
        return {"healthy": False, "http_status": None}
    match = _STATUS_LINE.match(response)
    status = int(match.group(1)) if match else None
    return {"healthy": status == 200, "http_status": status}


def unix_http_json(path: Path, resource: str) -> dict[str, object] | None:
    request = f"GET {resource} HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n".encode()
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(_HTTP_TIMEOUT)
            client.connect(str(path))
            client.sendall(request)
            with http.client.HTTPResponse(client) as response:
                response.begin()
                if response.status != 200:
                    return None
                document = json.loads(response.read(1024 * 1024))
                return document if isinstance(document, dict) else None
    except (OSError, ValueError, http.client.HTTPException):
        return None


def tcp_health(host: str, port: int) -> dict[str, object]:
    try:
        with socket.create_connection((host, port), timeout=_HTTP_TIMEOUT):
            pass
    except OSError:
        return {"healthy": False}
    return {"healthy": True}


def channel_worker_report(port: int, *, agent_name: str) -> dict[str, object]:
    """Read what the Channel worker says about itself.

    Two facts, both published by the worker rather than inferred here:

    ``/`` is the worker's own verdict on whether it can serve. It answers only
    while its inference executor is alive and it has not given up on LiveKit,
    and it answers from the same event loop that answers LiveKit's job
    requests — so a worker whose loop is wedged fails this even though its
    listening socket still accepts, which is exactly what a port probe cannot
    tell you.

    ``/worker`` names the agent it registered as. A worker serving a different
    name cannot take this product's jobs no matter how healthy it is.
    """

    healthy = http_health(f"http://127.0.0.1:{port}/")
    identity = http_json(f"http://127.0.0.1:{port}/worker")
    return {
        "healthy": bool(healthy["healthy"]),
        "http_status": healthy["http_status"],
        "agent_name": None if identity is None else identity.get("agent_name"),
        "worker_type": None if identity is None else identity.get("worker_type"),
        "dispatch_identity": identity is not None and identity.get("agent_name") == agent_name,
        "expected_agent_name": agent_name,
    }


def settle(
    probe: Callable[[], dict[str, object]],
    ready: Callable[[dict[str, object]], bool],
    *,
    seconds: float,
) -> dict[str, object]:
    """Give a probe a bounded chance to become true.

    A worker reconnecting to LiveKit is briefly unable to serve, and this same
    report decides whether a release is rolled back. A momentary answer is not
    a verdict; an answer that stays wrong for the whole window is.
    """

    deadline = time.monotonic() + max(seconds, 0)
    while True:
        report = probe()
        if ready(report) or time.monotonic() >= deadline:
            return report
        time.sleep(0.5)
