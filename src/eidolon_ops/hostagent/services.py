"""Ask eidolond to restart one service it manages.

The one executor for a single service (Ops 总纲 §1.5). eidolond already owns
everything a restart needs to be safe — which services it manages, which of
them may be touched, a revision to compare against and an audit record — so
this reads the service, asks once, and reads it again. It does not wait for
readiness, retry, or order dependencies: those are eidolond's.

Runs in two places with the same code: on a product Host inside the injected
host agent, and on a macOS source run directly from the operator's process,
which is the same machine and the same user as the socket.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from pathlib import Path
from urllib.parse import quote

from .primitives import TargetError, unix_http_request

#: Where a product Host's eidolond listens. A source run passes its own.
SYSTEM_SOCKET = Path("/run/eidolon/system.sock")
_SERVICES = "/api/system/v1/services"
#: eidolond service ids are short lowercase names; anything else is refused
#: here rather than turned into a request path.
_SERVICE_ID = re.compile(r"^[a-z][a-z0-9-]{0,63}$")
_REQUEST_ID = re.compile(r"^[A-Za-z0-9._:-]{1,96}$")
#: A restart returns when the program has been stopped and started again, and
#: a draining voice worker takes up to its own stop timeout (40 s) to go.
_RESTART_TIMEOUT = 120


def restart(payload: Mapping[str, object]) -> dict[str, object]:
    """The host agent action: restart one service on this Host."""

    return restart_service(
        SYSTEM_SOCKET, str(payload.get("service_id", "")), str(payload.get("request_id", ""))
    )


def restart_service(socket_path: Path, service_id: str, request_id: str) -> dict[str, object]:
    if not _SERVICE_ID.fullmatch(service_id):
        raise TargetError("service id is not a system service name")
    if not _REQUEST_ID.fullmatch(request_id):
        raise TargetError("request id is invalid")
    resource = f"{_SERVICES}/{quote(service_id)}"
    status, before = unix_http_request(socket_path, "GET", resource)
    if status != 200 or not isinstance(before, dict):
        return _refused(service_id, status, before, stage="read")
    desired = before.get("desired")
    revision = desired.get("revision") if isinstance(desired, dict) else None
    if not isinstance(revision, int) or isinstance(revision, bool):
        raise TargetError("eidolond reported a service without a desired revision")
    status, result = unix_http_request(
        socket_path,
        "POST",
        f"{resource}/restart",
        body={
            "operation": "system.service.restart",
            "request_id": request_id,
            "expected_revision": revision,
        },
        timeout=_RESTART_TIMEOUT,
    )
    if status != 200 or not isinstance(result, dict):
        return {**_refused(service_id, status, result, stage="restart"), "before": _state(before)}
    _, after = unix_http_request(socket_path, "GET", resource)
    return {
        "status": "restarted",
        "service_id": service_id,
        "request_id": request_id,
        "audit_position": result.get("audit_position"),
        "replayed": result.get("replayed"),
        "before": _state(before),
        "after": _state(after) if isinstance(after, dict) else None,
    }


def _state(document: Mapping[str, object]) -> dict[str, object]:
    desired = document.get("desired")
    return {
        "runtime_state": document.get("runtime_state"),
        "revision": desired.get("revision") if isinstance(desired, dict) else None,
        "detail": document.get("detail"),
    }


def _refused(
    service_id: str, status: int, document: object, *, stage: str
) -> dict[str, object]:
    """eidolond's own answer, kept as it said it.

    404 is a name it does not manage; 409 is one it will not touch now (disabled,
    external on this Host, mid-transition, or a revision that moved); 503 is a
    Host operation that failed. None of them is retried here.
    """

    detail = document.get("detail") if isinstance(document, dict) else document
    return {
        "status": "refused",
        "service_id": service_id,
        "stage": stage,
        "http_status": status,
        "detail": detail,
    }
