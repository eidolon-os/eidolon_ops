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
Add **declared keys the Host is missing**, and **optional whole-file inputs the
Host is missing**, and nothing else. Both declarations travel in the payload
rather than living here: the workstation owns what the product requires, and an
agent that carried its own copy would be a second opinion that drifts. Every
value comes from the staged file, so a secret shared between two files arrives
identical on both sides.

The second granularity is newer and narrower. It exists because "write-once"
and "install-only" had been collapsed into one rule: ``factory_setup_code`` may
never be overwritten, which was read as "only ``install`` may ever create it" —
so a Host declaring `claim_window = always_open` could hold that promise for
weeks with no way to receive the eight bytes that keep it, short of rebuilding
and reinstalling the whole release. Only inputs the contract already names in
``OPTIONAL_INSTALL_INPUTS`` are eligible, and only when they are absent.

What it will not do
-------------------
Change a value the Host already has. That is rotation — a different operation,
with a blast radius this one deliberately does not have — and a convergence that
quietly rotated would make "add the missing key" unsafe to run on a working
Host, which is the only kind of Host anybody runs it on. It also will not create
a *declared environment file* that is absent: one of those missing means this
Host was never installed, and inventing one here would paper over that. The
optional file inputs above are the opposite case and that is why they are
separable — their absence is a documented state the contract already allows,
not evidence that something else went wrong.
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
        # Said here rather than left for the reader to work out. A finding that
        # names no verb is one people route around, and this one used to name
        # none because none existed.
        **(
            {"repair": "run `repair-credentials` to set these to this machine's value"}
            if mismatched
            else {}
        ),
        "redaction": "credential values are compared on this Host and never returned",
    }


def _converge_opaque_files(
    data: dict, root: Path, *, apply: bool, stage: Path | None
) -> tuple[list[str], list[str], Path | None]:
    """Create the optional whole-file inputs this Host is missing, and no others.

    The second half of "additive", at the only other granularity an input comes
    in. The first half adds a key an environment file lacks and refuses to
    replace one; this adds a *file* the Host lacks and does not so much as read
    one that is there. Same rule, same refusal, one level up.

    It exists because write-once and install-only were being treated as one
    thing. ``factory_setup_code`` is write-once — nothing may overwrite it — and
    that was read as "only ``install`` may create it", which left a Host whose
    own declaration needed the file with no way to receive it short of a full
    reinstall: rebuild the release, stop the product, run the data baseline,
    switch every component. For eight bytes. Worse, the readiness fact that
    catches the gap (``claim_window_honored``) then reports a failure whose only
    remedy is that reinstall, which is how a red light becomes one people learn
    to ignore.

    Three things keep this narrow, and each is a refusal rather than a judgement:

    * **the workstation names what it offers.** The list travels in the payload
      like ``declared`` does, so a profile that names no Setup code offers no
      file and this does nothing at all;
    * **only what the contract already calls optional.** Anything outside
      ``OPTIONAL_INSTALL_INPUTS`` is refused here. That set is not a door this
      opens — it is an existing boundary that had no verb behind it;
    * **absence is the only thing it acts on.** A file that exists is not read,
      not compared, not chmod'ed. So this cannot rotate a secret, and it cannot
      disagree with ``install`` about one either: install's byte-for-byte gate
      still owns every file that is already there.
    """

    offered = data.get("declared_files") or []
    if not isinstance(offered, list) or any(not isinstance(name, str) for name in offered):
        raise TargetError("declared file inputs are not a list of names")

    added: list[str] = []
    missing: list[str] = []
    for name in sorted(set(offered)):
        if name not in contract.OPTIONAL_INSTALL_INPUTS:
            raise TargetError(f"{name} is not an optional install input")
        destination_value, user, group, mode = contract.INSTALL_INPUTS[name]
        destination = primitives.host_path(root, destination_value)
        if destination.is_symlink() or (
            destination.exists() and not destination.is_file()
        ):
            raise TargetError(f"input path is unsafe: {destination_value}")
        if destination.exists():
            # Held, not inspected. Whether this Host's copy matches the
            # workstation's is `install`'s question and it answers it byte for
            # byte; asking it here would be a second opinion that can only
            # disagree.
            continue
        missing.append(name)
        if not apply:
            continue
        if stage is None:
            stage = _staging_directory(data, root)
        staged = stage / name
        if not staged.is_file():
            raise TargetError(f"staged input is missing: {name}")
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.tmp")
        try:
            shutil.copyfile(staged, temporary)
            os.chmod(temporary, mode)
            _chown(temporary, user, group, root)
            os.replace(temporary, destination)
        finally:
            temporary.unlink(missing_ok=True)
        added.append(name)
    return added, missing, stage


