"""Ordinary-user bindings to the same state executor installed Hosts receive."""

from __future__ import annotations

import grp
import json
import uuid
from pathlib import Path

from eidolon_ops import source_assets
from eidolon_ops.component_contract import read_component_contracts
from eidolon_ops.errors import OperationsError
from eidolon_ops.host_identity import derive_host_lan_identity
from eidolon_ops.hostagent import authorities
from eidolon_ops.hostagent.primitives import TargetError
from eidolon_ops.hostagent.state_runtime import StateRuntime
from eidolon_ops.paths import HostProfile
from eidolon_ops.private_files import atomic_private_file
from eidolon_ops.source_runtime import source_operator


class SourceStateOperations:
    def __init__(self, profile: HostProfile, supervisor) -> None:
        self.profile = profile
        self.supervisor = supervisor

    def _bindings(self) -> tuple[dict, StateRuntime]:
        operator = source_operator(self.profile)
        group = grp.getgrgid(operator.pw_gid).gr_name
        product = self.supervisor._product()
        topology = read_component_contracts(
            {key: value.path for key, value in product.config.sources.items()},
            product.config.capabilities,
        )
        paths = self.profile.paths
        payload = {
            "units": list(product.config.units), "capabilities": sorted(product.config.capabilities),
            "memory_admin_url": f"http://127.0.0.1:{source_assets.PORTS['memory_admin']}",
            "authority_inventory": topology.authority_payload(
                map_path=lambda path: Path(source_assets.translate_fhs(self.profile, str(path))),
                identity=(operator.pw_name, group),
            ),
        }
        for entry in payload["authority_inventory"]:
            path = Path(entry["path"])
            if not any(path.is_relative_to(root) for root in (paths.state_root, paths.bootstrap_state_root)):
                raise OperationsError(f"declared state escapes source Host roots: {path}")

        def own(path: Path, user: str, file_group: str) -> None:
            if (user, file_group) != (operator.pw_name, group) or path.stat().st_uid != operator.pw_uid:
                raise OperationsError("source state must remain owned by the workspace operator")

        def stop():
            return {"output": self.supervisor._stop_host().stdout.strip()}

        def start():
            self.supervisor.transport.run((str(self.supervisor.script()), "product-source", "start"))
            health = product.health(wait_seconds=120)
            if health.get("status") != "healthy":
                raise OperationsError("restored source Host did not become healthy")
            return {"status": "started", "health": health}

        runtime = StateRuntime(
            staging_root=paths.cache_root / "backups",
            host_id=lambda: derive_host_lan_identity(
                (paths.bootstrap_state_root / "host_identity.ed25519").read_bytes()
            ).host_id,
            own=own, hand_to_operator=lambda path: own(path, operator.pw_name, group),
            stop=stop, start=start,
            prepare=lambda: self.supervisor.transport.run(
                (str(self.supervisor.script()), "product-source", "preflight")
            ),
        )
        return payload, runtime

    def backup(self, *, output: Path) -> dict[str, object]:
        payload, runtime = self._bindings()
        identifier = f"source-{uuid.uuid4().hex}"
        destination = output.resolve() / f"{identifier}-{runtime.host_id()}"
        try:
            result = authorities.backup({**payload, "release_id": identifier}, runtime=runtime, directory=destination)
        except TargetError as exc:
            raise OperationsError(str(exc)) from exc
        atomic_private_file(destination / "backup.json", json.dumps(result, indent=2).encode() + b"\n")
        return {**result, "local_directory": str(destination),
                "scope": "declared component state; see not_covered for exclusions", "complete_host_backup": False}

    def restore(self, *, source: Path, apply: bool) -> dict[str, object]:
        if source.is_symlink():
            raise OperationsError("backup source is unsafe")
        source = source.resolve()
        path = source / "backup.json"
        if path.is_symlink() or not path.is_file():
            raise OperationsError("backup manifest is missing or unsafe")
        try:
            manifest = json.loads(path.read_text())
        except (OSError, ValueError) as exc:
            raise OperationsError("backup manifest is invalid") from exc
        if not isinstance(manifest, dict):
            raise OperationsError("backup manifest is not an object")
        payload, runtime = self._bindings()
        payload.update({"manifest": manifest, "release_id": manifest.get("release_id")})
        try:
            _, prepared = authorities.restore_plan(payload, runtime=runtime, directory=source)
            if not apply:
                return {"status": "planned", "host_id": manifest["host_id"],
                        "authorities": [name for name, _, _ in prepared],
                        "replaces": [str(entry["path"]) for _, _, entry in prepared]}
            return authorities.restore(payload, runtime=runtime, directory=source)
        except TargetError as exc:
            raise OperationsError(str(exc)) from exc
