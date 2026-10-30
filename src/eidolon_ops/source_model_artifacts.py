"""Resolve source-host Laya assets from Models' service and artifact declarations.

No weight/version table or independent downloader: the selected runtime's model
root identifies one existing checksum-pinned component artifact.
"""
from __future__ import annotations

import tomllib
from pathlib import Path
from types import SimpleNamespace

from eidolon_ops.component_artifacts import carried_artifacts
from eidolon_ops.errors import OperationsError


def runtime_model_artifacts(contract, services_text: str, capabilities: frozenset[str], *,
                            participation_upstream_url: str = ""):
    services = tomllib.loads(services_text)
    declarations = contract.select(capabilities, include_explicit_artifacts=True)
    artifacts = carried_artifacts(SimpleNamespace(contracts=(declarations,)))
    selected = []
    for service, capability in (("smart_home", "local_laya"),
                                ("participation", "local_laya_participation")):
        if capability not in capabilities:
            continue
        backend = "rknn" if "rknpu2" in capabilities else "torch"
        if service == "participation" and participation_upstream_url:
            backend = "remote"
        if backend == "remote":
            continue
        try:
            model_root = Path(services[service][backend]["model_dir"])
        except (KeyError, TypeError) as exc:
            raise OperationsError(f"{service}.{backend} must declare a model_dir") from exc
        matches = [a for a in artifacts if a.install_root == model_root]
        if len(matches) != 1:
            raise OperationsError(f"{service}.{backend} must reference exactly one pinned artifact: {model_root}")
        selected.append(matches[0])
    return tuple(selected)