def converge(data: dict, root: Path) -> dict:
    """Add every declared key this Host is missing, from the staged files.

    ``apply`` false reports and writes nothing, because whoever runs this is
    holding a Host that currently works and is entitled to see the diff first.

    Two granularities, one rule. Keys inside an environment file, and whole
    files the contract already calls optional — see
    :func:`_converge_opaque_files`. Both add what is absent and refuse to touch
    what is present.
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

    added_files, missing_files, stage = _converge_opaque_files(
        data, root, apply=apply, stage=stage
    )

    return {
        "status": (
            "converged"
            if added or added_files
            else ("planned" if missing or missing_files else "already_current")
        ),
        # Names only. A report carrying the values would put every new secret in
        # a terminal's scrollback and in whatever captured it.
        "added": {name: keys for name, keys in sorted(added.items())},
        "missing": {name: keys for name, keys in sorted(missing.items())},
        # Kept in their own fields rather than folded in beside the key sets.
        # An operator reading this has to be able to tell "this Host gained a
        # key inside a file it already had" from "this Host gained a file",
        # because only the second one can make a declaration start being true.
        "added_files": added_files,
        "missing_files": missing_files,
        # Reported rather than raised: a profile may legitimately not install
        # every input, and the caller can tell which case it is looking at.
        "absent": sorted(absent),
        # Likewise reported, and for a sharper reason: nothing here can repair a
        # pair that disagrees, so raising would stop a convergence that is
        # otherwise correct and leave the Host worse off than before.
        "relationships": relationships,
        "applied": bool(added or added_files),
        "redaction": "credential values are never returned",
    }


def repair(data: dict, root: Path) -> dict:
    """Make every copy of one shared credential on this Host one value again.

    The third verb in this family, and the only one that changes a value a Host
    already holds. ``install`` compares each staged input byte for byte and
    refuses on a difference; ``converge`` adds a key a Host lacks and refuses to
    replace one. Neither can end the state ``verify_relationships`` finds, which
    is two files on this Host holding different values for one credential — and
    until this existed nothing could: the remedy was a reinstall, which rotates
    every secret on the Host to fix one of them.

    **What decides that a repair is warranted is disagreement on this Host**,
    not disagreement with the workstation. That distinction is the whole safety
    argument, so it is worth following through:

    - an operator who deliberately changed a credential changed every copy of
      it, because the product does not work otherwise. Those copies agree, this
      finds no disagreement, and **nothing is touched** — even though all of
      them differ from the workstation's value. Reverting that is the one thing
      this must never do, and it is prevented by construction rather than by a
      flag;
    - a Host whose copies disagree is already broken. Every path that could
      produce that wrote one copy and not the others. There is no state in which
      this fires and the Host was working.

    The value it aligns to is the staged one — this machine's input set, whose
    own copies are proven equal before anything is staged. That is the value
    ``install`` wrote in the first place, so the result is the Host the operator
    installed, with nothing else rotated.

    By the class and never by the pair. The companion authority token lives in
    five files joined by four pairs: correcting one of those pairs would move
    ``data.env`` and leave the three files that agreed with it behind.

    Dry unless asked. A dry run stages nothing and so cannot name the value it
    would write; it reports which slots hold the same value as which, which is
    what tells an operator that ``channel.env`` is the odd one out. Applying
    names what it wrote.
    """

    classes = contract.declared_credential_classes(data)
    if not classes:
        raise TargetError("repair requires the credential classes the product declares")
    apply = bool(data.get("apply"))
    stage: Path | None = None
    files: dict[str, dict[str, str] | None] = {}

    def _read(name: str) -> dict[str, str] | None:
        if name not in files:
            destination = primitives.host_path(root, contract.INSTALL_INPUTS[name][0])
            files[name] = (
                _parse_env(destination.read_text(encoding="utf-8"))
                if destination.is_file() and not destination.is_symlink()
                else None
            )
        return files[name]

    divided: list[dict[str, object]] = []
    repaired: list[dict[str, object]] = []
    unchecked: list[dict[str, str]] = []
    pending: dict[str, dict[str, str]] = {}

    for slots in classes:
        names = [f"{file}:{key}" for file, key in slots]
        held: dict[str, str] = {}
        absent: list[str] = []
        for (file, key), name in zip(slots, names, strict=True):
            values = _read(file)
            if values is None or key not in values:
                absent.append(name)
            else:
                held[name] = values[key]
        if absent:
            # Neither case is a value to correct: a file that is not there is a
            # Host that was never installed, and a key that is not there is what
            # `converge` adds.
            unchecked.append(
                {"slots": ", ".join(names), "reason": f"absent: {', '.join(sorted(absent))}"}
            )
            continue
        if len(set(held.values())) == 1:
            continue

        # Which slots hold the same value as which, named without naming a
        # value. This is the finding; everything below acts on it.
        groups = sorted(
            sorted(name for name, value in held.items() if value == distinct)
            for distinct in set(held.values())
        )
        finding: dict[str, object] = {"slots": names, "groups": groups}
        divided.append(finding)
        if not apply:
            continue

        if stage is None:
            stage = _staging_directory(data, root)
        staged: dict[str, str] = {}
        for (file, key), name in zip(slots, names, strict=True):
            path = stage / file
            if not path.is_file():
                raise TargetError(f"staged input is missing: {file}")
            offered = _parse_env(path.read_text(encoding="utf-8"))
            if key not in offered:
                raise TargetError(f"staged {file} does not carry {key}")
            staged[name] = offered[key]
        if len(set(staged.values())) != 1:
            # The workstation proves its own copies equal before staging them,
            # so this is that machine's problem and not this Host's. Refused
            # rather than resolved: a repair that guessed which copy was right
            # would write the wrong credential everywhere.
            raise TargetError(
                "staged inputs disagree about " + ", ".join(names) + "; "
                "repair the workstation's input set before delivering it"
            )
        target = next(iter(staged.values()))
        written = sorted(name for name, value in held.items() if value != target)
        for name in written:
            file, key = name.split(":", 1)
            pending.setdefault(file, dict(files[file] or {}))[key] = target
        repaired.append({**finding, "written": written})

    if apply and pending:
        for file, values in sorted(pending.items()):
            destination_value, user, group, mode = contract.INSTALL_INPUTS[file]
            destination = primitives.host_path(root, destination_value)
            temporary = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.tmp")
            try:
                temporary.write_text(
                    "".join(f"{key}={values[key]}\n" for key in sorted(values)),
                    encoding="utf-8",
                )
                os.chmod(temporary, mode)
                _chown(temporary, user, group, root)
                os.replace(temporary, destination)
            finally:
                temporary.unlink(missing_ok=True)

    return {
        "status": "repaired" if repaired else "divided" if divided else "consistent",
        "classes": len(classes),
        # The finding, dry or applied: which slots hold the same value as which.
        "divided": divided,
        "repaired": repaired,
        "unchecked": unchecked,
        # An env file is read at start, so a service still holds the credential
        # it was started with. Named rather than performed: when a Host restarts
        # is the operator's call, as it is for `converge`.
        "restart_required": sorted(pending),
        "applied": bool(apply and pending),
        "redaction": "credential values are compared and written here, never returned",
    }


def repair_secret_relationships(payload: dict) -> dict:
    """The action entry point: this Host, at its real root."""

    return repair(dict(payload), Path("/"))


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
