"""A credential the product grew must be able to reach a Host that is running.

The failure, end to end: a component added a credential, the generator learned
to mint it, the contract check learned to require it — and a Host installed
before that day had no way to receive it. ``install`` refuses when an input on
the Host differs from the staged one, which is right for resuming an interrupted
install and makes "the same file plus one new key" indistinguishable from
tampering. ``refresh`` carries only the two Host-bound files. So the credential
stayed on the workstation, the Host answered ``503 credential is not
configured`` for every memory and conversation read, and release after release
shipped green on top of it.

These tests pin the primitive that closes it and the two properties that make it
safe to run on a Host somebody depends on: it only ever *adds*, and it refuses
rather than rotating.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from eidolon_ops.hostagent import contract, lifecycle, secret_inputs
from eidolon_ops.hostagent.primitives import TargetError
from eidolon_ops.install_inputs import (
    DECLARED_ENV_KEYS,
    SHARED_CREDENTIALS,
    declared_credential_classes,
    declared_credential_relationships,
    declared_secret_env_keys,
)


def _host(root: Path, name: str, body: str) -> Path:
    """One credential file where the agent will look for it."""

    path = root / "etc/eidolon" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    path.chmod(0o600)
    return path


def _stage(root: Path, name: str, body: str) -> Path:
    stage = root / "var/tmp/eidolon-secrets-credential-convergence"
    stage.mkdir(parents=True, exist_ok=True)
    path = stage / name
    path.write_text(body, encoding="utf-8")
    return path


def _payload(declared: dict[str, list[str]], *, apply: bool) -> dict:
    payload: dict = {"declared": declared, "apply": apply}
    if apply:
        payload["release_id"] = "credential-convergence"
    return payload


def test_the_declaration_is_derived_rather_than_restated() -> None:
    """What the product requires has one author.

    The agent is told; it does not know. An agent carrying its own copy of the
    requirement would be a second opinion that drifts, which is the same shape
    as the defect this whole change is about.
    """

    declared = declared_secret_env_keys()
    for name, keys in declared.items():
        assert set(keys) == DECLARED_ENV_KEYS[name].required
    assert "bootstrap.env" not in declared, "nothing required is nothing to converge to"
    assert "admin.env" in declared

    # channel.env is in this accounting now. It used to be the one file outside
    # it, on the stated grounds that its key set was "deliberately open" so
    # "missing" could not be decided there. Neither half held: the required set
    # was written out by hand inside the contract check, 340 lines from the
    # table, and only the optional tail was ever open.
    channel = DECLARED_ENV_KEYS["channel.env"]
    assert set(declared["channel.env"]) == channel.required
    # And only `required` travels. An optional credential is legitimately
    # absent, so asking a Host for one could only ever report a Host that is
    # fine; a rendered field is not in the input set this converges from, so
    # there would be nothing to deliver.
    assert channel.optional and not set(declared["channel.env"]) & channel.optional
    assert channel.rendered and not set(declared["channel.env"]) & channel.rendered


def test_the_missing_credential_is_reported_before_it_is_written(tmp_path) -> None:
    """Dry by default: whoever runs this holds a Host that currently works."""

    _host(tmp_path, "admin.env", "EIDOLON_ADMIN_DATA_AUTHORITY_TOKEN=kept\n")
    declared = {
        "admin.env": [
            "EIDOLON_ADMIN_DATA_AUTHORITY_TOKEN",
            "EIDOLON_ADMIN_MEMORY_API_SERVICE_TOKEN",
        ]
    }

    report = secret_inputs.converge(_payload(declared, apply=False), tmp_path)

    assert report["status"] == "planned"
    assert report["missing"] == {
        "admin.env": ["EIDOLON_ADMIN_MEMORY_API_SERVICE_TOKEN"]
    }
    assert report["added"] == {}
    assert (
        _host(tmp_path, "admin.env", "EIDOLON_ADMIN_DATA_AUTHORITY_TOKEN=kept\n")
        .read_text(encoding="utf-8")
        == "EIDOLON_ADMIN_DATA_AUTHORITY_TOKEN=kept\n"
    )


def test_a_missing_credential_arrives_without_disturbing_the_rest(tmp_path) -> None:
    """The operation that did not exist, doing the only thing it needs to do."""

    host = _host(
        tmp_path,
        "admin.env",
        "EIDOLON_ADMIN_DATA_AUTHORITY_TOKEN=kept\n"
        "EIDOLON_ADMIN_LOCAL_API_SERVICE_TOKEN=also-kept\n",
    )
    _stage(
        tmp_path,
        "admin.env",
        "EIDOLON_ADMIN_DATA_AUTHORITY_TOKEN=kept\n"
        "EIDOLON_ADMIN_LOCAL_API_SERVICE_TOKEN=also-kept\n"
        "EIDOLON_ADMIN_MEMORY_API_SERVICE_TOKEN=newly-minted\n",
    )
    declared = {
        "admin.env": [
            "EIDOLON_ADMIN_DATA_AUTHORITY_TOKEN",
            "EIDOLON_ADMIN_LOCAL_API_SERVICE_TOKEN",
            "EIDOLON_ADMIN_MEMORY_API_SERVICE_TOKEN",
        ]
    }

    report = secret_inputs.converge(_payload(declared, apply=True), tmp_path)

    assert report["status"] == "converged"
    assert report["added"] == {"admin.env": ["EIDOLON_ADMIN_MEMORY_API_SERVICE_TOKEN"]}
    body = host.read_text(encoding="utf-8")
    assert "EIDOLON_ADMIN_MEMORY_API_SERVICE_TOKEN=newly-minted\n" in body
    assert "EIDOLON_ADMIN_DATA_AUTHORITY_TOKEN=kept\n" in body
    assert "EIDOLON_ADMIN_LOCAL_API_SERVICE_TOKEN=also-kept\n" in body


def test_a_value_the_host_already_holds_is_never_replaced(tmp_path) -> None:
    """Rotation is a different operation, and this one refuses to be it.

    Refusing rather than silently keeping the Host's value: an operator who
    staged a changed secret and read "converged" would believe it had been
    delivered. Refusing rather than rotating: a convergence that could rotate
    would not be safe to run on a working Host, and a working Host is the only
    kind anybody runs it on.
    """

    host = _host(tmp_path, "admin.env", "EIDOLON_ADMIN_DATA_AUTHORITY_TOKEN=live\n")
    _stage(
        tmp_path,
        "admin.env",
        "EIDOLON_ADMIN_DATA_AUTHORITY_TOKEN=different\n"
        "EIDOLON_ADMIN_MEMORY_API_SERVICE_TOKEN=new\n",
    )
    declared = {
        "admin.env": [
            "EIDOLON_ADMIN_DATA_AUTHORITY_TOKEN",
            "EIDOLON_ADMIN_MEMORY_API_SERVICE_TOKEN",
        ]
    }

    with pytest.raises(TargetError, match="would change"):
        secret_inputs.converge(_payload(declared, apply=True), tmp_path)

    assert host.read_text(encoding="utf-8") == "EIDOLON_ADMIN_DATA_AUTHORITY_TOKEN=live\n"


def test_a_host_that_already_holds_everything_is_left_alone(tmp_path) -> None:
    _host(tmp_path, "admin.env", "EIDOLON_ADMIN_DATA_AUTHORITY_TOKEN=kept\n")

    report = secret_inputs.converge(
        _payload({"admin.env": ["EIDOLON_ADMIN_DATA_AUTHORITY_TOKEN"]}, apply=True),
        tmp_path,
    )

    assert report["status"] == "already_current"
    assert report["applied"] is False


def test_a_credential_file_that_does_not_exist_is_not_invented(tmp_path) -> None:
    """A missing file means this Host was never installed. Say so; do not fix it."""

    report = secret_inputs.converge(
        _payload({"admin.env": ["EIDOLON_ADMIN_DATA_AUTHORITY_TOKEN"]}, apply=False),
        tmp_path,
    )

    assert report["absent"] == ["admin.env"]
    assert report["missing"] == {}
    assert not (tmp_path / "etc/eidolon/admin.env").exists()


def test_a_staged_file_that_cannot_supply_the_key_is_a_refusal(tmp_path) -> None:
    _host(tmp_path, "admin.env", "EIDOLON_ADMIN_DATA_AUTHORITY_TOKEN=kept\n")
    _stage(tmp_path, "admin.env", "EIDOLON_ADMIN_DATA_AUTHORITY_TOKEN=kept\n")

    with pytest.raises(TargetError, match="does not carry"):
        secret_inputs.converge(
            _payload(
                {
                    "admin.env": [
                        "EIDOLON_ADMIN_DATA_AUTHORITY_TOKEN",
                        "EIDOLON_ADMIN_MEMORY_API_SERVICE_TOKEN",
                    ]
                },
                apply=True,
            ),
            tmp_path,
        )


def test_a_file_outside_the_install_inputs_is_refused(tmp_path) -> None:
    """The declaration names install inputs, not paths.

    A payload naming an arbitrary file would make this action a way to append
    lines anywhere on the Host.
    """

    with pytest.raises(TargetError, match="not an install input"):
        secret_inputs.converge(
            _payload({"../../etc/passwd": ["ROOT"]}, apply=False), tmp_path
        )


def test_the_report_carries_names_and_never_values(tmp_path) -> None:
    """A report with secrets in it ends up in a terminal's scrollback."""

    _host(tmp_path, "admin.env", "")
    _stage(tmp_path, "admin.env", "EIDOLON_ADMIN_MEMORY_API_SERVICE_TOKEN=s3cret\n")

    report = secret_inputs.converge(
        _payload({"admin.env": ["EIDOLON_ADMIN_MEMORY_API_SERVICE_TOKEN"]}, apply=True),
        tmp_path,
    )

    assert "s3cret" not in repr(report)
    assert report["redaction"]


