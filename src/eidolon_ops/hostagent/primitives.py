"""Platform-neutral primitives: run a command, write a file, read a surface.

Nothing here knows a path, a unit name or a component. Everything that does is
either a fixed table in ``contract`` or arrives in the operator's payload.
"""

from __future__ import annotations

import base64
import fcntl
import grp
import hashlib
import http.client
import json
import os
import pwd
import socket
import ssl
import stat
import subprocess
import time
import uuid
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path


class TargetError(RuntimeError):
    """A target invariant or fixed operation failed closed."""

def run(
    command: Sequence[str],
    *,
    timeout: float = 120,
    cwd: Path | None = None,
    env: Mapping[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            tuple(command),
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=cwd,
            env=env,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise TargetError(f"command could not run: {command[0]}: {exc}") from exc

def checked(
    operation: str,
    command: Sequence[str],
    *,
    timeout: float = 120,
    cwd: Path | None = None,
    env: Mapping[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    result = run(command, timeout=timeout, cwd=cwd, env=env)
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or "no diagnostic output"
        raise TargetError(f"{operation} failed: {detail}")
    return result

def host_path(root: Path, path: Path) -> Path:
    if not path.is_absolute():
        raise TargetError(f"target path must be absolute: {path}")
    return path if root == Path("/") else root / path.relative_to("/")

def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()

def atomic_json(path: Path, document: object, *, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    payload = (json.dumps(document, indent=2, sort_keys=True) + "\n").encode("utf-8")
    try:
        with temporary.open("xb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)

def atomic_text(path: Path, value: str, *, mode: int = 0o644) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    payload = value.encode("utf-8")
    try:
        with temporary.open("xb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)

def atomic_symlink(target: Path, link: Path) -> None:
    link.parent.mkdir(parents=True, exist_ok=True)
    temporary = link.with_name(f".{link.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.symlink_to(target)
        os.replace(temporary, link)
    finally:
        temporary.unlink(missing_ok=True)

@contextmanager
def exclusive(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, 0o600)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise TargetError("another first-install transaction is in progress") from exc
        yield
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)

def decode_payload(value: str) -> dict[str, object]:
    try:
        document = json.loads(base64.urlsafe_b64decode(value.encode("ascii")))
    except (ValueError, UnicodeError, json.JSONDecodeError) as exc:
        raise TargetError("operator payload is invalid") from exc
    if not isinstance(document, dict):
        raise TargetError("operator payload must be an object")
    return document

def chown_path(path: Path, user: str, group: str) -> None:
    try:
        uid = pwd.getpwnam(user).pw_uid
        gid = grp.getgrnam(group).gr_gid
    except KeyError as exc:
        raise TargetError(f"required service identity is missing: {user}:{group}") from exc
    os.chown(path, uid, gid)

def give_to_invoking_operator(path: Path) -> None:
    """Hand a Host-produced file to whoever ran sudo, and to nobody else."""

    uid = os.environ.get("SUDO_UID")
    gid = os.environ.get("SUDO_GID")
    if uid is None or gid is None:
        return
    try:
        os.chown(path, int(uid), int(gid))
    except (OSError, ValueError) as exc:
        raise TargetError(f"backup could not be handed to the operator: {exc}") from exc

def now_timestamp() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")

def read_text(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return None

def read_uptime() -> float | None:
    value = read_text(Path("/proc/uptime"))
    if value is None:
        return None
    try:
        return float(value.split()[0])
    except (ValueError, IndexError):
        return None

def is_port(value: object) -> bool:
    return type(value) is int and 1 <= value <= 65535

class Budget:
    """One waiting window, shared by every probe allowed to spend it.

    Each probe used to get the whole window to itself. Two of them, each
    allowed 240 seconds, could outlast the 300-second deadline the operator's
    side holds — so a Host with one unhealthy worker did not report as
    degraded, it reported as a connection that timed out, which says nothing
    about the Host at all.

    A budget also makes the report able to say where the time went: whoever
    reads it should not have to guess which probe was the slow one.
    """

    __slots__ = ("_deadline", "total")

    def __init__(self, seconds: float) -> None:
        self.total = max(float(seconds), 0.0)
        self._deadline = time.monotonic() + self.total

    def remaining(self) -> float:
        return max(self._deadline - time.monotonic(), 0.0)

    def spent(self) -> float:
        return self.total - self.remaining()

    def exhausted(self) -> bool:
        return self.remaining() <= 0.0


def settle(
    probe: Callable[[], dict[str, object]],
    ready: Callable[[dict[str, object]], bool],
    *,
    seconds: float,
) -> dict[str, object]:
    """Give a probe a bounded chance to become true.

    A worker reconnecting to LiveKit cannot serve for a moment, and this report
    decides whether a release is rolled back. A momentary answer is not a
    verdict; one that stays wrong for the whole window is.
    """

    deadline = time.monotonic() + max(seconds, 0)
    while True:
        report = probe()
        if ready(report) or time.monotonic() >= deadline:
            return report
        time.sleep(0.5)

def private_file_check(path: Path, mode: int, user: str, group: str) -> dict[str, object]:
    try:
        metadata = path.stat(follow_symlinks=False)
        expected_uid = pwd.getpwnam(user).pw_uid
        expected_gid = grp.getgrnam(group).gr_gid
    except (OSError, KeyError):
        return {"healthy": False, "path": str(path), "error": "missing identity or file"}
    healthy = (
        stat.S_ISREG(metadata.st_mode)
        and not path.is_symlink()
        and stat.S_IMODE(metadata.st_mode) == mode
        and metadata.st_uid == expected_uid
        and metadata.st_gid == expected_gid
    )
    return {
        "healthy": healthy,
        "path": str(path),
        "mode": f"{stat.S_IMODE(metadata.st_mode):04o}",
        "owner": f"{metadata.st_uid}:{metadata.st_gid}",
    }

def unit_status(unit: str) -> dict[str, object]:
    result = run(
        (
            "/usr/bin/systemctl",
            "show",
            unit,
            "--property=LoadState,ActiveState,SubState,NRestarts",
        )
    )
    if result.returncode != 0:
        return {"error": result.stderr.strip() or result.stdout.strip() or "systemctl failed"}
    values: dict[str, object] = {}
    for line in result.stdout.splitlines():
        key, separator, value = line.partition("=")
        if separator and key in {"LoadState", "ActiveState", "SubState", "NRestarts"}:
            values[key] = int(value) if key == "NRestarts" and value.isdecimal() else value
    return values

def service_status(unit: str) -> dict[str, object]:
    enabled = run(("/usr/bin/systemctl", "is-enabled", unit), timeout=20)
    active = run(("/usr/bin/systemctl", "is-active", unit), timeout=20)
    return {
        "healthy": enabled.returncode == 0 and active.returncode == 0,
        "enabled": enabled.stdout.strip(),
        "active": active.stdout.strip(),
    }

def https_json_endpoint(host: str, port: int, path: str, *, label: str) -> dict[str, object]:
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    connection = http.client.HTTPSConnection(host, port, timeout=5, context=context)
    try:
        connection.request("GET", path)
        response = connection.getresponse()
        payload = response.read(1024 * 1024)
    except (OSError, http.client.HTTPException) as exc:
        raise TargetError(f"{label} self-check failed: {exc}") from exc
    finally:
        connection.close()
    if response.status != 200:
        raise TargetError(f"{label} self-check returned HTTP {response.status}")
    try:
        document = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise TargetError(f"{label} self-check returned invalid JSON") from exc
    if not isinstance(document, dict):
        raise TargetError(f"{label} self-check returned a non-object")
    return document

def https_json(host: str, port: int, path: str) -> tuple[int | None, dict[str, object] | None]:
    """One HTTPS request, reporting the status and any JSON body.

    Separate from :func:`https_json_endpoint` because some readiness facts live
    in a refusal: a Host that answers "not ready, and here is why" has told us
    something, and raising on the status would throw that away.
    """

    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    connection = http.client.HTTPSConnection(host, port, timeout=5, context=context)
    try:
        connection.request("GET", path)
        response = connection.getresponse()
        body = response.read(1024 * 1024)
        status = response.status
    except (OSError, http.client.HTTPException):
        return None, None
    finally:
        connection.close()
    try:
        document = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return status, None
    return status, document if isinstance(document, dict) else None


def http_json(host: str, port: int, path: str) -> tuple[int | None, dict[str, object] | None]:
    """One plain-HTTP request, reporting the status and any JSON body."""

    connection = http.client.HTTPConnection(host, port, timeout=5)
    try:
        connection.request("GET", path)
        response = connection.getresponse()
        body = response.read(1024 * 1024)
        status = response.status
    except (OSError, http.client.HTTPException):
        return None, None
    finally:
        connection.close()
    try:
        document = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return status, None
    return status, document if isinstance(document, dict) else None

def tcp_reachable(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=5):
            return True
    except OSError:
        return False

def environment_values(path: Path) -> dict[str, str]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError) as exc:
        raise TargetError(f"Host application environment is unreadable: {path}") from exc
    values: dict[str, str] = {}
    for raw in lines:
        if not raw:
            continue
        key, separator, value = raw.partition("=")
        if not separator or not key or not value or key in values:
            raise TargetError(f"Host application environment is invalid: {path}")
        values[key] = value
    return values

def environment_values_or_empty(path: Path) -> dict[str, str]:
    try:
        return environment_values(path)
    except TargetError:
        return {}


def unix_http_json(path: Path, resource: str) -> dict[str, object] | None:
    request = f"GET {resource} HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n".encode()
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(5)
            client.connect(str(path))
            client.sendall(request)
            with http.client.HTTPResponse(client) as response:
                response.begin()
                if response.status != 200:
                    return None
                document = json.loads(response.read(1024 * 1024))
                return document if isinstance(document, dict) else None
    except (OSError, ValueError, http.client.HTTPException):
        return None
