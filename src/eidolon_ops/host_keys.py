"""The host key a profile trusts, recorded where the profile can own it.

`StrictHostKeyChecking=yes` and `HostKeyAlias=<hostname>` mean this Host is
trusted by name, which is what keeps changing link from being a trust decision.
It also means two boards that publish the same name cannot both be trusted:
swapping the board behind `eidolon-pi5.local` presents a different key under a
name that already has one, and every operation refuses until someone edits the
file by hand.

That file used to be the operator's own `~/.ssh/known_hosts`, where the edit is
untracked, indistinguishable from their hundred other hosts, and easy to get
wrong in the direction that matters — deleting the wrong line fails loudly,
while pasting an unverified key fails silently and forever. So the profile owns
its own file, beside the private inputs that are already its own, and replacing
what it trusts is an operation with a plan rather than a text editor.

One key type is recorded, ed25519. OpenSSH offers several and a first
connection would store all of them, but then "the fingerprint" an operator
confirms is three fingerprints, and the one they compare is whichever they
happened to read. Every sshd this product runs on generates ed25519, it is
what OpenSSH prefers when known_hosts names it, and one line per Host means a
board swap is one line changed.

Reaching the Host is not a trust question here and does not need the wire: the
key is the same whichever link answers, so candidates are tried best-first and
the first answer wins. `ssh-keyscan` cannot bind to an interface the way the
transport does, and does not need to — a link-local address that leaves by the
wrong interface simply gets no answer, and the next candidate is tried.
"""

from __future__ import annotations

import os
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from eidolon_ops.endpoints import HostEndpoint
from eidolon_ops.errors import OperationsError
from eidolon_ops.process import ProcessRunner, SubprocessRunner

#: What a first connection would have stored, narrowed on purpose. See the
#: module docstring: three fingerprints make the one an operator checks
#: ambiguous.
KEY_TYPE = "ed25519"


@dataclass(frozen=True, slots=True)
class HostKey:
    """One host key, as both the line to record and the value a human checks."""

    key_type: str
    key: str
    fingerprint: str

    def line(self, alias: str) -> str:
        """The known_hosts entry, keyed by the alias trust is granted under."""

        return f"{alias} {self.key_type} {self.key}"


def scan(
    runner: ProcessRunner,
    endpoints: Sequence[HostEndpoint],
    *,
    port: int,
    timeout: float,
    keyscan: str = "ssh-keyscan",
    keygen: str = "ssh-keygen",
) -> tuple[HostKey, HostEndpoint]:
    """The key the Host presents right now, and which candidate answered.

    Deliberately not routed through the transport: the case this exists for is
    a Host whose recorded key is wrong, and the transport refuses that Host by
    design. This reads what is being presented; deciding whether to trust it
    belongs to the operator.
    """

    attempted: list[str] = []
    for endpoint in endpoints:
        attempted.append(endpoint.address)
        result = runner.run(
            (keyscan, "-T", str(int(timeout)), "-p", str(port), "-t", KEY_TYPE, endpoint.address),
            timeout=timeout + 10,
        )
        for raw in result.stdout.splitlines():
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            fields = line.split()
            if len(fields) < 3:
                continue
            key_type, key = fields[1], fields[2]
            return HostKey(
                key_type=key_type,
                key=key,
                fingerprint=_fingerprint(runner, key_type, key, keygen=keygen),
            ), endpoint
    tried = ", ".join(attempted) if attempted else "no candidate address"
    raise OperationsError(
        f"no {KEY_TYPE} host key could be read from this Host ({tried}). "
        "The name has to resolve and sshd has to answer before its key can be "
        "trusted; nothing was written."
    )


def _fingerprint(runner: ProcessRunner, key_type: str, key: str, *, keygen: str) -> str:
    """The SHA256 fingerprint, computed by ssh-keygen rather than by us.

    The operator compares this against what the Host itself prints, so it has
    to be the same function on both sides — not a hash that merely looks like
    one.
    """

    result = runner.run(
        (keygen, "-l", "-f", "-"),
        input_bytes=f"{key_type} {key}\n".encode(),
        timeout=30,
    )
    for field in result.stdout.split():
        if field.startswith("SHA256:"):
            return field
    raise OperationsError("ssh-keygen did not report a SHA256 fingerprint for the scanned host key")


def _matching_lines(path: Path, alias: str) -> tuple[str, ...]:
    """Let OpenSSH identify its own hashed hosts, patterns and markers."""
    if not path.exists():
        return ()
    result = SubprocessRunner().run(("ssh-keygen", "-F", alias, "-f", str(path)), timeout=30)
    if result.returncode not in {0, 1} or result.stderr.strip():
        raise OperationsError(f"could not inspect SSH trust for {alias!r} in {path}")
    return tuple(
        line.strip() for line in result.stdout.splitlines()
        if line.strip() and not line.startswith("#")
    )


def _plain_entry(line: str) -> list[str]:
    fields = line.split()
    if len(fields) < 3 or fields[0].startswith("@"):
        raise OperationsError(
            "trust-host-key cannot replace a revoked or certificate-authority entry; "
            "review this Host's known_hosts policy explicitly before changing its key"
        )
    # A broad pattern represents a policy for other Hosts too, not one key to replace.
    if not fields[0].startswith("|1|") and any(c in fields[0] for c in "*?!"):
        raise OperationsError(
            "trust-host-key cannot replace a wildcard or negated host pattern; "
            "give this Host an explicit known_hosts entry first"
        )
    return fields


def recorded(path: Path, alias: str) -> tuple[str, ...]:
    """Every key already trusted under ``alias``, as raw ``type key`` pairs."""
    entries: list[str] = []
    for line in _matching_lines(path, alias):
        fields = _plain_entry(line)
        entries.append(f"{fields[1]} {fields[2]}")
    return tuple(entries)


def fingerprints(
    runner: ProcessRunner, entries: Sequence[str], *, keygen: str = "ssh-keygen"
) -> tuple[str, ...]:
    """Fingerprints for recorded entries, so a plan can show what it replaces."""

    values: list[str] = []
    for entry in entries:
        key_type, _, key = entry.partition(" ")
        try:
            values.append(_fingerprint(runner, key_type, key.strip(), keygen=keygen))
        except OperationsError:
            values.append("unreadable")
    return tuple(values)


def write(path: Path, alias: str, host_key: HostKey) -> None:
    """Replace every entry for ``alias`` with this one, atomically.

    Other aliases in the file are preserved: a profile-owned file is still a
    file an operator may have pointed two Hosts at. The write is atomic and
    mode 0600 because a truncated known_hosts is a Host that cannot be reached
    at all, and because this sits beside the private inputs.
    """

    path.parent.mkdir(parents=True, exist_ok=True)
    matched = set(_matching_lines(path, alias))
    for line in matched:
        _plain_entry(line)
    kept: list[str] = []
    if path.exists():
        for raw in path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line:
                continue
            if line not in matched:
                kept.append(raw)
                continue
            fields = _plain_entry(line)
            if not fields[0].startswith("|1|"):
                remaining = [name for name in fields[0].split(",") if name.casefold() != alias.casefold()]
                if remaining:
                    kept.append(" ".join([",".join(remaining), *fields[1:]]))
    kept.append(host_key.line(alias))
    handle, temporary = tempfile.mkstemp(dir=path.parent, prefix=".known_hosts-")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            stream.write("\n".join(kept) + "\n")
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise
