"""Delivering the Setup code a device was manufactured with (ADR-0007).

An unclaimed Host stands up the claim window the code on its chassis is for
only if it was given that code. Ops renders the file from `app.setup_code`
rather than collecting a second copy of a value the profile already holds —
the value living in two places is the drift this whole change exists to stop.

The file's absence is the switch, so "no code configured, no file staged" is
the property that keeps a development fleet sharing one code safe: an
unexpiring window plus a code everyone knows is an open door.
"""

from __future__ import annotations

import dataclasses

from eidolon_ops.hostagent.contract import OPTIONAL_INSTALL_INPUTS, SECRET_INPUTS
from tests.test_controller import FakeTransport, _app

STAGED_NAME = "factory_setup_code"


def test_the_decision_is_the_profile_and_nothing_else(config) -> None:
    """What gets delivered is `app.setup_code`, read from one place.

    Collecting a second copy in an operator-supplied file is the drift this
    change exists to stop; a Host that was given no code gets no file, which is
    the switch.
    """

    from eidolon_ops.controller import EidolonPiController
    from tests.test_controller import ControllerRunner

    with_code = EidolonPiController(
        config,
        ControllerRunner(config),
        transport=FakeTransport(),
        app=dataclasses.replace(_app(), setup_code="48213097"),
    )
    without_code = EidolonPiController(
        config, ControllerRunner(config), transport=FakeTransport(), app=_app()
    )
    no_app = EidolonPiController(
        config, ControllerRunner(config), transport=FakeTransport()
    )

    assert with_code.host_layer._factory_setup_code() == "48213097"
    assert without_code.host_layer._factory_setup_code() is None
    assert no_app.host_layer._factory_setup_code() is None


def test_the_code_lands_beside_the_identity_it_was_made_with(config) -> None:
    """Same directory, same owner, same 0600, same write-once channel."""

    destination, user, group, mode = SECRET_INPUTS[STAGED_NAME]
    identity_destination, identity_user, identity_group, identity_mode = SECRET_INPUTS[
        "host_identity.ed25519"
    ]

    assert destination.parent == identity_destination.parent
    assert (user, group, mode) == (identity_user, identity_group, identity_mode)
    assert mode == 0o600


def test_the_code_is_optional_and_says_so_in_one_place(config) -> None:
    """Its absence is a supported state, unlike every other secret input.

    Declared once, so the install's set check and the required-secrets drift
    check read the same list instead of each carrying an exception.
    """

    assert STAGED_NAME in OPTIONAL_INSTALL_INPUTS
    assert frozenset(SECRET_INPUTS) >= OPTIONAL_INSTALL_INPUTS
    assert "host_identity.ed25519" not in OPTIONAL_INSTALL_INPUTS


# --- the Host agent must accept a stage that carries it ---------------------


def test_the_agent_accepts_a_stage_carrying_the_optional_code(tmp_path) -> None:
    """The staged set is compared exactly, so an optional name needs excusing.

    Without it the delivery lands and the install refuses the stage as invalid
    — a failure that would appear only on a real Host, at the moment the code
    was finally being delivered somewhere.

    `test_target_agent.py` builds its stage from every name in `SECRET_INPUTS`,
    which now includes this one, so that suite is what catches a regression
    here; this states the property in the place someone reads it.
    """

    from eidolon_ops.hostagent import contract as agent_contract
    from eidolon_ops.hostagent.install import TargetInstaller

    stage = tmp_path / "secret-stage"
    stage.mkdir()
    for name in agent_contract.SECRET_INPUTS:
        (stage / name).write_text(f"private-{name}", encoding="utf-8")

    installer = TargetInstaller.__new__(TargetInstaller)
    installer.secret_stage = stage

    digests = installer._input_digests()

    assert set(digests) == set(agent_contract.SECRET_INPUTS)
    assert STAGED_NAME in digests

    # And an undeclared file is still refused.
    (stage / "stowaway").write_text("x", encoding="utf-8")
    import pytest as _pytest

    from eidolon_ops.hostagent.primitives import TargetError

    with _pytest.raises(TargetError, match="staging file set"):
        installer._input_digests()