def test_both_sides_of_a_shared_credential_are_declared(tmp_path) -> None:
    """The pair that broke, asserted as a pair.

    ``EIDOLON_ADMIN_MEMORY_API_SERVICE_TOKEN`` and ``EIDOLON_MEMORY_API_TOKEN``
    are one secret with two names, and a convergence that delivered one side
    would leave the Host refusing exactly as before — with the fault now on the
    other side of the loopback.
    """

    declared = declared_secret_env_keys()
    pairs = {
        (left_file, left_key, right_file, right_key)
        for left_file, left_key, right_file, right_key, _ in SHARED_CREDENTIALS
    }
    assert (
        "admin.env",
        "EIDOLON_ADMIN_MEMORY_API_SERVICE_TOKEN",
        "memory.env",
        "EIDOLON_MEMORY_API_TOKEN",
    ) in pairs
    for left_file, left_key, right_file, right_key in pairs:
        if left_file in declared:
            assert left_key in declared[left_file], f"{left_file}:{left_key}"
        if right_file in declared:
            assert right_key in declared[right_file], f"{right_file}:{right_key}"


def test_convergence_reads_rotation_off_the_declared_keys_only(tmp_path) -> None:
    """A Host-bound field that differs is not a rotation.

    A staged Host-bound file carries fields the controller renders for this Host
    — where devices reach LiveKit, whether plain ``ws://`` is allowed — and those
    change whenever the Host binding changes. Comparing them here would refuse a
    convergence with "rotate through a reinstall", which is both wrong and
    unactionable: those fields have their own delivery path and were never
    rotated. The credential beside them is still compared, because that refusal
    is the one an operator needs.
    """

    declared = {"channel.env": ["EIDOLON_CHANNEL_PROVIDER_TOKEN", "PAIRING_JWT_SECRET"]}
    _host(
        tmp_path,
        "channel.env",
        "EIDOLON_CHANNEL_PROVIDER_TOKEN=kept\n"
        "EIDOLON_LIVEKIT_CLIENT_URL=ws://192.168.1.9:7880\n",
    )
    _stage(
        tmp_path,
        "channel.env",
        "EIDOLON_CHANNEL_PROVIDER_TOKEN=kept\n"
        "PAIRING_JWT_SECRET=arrives\n"
        # The Host moved networks since it was installed. Not a rotation.
        "EIDOLON_LIVEKIT_CLIENT_URL=ws://10.0.0.4:7880\n",
    )

    report = secret_inputs.converge(_payload(declared, apply=True), tmp_path)

    assert report["added"] == {"channel.env": ["PAIRING_JWT_SECRET"]}
    body = (tmp_path / "etc/eidolon/channel.env").read_text(encoding="utf-8")
    assert "PAIRING_JWT_SECRET=arrives" in body
    # Untouched: convergence adds keys, and this one is the Host's.
    assert "EIDOLON_LIVEKIT_CLIENT_URL=ws://192.168.1.9:7880" in body


