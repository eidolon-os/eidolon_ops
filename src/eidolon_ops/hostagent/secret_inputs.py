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


def verify_relationships(
    relationships: tuple[tuple[str, str, str, str, str], ...], root: Path
) -> dict:
    """Prove the credentials two files on this Host share are still one value.

    Nothing on a Host has ever asked this. The relationships are proven when the
    input set is written, on the workstation, by the install contract — and that
    runs on ``install --apply`` and nowhere else. After that the two copies live
    on the Host and drift independently, and four of the six pairs fail in ways
    no gate here can see:

    - a Channel provider token that stopped matching Hub's or Admin's answers
      401 to a reconciliation nobody is watching;
    - a pairing JWT secret that stopped matching the Agent's fails signature
      verification inside ``chat()``, so the first symptom is a person talking
      to a device;
    - a companion authority token that stopped matching Data's 401s a runtime
      call.

    Only the LiveKit pair shows up anywhere, and then only as the side effect of
    a worker that cannot register. So this reads both sides and compares them.

    Values are compared here and never leave: the report names the pair and the
    two places it lives, and nothing else. A digest would be no more useful to
    the operator and one more thing to be careless with.

    Fail-closed on a file this Host does not have. Every one of these files is
    part of a full install, so a missing one is not a profile that declined it —
    it is a Host that cannot hold the relationship at all, and reporting that as
    agreement would be the same lie this check exists to stop telling.
    """

    mismatched: list[dict[str, str]] = []
    unchecked: list[dict[str, str]] = []
    values: dict[str, dict[str, str] | None] = {}

    def _read(name: str) -> dict[str, str] | None:
        if name not in values:
            destination = primitives.host_path(root, contract.INSTALL_INPUTS[name][0])
            values[name] = (
                _parse_env(destination.read_text(encoding="utf-8"))
                if destination.is_file() and not destination.is_symlink()
                else None
            )
        return values[name]

    for left_file, left_key, right_file, right_key, label in relationships:
        where = {
            "label": label,
            "left": f"{left_file}:{left_key}",
            "right": f"{right_file}:{right_key}",
        }
        left, right = _read(left_file), _read(right_file)
        absent = [name for name, side in ((left_file, left), (right_file, right)) if side is None]
        if absent:
            unchecked.append({**where, "reason": f"not installed: {', '.join(sorted(set(absent)))}"})
            continue
        missing = [
            f"{name}:{key}"
            for name, side, key in (
                (left_file, left, left_key),
                (right_file, right, right_key),
            )
            if key not in side
        ]
        if missing:
            # Distinct from a mismatch, and already the subject of ``converge``:
            # a Host short a declared credential is repairable, a Host holding
            # two different ones is not.
            unchecked.append({**where, "reason": f"absent: {', '.join(missing)}"})
            continue
        if left[left_key] != right[right_key]:
            mismatched.append(where)

    return {
        "status": (
            "not_declared"
            if not relationships
            else "mismatched"
            if mismatched
            else "unverified"
            if unchecked
            else "agreed"
        ),
        "declared": len(relationships),
        "compared": len(relationships) - len(unchecked),
        "mismatched": mismatched,
        "unchecked": unchecked,
        "redaction": "credential values are compared on this Host and never returned",
    }


def converge(data: dict, root: Path) -> dict:
    """Add every declared key this Host is missing, from the staged files.

    ``apply`` false reports and writes nothing, because whoever runs this is
    holding a Host that currently works and is entitled to see the diff first.
    """

    declared = data.get("declared")
    if not isinstance(declared, dict) or not declared:
        raise TargetError("convergence requires a declared key set")
    apply = bool(data.get("apply"))
    # Answered on the way past, because this action already opens every one of
    # these files and the operator running it is asking about credentials. It
    # does not change what convergence does: adding a key a Host lacks is not
    # the fix for two files that disagree, and pretending otherwise would make
    # the report say a repair had happened.
    relationships = verify_relationships(
        contract.declared_credential_relationships(data), root
    )
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
        #
        # Over the *declared* keys, not every key the staged file happens to
        # carry. A staged Host-bound file also holds fields the controller
        # renders for this Host — where devices reach LiveKit, where its Owner
        # domain is — and those differ whenever the Host binding changes, which
        # is not a rotation and has its own delivery path. Comparing them here
        # would refuse a convergence for a reason that is not true.
        rotated = [
            key
            for key in keys
            if key in offered and key in present and present[key] != offered[key]
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
        # Likewise reported, and for a sharper reason: nothing here can repair a
        # pair that disagrees, so raising would stop a convergence that is
        # otherwise correct and leave the Host worse off than before.
        "relationships": relationships,
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
