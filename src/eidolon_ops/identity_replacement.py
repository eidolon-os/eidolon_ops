"""Prepare a new Host before an explicit factory reset, using the normal issuers."""

from __future__ import annotations

import os
import shutil
import tempfile
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path

from eidolon_ops.config import OperationsConfig
from eidolon_ops.errors import InstallInputError
from eidolon_ops.host_identity import derive_host_lan_identity
from eidolon_ops.install_inputs import initialize_install_inputs, target_directory
from eidolon_ops.owner_domain_assets import ensure_owner_domain_assets
from eidolon_ops.private_inputs import ensure_private_parent


@contextmanager
def replacement_inputs(
    config: OperationsConfig,
    read_exact_file: Callable[[str, str, str], str],
    *,
    owner_root: Path,
    port: int,
) -> Iterator[IdentityReplacement]:
    """Nothing live changes until commit; keep retired inputs private for recovery."""
    target = target_directory(config)
    ensure_private_parent(target.parent)
    ensure_private_parent(owner_root.parent)
    if target.parent.stat().st_dev != owner_root.parent.stat().st_dev:
        raise InstallInputError(
            "identity inputs and Owner material must share a filesystem for replacement"
        )
    for path in (target, owner_root, target.parent / "host_identity.ed25519"):
        if path.is_symlink():
            raise InstallInputError("identity replacement refuses symbolic links")
    with tempfile.TemporaryDirectory(prefix=".new-host-", dir=target.parent) as temporary:
        staged = Path(temporary)
        staged_config = replace(
            config,
            install_files={
                name: staged / "inputs" / path.name for name, path in config.install_files.items()
            },
        )
        initialize_install_inputs(staged_config, read_exact_file, new_identity=True)
        identity = derive_host_lan_identity((staged / "inputs/host_identity.ed25519").read_bytes())
        # Keep this staging directory beside its destination so publishing is a rename.
        with tempfile.TemporaryDirectory(
            prefix=".new-owner-", dir=owner_root.parent
        ) as owner_temporary:
            staged_owner = Path(owner_temporary) / "owner"
            owner = ensure_owner_domain_assets(staged_owner, identity, port)
            replacement = IdentityReplacement(
                target, owner_root, staged, staged_owner, identity.host_id, owner.owner_domain_id
            )
            yield replacement


class IdentityReplacement:
    def __init__(
        self,
        target: Path,
        owner_root: Path,
        staged: Path,
        staged_owner: Path,
        host_id: str,
        owner_id: str,
    ) -> None:
        self.target, self.owner_root = target, owner_root
        self.staged, self.staged_owner = staged, staged_owner
        self.host_id, self.owner_id = host_id, owner_id

    def commit(self) -> dict[str, object]:
        retired = Path(tempfile.mkdtemp(prefix="retired-host-", dir=self.target.parent))
        os.chmod(retired, 0o700)
        anchor = self.target.parent / "host_identity.ed25519"
        moves = (
            (self.target, retired / "inputs"),
            (self.owner_root, retired / "owner-domain"),
            (anchor, retired / "host_identity.ed25519"),
        )
        moved, published = [], []
        try:
            for source, destination in moves:
                if source.exists():
                    source.rename(destination)
                    moved.append((source, destination))
            for source, destination in (
                (self.staged / "inputs", self.target),
                (self.staged_owner, self.owner_root),
                (self.staged / "host_identity.ed25519", anchor),
            ):
                source.rename(destination)
                published.append(destination)
        except Exception:
            for path in reversed(published):
                shutil.rmtree(path) if path.is_dir() else path.unlink()
            for source, destination in reversed(moved):
                destination.rename(source)
            raise
        return {
            "status": "new_host_identity",
            "host_id": self.host_id,
            "owner_domain_id": self.owner_id,
            "retired_inputs": str(retired),
            "pairing": "add this new Host in Mobile; previous pairings are not reused",
        }