def test_a_rotated_declared_credential_is_still_refused(tmp_path) -> None:
    """Narrowing the comparison must not lose the refusal it exists for."""

    declared = {"channel.env": ["EIDOLON_CHANNEL_PROVIDER_TOKEN", "PAIRING_JWT_SECRET"]}
    _host(tmp_path, "channel.env", "EIDOLON_CHANNEL_PROVIDER_TOKEN=installed\n")
    _stage(
        tmp_path,
        "channel.env",
        "EIDOLON_CHANNEL_PROVIDER_TOKEN=rotated\nPAIRING_JWT_SECRET=arrives\n",
    )

    with pytest.raises(TargetError, match="EIDOLON_CHANNEL_PROVIDER_TOKEN"):
        secret_inputs.converge(_payload(declared, apply=True), tmp_path)


def _relationship(left_file, left_key, right_file, right_key, label="Test pair"):
    return {
        "left_file": left_file,
        "left_key": left_key,
        "right_file": right_file,
        "right_key": right_key,
        "label": label,
    }


def test_the_relationships_travel_rather_than_being_known_on_the_host() -> None:
    """One author, same as the key declaration beside it.

    An agent holding its own copy of which credentials must match would be a
    second opinion that drifts — which is exactly the defect this check looks
    for, so building it out of a second copy would be absurd.
    """

    wire = declared_credential_relationships()
    assert len(wire) == len(SHARED_CREDENTIALS)
    assert [
        (e["left_file"], e["left_key"], e["right_file"], e["right_key"], e["label"])
        for e in wire
    ] == [tuple(pair) for pair in SHARED_CREDENTIALS]
    # And it survives the wire: the agent refuses a shape it did not expect.
    assert contract.declared_credential_relationships({"credential_relationships": wire})
    # Every relationship the product declares, not only the ones that prompted
    # this: six of them involve channel.env and four of those fail silently,
    # but a Host can hold any of the seventeen wrongly and none was checked.
    channel = [e for e in wire if "channel.env" in {e["left_file"], e["right_file"]}]
    assert len(channel) == 6 and len(wire) == 17


