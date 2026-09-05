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
    ConfigurationError,
    OperationsConfig,
    validate_private_local_file,
)
from eidolon_ops.errors import InstallInputError, OperationsError
from eidolon_ops.install_inputs import validate_install_input_contract
from eidolon_ops.private_inputs import refresh_derived_settings
from eidolon_ops.process import ProcessRunner, checked
from eidolon_ops.release_matrix import ReleaseMatrixError, validate_release_matrix
from eidolon_ops.source_resolution import ResolvedSource, SourceResolver
from eidolon_ops.workstation_toolchain import ensure_workstation_uv

#: Release formats this deployer speaks. The activator reports its own versions
#: through `eidolon-release contract`, so interoperability is proven against a
#: declared contract rather than against the Kernel repository layout.
RELEASE_TOOL_CONTRACT = {
    "tool": "eidolon-release",
    "cli_contract_version": 1,
    "bundle_schema_version": 3,
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
    def __init__(
        self,
        config: OperationsConfig,
        runner: ProcessRunner,
        *,
        git: str,
        sources: SourceResolver | None = None,
        allow_dirty: bool = False,
    ) -> None:
        self.config = config
        self.runner = runner
        self.git = git
        #: Shared with everything else in this operation that needs to know
        #: which commit a source ships — the bundle seal, the resumed-bundle
        #: check, the Host provenance record. One resolution, one answer.
        self.sources = sources or SourceResolver(config, runner, git=git, allow_dirty=allow_dirty)

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
            raise OperationsError("required workstation command is missing: " + ", ".join(missing))

    def run(
        self, *, require_install_files: bool, require_clean_sources: bool = True
    ) -> dict[str, object]:
        """Prove this workstation can seal a release, and say what it would seal.

        ``require_clean_sources`` is off for diagnosis only. A doctor that
        refuses to answer because a sibling repository has an edited test file
        is a doctor that cannot diagnose the thing it was asked about; the same
        split the install-input contract already makes here. Diagnosis reports;
        shipping refuses.
        """

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
        source_evidence = self._source_evidence()
        if require_clean_sources:
            self.sources.require_clean()
        # After resolution, because it asks which commit the Kernel checkout is
        # on and that is now a question with one answer instead of a declaration.
        release_contract = self._release_tool_contract(release_cli)
        try:
            release_matrix = validate_release_matrix(
                source_evidence,
                self.read_exact_source_file,
                self.config.capabilities,
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
            # The five facts per source, in the same shape the Host records, so
            # the workstation's answer and the board's evidence are comparable
            # without translating between two vocabularies.
            "source_provenance": self.sources.provenance(),
            "source_selection": self.sources.selection(),
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
        """Resolve these sources so a caller reading their files gets one answer.

        Kept as a verb of its own because ``init-inputs`` reads component
        settings without shipping anything, and it must read them from a commit
        rather than from whatever the worktree currently says.
        """

        for source_id in source_ids:
            self.sources.revision(source_id)

    def read_exact_source_file(self, source_id: str, revision: str, path: str) -> str:
        if self.sources.revision(source_id) != revision:
            raise OperationsError(f"release revision drifted during matrix validation: {source_id}")
        return checked(
            f"exact systemd asset verification for {source_id}:{path}",
            self.runner.run(
                (
                    self.git,
                    "-C",
                    str(self.config.sources[source_id].path),
                    "show",
                    f"{revision}:{path}",
                )
            ),
        ).stdout

    def _source_evidence(self) -> dict[str, str]:
        return self.sources.revisions()

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
        here does nothing unless the commit that ships contains it. Without this
        check that mismatch is invisible until an install has already run.

        The check used to say "move the eidolon_kernel pin", and that advice
        stopped existing when the shipped commit became the Kernel checkout's
        own HEAD — worse, the digest comparison then agreed by construction,
        because the activator lives inside the tree being archived. So the two
        premises that made it agree are now asserted instead of assumed: the
        activator being run really does come out of the checkout resolution
        read, and that checkout has nothing uncommitted under the deploy
        package. An uncommitted fix in there is precisely the mistake this
        guards, and it now gets told what it is rather than being told to move
        a pin that no longer exists.
        """

        kernel = self.sources.resolve()["eidolon_kernel"]
        self._require_activator_checkout(kernel)
        listing = checked(
            "shipped eidolon_deploy listing",
            self.runner.run(
                (
                    self.git,
                    "-C",
                    str(kernel.path),
                    "ls-tree",
                    "-r",
                    kernel.revision,
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
            raise OperationsError("the shipped Kernel commit has no eidolon_deploy package")
        digest = hashlib.sha256()
        for relative, blob in sorted(entries):
            digest.update(f"{relative}:{blob}\n".encode())
        if reported != digest.hexdigest():
            raise OperationsError(
                "the configured eidolon-release is not the one this release will ship: "
                f"it does not match {_DEPLOY_PACKAGE} at {kernel.revision[:12]}"
                + (
                    ", which is the commit pinned for this reproduction run; drop the "
                    "eidolon_kernel pin or check that commit out to seal from it"
                    if kernel.pinned
                    else ", which is this checkout's HEAD; rebuild the activator's "
                    "environment from it"
                )
            )

    def _require_activator_checkout(self, kernel: ResolvedSource) -> None:
        """Prove the activator being run is the checkout's, and nothing is uncommitted.

        Two premises, both invisible until they are false. An activator taken
        from some other clone can agree with the shipped digest by coincidence;
        an uncommitted change under the deploy package cannot ship at all, and
        the digest comparison alone reports that as an unexplained mismatch.
        """

        release_cli = self.config.workspace.release_cli
        toplevel = checked(
            "activator checkout identification",
            self.runner.run(
                (self.git, "-C", str(release_cli.parent), "rev-parse", "--show-toplevel")
            ),
        ).stdout.strip()
        if not toplevel or Path(toplevel).resolve() != kernel.path.resolve():
            raise OperationsError(
                "workspace.release_cli is not inside the eidolon_kernel checkout this "
                f"release is sealed from: {release_cli} lives in {toplevel or 'no repository'}, "
                f"expected {kernel.path}"
            )
        uncommitted = checked(
            "shipped activator worktree state",
            self.runner.run(
                (
                    self.git,
                    "-C",
                    str(kernel.path),
                    "status",
                    "--porcelain",
                    "--",
                    _DEPLOY_PACKAGE,
                )
            ),
        ).stdout.strip()
        if uncommitted:
            raise OperationsError(
                f"the eidolon-release this deployer runs has uncommitted changes under "
                f"{_DEPLOY_PACKAGE}, so they cannot be in the release it seals: commit "
                "them in eidolon_kernel first"
            )

    def _validate_install_inputs(self) -> dict[str, object]:
        if set(self.config.install_files) != set(INSTALL_FILE_NAMES):
            raise ConfigurationError("install.files is required for install or expansion")
        for name in INSTALL_FILE_NAMES:
            validate_private_local_file(
                self.config.install_files[name], label=f"install.files.{name}"
            )
        return self.validate_input_contract()

    def validate_input_contract(self) -> dict[str, object]:
        """Prove the input set still holds what the product declares.

        Split out from the install-only checks above so the deploy path can run
        it without the rest. The permissions and ownership of each private file
        are an install concern; whether the set is *complete* is a concern of
        anything that expects the Host to work afterwards — and gating this on
        the install path is why a Host spent two weeks two credentials short
        while release after release reported green.

        Deliberately not called from ``run``: ``doctor`` goes through there, and
        a doctor that refuses to answer because the input set is short is a
        doctor that cannot diagnose the one thing it was asked about. Diagnosis
        reports; shipping refuses.
        """

        try:
            # ``refresh_derived`` because two of the fourteen inputs are not
            # operator material: the three settings files are a pure function of
            # the pinned commits plus this repository's overlay, and the provider
            # keys have their one home in a component's own `config/.env`. Both
            # were materialized here and then required to be byte-equal to what
            # they derive from, with no verb on this path to reconcile them -- so
            # a component changing a default, or an operator rotating an LLM key
            # where it is typed, stopped every operation on both Hosts. The gate
            # still holds for the eleven files a human authored.
            return validate_install_input_contract(
                self.sources.resolved_config(),
                self.read_exact_source_file,
                refresh_derived=True,
            )
        except InstallInputError as exc:
            raise OperationsError(str(exc)) from exc

    def refresh_product_settings(self) -> dict[str, object]:
        """Materialize settings from this operation's exact source revisions.

        Deploy does not need the workstation's provider credential sources: the
        installed Host already owns and separately proves those credentials. It
        does need the three non-secret settings inputs because they travel with
        every code cutover. Keeping this narrower than the install contract
        prevents an unrelated key-source file from blocking a normal update.
        """

        names = ("agent_settings", "channel_settings", "memory_settings")
        paths = [self.config.install_files[name] for name in names]
        parents = {path.parent for path in paths}
        if len(parents) != 1:
            raise OperationsError("product settings inputs must share one private directory")
        for name, path in zip(names, paths, strict=True):
            validate_private_local_file(path, label=f"install.files.{name}")
        try:
            refreshed = refresh_derived_settings(
                parents.pop(),
                self.sources.resolved_config(),
                self.read_exact_source_file,
            )
        except InstallInputError as exc:
            raise OperationsError(str(exc)) from exc
        return {
            "status": "exact",
            "refreshed": refreshed,
            "sources": {
                source_id: self.sources.revision(source_id)
                for source_id in ("eidolon_agent", "eidolon_channel", "eidolon_memory")
            },
        }
