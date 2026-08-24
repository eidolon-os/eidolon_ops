"""Install the pinned foundation: verified downloads and managed links only."""

from __future__ import annotations

import os
import shutil
import tarfile
import tempfile
import time
import uuid
from collections.abc import Iterator, Mapping
from contextlib import contextmanager, suppress
from pathlib import Path, PurePosixPath

from . import contract, foundation, primitives
from .primitives import TargetError


def download_verified(artifact: Mapping[str, str]) -> Path:
    foundation.FOUNDATION_CACHE.mkdir(parents=True, exist_ok=True, mode=0o755)
    suffixes = {
        "node-tar": ".tar.xz",
        "pip-wheel": ".whl",
        "tar-binary": ".tar.gz",
    }
    try:
        suffix = suffixes[artifact["kind"]]
    except KeyError as exc:
        raise TargetError(f"unsupported foundation artifact kind: {artifact['kind']}") from exc
    filename = f"{artifact['artifact_id']}-{artifact['version']}{suffix}"
    if artifact["kind"] == "pip-wheel":
        filename = PurePosixPath(artifact["url"].split("?", 1)[0]).name
        if not filename.endswith(".whl") or "/" in filename or filename in {"", ".", ".."}:
            raise TargetError("pip wheel URL does not contain a valid filename")
    destination = foundation.FOUNDATION_CACHE / filename
    if destination.is_file() and primitives.file_sha256(destination) == artifact["sha256"]:
        return destination
    temporary = destination.with_name(f".{destination.name}.partial")
    headers = ()
    if artifact["url"].startswith("https://api.github.com/"):
        headers = (
            "--header",
            "Accept: application/octet-stream",
            "--header",
            "X-GitHub-Api-Version: 2022-11-28",
        )
    primitives.checked(
        f"download {artifact['artifact_id']}",
        (
            "/usr/bin/curl",
            "--fail",
            "--location",
            "--retry",
            "3",
            "--retry-all-errors",
            "--connect-timeout",
            "20",
            "--speed-limit",
            "1024",
            "--speed-time",
            "60",
            "--continue-at",
            "-",
            *headers,
            "--output",
            str(temporary),
            artifact["url"],
        ),
        timeout=900,
    )
    if primitives.file_sha256(temporary) != artifact["sha256"]:
        temporary.unlink(missing_ok=True)
        raise TargetError(f"downloaded artifact hash mismatch: {artifact['artifact_id']}")
    os.chmod(temporary, 0o644)
    os.replace(temporary, destination)
    return destination

def install_managed_link(link: Path, target: Path) -> None:
    if link.is_symlink() and link.resolve() == target.resolve():
        return
    if link.exists() or link.is_symlink():
        raise TargetError(f"refusing to replace unmanaged executable: {link}")
    temporary = link.with_name(f".{link.name}.{uuid.uuid4().hex}.tmp")
    temporary.symlink_to(target)
    os.replace(temporary, link)

def install_tar_binary(artifact: Mapping[str, str]) -> None:
    archive_path = download_verified(artifact)
    destination = (
        foundation.FOUNDATION_LIBRARY / artifact["artifact_id"] / artifact["version"] / artifact["executable"]
    )
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
    if not destination.is_file():
        with tarfile.open(archive_path, "r:*") as archive:
            members = [
                member
                for member in archive.getmembers()
                if member.isfile()
                and PurePosixPath(member.name).name == artifact["executable"]
                and member.size <= 256 * 1024**2
            ]
            if len(members) != 1:
                raise TargetError(
                    f"artifact has an invalid executable set: {artifact['artifact_id']}"
                )
            stream = archive.extractfile(members[0])
            if stream is None:
                raise TargetError(f"artifact executable cannot be read: {artifact['artifact_id']}")
            temporary = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.tmp")
            try:
                with temporary.open("xb") as output:
                    shutil.copyfileobj(stream, output)
                    output.flush()
                    os.fsync(output.fileno())
                os.chmod(temporary, 0o755)
                os.replace(temporary, destination)
            finally:
                temporary.unlink(missing_ok=True)
    install_managed_link(foundation.LOCAL_BIN / artifact["executable"], destination)

def safe_node_member(member: tarfile.TarInfo, top: str) -> bool:
    name = PurePosixPath(member.name)
    if name.is_absolute() or ".." in name.parts or not name.parts or name.parts[0] != top:
        return False
    if member.issym() or member.islnk():
        target = PurePosixPath(member.linkname)
        if target.is_absolute():
            return False
        combined = name.parent.joinpath(target)
        depth = 0
        for part in combined.parts:
            depth += -1 if part == ".." else (0 if part in {"", "."} else 1)
            if depth < 1:
                return False
    return True