def test_a_host_holding_two_different_values_is_named(tmp_path) -> None:
    """The failure that had nowhere to be seen.

    Hub presents this token to the Channel provider. When the two stopped
    matching, the provider answered 401 to a reconciliation nobody watches, and
    every gate on this Host stayed green.
    """

    _host(tmp_path, "hub.env", "EIDOLON_HUB_CHANNEL_PROVIDER_TOKEN=installed\n")
    _host(tmp_path, "channel.env", "EIDOLON_CHANNEL_PROVIDER_TOKEN=drifted\n")

    report = secret_inputs.verify_relationships(
        (
            (
                "hub.env",
                "EIDOLON_HUB_CHANNEL_PROVIDER_TOKEN",
                "channel.env",
                "EIDOLON_CHANNEL_PROVIDER_TOKEN",
                "Hub/Channel Provider token",
            ),
        ),
        tmp_path,
    )

    assert report["status"] == "mismatched"
    assert report["mismatched"] == [
        {
            "label": "Hub/Channel Provider token",
            "left": "hub.env:EIDOLON_HUB_CHANNEL_PROVIDER_TOKEN",
            "right": "channel.env:EIDOLON_CHANNEL_PROVIDER_TOKEN",
        }
    ]
    # Names and places only. Neither value appears anywhere in the answer.
    assert "installed" not in repr(report) and "drifted" not in repr(report)


def test_agreement_is_reported_as_agreement(tmp_path) -> None:
    _host(tmp_path, "agent.env", "PAIRING_JWT_SECRET=one-value\n")
    _host(tmp_path, "channel.env", "PAIRING_JWT_SECRET=one-value\n")

    report = secret_inputs.verify_relationships(
        (("agent.env", "PAIRING_JWT_SECRET", "channel.env", "PAIRING_JWT_SECRET", "Agent/Channel JWT"),),
        tmp_path,
    )

    assert report["status"] == "agreed"
    assert report["compared"] == 1
    assert report["mismatched"] == [] and report["unchecked"] == []


