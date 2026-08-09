"""Exact-commit compatibility gates for release-owned system assets."""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Mapping
from dataclasses import dataclass


class ReleaseMatrixError(ValueError):
    """A pinned component cannot participate in the reviewed Host contract."""


@dataclass(frozen=True, slots=True)
class SystemdAssetContract:
    source_id: str
    path: str
    unit: str
    component_root: str | None


SYSTEMD_ASSET_CONTRACTS = (
    SystemdAssetContract(
        "eidolon_kernel", "deploy/systemd/eidolond.service", "eidolond.service", "eidolon_kernel"
    ),
    SystemdAssetContract(
        "eidolon_kernel",
        "deploy/systemd/eidolon-data.service",
        "eidolon-data.service",
        "eidolon_data",
    ),
    SystemdAssetContract(
        "eidolon_kernel",
        "deploy/systemd/eidolon-data-workspace.service",
        "eidolon-data-workspace.service",
        "eidolon_data",
    ),
    SystemdAssetContract(
        "eidolon_kernel",
        "deploy/systemd/eidolon-hub.service",
        "eidolon-hub.service",
        "eidolon_hub",
    ),
    SystemdAssetContract(
        "eidolon_kernel",
        "deploy/systemd/eidolon-kernel.service",
        "eidolon-kernel.service",
        "eidolon_kernel",
    ),
    SystemdAssetContract(
        "eidolon_admin",
        "deploy/systemd/eidolon-bootstrapd.service",
        "eidolon-bootstrapd.service",
        "eidolon_admin",
    ),
    SystemdAssetContract(
        "eidolon_admin",
        "deploy/systemd/eidolon-local-api.service",
        "eidolon-local-api.service",
        "eidolon_admin",
    ),
    SystemdAssetContract(
        "eidolon_admin",
        "deploy/systemd/eidolon-admin.service",
        "eidolon-admin.service",
        "eidolon_admin",
    ),
    SystemdAssetContract(
        "eidolon_kernel",
        "deploy/systemd/eidolon-nats.service",
        "eidolon-nats.service",
        None,
    ),
    SystemdAssetContract(
        "eidolon_kernel",
        "deploy/systemd/eidolon-livekit.service",
        "eidolon-livekit.service",
        None,
    ),
    SystemdAssetContract(
        "eidolon_kernel",
        "deploy/systemd/eidolon-memory-supervisor.service",
        "eidolon-memory-supervisor.service",
        "eidolon_memory",
    ),
    SystemdAssetContract(
        "eidolon_kernel",
        "deploy/systemd/eidolon-memory-discovery.service",
        "eidolon-memory-discovery.service",
        "eidolon_memory",
    ),
    SystemdAssetContract(
        "eidolon_kernel",
        "deploy/systemd/eidolon-agent.service",
        "eidolon-agent.service",
        "eidolon_agent",
    ),
    SystemdAssetContract(
        "eidolon_kernel",
        "deploy/systemd/eidolon-channel.service",
        "eidolon-channel.service",
        "eidolon_channel",
    ),
)

_FORBIDDEN_RUNTIME_PATHS = ("/srv/eidolon", "/Users/", "%(ENV_HOME)s/eidolon")
_HOST_PROFILE_LINE = "EnvironmentFile=/etc/eidolon/host.env"


def validate_release_systemd_matrix(
    revisions: Mapping[str, str],
    read_exact_file: Callable[[str, str, str], str],
) -> dict[str, object]:
    """Validate systemd bytes from Git objects, never from sibling working trees."""

    expected_units = {contract.unit for contract in SYSTEMD_ASSET_CONTRACTS}
    if len(expected_units) != len(SYSTEMD_ASSET_CONTRACTS):
        raise ReleaseMatrixError("systemd matrix contains duplicate unit contracts")
    assets: dict[str, dict[str, str]] = {}
    violations: list[str] = []
    for contract in SYSTEMD_ASSET_CONTRACTS:
        revision = revisions.get(contract.source_id)
        if revision is None:
            raise ReleaseMatrixError(f"missing release revision: {contract.source_id}")
        try:
            value = read_exact_file(contract.source_id, revision, contract.path)
        except Exception as exc:
            raise ReleaseMatrixError(
                f"cannot read exact systemd asset: {contract.source_id}:{contract.path}"
            ) from exc
        label = f"{contract.source_id}@{revision[:12]}:{contract.path}"
        if "[Unit]" not in value or "[Service]" not in value:
            violations.append(f"{label}: missing systemd Unit/Service sections")
        if _HOST_PROFILE_LINE not in value:
            violations.append(f"{label}: missing sealed Host profile EnvironmentFile")
        for forbidden in _FORBIDDEN_RUNTIME_PATHS:
            if forbidden in value:
                violations.append(f"{label}: forbidden runtime path {forbidden}")
        if contract.component_root is not None:
            expected_root = f"/opt/eidolon/current/{contract.component_root}/"
            exec_lines = [line for line in value.splitlines() if line.startswith("ExecStart=")]
            if not exec_lines or not any(expected_root in line for line in exec_lines):
                violations.append(
                    f"{label}: ExecStart must use exact component root {expected_root}"
                )
        assets[contract.unit] = {
            "source_id": contract.source_id,
            "revision": revision,
            "path": contract.path,
            "sha256": hashlib.sha256(value.encode("utf-8")).hexdigest(),
        }
    if violations:
        raise ReleaseMatrixError(
            "release systemd matrix is incompatible:\n" + "\n".join(violations)
        )
    return {
        "status": "compatible",
        "units": assets,
        "contract": "fhs-opt-host-profile-v1",
    }
