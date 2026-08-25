"""Seal a release here, get it onto the Host, and build it there.

Every step is resumable and every resumed step re-proves what it is resuming:
a bundle that drifted between attempts is refused rather than continued.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

from eidolon_ops.config import SOURCE_IDS, OperationsConfig
from eidolon_ops.embedding_model import (
    PINNED_EMBEDDING_MODEL,
    embedding_model_digest,
    ensure_workstation_embedding_model,
    host_embedding_model_root,
)
from eidolon_ops.errors import OperationsError
from eidolon_ops.process import ProcessRunner, checked
from eidolon_ops.progress import Journal
from eidolon_ops.source_resolution import SourceResolver
from eidolon_ops.transport import SSHTransport
from eidolon_ops.workstation_toolchain import ensure_workstation_uv

#: Dependencies this workstation has already fetched, kept between builds so a
#: release costs the network only what actually changed.
#:
#: It lives with the pinned workstation toolchain, not beside the bundles. A
#: bundle is an output and ``bundle_root`` may legitimately be a temp directory
#: the system sweeps -- but this is not an output, it is the thing whose whole
#: purpose is to still be there next time, and uv binds a cache to the absolute
#: path it was built at: every entry in it points at that root, so a cache that
#: moves is a cache full of dangling links. Keeping it under the toolchain root,
#: which the profile is already required to place somewhere durable, means the
#: path it is bound to is one nothing sweeps and nothing relocates.
_KEPT_DEPENDENCY_CACHE = "uv-cache"
_BUNDLE_SHAPE = {
    "bundle.json",
    "prepare_target.py",
    "python-dependencies.tar.gz",
    "sources",
}
_UPLOAD_GUARD_STATES = {"ready_for_upload", "resume_upload", "ready_for_prepare"}

# A sealed bundle coexists with its unpacked sources, environments and upload
# until transaction commit. Keep an explicit operating reserve beyond that
# estimate so a release cannot consume the space needed for logs and state.
_TARGET_EXPANSION_FACTOR = 4
_CAPACITY_RESERVE_BYTES = 1024**3


class BundleTransfer:
    def __init__(
        self,
        config: OperationsConfig,
        runner: ProcessRunner,
        transport: SSHTransport,
        sources: SourceResolver | None = None,
    ) -> None:
        self.config = config
        self.runner = runner
        self.transport = transport
        #: The same resolution preflight proved, not a second opinion about
        #: which commits this release is. Two answers here is the whole class of
        #: bug this replaced.
        self.sources = sources or SourceResolver(config, runner)

    def _workstation_uv(self) -> Path:
        override = self.config.workspace.uv
        if override is not None:
            return override
        return ensure_workstation_uv(self.config.workspace.toolchain_root)

    def prepare(
        self,
        release_id: str,
        *,
        reuse: bool = False,
        cutover_mode: str = "reversible",
        journal: Journal | None = None,
    ) -> list[dict[str, object]]:
        """Seal, transfer and build one release, recording each phase as it lands.

        The caller may pass its own journal, in which case the phases join the
        operation's own list in the order they actually happened rather than
        arriving as one block at the end. These are the slowest phases Ops has
        — a bundle seal, an rsync, a native build on a Pi — so they are also
        the ones worth watching.
        """

        phases = Journal() if journal is None else journal
        output = self.config.workspace.bundle_root / release_id
        output.parent.mkdir(parents=True, exist_ok=True)
        if reuse and not output.exists():
            # Backward-compatible activation of a release prepared by another
            # workstation. The subsequent sealed descriptor dry-run is still
            # authoritative and fails closed when the target is not prepared.
            phases.begin("release_reclaim_prepare")
            reclaim = self.reclaim(release_id, phase="prepare", required_bytes=0)
            phases.append({"phase": "release_reclaim_prepare", "result": reclaim})
            self.require_reclamation(reclaim, "ready")
            return phases
        phases.begin("bundle")
        if output.exists():
            if not reuse:
                raise OperationsError(
                    f"bundle output already exists; use --resume or a new ID: {output}"
                )
            transfer_id = self.validate_existing(output, release_id, cutover_mode=cutover_mode)
            bundle_result: dict[str, object] = {
                "status": "reused_validated_bundle",
                "manifest": str(output / "bundle.json"),
                "sha256": transfer_id,
            }
        else:
            bundle_result = self._seal(output, release_id, cutover_mode=cutover_mode)
            transfer_id = self.validate_existing(output, release_id, cutover_mode=cutover_mode)
        phases.append({"phase": "bundle", "result": bundle_result})
        phases.begin("release_reclaim_prepare")
        bundle_bytes = self._bundle_bytes(output)
        reclaim = self.reclaim(
            release_id,
            phase="prepare",
            required_bytes=bundle_bytes * (1 + _TARGET_EXPANSION_FACTOR),
        )
        phases.append({"phase": "release_reclaim_prepare", "result": reclaim})
        self.require_reclamation(reclaim, "ready")
        try:
            phases.begin("upload_guard")
            guard = self.transport.run_agent(
                "guard-upload",
                {"release_id": release_id, "transfer_id": transfer_id},
                sudo=False,
            )
            phases.append({"phase": "upload_guard", "result": guard})
            if guard.get("status") == "already_prepared":
                return phases
            if guard.get("status") not in _UPLOAD_GUARD_STATES:
                raise OperationsError("remote upload guard returned invalid evidence")
            remote_bundle = f"/var/tmp/eidolon-release-{release_id}"
            phases.begin("upload_finalize")
            if guard.get("status") != "ready_for_prepare":
                self.transport.upload_directory_resumable(output, remote_bundle)
            finalized = self.transport.run_agent(
                "finalize-upload",
                {"release_id": release_id, "transfer_id": transfer_id},
                sudo=False,
            )
            if finalized.get("status") not in {"finalized", "already_finalized"}:
                raise OperationsError("remote upload finalization returned invalid evidence")
            phases.append({"phase": "upload_finalize", "result": finalized})
            phases.begin("embedding_model")
            phases.append({"phase": "embedding_model", "result": self._carry_embedding_model()})
            phases.begin("prepare")
            phases.append({"phase": "prepare", "result": self._build_on_target(remote_bundle)})
        except Exception as exc:
            self._abort_after_failure(release_id, phases, exc)
        return phases

    def reclaim(
        self,
        release_id: str,
        *,
        phase: str,
        required_bytes: int = 0,
    ) -> dict[str, object]:
        return self.transport.run_agent(
            "reclaim-releases",
            {
                "release_id": release_id,
                "phase": phase,
                "required_bytes": required_bytes,
                "reserve_bytes": _CAPACITY_RESERVE_BYTES,
            },
            timeout=300,
        )

    @staticmethod
    def require_reclamation(result: dict[str, object], expected: str) -> None:
        capacity = result.get("capacity")
        if result.get("status") == "insufficient_capacity" and isinstance(capacity, dict):
            raise OperationsError(
                "Host has insufficient release capacity: "
                f"free={capacity.get('free_bytes_after')} "
                f"required={capacity.get('effective_required_bytes')} "
                f"reserve={capacity.get('reserve_bytes')}"
            )
        if result.get("status") != expected:
            raise OperationsError("release reclamation returned invalid evidence")

    def _abort_after_failure(
        self,
        release_id: str,
        phases: Journal,
        primary_error: Exception,
    ) -> None:
        try:
            phases.begin("release_reclaim_abort")
            aborted = self.reclaim(release_id, phase="abort")
            self.require_reclamation(aborted, "aborted")
            phases.append({"phase": "release_reclaim_abort", "result": aborted})
        except Exception as cleanup_error:
            raise OperationsError(
                f"release preparation failed ({primary_error}); candidate cleanup also failed: "
                f"{cleanup_error}"
            ) from cleanup_error
        raise primary_error

    @staticmethod
    def _bundle_bytes(output: Path) -> int:
        return sum(path.stat().st_size for path in output.rglob("*") if path.is_file())

    def _carry_embedding_model(self) -> dict[str, object]:
        """Put the pinned encoder on the Host, once, and leave it there.

        Carried beside a release rather than inside one: the palace built with
        this encoder outlives any single release, and a hundred megabytes that
        did not change should not be paid for again on every update. Skipped
        when the Host already holds this exact pin — which is asked of the
        digest the Host recorded, not of the directory existing.
        """

        artifact = PINNED_EMBEDDING_MODEL
        destination = host_embedding_model_root(artifact)
        expected = embedding_model_digest(artifact)
        # With sudo, like every other question asked of Host state: the model
        # root sits under /var/lib/eidolon, which the operator account cannot
        # even traverse.
        held = self.transport.run_agent(
            "embedding-model-state",
            {"destination": str(destination)},
        )
        if held.get("status") == "held" and held.get("digest") == expected:
            return {"status": "already_held", "model": artifact.model_id}

        source = ensure_workstation_embedding_model(
            self.config.workspace.toolchain_root, artifact
        )
        staging = f"/var/tmp/eidolon-encoder-{expected[:12]}"
        self.transport.run(("rm", "-rf", staging))
        self.transport.upload(source, staging, recursive=True)
        self.transport.run_agent(
            "install-embedding-model",
            {"staging": staging, "destination": str(destination)},
        )
        return {"status": "carried", "model": artifact.model_id}

    def _seal(
        self, output: Path, release_id: str, *, cutover_mode: str
    ) -> dict[str, object]:
        command = [
            str(self.config.workspace.release_cli),
            "bundle",
            release_id,
            str(output),
        ]
        command.extend(("--cutover-mode", cutover_mode))
        for source_id in SOURCE_IDS:
            flag = source_id.removeprefix("eidolon_").replace("eidolon-", "")
            command.extend((f"--{flag}-repo", str(self.config.sources[source_id].path)))
        for source_id in SOURCE_IDS:
            flag = source_id.removeprefix("eidolon_").replace("eidolon-", "")
            command.extend((f"--{flag}-revision", self.sources.revision(source_id)))
        # The same uv preflight proved, not a second opinion about which one
        # this workstation has.
        command.extend(("--uv", str(self._workstation_uv())))
        environment = os.environ.copy()
        environment.update(
            {
                "UV_DEFAULT_INDEX": self.config.workspace.python_index_url,
                "UV_HTTP_TIMEOUT": str(self.config.workspace.python_http_timeout_seconds),
                "UV_HTTP_RETRIES": str(self.config.workspace.python_http_retries),
                "UV_CONCURRENT_DOWNLOADS": str(
                    self.config.workspace.python_concurrent_downloads
                ),
                "EIDOLON_RELEASE_UV_CACHE": str(
                    self.config.workspace.toolchain_root / _KEPT_DEPENDENCY_CACHE
                ),
            }
        )
        bundle = checked(
            "commit-pinned source bundle",
            self.runner.run(command, env=environment, timeout=1800),
        )
        return parse_json(bundle.stdout, "bundle")

    def _build_on_target(self, remote_bundle: str) -> dict[str, object]:
        workspace = self.config.workspace
        result = self.transport.run(
            (
                "/usr/bin/env",
                f"UV_DEFAULT_INDEX={workspace.python_index_url}",
                f"UV_HTTP_TIMEOUT={workspace.python_http_timeout_seconds}",
                f"UV_HTTP_RETRIES={workspace.python_http_retries}",
                f"UV_CONCURRENT_DOWNLOADS={workspace.python_concurrent_downloads}",
                "/usr/bin/python3",
                f"{remote_bundle}/prepare_target.py",
                remote_bundle,
                "--uv",
                str(self.config.host.remote_uv),
            ),
            sudo=True,
            timeout=3600,
            operation="target-native release preparation",
        )
        return parse_json(result.stdout, "target-native release preparation")

    def validate_existing(
        self, output: Path, release_id: str, *, cutover_mode: str = "reversible"
    ) -> str:
        manifest = output / "bundle.json"
        try:
            document = json.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise OperationsError("existing bundle manifest is unreadable") from exc
        sources = document.get("sources") if isinstance(document, dict) else None
        if (
            not isinstance(document, dict)
            or document.get("release_id") != release_id
            or document.get("cutover_mode") != cutover_mode
            or not isinstance(sources, list)
            or len(sources) != len(SOURCE_IDS)
        ):
            raise OperationsError("existing bundle identity or source set is invalid")
        for source_id, item in zip(SOURCE_IDS, sources, strict=True):
            expected_revision = self.sources.revision(source_id)
            if (
                not isinstance(item, dict)
                or item.get("source_id") != source_id
                or item.get("archive") != f"sources/{source_id}.tar"
                or not isinstance(item.get("sha256"), str)
            ):
                raise OperationsError(f"existing bundle source record drifted: {source_id}")
            if item.get("revision") != expected_revision:
                # Correct, and now reachable by simply committing something: a
                # bundle is one exact combination of commits, so a resume onto a
                # moved HEAD is a different release wearing the same id. Say
                # both ways out, because a refusal that only says "drifted"
                # gets answered by deleting the directory.
                raise OperationsError(
                    f"this release id was sealed from a different commit of {source_id} "
                    f"({item.get('revision')}); the repository now resolves to "
                    f"{expected_revision}. Continue the original combination with "
                    f"--revision {source_id}={item.get('revision')} (repeat per moved "
                    "source), or seal what the repositories hold now under a new "
                    "--release-id"
                )
            if file_sha256(output / str(item["archive"])) != item["sha256"]:
                raise OperationsError(f"existing bundle source digest drifted: {source_id}")
        preparer = document.get("preparer")
        if (
            not isinstance(preparer, dict)
            or preparer.get("path") != "prepare_target.py"
            or not isinstance(preparer.get("sha256"), str)
            or file_sha256(output / "prepare_target.py") != preparer["sha256"]
        ):
            raise OperationsError("existing bundle preparer digest drifted")
        dependencies = document.get("python_dependencies")
        if (
            document.get("schema_version") != 2
            or not isinstance(dependencies, dict)
            or dependencies.get("path") != "python-dependencies.tar.gz"
            or dependencies.get("uv_version") != "0.11.15"
            or dependencies.get("python_version") != "3.13"
            or dependencies.get("platform") != "aarch64-manylinux_2_40"
            or dependencies.get("index_url") != self.config.workspace.python_index_url
            or not isinstance(dependencies.get("sha256"), str)
            or file_sha256(output / "python-dependencies.tar.gz") != dependencies["sha256"]
        ):
            raise OperationsError("existing bundle Python dependency cache drifted")
        return file_sha256(manifest)


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(block)
    except OSError as exc:
        raise OperationsError(f"bundle file is unreadable: {path}") from exc
    return digest.hexdigest()


def parse_json(value: str, operation: str) -> dict[str, object]:
    try:
        document = json.loads(value)
    except json.JSONDecodeError as exc:
        raise OperationsError(f"{operation} did not return one JSON document") from exc
    if not isinstance(document, dict):
        raise OperationsError(f"{operation} returned a non-object JSON document")
    return document