def install_node(artifact: Mapping[str, str]) -> None:
    archive_path = download_verified(artifact)
    top = f"node-v{artifact['version']}-linux-arm64"
    destination = foundation.LOCAL_LIB / top
    if not destination.is_dir():
        with tempfile.TemporaryDirectory(prefix="eidolon-node-", dir=foundation.LOCAL_LIB) as raw:
            stage = Path(raw)
            with tarfile.open(archive_path, "r:xz") as archive:
                members = archive.getmembers()
                if not members or not all(safe_node_member(member, top) for member in members):
                    raise TargetError("Node archive contains an unsafe member")
                archive.extractall(stage, filter="data")
            extracted = stage / top
            if not (extracted / "bin/node").is_file():
                raise TargetError("Node archive is missing bin/node")
            os.replace(extracted, destination)
    for executable in ("node", "npm", "npx", "corepack"):
        install_managed_link(foundation.LOCAL_BIN / executable, destination / "bin" / executable)

def install_uv(artifact: Mapping[str, str]) -> None:
    current = foundation.binary_version("uv")
    if current["healthy"]:
        return
    path = foundation.LOCAL_BIN / "uv"
    if path.exists() or path.is_symlink():
        raise TargetError(f"refusing to replace unmanaged executable: {path}")
    wheel = download_verified(artifact)
    requirement = contract.VAR_TMP / f"eidolon-uv-{uuid.uuid4().hex}.txt"
    try:
        requirement.write_text(
            f"uv @ {wheel.as_uri()} --hash=sha256:{artifact['sha256']}\n",
            encoding="utf-8",
        )
        os.chmod(requirement, 0o600)
        primitives.checked(
            "install pinned uv wheel",
            (
                "/usr/bin/python3",
                "-m",
                "pip",
                "install",
                "--break-system-packages",
                "--disable-pip-version-check",
                "--no-deps",
                "--no-index",
                "--only-binary=:all:",
                "--require-hashes",
                "--requirement",
                str(requirement),
            ),
            timeout=900,
        )
    finally:
        requirement.unlink(missing_ok=True)

def install_journal_persistence(content: str) -> None:
    """Make this Host keep its own account of itself across a reboot.

    Raspberry Pi OS keeps the journal in RAM to spare the SD card, which means
    every restart erases the record of whatever went wrong before it. Writing
    the drop-in is only half of it: journald has to be told, or the file sits
    there being correct while the logs stay in memory, and the Host looks
    fixed without being fixed.
    """

    foundation.JOURNAL_PERSISTENCE.parent.mkdir(parents=True, exist_ok=True)
    existing = primitives.read_text(foundation.JOURNAL_PERSISTENCE)
    if existing == content and foundation.journal_is_persistent():
        return
    primitives.atomic_text(foundation.JOURNAL_PERSISTENCE, content, mode=0o644)
    foundation.JOURNAL_DIRECTORY.mkdir(parents=True, exist_ok=True)
    primitives.checked(
        "adopt persistent journal storage",
        ("/usr/bin/systemctl", "restart", "systemd-journald"),
        timeout=60,
    )
    # journald migrates what it is holding in RAM into the new directory on
    # its own; flushing makes that happen now rather than at some later
    # rotation, so the next question asked of this Host can be answered.
    # Best effort on purpose: the drop-in is written and journald has been
    # restarted, so persistence is already in force — failing an install over
    # the timing of a migration would be refusing the fix to hurry it.
    with suppress(TargetError):
        primitives.run(("/usr/bin/journalctl", "--flush"), timeout=60)


