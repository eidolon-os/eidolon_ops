"""Prove the workstation, its sources and its activator before anything ships.

Everything here reads; nothing here changes a Host. It exists so that an
install refuses on this machine — where the fix is cheap — rather than halfway
through a release on a board.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
from pathlib import Path

from eidolon_ops.config import (
    INSTALL_FILE_NAMES,
    SOURCE_IDS,
    ConfigurationError,
    OperationsConfig,
    SourceConfig,
    validate_private_local_file,
)
from eidolon_ops.errors import InstallInputError, OperationsError
from eidolon_ops.install_inputs import validate_install_input_contract
from eidolon_ops.process import ProcessRunner, checked
from eidolon_ops.release_matrix import ReleaseMatrixError, validate_release_matrix
from eidolon_ops.workstation_toolchain import ensure_workstation_uv

#: Release formats this deployer speaks. The activator reports its own versions
#: through `eidolon-release contract`, so interoperability is proven against a
#: declared contract rather than against the Kernel repository layout.
RELEASE_TOOL_CONTRACT = {
    "tool": "eidolon-release",
    "cli_contract_version": 1,
    "bundle_schema_version": 2,
    "descriptor_schema_version": 2,
    "snapshot_schema_version": 2,
}

#: Component-neutral operator entries published inside every sealed release.
RELEASE_ACTIVATOR = ".release/bin/eidolon-release"
RELEASE_INTERPRETER = ".release/bin/python"

#: Sealing runs from the release's own activator, so the one this deployer runs
#: must be byte-identical to the one the pinned Kernel commit ships.
_DEPLOY_PACKAGE = "eidolon_deploy"
_DEPLOY_DIGEST_SUFFIXES = (".py", ".json")
_PINNED_UV_VERSION = "uv 0.11.15"


class ReleasePreflight:
    def __init__(self, config: OperationsConfig, runner: ProcessRunner, *, git: str) -> None:
        self.config = config
        self.runner = runner
        self.git = git

    def validate_ssh_material(self) -> None:
        validate_private_local_file(self.config.host.identity_file, label="host.identity_file")
        known_hosts = self.config.host.known_hosts_file
        if not known_hosts.is_file() or known_hosts.is_symlink() or known_hosts.stat().st_size == 0:
            raise ConfigurationError(
                f"host.known_hosts_file must be a non-empty regular file: {known_hosts}"
            )
        if stat.S_IMODE(known_hosts.stat().st_mode) & 0o022:
            raise ConfigurationError("host.known_hosts_file must not be group/world writable")

    def workstation_uv(self) -> Path:
        """The uv this release is sealed with.

        A profile may name its own, for a workstation that has to. Otherwise
        Ops materializes the pinned one — which is the difference between a pin
        and a path: the pinned artifact is placed where it was promised, rather
        than expected to have survived wherever it was last built.
        """

        override = self.config.workspace.uv
        if override is not None:
            return override
        return ensure_workstation_uv(self.config.workspace.toolchain_root)

    def require_commands(self, commands: tuple[str, ...]) -> None:
        missing = [command for command in commands if shutil.which(command) is None]
        if missing:
            raise OperationsError(
                "required workstation command is missing: " + ", ".join(missing)
            )

    def run(self, *, require_install_files: bool) -> dict[str, object]:
        self.validate_ssh_material()
        self.require_commands((self.git, "git-lfs", "ssh", "scp", "rsync"))
        release_cli = self.config.workspace.release_cli
        if not release_cli.is_file() or not os.access(release_cli, os.X_OK):
            raise OperationsError(
                f"eidolon-release CLI is missing or not executable: {release_cli}"
            )
        local_uv = self.workstation_uv()
        if not local_uv.is_file() or not os.access(local_uv, os.X_OK):
            raise OperationsError(f"pinned local uv executable is missing: {local_uv}")
        local_uv_version = checked(
            "pinned local uv verification",
            self.runner.run((str(local_uv), "--version")),
        ).stdout.strip()
        if local_uv_version != _PINNED_UV_VERSION and not local_uv_version.startswith(
            f"{_PINNED_UV_VERSION} "
        ):
            raise OperationsError(
                f"pinned local uv must be 0.11.15, got: {local_uv_version or 'no version'}"
            )
        release_contract = self._release_tool_contract(release_cli)
        source_evidence = self._source_evidence()
        try:
            release_matrix = validate_release_matrix(
                source_evidence,
                self.read_exact_source_file,
            )
        except ReleaseMatrixError as exc:
            raise OperationsError(str(exc)) from exc
        install_input_contract = None
        if require_install_files:
            install_input_contract = self._validate_install_inputs()
        return {
            "release_cli": str(release_cli),
            "release_tool_contract": release_contract,
            "local_uv": str(local_uv),
            "local_uv_version": local_uv_version,
            "sources": source_evidence,
            "release_matrix": release_matrix,
            "ssh": {
                "target": self.config.host.target,
                "port": self.config.host.port,
                "batch_mode": True,
                "strict_host_key_checking": True,
            },
            "python_resolver": {
                "index_url": self.config.workspace.python_index_url,
                "http_timeout_seconds": self.config.workspace.python_http_timeout_seconds,
                "http_retries": self.config.workspace.python_http_retries,
                "concurrent_downloads": self.config.workspace.python_concurrent_downloads,
                "locked": True,
            },
            "install_prerequisites_checked": require_install_files,
            "install_input_contract": install_input_contract,
        }

    def require_exact_commits(self, source_ids: tuple[str, ...]) -> None:
        for source_id in source_ids:
            self._require_exact_commit(source_id, self.config.sources[source_id])

    def read_exact_source_file(self, source_id: str, revision: str, path: str) -> str:
        source = self.config.sources[source_id]
        if source.revision != revision:
            raise OperationsError(f"release revision drifted during matrix validation: {source_id}")
        return checked(
            f"exact systemd asset verification for {source_id}:{path}",
            self.runner.run((self.git, "-C", str(source.path), "show", f"{revision}:{path}")),
        ).stdout

    def _source_evidence(self) -> dict[str, str]:
        evidence: dict[str, str] = {}
        for source_id in SOURCE_IDS:
            source = self.config.sources[source_id]
            if not source.path.is_dir() or not (source.path / ".git").exists():
                raise OperationsError(f"source repository is missing: {source_id}: {source.path}")
            self._require_exact_commit(source_id, source)
            self._require_tag_resolves(source_id, source)
            evidence[source_id] = source.revision
        return evidence

    def _require_exact_commit(self, source_id: str, source: SourceConfig) -> None:
        result = checked(
            f"exact commit verification for {source_id}",
            self.runner.run(
                (
                    self.git,
                    "-C",
                    str(source.path),
                    "rev-parse",
                    "--verify",
                    f"{source.revision}^{{commit}}",
                )
            ),
        )
        if result.stdout.strip() != source.revision:
            raise OperationsError(f"source revision is not the exact commit object: {source_id}")

    def _require_tag_resolves(self, source_id: str, source: SourceConfig) -> None:
        """Prove an annotated tag still names the commit this release pins.

        A tag is a movable reference, so it cannot define a release. It can
        still make one reviewable — as long as moving it is reported instead of
        silently followed.
        """

        if source.tag is None:
            return
        resolved = checked(
            f"tag resolution for {source_id}",
            self.runner.run(
                (
                    self.git,
                    "-C",
                    str(source.path),
                    "rev-parse",
                    "--verify",
                    f"{source.tag}^{{commit}}",
                )
            ),
        ).stdout.strip()
        if resolved != source.revision:
            raise OperationsError(
                f"tag no longer names the pinned commit: {source_id} "
                f"{source.tag} -> {resolved[:12]}, expected {source.revision[:12]}"
            )

    def _release_tool_contract(self, release_cli: Path) -> dict[str, object]:
        """Prove the activator speaks this deployer's release formats.

        The activator reports its own contract, so a stale or modified copy is
        rejected without this deployer knowing where it lives in its repository.
        """

        result = checked(
            "eidolon-release contract verification",
            self.runner.run((str(release_cli), "contract"), timeout=60),
        )
        try:
            document = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise OperationsError("eidolon-release contract output is not JSON") from exc
        if not isinstance(document, dict):
            raise OperationsError("eidolon-release contract output is not an object")
        mismatched = sorted(
            name
            for name, expected in RELEASE_TOOL_CONTRACT.items()
            if document.get(name) != expected
        )
        if mismatched:
            raise OperationsError(
                "eidolon-release does not speak this deployer's release contract: "
                + ", ".join(mismatched)
            )
        published = {
            "activator_relative_path": RELEASE_ACTIVATOR,
            "interpreter_relative_path": RELEASE_INTERPRETER,
        }
        if any(document.get(name) != value for name, value in published.items()):
            raise OperationsError("eidolon-release publishes unexpected operator entries")
        self._require_shipped_activator(document.get("package_digest"))
        return document

    def _require_shipped_activator(self, reported: object) -> None:
        """Refuse to build a release with an activator that will not ship in it.

        Sealing runs on the target from the release's own copy, so a fix made
        here does nothing unless the Kernel pin moves with it. Without this
        check that mismatch is invisible until an install has already run.
        """

        source = self.config.sources["eidolon_kernel"]
        listing = checked(
            "pinned eidolon_deploy listing",
            self.runner.run(
                (
                    self.git,
                    "-C",
                    str(source.path),
                    "ls-tree",
                    "-r",
                    source.revision,
                    "--",
                    _DEPLOY_PACKAGE,
                )
            ),
        ).stdout
        entries: list[tuple[str, str]] = []
        for line in listing.splitlines():
            metadata, separator, path = line.partition("\t")
            if not separator:
                continue
            relative = path[len(_DEPLOY_PACKAGE) + 1 :]
            if relative.endswith(_DEPLOY_DIGEST_SUFFIXES):
                entries.append((relative, metadata.split()[2]))
        if not entries:
            raise OperationsError("the pinned Kernel commit ships no eidolon_deploy package")
        digest = hashlib.sha256()
        for relative, blob in sorted(entries):
            digest.update(f"{relative}:{blob}\n".encode())
        if reported != digest.hexdigest():
            raise OperationsError(
                "the configured eidolon-release is not the one this release will "
                "ship: move the eidolon_kernel pin to the commit that contains it"
            )

    def _validate_install_inputs(self) -> dict[str, object]:
        if set(self.config.install_files) != set(INSTALL_FILE_NAMES):
            raise ConfigurationError("install.files is required for install or expansion")
        for name in INSTALL_FILE_NAMES:
            validate_private_local_file(
                self.config.install_files[name], label=f"install.files.{name}"
            )
        try:
            return validate_install_input_contract(self.config, self.read_exact_source_file)
        except InstallInputError as exc:
            raise OperationsError(str(exc)) from exc
