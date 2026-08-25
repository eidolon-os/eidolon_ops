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

from eidolon_ops.hostagent import secret_inputs
from eidolon_ops.hostagent.primitives import TargetError
from eidolon_ops.install_inputs import (
    DECLARED_ENV_KEYS,
    SHARED_CREDENTIALS,
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
        assert set(keys) == DECLARED_ENV_KEYS[name]
    # channel.env is excluded because its key set is deliberately open: optional
    # provider credentials are legitimately absent, so "missing" is not a
    # decidable question there.
    assert "channel.env" not in declared
    assert "bootstrap.env" not in declared, "nothing declared is nothing to converge to"
    assert "admin.env" in declared


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
