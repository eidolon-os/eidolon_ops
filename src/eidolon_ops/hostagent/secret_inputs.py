"""Bring a Host's credential files up to the set the product declares.

The gap this closes, stated plainly: a component grows a credential, the
generator learns to mint it, the contract check learns to require it — and every
Host installed before that day has no supported way to receive it. Reinstalling
rotates every secret and re-anchors the Host identity; hand-editing secrets over
SSH is not an operation. So the credential stayed on the workstation, the Host
kept answering ``503 credential is not configured``, and releases kept shipping
green on top of it.

Why installing again does not do this. ``install`` compares each staged input to
the one on the Host byte for byte and refuses on a difference, which is the right
rule for resuming an interrupted install — but it makes "the same file plus one
new key" indistinguishable from "somebody tampered with this". The two need
different verbs, and this is the additive one.

What it will do
---------------
Add **declared keys the Host is missing**, and nothing else. The declaration
travels in the payload rather than living here: the workstation owns what the
product requires, and an agent that carried its own copy would be a second
opinion that drifts. Every value comes from the staged file, so a secret shared
between two files arrives identical on both sides.

What it will not do
-------------------
Change a value the Host already has. That is rotation — a different operation,
with a blast radius this one deliberately does not have — and a convergence that
quietly rotated would make "add the missing key" unsafe to run on a working
Host, which is the only kind of Host anybody runs it on. It also will not create
a file that is absent: a credential file that does not exist means this Host was
never installed, and inventing one here would paper over that.
"""

from __future__ import annotations

import os
import shutil
import uuid
from pathlib import Path

from . import contract, primitives
from .primitives import TargetError


def _parse_env(text: str) -> dict[str, str]:
    """Keys and values from an environment file, ignoring what is not one.

    Deliberately small and deliberately not shared with the workstation's
    parser: this module ships to the Host inside a single-file payload with no
    dependency on anything installed there.
    """

    values: dict[str, str] = {}
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        values[key.strip()] = value.strip()
    return values


def converge(data: dict, root: Path) -> dict:
    """Add every declared key this Host is missing, from the staged files.

    ``apply`` false reports and writes nothing, because whoever runs this is
    holding a Host that currently works and is entitled to see the diff first.
    """

    declared = data.get("declared")
    if not isinstance(declared, dict) or not declared:
        raise TargetError("convergence requires a declared key set")
    apply = bool(data.get("apply"))
    # Resolved on first use, not up front: a Host that already holds every
    # declared key needs nothing staged, and demanding a staging directory to
    # tell somebody "already current" would make the safe case the awkward one.
    stage: Path | None = None

    added: dict[str, list[str]] = {}
    missing: dict[str, list[str]] = {}
    absent: list[str] = []

    for name in sorted(declared):
        keys = declared[name]
        if not isinstance(keys, list):
            raise TargetError(f"declared keys for {name} are not a list")
        if name not in contract.INSTALL_INPUTS:
            raise TargetError(f"{name} is not an install input")
        destination_value, user, group, mode = contract.INSTALL_INPUTS[name]
        destination = primitives.host_path(root, destination_value)
        if destination.is_symlink() or (
            destination.exists() and not destination.is_file()
        ):
            raise TargetError(f"credential path is unsafe: {destination_value}")
        if not destination.exists():
            # Not an error to report and not a file to create: this Host has no
            # such credential file, which is an install that never happened.
            absent.append(name)
            continue
        present = _parse_env(destination.read_text(encoding="utf-8"))
        wanted = [key for key in keys if key not in present]
        if not wanted:
            continue
        missing[name] = sorted(wanted)
        if not apply:
            continue

        if stage is None:
            stage = _staging_directory(data, root)
        staged = stage / name
        if not staged.is_file():
            raise TargetError(f"staged input is missing: {name}")
        offered = _parse_env(staged.read_text(encoding="utf-8"))
        unavailable = [key for key in wanted if key not in offered]
        if unavailable:
            raise TargetError(
                f"staged {name} does not carry {', '.join(sorted(unavailable))}"
            )
        # Rotation is a different operation. Refusing here rather than silently
        # keeping the Host's value, because an operator who staged a changed
        # secret and saw "converged" would believe it had been delivered.
        rotated = [
            key
            for key, value in offered.items()
            if key in present and present[key] != value
        ]
        if rotated:
            raise TargetError(
                f"staged {name} would change {', '.join(sorted(rotated))}; "
                "convergence only adds keys, so rotate through a reinstall"
            )

        body = destination.read_text(encoding="utf-8")
        if body and not body.endswith("\n"):
            body += "\n"
        body += "".join(f"{key}={offered[key]}\n" for key in sorted(wanted))
        temporary = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.tmp")
        try:
            temporary.write_text(body, encoding="utf-8")
            os.chmod(temporary, mode)
            _chown(temporary, user, group, root)
            os.replace(temporary, destination)
        finally:
            temporary.unlink(missing_ok=True)
        added[name] = sorted(wanted)

    return {
        "status": "converged" if added else ("planned" if missing else "already_current"),
        # Names only. A report carrying the values would put every new secret in
        # a terminal's scrollback and in whatever captured it.
        "added": {name: keys for name, keys in sorted(added.items())},
        "missing": {name: keys for name, keys in sorted(missing.items())},
        # Reported rather than raised: a profile may legitimately not install
        # every input, and the caller can tell which case it is looking at.
        "absent": sorted(absent),
        "applied": bool(added),
        "redaction": "credential values are never returned",
    }


def _staging_directory(data: dict, root: Path) -> Path:
    """Where the operator put this release's inputs, derived rather than taken.

    The same derivation ``install`` uses, and checked the same way: a path this
    action accepted verbatim would let a caller name any directory on the Host as
    the source of its credentials.
    """

    release_id = contract.fixed_release_id(data)
    declared = contract.VAR_TMP / f"eidolon-secrets-{release_id}"
    if (
        declared.parent != contract.VAR_TMP
        or contract.STAGING_NAME.fullmatch(declared.name) is None
    ):
        raise TargetError("secret staging path is unsafe")
    stage = primitives.host_path(root, declared)
    if not stage.is_dir():
        raise TargetError("staged input directory is missing")
    return stage


def converge_secret_inputs(payload: dict) -> dict:
    """The action entry point: this Host, at its real root."""

    return converge(dict(payload), Path("/"))


def _chown(path: Path, user: str, group: str, root: Path) -> None:
    """Owner and group, when this agent is running where they exist."""

    if root != Path("/") or os.geteuid() != 0:
        return
    shutil.chown(path, user=user, group=group)
