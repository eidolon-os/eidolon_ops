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
    authority_reset,
    authority_restore,
    cutover,
    foundation,
    foundation_install,
    host_application,
    identities,
    install,
    kernel_schema,
    lifecycle,
    probe,
    reclamation,
    reset,
    secret_inputs,
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
    "release-artifact-state": (staging, "release_artifact_state"),
    "guard-release-artifacts": (staging, "guard_release_artifacts"),
    "finalize-release-artifacts": (staging, "finalize_release_artifacts"),
    "cleanup-stage": (staging, "cleanup_stage"),
    "reclaim-releases": (reclamation, "reclaim"),
    "component-artifact-state": (staging, "component_artifact_state"),
    "install-component-artifact": (staging, "install_component_artifact"),
    "install": (install, "install"),
    "converge-secret-inputs": (secret_inputs, "converge_secret_inputs"),
    "ensure-service-identities": (identities, "ensure_service_identities"),
    "controller-reset": (lifecycle, "controller_reset"),
    "commissioning-code": (lifecycle, "commissioning_code"),
    "refresh-host-application": (host_application, "refresh_host_application"),
    "backup": (authorities, "backup"),
    "restore": (authorities, "restore"),
    "authority-lineage": (authority_reset, "authority_lineage"),
    "authority-reset-plan": (authority_reset, "authority_reset_plan"),
    "authority-reset": (authority_reset, "reset_owner_authority"),
    "authority-backup": (authority_restore, "backup"),
    "authority-restore-plan": (authority_restore, "restore_plan"),
    "authority-restore": (authority_restore, "restore"),
    "authority-restore-stage-reset": (authority_restore, "clear_restore_stage"),
    "authority-restore-stage-finalize": (authority_restore, "finalize_restore_stage"),
    "release-cutover-snapshot": (cutover, "snapshot"),
    "release-cutover-restore": (cutover, "restore"),
    "release-cutover-finalize": (cutover, "finalize"),
    "active-release": (lifecycle, "active_release"),
    "release-sources": (lifecycle, "release_sources"),
    "kernel-schema-plan": (kernel_schema, "kernel_schema_plan"),
    "kernel-schema-reset": (kernel_schema, "kernel_schema_reset"),
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
