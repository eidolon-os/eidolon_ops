"""What a Host can do that another Host cannot, named once.

A release brings the same components to every Host, and not every Host runs
all of them. A board with an NPU speaks and listens with its own models; one
without reaches a provider for the same two things, and should not be carrying
two gigabytes of weights it will never load.

Which of the two a Host is could be written as its platform — "install this
unit on rk3588" — and that would be wrong twice over. It would put a machine's
name inside a component that has no business knowing it, and the fourth board
would mean editing every component again. A component states what it needs
present; a Host states what it has. Neither names the other.

The set is closed and lives here rather than being any string a contract feels
like writing, because the failure of an open set is silent: a capability
misspelt in a component contract is a capability no Host ever provides, so the
unit is simply never installed and nothing says why. Adding a capability is
one line in this file, reviewed the way the foundation profile is reviewed.
"""

from __future__ import annotations

from eidolon_ops.errors import OperationsError

__all__ = ["HOST_CAPABILITIES", "require_known_capability"]

#: Every capability a Host may provide and a component may require.
HOST_CAPABILITIES: frozenset[str] = frozenset(
    {
        #: A Rockchip NPU with the RKNPU2 runtime, which is what makes RKNN and
        #: RKLLM model artifacts loadable rather than dead weight.
        "rknpu2",
        #: Speech recognition runs on this Host instead of a provider.
        "local_asr",
        #: Speech synthesis runs on this Host instead of a provider.
        "local_tts",
        #: The conversational model runs on this Host instead of a provider.
        "local_llm",
    }
)


def require_known_capability(value: str, *, label: str) -> str:
    """Refuse a capability nobody defined, on either side of the match."""

    if value not in HOST_CAPABILITIES:
        known = ", ".join(sorted(HOST_CAPABILITIES))
        raise OperationsError(
            f"{label}: unknown Host capability {value!r}. "
            f"A capability must be one of: {known}. Add it to "
            f"eidolon_ops.capabilities.HOST_CAPABILITIES if it is real — an "
            f"unknown name would otherwise select nothing, silently."
        )
    return value