def test_a_pair_that_could_not_be_compared_is_not_agreement(tmp_path) -> None:
    """Fail closed, and say which half was missing.

    Every one of these files belongs to a full install, so a missing one is not
    a profile that declined it. Reporting "agreed" because there was nothing to
    compare would be the same kind of lie this check exists to stop telling.
    """

    _host(tmp_path, "hub.env", "EIDOLON_HUB_CHANNEL_PROVIDER_TOKEN=installed\n")
    pair = (
        "hub.env",
        "EIDOLON_HUB_CHANNEL_PROVIDER_TOKEN",
        "channel.env",
        "EIDOLON_CHANNEL_PROVIDER_TOKEN",
        "Hub/Channel Provider token",
    )

    absent_file = secret_inputs.verify_relationships((pair,), tmp_path)
    assert absent_file["status"] == "unverified"
    assert absent_file["compared"] == 0
    assert "not installed: channel.env" in absent_file["unchecked"][0]["reason"]

    # A file that exists without the key is a different report, because it is a
    # different fix: that one convergence repairs.
    _host(tmp_path, "channel.env", "LIVEKIT_API_KEY=unrelated\n")
    absent_key = secret_inputs.verify_relationships((pair,), tmp_path)
    assert absent_key["status"] == "unverified"
    assert "absent: channel.env:EIDOLON_CHANNEL_PROVIDER_TOKEN" in (
        absent_key["unchecked"][0]["reason"]
    )


def test_an_older_workstation_declares_nothing_and_that_is_not_a_failure(tmp_path) -> None:
    """Nobody asked, which is not the same as this Host answering badly."""

    report = secret_inputs.verify_relationships((), tmp_path)
    assert report["status"] == "not_declared"
    assert contract.declared_credential_relationships({}) == ()


def test_the_agent_refuses_a_relationship_it_cannot_place(tmp_path) -> None:
    """Checked rather than believed, like every other field in this payload."""

    with pytest.raises(TargetError, match="names no install input"):
        contract.declared_credential_relationships(
            {"credential_relationships": [_relationship("nope.env", "A", "hub.env", "B")]}
        )
    with pytest.raises(TargetError, match="must name exactly"):
        contract.declared_credential_relationships(
            {"credential_relationships": [{"left_file": "hub.env"}]}
        )
    with pytest.raises(TargetError, match="must be an array"):
        contract.declared_credential_relationships({"credential_relationships": "hub.env"})


def test_convergence_reports_a_pair_it_cannot_repair(tmp_path) -> None:
    """Adding a key is not the fix for two files holding different values.

    So it is reported beside the repair rather than folded into it: a report
    that said `converged` while Hub still could not authenticate to the Channel
    provider is the green-over-broken shape this whole area keeps producing.
    """

    _host(tmp_path, "hub.env", "EIDOLON_HUB_CHANNEL_PROVIDER_TOKEN=installed\n")
    _host(
        tmp_path,
        "channel.env",
        "EIDOLON_CHANNEL_PROVIDER_TOKEN=drifted\nLIVEKIT_API_KEY=kept\n",
    )
    payload = {
        **_payload({"channel.env": ["LIVEKIT_API_KEY", "LIVEKIT_API_SECRET"]}, apply=False),
        "credential_relationships": [
            _relationship(
                "hub.env",
                "EIDOLON_HUB_CHANNEL_PROVIDER_TOKEN",
                "channel.env",
                "EIDOLON_CHANNEL_PROVIDER_TOKEN",
                "Hub/Channel Provider token",
            )
        ],
    }

    report = secret_inputs.converge(payload, tmp_path)

    # The repair it can do is unaffected...
    assert report["missing"] == {"channel.env": ["LIVEKIT_API_SECRET"]}
    # ...and the one it cannot is named rather than swallowed.
    assert report["relationships"]["status"] == "mismatched"
    assert report["relationships"]["mismatched"][0]["label"] == "Hub/Channel Provider token"


