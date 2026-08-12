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
from eidolon_ops.errors import OperationsError
from eidolon_ops.process import ProcessRunner, checked
from eidolon_ops.transport import SSHTransport

#: Dependencies this workstation has already fetched, kept between builds so a
#: release costs the network only what actually changed. It sits beside the
#: bundles rather than inside a build, because uv binds a cache to the path it
#: was built at and a build directory does not outlive the build. Named a dot
#: entry so it cannot be mistaken for a release id when the root is listed.
_KEPT_DEPENDENCY_CACHE = ".uv-cache"
_BUNDLE_SHAPE = {
    "bundle.json",
    "prepare_target.py",
    "python-dependencies.tar.gz",
    "sources",
}
_UPLOAD_GUARD_STATES = {"ready_for_upload", "resume_upload", "ready_for_prepare"}


class BundleTransfer:
    def __init__(
        self,
        config: OperationsConfig,
        runner: ProcessRunner,
        transport: SSHTransport,
    ) -> None:
        self.config = config
        self.runner = runner
        self.transport = transport

    def prepare(self, release_id: str, *, reuse: bool = False) -> list[dict[str, object]]:
        output = self.config.workspace.bundle_root / release_id
        output.parent.mkdir(parents=True, exist_ok=True)
        if reuse and not output.exists():
            # Backward-compatible activation of a release prepared by another
            # workstation. The subsequent sealed descriptor dry-run is still
            # authoritative and fails closed when the target is not prepared.
            return []
        if output.exists():
            if not reuse:
                raise OperationsError(
                    f"bundle output already exists; use --resume or a new ID: {output}"
                )
            transfer_id = self.validate_existing(output, release_id)
            bundle_result: dict[str, object] = {
                "status": "reused_validated_bundle",
                "manifest": str(output / "bundle.json"),
                "sha256": transfer_id,
            }
        else:
            bundle_result = self._seal(output, release_id)
            transfer_id = self.validate_existing(output, release_id)
        guard = self.transport.run_agent(
            "guard-upload",
            {"release_id": release_id, "transfer_id": transfer_id},
            sudo=False,
        )
        if guard.get("status") == "already_prepared":
            return [
                {"phase": "bundle", "result": bundle_result},
                {"phase": "upload_guard", "result": guard},
            ]
        if guard.get("status") not in _UPLOAD_GUARD_STATES:
            raise OperationsError("remote upload guard returned invalid evidence")
        remote_bundle = f"/var/tmp/eidolon-release-{release_id}"
        if guard.get("status") != "ready_for_prepare":
            self.transport.upload_directory_resumable(output, remote_bundle)
        finalized = self.transport.run_agent(
            "finalize-upload",
            {"release_id": release_id, "transfer_id": transfer_id},
            sudo=False,
        )
        if finalized.get("status") not in {"finalized", "already_finalized"}:
            raise OperationsError("remote upload finalization returned invalid evidence")
        prepare = self._build_on_target(remote_bundle)
        return [
            {"phase": "bundle", "result": bundle_result},
            {"phase": "upload_guard", "result": guard},
            {"phase": "upload_finalize", "result": finalized},
            {"phase": "prepare", "result": prepare},
        ]

    def _seal(self, output: Path, release_id: str) -> dict[str, object]:
        command = [
            str(self.config.workspace.release_cli),
            "bundle",
            release_id,
            str(output),
        ]
        for source_id in SOURCE_IDS:
            flag = source_id.removeprefix("eidolon_").replace("eidolon-", "")
            command.extend((f"--{flag}-repo", str(self.config.sources[source_id].path)))
        for source_id in SOURCE_IDS:
            flag = source_id.removeprefix("eidolon_").replace("eidolon-", "")
            command.extend((f"--{flag}-revision", self.config.sources[source_id].revision))
        command.extend(("--uv", str(self.config.workspace.uv)))
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
                    self.config.workspace.bundle_root / _KEPT_DEPENDENCY_CACHE
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

    def validate_existing(self, output: Path, release_id: str) -> str:
        manifest = output / "bundle.json"
        try:
            document = json.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise OperationsError("existing bundle manifest is unreadable") from exc
        sources = document.get("sources") if isinstance(document, dict) else None
        if (
            not isinstance(document, dict)
            or document.get("release_id") != release_id
            or not isinstance(sources, list)
            or len(sources) != len(SOURCE_IDS)
        ):
            raise OperationsError("existing bundle identity or source set is invalid")
        for source_id, item in zip(SOURCE_IDS, sources, strict=True):
            expected_revision = self.config.sources[source_id].revision
            if (
                not isinstance(item, dict)
                or item.get("source_id") != source_id
                or item.get("revision") != expected_revision
                or item.get("archive") != f"sources/{source_id}.tar"
                or not isinstance(item.get("sha256"), str)
            ):
                raise OperationsError(f"existing bundle source record drifted: {source_id}")
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
