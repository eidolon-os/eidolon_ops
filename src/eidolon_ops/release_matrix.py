"""Exact-commit compatibility gates for release-owned system assets.

Units and the Host-bound settings template are both read out of Git objects at
the commits a release pins, never from a sibling working tree, and both are
gated here so an incompatible release is refused while it is still a plan.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Mapping
from dataclasses import dataclass

from eidolon_ops.hub_assets import HubAssetError, hub_settings_template


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
        "deploy/systemd/eidolon-unit-applier.socket",
        "eidolon-unit-applier.socket",
        # None: a socket unit has no ExecStart to root in a component.
        None,
    ),
    SystemdAssetContract(
        "eidolon_kernel",
        "deploy/systemd/eidolon-unit-applier.service",
        "eidolon-unit-applier.service",
        "eidolon_kernel",
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
        "deploy/systemd/eidolon-lifecycle-workflow.service",
        "eidolon-lifecycle-workflow.service",
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
        "deploy/systemd/eidolon-memory-embedder.service",
        "eidolon-memory-embedder.service",
        "eidolon_memory",
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
        "deploy/systemd/eidolon-channel-provider.service",
        "eidolon-channel-provider.service",
        "eidolon_channel",
    ),
    SystemdAssetContract(
        "eidolon_kernel",
        "deploy/systemd/eidolon-channel.service",
        "eidolon-channel.service",
        "eidolon_channel",
    ),
)

#: Unit files a capability brings, kept out of the tuple above because that one
#: is read for every release: an entry there whose repository this Host does
#: not pin fails the whole matrix with a missing revision. eidolon_ops.config
#: states the same capability/unit pairing and a test holds the two together.
CAPABILITY_SYSTEMD_ASSETS: dict[str, tuple[SystemdAssetContract, ...]] = {
    "local_asr": (
        SystemdAssetContract(
            "eidolon_models",
            "deploy/systemd/eidolon-asr.service",
            "eidolon-asr.service",
            "eidolon_models",
        ),
    ),
    "local_llm": (
        SystemdAssetContract(
            "eidolon_models",
            "deploy/systemd/eidolon-llm.service",
            "eidolon-llm.service",
            "eidolon_models",
        ),
    ),
}


def systemd_asset_contracts(
    capabilities: frozenset[str] = frozenset(),
) -> tuple[SystemdAssetContract, ...]:
    """The unit files this Host's release must carry."""

    extra: list[SystemdAssetContract] = []
    for capability in sorted(capabilities):
        for contract in CAPABILITY_SYSTEMD_ASSETS.get(capability, ()):
            if contract not in extra:
                extra.append(contract)
    return SYSTEMD_ASSET_CONTRACTS + tuple(extra)


_FORBIDDEN_RUNTIME_PATHS = ("/srv/eidolon", "/Users/", "%(ENV_HOME)s/eidolon")
_HOST_PROFILE_LINE = "EnvironmentFile=/etc/eidolon/host.env"

#: Where the rendered Hub settings land. Ops owns the rendering, so this gate
#: is about the template it renders rather than about the file's ownership,
#: which the Host application contract states.
HUB_SETTINGS_DESTINATION = "/etc/eidolon/generated/hub.yaml"


def validate_release_systemd_matrix(
    revisions: Mapping[str, str],
    read_exact_file: Callable[[str, str, str], str],
    capabilities: frozenset[str] = frozenset(),
) -> dict[str, object]:
    """Validate systemd bytes from Git objects, never from sibling working trees."""

    contracts = systemd_asset_contracts(capabilities)
    expected_units = {contract.unit for contract in contracts}
    if len(expected_units) != len(contracts):
        raise ReleaseMatrixError("systemd matrix contains duplicate unit contracts")
    assets: dict[str, dict[str, str]] = {}
    violations: list[str] = []
    for contract in contracts:
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
        # A .socket unit is checked against what a socket unit *is*: it declares
        # a listener, not a process. No [Service], nothing to hand an
        # environment to, no ExecStart to root in a component — requiring those
        # was requiring a listener to look like a service. The record written
        # below is the same either way; only the checks fork.
        if contract.unit.endswith(".socket"):
            if "[Unit]" not in value or "[Socket]" not in value:
                violations.append(f"{label}: missing systemd Unit/Socket sections")
            if "ListenStream=" not in value:
                violations.append(f"{label}: socket unit declares no ListenStream")
        else:
            if "[Unit]" not in value or "[Service]" not in value:
                violations.append(f"{label}: missing systemd Unit/Service sections")
            if _HOST_PROFILE_LINE not in value:
                violations.append(f"{label}: missing sealed Host profile EnvironmentFile")
            if contract.component_root is not None:
                expected_root = f"/opt/eidolon/current/{contract.component_root}/"
                exec_lines = [
                    line for line in value.splitlines() if line.startswith("ExecStart=")
                ]
                if not exec_lines or not any(expected_root in line for line in exec_lines):
                    violations.append(
                        f"{label}: ExecStart must use exact component root {expected_root}"
                    )
        for forbidden in _FORBIDDEN_RUNTIME_PATHS:
            if forbidden in value:
                violations.append(f"{label}: forbidden runtime path {forbidden}")
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


def validate_release_settings_matrix(
    revisions: Mapping[str, str],
    read_exact_file: Callable[[str, str, str], str],
) -> dict[str, object]:
    """Prove the Host-bound settings template is readable and still renderable.

    Rendering the Hub settings is the last thing that happens before they are
    staged onto a Host, which is a poor place to discover that the component
    they come from renamed the line being rewritten. Reading them from the
    pinned commit here moves that discovery into the release evidence, alongside
    which component the bytes came from — the answer changed once already.
    """

    try:
        template = hub_settings_template(revisions, read_exact_file)
    except HubAssetError as exc:
        raise ReleaseMatrixError(str(exc)) from exc
    return {
        HUB_SETTINGS_DESTINATION: {
            "source_id": template.source_id,
            "revision": template.revision,
            "path": template.path,
            "sha256": hashlib.sha256(template.text.encode("utf-8")).hexdigest(),
            "legacy_template": template.is_legacy,
        }
    }


def validate_release_matrix(
    revisions: Mapping[str, str],
    read_exact_file: Callable[[str, str, str], str],
    capabilities: frozenset[str] = frozenset(),
) -> dict[str, object]:
    """Every exact-commit gate a release has to pass, in one reviewed payload."""

    return {
        **validate_release_systemd_matrix(revisions, read_exact_file, capabilities),
        "settings": validate_release_settings_matrix(revisions, read_exact_file),
    }