def test_doctor_is_where_a_drifted_pair_turns_red(tmp_path) -> None:
    """The point of the whole check: somewhere an operator actually reads.

    Not a readiness fact — that gates releases and would roll one back over
    something a release cannot cause or fix. Not a deploy refusal either: a pair
    that disagrees is real, but no verb repairs it today, and a gate that
    refuses without naming what to run is one people learn to work around.
    `doctor` is the verb for "what is wrong with this Host", and it blocks
    nothing.
    """

    payload = {
        "units": list(contract.PRODUCT_UNITS),
        "data": {name: str(path) for name, path in contract.FIXED_DATA.items()},
        "remote_uv": "/definitely/missing/uv",
        "credential_relationships": [
            _relationship(
                "agent.env",
                "PAIRING_JWT_SECRET",
                "channel.env",
                "PAIRING_JWT_SECRET",
                "Agent/Channel JWT",
            )
        ],
    }

    _host(tmp_path, "agent.env", "PAIRING_JWT_SECRET=one-value\n")
    _host(tmp_path, "channel.env", "PAIRING_JWT_SECRET=one-value\n")
    agreed = lifecycle.doctor_host(payload, root=tmp_path)
    assert agreed["checks"]["credential_relationships"] is True
    assert agreed["credential_relationships"]["status"] == "agreed"

    # Now the two stop agreeing. Every other surface on this Host is unchanged:
    # both services start, both answer their health checks, and the failure only
    # appears when somebody speaks to a device.
    _host(tmp_path, "channel.env", "PAIRING_JWT_SECRET=drifted\n")
    drifted = lifecycle.doctor_host(payload, root=tmp_path)
    assert drifted["checks"]["credential_relationships"] is False
    assert drifted["status"] == "degraded"
    # And it says which pair, because a bare false sends an operator reading ten
    # mode-0600 files with `sudo cat`.
    assert drifted["credential_relationships"]["mismatched"][0]["label"] == "Agent/Channel JWT"


def test_doctor_does_not_fail_a_host_nobody_asked_about(tmp_path) -> None:
    """An older workstation sends no relationships; that is not a finding."""

    payload = {
        "units": list(contract.PRODUCT_UNITS),
        "data": {name: str(path) for name, path in contract.FIXED_DATA.items()},
        "remote_uv": "/definitely/missing/uv",
    }

    result = lifecycle.doctor_host(payload, root=tmp_path)

    assert result["checks"]["credential_relationships"] is True
    assert result["credential_relationships"]["status"] == "not_declared"


# -- repair ---------------------------------------------------------------
#
# The third verb, and the only one that replaces a value a Host already holds.
# Every safety rule it relies on is pinned below, because the rules are the
# reason it is allowed to exist.


def _classes(*groups):
    return [{"slots": [{"file": f, "key": k} for f, k in group]} for group in groups]


def _repair_payload(classes, *, apply: bool) -> dict:
    payload: dict = {"credential_classes": classes, "apply": apply}
    if apply:
        payload["release_id"] = "credential-convergence"
    return payload


_COMPANION = (
    ("data.env", "EIDOLON_DATA_COMPANION_AUTHORITY_TOKEN"),
    ("kernel.env", "EIDOLON_KERNEL_COMPANION_AUTHORITY_TOKEN"),
    ("channel.env", "EIDOLON_DATA_COMPANION_AUTHORITY_TOKEN"),
)


def _companion_host(root, data, kernel, channel) -> None:
    _host(root, "data.env", f"EIDOLON_DATA_COMPANION_AUTHORITY_TOKEN={data}\n")
    _host(root, "kernel.env", f"EIDOLON_KERNEL_COMPANION_AUTHORITY_TOKEN={kernel}\n")
    _host(root, "channel.env", f"EIDOLON_DATA_COMPANION_AUTHORITY_TOKEN={channel}\n")


def _companion_stage(root, value) -> None:
    _stage(root, "data.env", f"EIDOLON_DATA_COMPANION_AUTHORITY_TOKEN={value}\n")
    _stage(root, "kernel.env", f"EIDOLON_KERNEL_COMPANION_AUTHORITY_TOKEN={value}\n")
    _stage(root, "channel.env", f"EIDOLON_DATA_COMPANION_AUTHORITY_TOKEN={value}\n")


