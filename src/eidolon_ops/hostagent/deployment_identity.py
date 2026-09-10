"""Read the installed authority; a code update never issues identity material."""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path

from . import authority_reset, contract, primitives
from .primitives import TargetError

PRESERVED_INPUTS = (
    "host_identity.ed25519", "hub.crt", "hub.key",
    "owner-domain-descriptor.json", "owner-domain-root-ca.pem",
    "authority-signing-certificate.pem", "authority-bootstrap.json",
    *contract.REFRESHABLE_HOST_BOUND_INPUTS, "factory_setup_code",
)


def observe(payload: Mapping[str, object], *, root: Path = Path("/")) -> dict[str, object]:
    del payload
    lineage = authority_reset.established_lineage(root=root)["established"]
    if not isinstance(lineage, dict):
        raise TargetError("installed Authority is inconsistent; restore its data before updating code")
    hashes: dict[str, str | None] = {}
    for name in PRESERVED_INPUTS:
        path = primitives.host_path(root, contract.INSTALL_INPUTS[name][0])
        if name in {"authority-bootstrap.json", "factory_setup_code"} and not path.exists() and not path.is_symlink():
            hashes[name] = None
            continue
        if path.is_symlink() or not path.is_file():
            raise TargetError(f"installed identity input is missing or unsafe: {name}")
        hashes[name] = primitives.file_sha256(path)
    descriptor_path = primitives.host_path(root, authority_reset.OWNER_DESCRIPTOR)
    try:
        descriptor = json.loads(descriptor_path.read_text())
    except (ValueError, OSError) as exc:
        raise TargetError("installed Owner descriptor is unreadable") from exc
    if not isinstance(descriptor, dict) or any(
        descriptor.get(key) != lineage[key]
        for key in ("owner_domain_id", "owner_domain_generation")
    ):
        raise TargetError("installed Owner descriptor and database identify different authorities")
    uri = descriptor.get("descriptor_uri")
    if not isinstance(uri, str) or not uri.startswith("https://"):
        raise TargetError("installed Owner descriptor URI is invalid")
    return {"status": "observed", "authority": lineage, "descriptor_uri": uri, "preserved_files": hashes}
