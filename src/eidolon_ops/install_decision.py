"""Which of the few true situations an install has found, decided once.

An install used to make this decision in two places and on the wrong evidence.
The Owner Authority gate compared a generation the workstation kept against the
one the Host holds — a bare integer against a bare integer, with nothing to say
whether they were even about the same board — and only after that gate had
passed did the hardware delivery binding get a look. So the one fact that could
have answered the question ("is this the board this identity was delivered
to?") was consulted last, and the counter that could not answer it was
consulted first. On 2026-09-10 that ordering let one Owner's material be handed
to a second board without a word, and afterwards refused the first board with a
sentence that named only a backup nobody had and a factory reset.

This module is the replacement: one pure function over what the Host reports
and what this profile records, returning a named situation. Every refusing
situation has exactly one honest sentence, and no situation is reached by a
cache being stale, because there is no cache — the workstation keeps no copy
of what the Host has established.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from urllib.parse import urlparse

from eidolon_ops.hostagent.hardware import HostHardwareError, verify_binding


class InstallDecision(StrEnum):
    """What an install found, and therefore what it may do."""

    #: No binding and no Authority: a board this profile has never delivered
    #: to, holding nothing. Bootstrap a fresh Authority and, once the Host
    #: proves it established it, record the board it went to.
    FIRST_INSTALL = "first_install"
    #: The board this identity was delivered to, holding the Authority it
    #: established. Render from what it holds; nothing new is issued.
    CONTINUE = "continue_established_lineage"
    #: A Host that established this Owner's Authority and holds this profile's
    #: identity, from before delivery bindings were written. The evidence is
    #: recoverable from the Host itself, so the binding is recorded rather than
    #: the Host refused forever.
    ADOPT_DELIVERY = "adopt_delivery_evidence"
    #: The Host's own two copies of its lineage disagree. Something on the Host
    #: was lost or replaced; only its complete backup can say what.
    HOST_INCIDENT = "host_incident"
    #: The Host has established an Authority this Owner root never issued.
    FOREIGN_AUTHORITY = "foreign_authority"
    #: This profile's identity was delivered to a different board.
    WRONG_BOARD = "wrong_board"
    #: The board this identity was delivered to no longer holds an Authority.
    #: It established one once — the binding is only ever written after the
    #: Host proved that — so this really is the recovery case.
    AUTHORITY_LOST = "authority_lost"
    #: A Host that established this Owner's Authority but cannot show it holds
    #: this profile's identity, and nothing records a delivery to it.
    IDENTITY_UNPROVEN = "identity_unproven"

    @property
    def proceeds(self) -> bool:
        return self in {
            InstallDecision.FIRST_INSTALL,
            InstallDecision.CONTINUE,
            InstallDecision.ADOPT_DELIVERY,
        }

    @property
    def records_delivery(self) -> bool:
        """Whether a completed operation writes this profile's delivery binding."""

        return self in {InstallDecision.FIRST_INSTALL, InstallDecision.ADOPT_DELIVERY}

    @property
    def host_established(self) -> bool:
        return self in {InstallDecision.CONTINUE, InstallDecision.ADOPT_DELIVERY}


@dataclass(frozen=True, slots=True)
class InstallContext:
    """What one Host reported about itself, in one round trip.

    ``established`` is the Host's lineage when its database marker and its
    external anchor agree, and None otherwise — the agent computes it, and the
    decision below trusts neither copy alone.
    """

    marker: Mapping[str, object] | None
    anchor: Mapping[str, object] | None
    established: Mapping[str, object] | None
    #: The signed Owner directory the Host serves, verbatim, or None when it
    #: serves none. The document rather than a digest, because adopting it
    #: means verifying its signature against the Owner root this side holds.
    directory: str | None
    hardware: Mapping[str, str]
    #: SHA-256 of the Host identity secret the Host holds, or None when it holds
    #: none. Compared against this profile's own copy: a Host that holds the
    #: very secret this profile issued was delivered to by this profile.
    identity_sha256: str | None

    @classmethod
    def from_observation(cls, observed: Mapping[str, object]) -> InstallContext:
        keys = {
            "status",
            "marker",
            "anchor",
            "established",
            "directory",
            "hardware",
            "identity_sha256",
        }
        if observed.get("status") != "observed" or set(observed) != keys:
            raise ValueError("install context observation is invalid")
        for name in ("marker", "anchor", "established"):
            value = observed[name]
            if value is not None and not isinstance(value, Mapping):
                raise ValueError(f"install context {name} is invalid")
        directory = observed["directory"]
        if directory is not None and not isinstance(directory, str):
            raise ValueError("install context directory is invalid")
        hardware = observed["hardware"]
        if not isinstance(hardware, Mapping) or set(hardware) != {"kind", "fingerprint"}:
            raise ValueError("install context hardware is invalid")
        identity = observed["identity_sha256"]
        if identity is not None and (
            not isinstance(identity, str) or re.fullmatch(r"[0-9a-f]{64}", identity) is None
        ):
            raise ValueError("install context identity digest is invalid")
        return cls(
            marker=observed["marker"],
            anchor=observed["anchor"],
            established=observed["established"],
            directory=directory,
            hardware={"kind": str(hardware["kind"]), "fingerprint": str(hardware["fingerprint"])},
            identity_sha256=identity,
        )

    def report(self) -> dict[str, object]:
        """The observation as an operation reports it: everything but the document."""

        return {
            "marker": None if self.marker is None else dict(self.marker),
            "anchor": None if self.anchor is None else dict(self.anchor),
            "established": None if self.established is None else dict(self.established),
            "serves_directory": self.directory is not None,
            "hardware": dict(self.hardware),
            "holds_identity": self.identity_sha256 is not None,
        }


