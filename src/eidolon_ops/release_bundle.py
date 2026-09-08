"""Seal a release here, get it onto the Host, and build it there.

Every step is resumable and every resumed step re-proves what it is resuming:
a bundle that drifted between attempts is refused rather than continued.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
import time
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

#: Where a Host keeps the interpreter uv fetches for it. Beside the other
#: foundation binaries rather than under root's home, because the services that
#: run on it are unprivileged and root's home is not theirs to enter.
TARGET_PYTHON_INSTALL_DIR = "/usr/local/lib/eidolon-foundation/python"
_RELEASE_ARTIFACT_STORE = "release-artifacts-v1"
_BUNDLE_SHAPE = {
    "bundle.json",
    "prepare_target.py",
    "artifacts",
    "sources",
}
_UPLOAD_GUARD_STATES = {"ready_for_upload", "resume_upload", "ready_for_prepare"}

# A sealed bundle coexists with its unpacked sources, environments and upload
# until transaction commit. Keep an explicit operating reserve beyond that
# estimate so a release cannot consume the space needed for logs and state.
_TARGET_EXPANSION_FACTOR = 4
_CAPACITY_RESERVE_BYTES = 1024**3
_LOCAL_BUNDLE_RETENTION_SECONDS = 48 * 60 * 60


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

    def cleanup_local_after_success(
        self,
        release_id: str,
        *,
        now: float | None = None,
        retention_seconds: int = _LOCAL_BUNDLE_RETENTION_SECONDS,
    ) -> dict[str, object]:
        """Remove a terminal release output and expired resumable outputs.

        This is deliberately called only after the Host has committed a
        successful install or activation.  Failed and dry-run transactions keep
        their bundle for ``--resume``; they become eligible for the bounded
        fallback sweep only after the retention window.

        Cleanup is best effort.  A release that is healthy on the Host must not
        be reported as failed merely because its workstation output could not be
        removed.
        """

        current = self._remove_local_bundle(release_id)
        cutoff = (time.time() if now is None else now) - retention_seconds
        expired: list[dict[str, object]] = []
        failures: list[dict[str, str]] = []
        if current["status"] == "cleanup_failed":
            failures.append({"path": str(current["path"]), "error": str(current["error"])})
        root = self.config.workspace.bundle_root
        try:
            candidates = tuple(root.iterdir()) if root.is_dir() else ()
        except OSError as exc:
            failures.append({"path": str(root), "error": str(exc)})
            candidates = ()
        for candidate in candidates:
            if (
                candidate.name == release_id
                or candidate.name.startswith(".")
                or candidate.name == _KEPT_DEPENDENCY_CACHE
                or candidate.is_symlink()
                or not candidate.is_dir()
                or not any((candidate / name).exists() for name in _BUNDLE_SHAPE)
            ):
                continue
            try:
                newest_mtime = candidate.lstat().st_mtime
                for path in candidate.rglob("*"):
                    newest_mtime = max(newest_mtime, path.lstat().st_mtime)
            except OSError as exc:
                failures.append({"path": str(candidate), "error": str(exc)})
                continue
            if newest_mtime > cutoff:
                continue
            result = self._remove_local_bundle(candidate.name)
            if result["status"] == "removed":
                expired.append(result)
            elif result["status"] == "cleanup_failed":
                failures.append({"path": str(result["path"]), "error": str(result["error"])})
        return {
            "status": "cleaned" if not failures else "cleanup_incomplete",
            "current": current,
            "expired": expired,
            "retention_seconds": retention_seconds,
            "failures": failures,
        }

    def _remove_local_bundle(self, release_id: str) -> dict[str, object]:
        output = self.config.workspace.bundle_root / release_id
        if output.is_symlink():
            return {
                "status": "cleanup_failed",
                "path": str(output),
                "error": "refusing to remove a symlinked bundle",
            }
        if not output.exists():
            return {"status": "absent", "path": str(output), "bytes": 0}
        if not output.is_dir():
            return {
                "status": "cleanup_failed",
                "path": str(output),
                "error": "bundle output is not a directory",
            }
        try:
            size = self._bundle_bytes(output)
            shutil.rmtree(output)
        except OSError as exc:
            return {
                "status": "cleanup_failed",
                "path": str(output),
                "error": str(exc),
            }
        return {"status": "removed", "path": str(output), "bytes": size}

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
                self.transport.upload_directory_resumable(
                    output, remote_bundle, exclude=("artifacts",)
                )
            finalized = self.transport.run_agent(
                "finalize-upload",
                {"release_id": release_id, "transfer_id": transfer_id},
                sudo=False,
            )
            if finalized.get("status") not in {"finalized", "already_finalized"}:
                raise OperationsError("remote upload finalization returned invalid evidence")
            phases.append({"phase": "upload_finalize", "result": finalized})
            phases.begin("release_artifacts")
            phases.append(
                {
                    "phase": "release_artifacts",
                    "result": self._carry_release_artifacts(output, transfer_id),
                }
            )
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

        source = ensure_workstation_embedding_model(self.config.workspace.toolchain_root, artifact)
        staging = f"/var/tmp/eidolon-encoder-{expected[:12]}"
        self.transport.run(("rm", "-rf", staging))
        self.transport.upload(source, staging, recursive=True)
        self.transport.run_agent(
            "install-embedding-model",
            {"staging": staging, "destination": str(destination)},
        )
        return {"status": "carried", "model": artifact.model_id}

    def _carry_release_artifacts(self, output: Path, transfer_id: str) -> dict[str, object]:
        """Install only content-addressed objects this Host does not hold.

        The release manifest binds every object by digest and size.  The Host
        answers from bytes it re-hashes, not from names, and finalization repeats
        that proof before an object becomes visible in the durable store.
        """

        artifacts = self._artifact_records(output)
        identities = [{"sha256": item["sha256"], "size": item["size"]} for item in artifacts]
        state = self.transport.run_agent(
            "release-artifact-state", {"artifacts": identities}, timeout=300
        )
        missing_wire = state.get("missing")
        if state.get("status") not in {"complete", "missing"} or not isinstance(missing_wire, list):
            raise OperationsError("Host returned invalid release artifact state")
        missing = {value for value in missing_wire if isinstance(value, str)}
        if len(missing) != len(missing_wire):
            raise OperationsError("Host returned invalid missing artifact identity")
        by_digest = {str(item["sha256"]): item for item in artifacts}
        if not missing <= set(by_digest):
            raise OperationsError("Host requested an artifact outside the release manifest")
        if not missing:
            return {
                "status": "already_held",
                "objects": len(artifacts),
                "bytes": 0,
            }

        wanted = [
            {"sha256": digest, "size": by_digest[digest]["size"]} for digest in sorted(missing)
        ]
        guard = self.transport.run_agent(
            "guard-release-artifacts",
            {"transfer_id": transfer_id, "artifacts": wanted},
            sudo=False,
        )
        if guard.get("status") not in {"ready_for_upload", "resume_upload"}:
            raise OperationsError("release artifact upload guard returned invalid evidence")
        remote = f"/var/tmp/eidolon-artifacts-{transfer_id[:16]}"
        with tempfile.TemporaryDirectory(prefix="eidolon-release-artifacts-") as raw:
            root = Path(raw)
            object_root = root / "sha256"
            object_root.mkdir()
            for digest in sorted(missing):
                record = by_digest[digest]
                source = output / str(record["bundle_path"])
                destination = object_root / digest
                try:
                    os.link(source, destination)
                except OSError:
                    shutil.copyfile(source, destination)
            self.transport.upload_directory_resumable(root, remote)
        finalized = self.transport.run_agent(
            "finalize-release-artifacts",
            {"transfer_id": transfer_id, "artifacts": wanted},
            timeout=1800,
        )
        if finalized.get("status") not in {"installed", "already_installed"}:
            raise OperationsError("release artifact finalization returned invalid evidence")
        return {
            "status": finalized["status"],
            "objects": len(wanted),
            "bytes": sum(int(item["size"]) for item in wanted),
        }

    @staticmethod
    def _artifact_records(output: Path) -> list[dict[str, object]]:
        document = parse_json((output / "bundle.json").read_text(encoding="utf-8"), "bundle")
        artifacts = document.get("artifacts")
        if not isinstance(artifacts, list) or not all(isinstance(item, dict) for item in artifacts):
            raise OperationsError("bundle artifact manifest is invalid")
        return artifacts

    def _sealed_source_ids(self) -> tuple[str, ...]:
        """Every repository this release carries, baseline plus capabilities.

        Ordered with the baseline first so the bundle's own source records stay
        in a stable order across Hosts that differ only in what they can do.
        """

        extra = sorted(set(self.config.sources) - set(SOURCE_IDS))
        return (*SOURCE_IDS, *extra)

    def _seal(self, output: Path, release_id: str, *, cutover_mode: str) -> dict[str, object]:
        command = [
            str(self.config.workspace.release_cli),
            "bundle",
            release_id,
            str(output),
        ]
        command.extend(("--cutover-mode", cutover_mode))
        # What this Host can do, so the release contract expects the same sets
        # Ops does. Told rather than inferred: the contract is validated on the
        # Host, before Ops' config exists there, so it has to be in the bundle.
        for capability in sorted(self.config.capabilities):
            command.extend(("--capability", capability))
        # The sources this Host pins, which already includes what a capability
        # adds — `config.sources` is checked against `expected_sources` when the
        # config loads, so a capability without its repository never gets here.
        for source_id in self._sealed_source_ids():
            flag = source_id.removeprefix("eidolon_").replace("eidolon-", "")
            command.extend((f"--{flag}-repo", str(self.config.sources[source_id].path)))
        for source_id in self._sealed_source_ids():
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
                "UV_CONCURRENT_DOWNLOADS": str(self.config.workspace.python_concurrent_downloads),
                "EIDOLON_RELEASE_UV_CACHE": str(
                    self.config.workspace.toolchain_root / _KEPT_DEPENDENCY_CACHE
                ),
                "EIDOLON_RELEASE_ARTIFACT_STORE": str(
                    self.config.workspace.toolchain_root / _RELEASE_ARTIFACT_STORE
                ),
            }
        )
        bundle = checked(
            "commit-pinned source bundle",
            self.runner.run(command, env=environment, timeout=1800),
        )
        return parse_json(bundle.stdout, "bundle")

    def _build_on_target(self, remote_bundle: str) -> dict[str, object]:
        """Build each component's venv on the Host, with its own interpreter.

        UV_PYTHON_INSTALL_DIR is the difference between a Host that starts and
        one that does not. This runs under sudo, so uv's default managed-Python
        location is /root/.local/share/uv — and /root is 0700, which every
        service user is refused at. The venvs then point their shebang at an
        interpreter none of them may execute, and systemd reports 203/EXEC
        against the console script rather than against the Python behind it.

        It only bites where uv has to fetch an interpreter at all: a Host whose
        system Python satisfies the pin never downloads one. Raspberry Pi OS
        ships 3.13 and does; this board ships 3.14.4 against a >=3.13,<3.14
        pin and does not. So the first board to need this was the second board.

        The foundation library is where it goes because that is already what
        that directory is for -- root-owned, world-traversable, and outliving
        any one release, which nats-server and livekit-server sit in for the
        same reasons.
        """

        workspace = self.config.workspace
        result = self.transport.run(
            (
                "/usr/bin/env",
                f"UV_PYTHON_INSTALL_DIR={TARGET_PYTHON_INSTALL_DIR}",
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
            or len(sources) != len(self._sealed_source_ids())
        ):
            raise OperationsError("existing bundle identity or source set is invalid")
        for source_id, item in zip(self._sealed_source_ids(), sources, strict=True):
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
        artifacts = document.get("artifacts")
        if not isinstance(artifacts, list) or len(artifacts) != 9:
            raise OperationsError("existing bundle artifact set drifted")
        artifact_ids: set[str] = set()
        artifact_digests: set[str] = set()
        channel_paths: set[str] = set()
        for item in artifacts:
            if (
                not isinstance(item, dict)
                or set(item)
                != {
                    "artifact_id",
                    "kind",
                    "sha256",
                    "size",
                    "bundle_path",
                    "install_path",
                }
                or not isinstance(item.get("artifact_id"), str)
                or item["artifact_id"] in artifact_ids
                or item.get("kind") not in {"dependency-cache", "channel-model"}
                or not isinstance(item.get("sha256"), str)
                or len(item["sha256"]) != 64
                or not isinstance(item.get("size"), int)
                or isinstance(item.get("size"), bool)
                or item["size"] < 0
                or item.get("bundle_path") != f"artifacts/sha256/{item['sha256']}"
                or not isinstance(item.get("install_path"), str)
            ):
                raise OperationsError("existing bundle artifact record drifted")
            if item["kind"] == "dependency-cache":
                if item["artifact_id"] != "python-dependencies" or item["install_path"] != "":
                    raise OperationsError("existing dependency artifact record drifted")
            else:
                if (
                    not item["install_path"]
                    or item["artifact_id"] != f"channel-model:{item['install_path']}"
                    or item["install_path"] in channel_paths
                ):
                    raise OperationsError("existing Channel artifact record drifted")
                channel_paths.add(item["install_path"])
            path = output / item["bundle_path"]
            if (
                not path.is_file()
                or path.is_symlink()
                or path.stat().st_size != item["size"]
                or file_sha256(path) != item["sha256"]
            ):
                raise OperationsError(
                    f"existing bundle artifact bytes drifted: {item['artifact_id']}"
                )
            artifact_ids.add(item["artifact_id"])
            artifact_digests.add(item["sha256"])
        artifact_root = output / "artifacts/sha256"
        if (
            "python-dependencies" not in artifact_ids
            or len(channel_paths) != 8
            or not artifact_root.is_dir()
            or artifact_root.is_symlink()
            or {path.name for path in artifact_root.iterdir()} != artifact_digests
        ):
            raise OperationsError("existing bundle artifact directory drifted")
        dependencies = document.get("python_dependencies")
        if (
            document.get("schema_version") != 3
            or not isinstance(dependencies, dict)
            or dependencies.get("artifact_id") != "python-dependencies"
            or dependencies.get("uv_version") != "0.11.15"
            or dependencies.get("python_version") != "3.13"
            or dependencies.get("platform") != "aarch64-manylinux_2_40"
            or dependencies.get("index_url") != self.config.workspace.python_index_url
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