def test_a_consistently_changed_host_is_never_touched(tmp_path) -> None:
    """The one thing this verb must never do, prevented by construction.

    An operator who deliberately changed a credential changed every copy of it,
    because the product does not work otherwise. Those copies agree, so there is
    no division to find, and the staged value is never even read — let alone
    written over theirs. Note that every slot here differs from what is staged.
    """

    _companion_host(tmp_path, "operators-own", "operators-own", "operators-own")
    _companion_stage(tmp_path, "this-machines-value")

    report = secret_inputs.repair(_repair_payload(_classes(_COMPANION), apply=True), tmp_path)

    assert report["status"] == "consistent"
    assert report["applied"] is False and report["divided"] == []
    for name, key in _COMPANION:
        body = (tmp_path / "etc/eidolon" / name).read_text(encoding="utf-8")
        assert f"{key}=operators-own" in body


def test_a_divided_credential_is_aligned_by_the_class_not_the_pair(tmp_path) -> None:
    """Why the unit is the credential and never the relationship.

    Three of these slots agree with each other and disagree with the fourth. Of
    the pairs that join them, only the ones crossing that line are mismatched —
    repairing those alone would move `data.env` to the staged value and leave
    `kernel.env`, which agreed with it, behind. The same Host, broken a
    different way.
    """

    _companion_host(tmp_path, "stale", "stale", "this-machines-value")
    _companion_stage(tmp_path, "this-machines-value")

    report = secret_inputs.repair(_repair_payload(_classes(_COMPANION), apply=True), tmp_path)

    assert report["status"] == "repaired"
    assert report["repaired"][0]["written"] == [
        "data.env:EIDOLON_DATA_COMPANION_AUTHORITY_TOKEN",
        "kernel.env:EIDOLON_KERNEL_COMPANION_AUTHORITY_TOKEN",
    ]
    for name, key in _COMPANION:
        body = (tmp_path / "etc/eidolon" / name).read_text(encoding="utf-8")
        assert f"{key}=this-machines-value" in body
    # Named, not performed: each service still holds what it started with.
    assert report["restart_required"] == ["data.env", "kernel.env"]


def test_a_dry_run_names_who_disagrees_with_whom_and_writes_nothing(tmp_path) -> None:
    """It stages nothing, so it cannot name a value — and does not need to.

    What an operator needs is which file is the odd one out, and that is
    knowable from the Host alone.
    """

    _companion_host(tmp_path, "stale", "stale", "this-machines-value")

    report = secret_inputs.repair(_repair_payload(_classes(_COMPANION), apply=False), tmp_path)

    assert report["status"] == "divided" and report["applied"] is False
    assert report["divided"][0]["groups"] == [
        ["channel.env:EIDOLON_DATA_COMPANION_AUTHORITY_TOKEN"],
        [
            "data.env:EIDOLON_DATA_COMPANION_AUTHORITY_TOKEN",
            "kernel.env:EIDOLON_KERNEL_COMPANION_AUTHORITY_TOKEN",
        ],
    ]
    assert "stale" not in repr(report) and "this-machines-value" not in repr(report)
    assert (tmp_path / "etc/eidolon/data.env").read_text(encoding="utf-8") == (
        "EIDOLON_DATA_COMPANION_AUTHORITY_TOKEN=stale\n"
    )


def test_a_staged_set_that_disagrees_with_itself_is_refused(tmp_path) -> None:
    """The workstation proves its own copies equal before staging them.

    Reaching this means that proof was skipped. Refused rather than resolved:
    picking one of two staged copies would write the wrong credential into every
    file in the class.
    """

    _companion_host(tmp_path, "one", "two", "two")
    _stage(tmp_path, "data.env", "EIDOLON_DATA_COMPANION_AUTHORITY_TOKEN=alpha\n")
    _stage(tmp_path, "kernel.env", "EIDOLON_KERNEL_COMPANION_AUTHORITY_TOKEN=beta\n")
    _stage(tmp_path, "channel.env", "EIDOLON_DATA_COMPANION_AUTHORITY_TOKEN=alpha\n")

    with pytest.raises(TargetError, match="staged inputs disagree"):
        secret_inputs.repair(_repair_payload(_classes(_COMPANION), apply=True), tmp_path)

    # And nothing was written on the way to refusing.
    assert "one" in (tmp_path / "etc/eidolon/data.env").read_text(encoding="utf-8")