def decide_install(
    context: InstallContext,
    *,
    owner_domain_id: str,
    host_id: str,
    identity_sha256: str,
    binding: bytes | None,
) -> InstallDecision:
    """The situation an install is in, from the Host's report and this profile's record.

    Ordered so that each question is asked only once the earlier ones have
    answers: a Host whose own copies disagree is an incident whatever this
    profile records; a Host holding another Owner's Authority is foreign
    whatever board it is; and only then does the delivery binding say whether
    this is the board this identity went to.
    """

    if context.anchor is not None and context.marker != context.anchor:
        return InstallDecision.HOST_INCIDENT
    if context.marker is not None and context.anchor is None:
        return InstallDecision.HOST_INCIDENT
    established = context.established
    if established is not None and established.get("owner_domain_id") != owner_domain_id:
        return InstallDecision.FOREIGN_AUTHORITY
    if binding is not None:
        try:
            verify_binding(binding, host_id, dict(context.hardware))
        except HostHardwareError:
            return InstallDecision.WRONG_BOARD
        if established is None:
            return InstallDecision.AUTHORITY_LOST
        return InstallDecision.CONTINUE
    if established is None:
        return InstallDecision.FIRST_INSTALL
    if context.identity_sha256 == identity_sha256 and directory_names_host(
        context.directory, host_id
    ):
        return InstallDecision.ADOPT_DELIVERY
    return InstallDecision.IDENTITY_UNPROVEN


def directory_names_host(directory: str | None, host_id: str) -> bool:
    """Whether the served directory publishes itself under this public Host id.

    The signed directory carries its own route, and that route's hostname is
    derived from the Host id — so a Host serving a directory that names another
    id is not this Host, however it answered on the network.
    """

    if directory is None:
        return False
    try:
        document = json.loads(directory)
    except ValueError:
        return False
    uri = document.get("descriptor_uri") if isinstance(document, dict) else None
    hostname = urlparse(uri).hostname if isinstance(uri, str) else None
    match = re.fullmatch(r"eidolon-hub-([0-9a-f]{20})\.local", hostname or "")
    return match is not None and "ehost-" + match.group(1) == host_id


def refusal(decision: InstallDecision, context: InstallContext, *, owner_domain_id: str) -> str:
    """The one sentence each refusing situation gets. Never for one that proceeds."""

    hardware = context.hardware.get("fingerprint")
    match decision:
        case InstallDecision.HOST_INCIDENT:
            return (
                "AUTHORITY_RECOVERY_REQUIRED: this Host's Hub database marker and its saved "
                f"authorization anchor do not agree (marker {context.marker}, anchor "
                f"{context.anchor}). Something on the Host was lost or replaced; restore its "
                "complete backup. Nothing was shipped."
            )
        case InstallDecision.FOREIGN_AUTHORITY:
            established = context.established or {}
            return (
                f"FOREIGN_AUTHORITY: this Host has established {established.get('owner_domain_id')}, "
                f"and this profile's Owner material is {owner_domain_id}. It belongs to another "
                "Owner. Use that Owner's profile, or create a new Host on it (all its data is "
                "lost; Mobile must pair again) with install --reset-existing "
                "--wipe-authority-data --apply."
            )
        case InstallDecision.WRONG_BOARD:
            return (
                "WRONG_BOARD: this profile's Host identity was delivered to another board, and "
                f"the board answering now reports {hardware}. Its credentials are not reused "
                "implicitly: start a new profile with its own inputs for this board, or move the "
                "identity with an explicit complete restore."
            )
        case InstallDecision.AUTHORITY_LOST:
            return (
                "AUTHORITY_LOST: this is the board this profile delivered its identity to, and it "
                "established an Authority once, but holds none now. Restore its Authority "
                "backup, or create a new Host deliberately (all data is lost; Mobile must pair "
                "again) with install --reset-existing --wipe-authority-data --apply."
            )
        case InstallDecision.IDENTITY_UNPROVEN:
            return (
                "IDENTITY_UNPROVEN: this Host has established this Owner's Authority, but does "
                "not show it holds this profile's Host identity, and this profile records no "
                "delivery to any board. A Host that cannot prove that is either a different "
                "board or one this profile never installed. Nothing was shipped."
            )
    raise ValueError(f"{decision} is not a refusal")
