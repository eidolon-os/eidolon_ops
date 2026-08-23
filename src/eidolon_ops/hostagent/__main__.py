"""The one entry point the injected payload runs.

An action name and a base64 payload arrive on argv; one JSON document leaves on
stdout. The table below is the whole wire surface between Ops and a Host.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Mapping, Sequence
from types import ModuleType

from . import (
    authorities,
    foundation,
    foundation_install,
    host_application,
    identities,
    install,
    lifecycle,
    probe,
    reset,
    staging,
)
from .primitives import TargetError, decode_payload

#: Each action names the module that owns it and the operation to call. Resolved
#: at dispatch rather than bound here, so the owning module stays the one place
#: an operation is defined — and the one place a test replaces it.
ACTIONS: dict[str, tuple[ModuleType, str]] = {
    "status": (lifecycle, "status"),
    "foundation-doctor": (foundation, "foundation_doctor"),
    "foundation-install": (foundation_install, "foundation_install"),
    "app-ready": (probe, "app_ready"),
    "doctor-host": (lifecycle, "doctor_host"),
    "guard-upload": (staging, "guard_upload"),
    "finalize-upload": (staging, "finalize_upload"),
    "cleanup-stage": (staging, "cleanup_stage"),
    "embedding-model-state": (staging, "embedding_model_state"),
    "install-embedding-model": (staging, "install_embedding_model"),
    "install": (install, "install"),
    "ensure-service-identities": (identities, "ensure_service_identities"),
    "controller-reset": (lifecycle, "controller_reset"),
    "commissioning-code": (lifecycle, "commissioning_code"),
    "refresh-host-application": (host_application, "refresh_host_application"),
    "backup": (authorities, "backup"),
    "restore": (authorities, "restore"),
    "active-release": (lifecycle, "active_release"),
    "reset-plan": (reset, "reset_plan"),
    "reset-host": (reset, "reset_host"),
    "rollback-plan": (lifecycle, "rollback_plan"),
    "logs": (lifecycle, "logs"),
    "diagnose": (lifecycle, "diagnose"),
}
#: Lifecycle takes the action itself, because start, stop and restart differ
#: only in which half of the transition they run.
LIFECYCLE_ACTIONS = ("start", "stop", "restart")


def run_action(action: str, payload: Mapping[str, object]) -> dict[str, object]:
    if action in LIFECYCLE_ACTIONS:
        return lifecycle.lifecycle(action, payload)
    try:
        module, name = ACTIONS[action]
    except KeyError as exc:
        raise TargetError("unknown target action") from exc
    return getattr(module, name)(payload)


def main(argv: Sequence[str] | None = None) -> int:
    arguments = tuple(sys.argv[1:] if argv is None else argv)
    if len(arguments) != 2:
        print(
            json.dumps({"status": "failed", "error": "expected action and payload"}),
            file=sys.stderr,
        )
        return 2
    action, encoded = arguments
    try:
        result = run_action(action, decode_payload(encoded))
    except Exception as exc:
        print(json.dumps({"status": "failed", "error": str(exc)}, sort_keys=True), file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
