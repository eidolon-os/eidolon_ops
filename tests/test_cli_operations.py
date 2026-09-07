"""Every verb this CLI offers must be a verb it can actually perform.

The failure this gate exists for is worth stating exactly, because it looked
like nothing: ``add-input-credentials`` was a real subcommand with real help
text, dispatched to ``controller.add_missing_input_credentials``. That method
lived on the release executor and was never carried over to the adapter-based
facade the CLI builds, so the verb raised ``AttributeError`` on every Host. It
was also the *only* supported way to give a Host a credential the product had
grown after installation — so the one operation that could have fixed a Host
refusing every memory read was itself unreachable, and had been since the
facade landed.

Nothing failed because nothing compared the two lists. There were two: the
argparse subcommands, and whatever the ``elif`` chain happened to call. This file
is the comparison.
"""

from __future__ import annotations

import argparse
import inspect

import pytest

from eidolon_ops.host_cli import OPERATIONS, _parser
from eidolon_ops.host_controller import HostController


def _subcommands() -> set[str]:
    actions = [
        action
        for action in _parser()._actions
        if isinstance(action, argparse._SubParsersAction)
    ]
    assert len(actions) == 1, "this CLI has exactly one subcommand group"
    return set(actions[0].choices)


def test_every_verb_the_parser_accepts_can_be_dispatched() -> None:
    missing = _subcommands() - set(OPERATIONS)
    assert not missing, (
        f"subcommand(s) with no handler: {sorted(missing)}. A verb the parser "
        "accepts and the table does not is a verb that fails at runtime."
    )


def test_the_table_offers_nothing_the_parser_does_not() -> None:
    stale = set(OPERATIONS) - _subcommands()
    assert not stale, f"handlers for verbs nobody can type: {sorted(stale)}"


@pytest.mark.parametrize("operation", sorted(OPERATIONS))
def test_every_handler_calls_something_the_controller_has(operation: str) -> None:
    """Resolved against the class, before any Host is contacted.

    Reading the handler's source rather than calling it: dispatching for real
    would need a Host profile, an SSH transport and a machine at the other end,
    which is why this was never checked. The method name is right there in the
    lambda, and a name that is not an attribute of ``HostController`` is the
    entire defect.
    """

    source = inspect.getsource(OPERATIONS[operation])
    called = {
        name
        for name in _controller_calls(source)
        if not name.startswith("_")
    }
    assert called, f"{operation} calls nothing on the controller"
    for name in sorted(called):
        assert hasattr(HostController, name), (
            f"{operation} dispatches to HostController.{name}, which does not "
            "exist — the exact shape of the add-input-credentials defect"
        )


def _controller_calls(source: str) -> set[str]:
    """Attribute names taken off ``controller`` in a handler's source."""

    found: set[str] = set()
    marker = "controller."
    index = source.find(marker)
    while index != -1:
        rest = source[index + len(marker) :]
        name = ""
        for character in rest:
            if character.isalnum() or character == "_":
                name += character
            else:
                break
        if name:
            found.add(name)
        index = source.find(marker, index + 1)
    return found


def test_the_repair_verb_is_reachable() -> None:
    """Named on its own, because its absence is what this file was written for.

    A generic gate can be satisfied by deleting the verb. This asserts the verb
    exists *and* leads somewhere: an already-installed Host must have a
    supported way to receive a credential the product grew.
    """

    assert "converge-inputs" in _subcommands()
    assert "converge-inputs" in OPERATIONS
    assert hasattr(HostController, "converge_inputs")


def test_the_lifecycle_verbs_share_one_handler_rather_than_three() -> None:
    """start/stop/restart differ only in the word they pass on.

    One handler named three times, not three that happen to agree: three bodies
    would be three places to fix the next flag, and the CLI has already shipped
    that mistake once with ``--force-cleanup``.
    """

    handlers = {OPERATIONS[name] for name in ("start", "stop", "restart")}
    assert len(handlers) == 1