@contextmanager
def foundation_apt_options(contract: Mapping[str, object]) -> Iterator[tuple[str, ...]]:
    version = foundation.os_release().get("VERSION_ID", "").split(".", 1)[0]
    suites = {"13": "trixie"}
    if version not in suites:
        raise TargetError("foundation apt mirror requires reviewed Debian version")
    mirrors = contract["apt_mirrors"]
    if not isinstance(mirrors, dict):
        raise TargetError("foundation apt mirror contract is invalid")
    suite = suites[version]
    with tempfile.TemporaryDirectory(prefix="eidolon-apt-") as temporary:
        root = Path(temporary)
        source_parts = root / "parts"
        lists = root / "lists"
        source_parts.mkdir()
        (lists / "partial").mkdir(parents=True)
        sources = root / "eidolon.sources"
        sources.write_text(
            "\n".join(
                (
                    "Types: deb",
                    f"URIs: {mirrors['debian']}",
                    f"Suites: {suite} {suite}-updates",
                    "Components: main contrib non-free non-free-firmware",
                    "Signed-By: /usr/share/keyrings/debian-archive-keyring.pgp",
                    "",
                    "Types: deb",
                    f"URIs: {mirrors['raspberrypi']}",
                    f"Suites: {suite}",
                    "Components: main",
                    "Signed-By: /usr/share/keyrings/raspberrypi-archive-keyring.pgp",
                    "",
                    "Types: deb",
                    f"URIs: {mirrors['security']}",
                    f"Suites: {suite}-security",
                    "Components: main contrib non-free non-free-firmware",
                    "Signed-By: /usr/share/keyrings/debian-archive-keyring.pgp",
                    "",
                )
            ),
            encoding="utf-8",
        )
        yield (
            *foundation.APT_COMMAND_OPTIONS,
            "-o",
            f"Dir::Etc::sourcelist={sources}",
            "-o",
            f"Dir::Etc::sourceparts={source_parts}",
            "-o",
            f"Dir::State::lists={lists}",
        )

def foundation_install(payload: Mapping[str, object]) -> dict[str, object]:
    contract = foundation.foundation_contract(payload)
    if os.geteuid() != 0:
        raise TargetError("foundation installation requires root")
    platform_checks = foundation.foundation_platform_checks()
    required_platform = dict(platform_checks)
    if not all(required_platform.values()):
        raise TargetError(f"unsupported Raspberry Pi host platform: {required_platform}")
    with primitives.exclusive(foundation.FOUNDATION_LOCK):
        evidence: dict[str, object] = {
            "schema_version": 1,
            "profile": foundation.FOUNDATION_PROFILE,
            "status": "installing",
            "phase": "validated",
            "artifacts": {
                artifact["artifact_id"]: {
                    "version": artifact["version"],
                    "sha256": artifact["sha256"],
                }
                for artifact in contract["artifacts"]
            },
            "error": None,
            "updated_at": int(time.time()),
        }

        def record(status: str, phase: str, error: str | None = None) -> None:
            evidence.update(
                {
                    "status": status,
                    "phase": phase,
                    "error": error,
                    "updated_at": int(time.time()),
                }
            )
            primitives.atomic_json(foundation.FOUNDATION_EVIDENCE, evidence)

        phase = "validated"
        record("installing", phase)
        try:
            environment = dict(os.environ)
            environment["DEBIAN_FRONTEND"] = "noninteractive"
            with foundation_apt_options(contract) as apt_options:
                primitives.checked(
                    "refresh apt metadata",
                    ("/usr/bin/apt-get", *apt_options, "update"),
                    timeout=900,
                    env=environment,
                )
                primitives.checked(
                    "install foundation packages",
                    (
                        "/usr/bin/apt-get",
                        *apt_options,
                        "install",
                        "-y",
                        "--no-install-recommends",
                        *contract["apt_packages"],
                    ),
                    timeout=1800,
                    env=environment,
                )
            phase = "packages"
            record("installing", phase)
            for artifact in contract["artifacts"]:
                kind = artifact["kind"]
                if kind == "tar-binary":
                    install_tar_binary(artifact)
                elif kind == "pip-wheel":
                    install_uv(artifact)
                elif kind == "node-tar":
                    install_node(artifact)
                else:
                    raise TargetError(f"unsupported foundation artifact kind: {kind}")
            phase = "artifacts"
            record("installing", phase)
            install_journal_persistence(str(contract["journal_persistence"]))
            phase = "journal"
            record("installing", phase)
            for unit in contract["services"]:
                primitives.checked(
                    f"enable foundation service {unit}",
                    ("/usr/bin/systemctl", "enable", "--now", unit),
                    timeout=120,
                )
            phase = "completed"
            record("installed", phase)
            result = foundation.foundation_doctor(payload)
            if result["status"] != "healthy":
                raise TargetError(
                    "foundation installation completed but the health gate is degraded"
                )
            result["evidence"] = dict(evidence)
            return {
                "status": "installed",
                "profile": foundation.FOUNDATION_PROFILE,
                "doctor": result,
            }
        except Exception as exc:
            record("failed", phase, str(exc))
            raise