def test_repair_leaves_other_keys_in_a_file_alone(tmp_path) -> None:
    """It rewrites one field, not the file it lives in."""

    _host(
        tmp_path,
        "data.env",
        "EIDOLON_DATA_COMPANION_AUTHORITY_TOKEN=stale\n"
        "EIDOLON_DATA_SQLITE_PATH=/var/lib/eidolon/eidolon-system.sqlite3\n",
    )
    _host(tmp_path, "kernel.env", "EIDOLON_KERNEL_COMPANION_AUTHORITY_TOKEN=correct\n")
    _stage(tmp_path, "data.env", "EIDOLON_DATA_COMPANION_AUTHORITY_TOKEN=correct\n")
    _stage(tmp_path, "kernel.env", "EIDOLON_KERNEL_COMPANION_AUTHORITY_TOKEN=correct\n")

    secret_inputs.repair(
        _repair_payload(_classes(_COMPANION[:2]), apply=True), tmp_path
    )

    body = (tmp_path / "etc/eidolon/data.env").read_text(encoding="utf-8")
    assert "EIDOLON_DATA_COMPANION_AUTHORITY_TOKEN=correct" in body
    assert "EIDOLON_DATA_SQLITE_PATH=/var/lib/eidolon/eidolon-system.sqlite3" in body


def test_a_missing_key_is_convergence_s_job_not_this_one(tmp_path) -> None:
    """Crisp contracts: convergence adds a key, repair corrects a value.

    A slot that is not there has no value to correct, and inventing one here
    would make two verbs able to write the same credential by different rules.
    """

    _host(tmp_path, "data.env", "EIDOLON_DATA_COMPANION_AUTHORITY_TOKEN=held\n")
    _host(tmp_path, "kernel.env", "SOMETHING_ELSE=x\n")
    _stage(tmp_path, "data.env", "EIDOLON_DATA_COMPANION_AUTHORITY_TOKEN=held\n")
    _stage(tmp_path, "kernel.env", "EIDOLON_KERNEL_COMPANION_AUTHORITY_TOKEN=held\n")

    report = secret_inputs.repair(
        _repair_payload(_classes(_COMPANION[:2]), apply=True), tmp_path
    )

    assert report["status"] == "consistent"
    assert report["unchecked"][0]["reason"] == (
        "absent: kernel.env:EIDOLON_KERNEL_COMPANION_AUTHORITY_TOKEN"
    )
    assert "EIDOLON_KERNEL_COMPANION_AUTHORITY_TOKEN" not in (
        tmp_path / "etc/eidolon/kernel.env"
    ).read_text(encoding="utf-8")


def test_the_classes_travel_and_the_agent_refuses_a_shape_it_cannot_place() -> None:
    """Seventeen pairs, thirteen credentials, and one slot in exactly one of them."""

    wire = declared_credential_classes()
    parsed = contract.declared_credential_classes({"credential_classes": wire})
    assert len(parsed) == 13
    assert max(len(slots) for slots in parsed) == 5
    assert sum(len(slots) for slots in parsed) == len({s for c in parsed for s in c})

    with pytest.raises(TargetError, match="at least two slots"):
        contract.declared_credential_classes(
            {"credential_classes": _classes([("data.env", "A")])}
        )
    with pytest.raises(TargetError, match="names no install input"):
        contract.declared_credential_classes(
            {"credential_classes": _classes([("nope.env", "A"), ("data.env", "B")])}
        )
    with pytest.raises(TargetError, match="in two classes"):
        contract.declared_credential_classes(
            {
                "credential_classes": _classes(
                    [("data.env", "A"), ("hub.env", "B")],
                    [("data.env", "A"), ("agent.env", "C")],
                )
            }
        )


def test_repair_refuses_to_run_with_nothing_declared(tmp_path) -> None:
    """Unlike the read-only check, which treats silence as nobody asking.

    This one writes. A payload that declares nothing would be a repair with no
    definition of what it is repairing toward.
    """

    with pytest.raises(TargetError, match="requires the credential classes"):
        secret_inputs.repair({"apply": False}, tmp_path)
