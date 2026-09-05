"""Who installs the non-Eidolon foundation a release assumes is already there."""

from __future__ import annotations

from eidolon_ops.controller import EidolonPiController
from eidolon_ops.model import Capability
from eidolon_ops.ports import PackageManagerKind


class UnmanagedPackages:
    """A workstation. Its packages belong to whoever set the machine up."""

    kind = PackageManagerKind.NONE

    @property
    def capabilities(self) -> frozenset[Capability]:
        return frozenset()

    def provision(self, *, apply: bool) -> dict[str, object]:
        raise NotImplementedError("this Host has no package manager capability")

    def doctor(self) -> dict[str, object]:
        return {
            "status": "unmanaged",
            "reason": "the operator's own machine provides this Host's foundation",
        }


class AptPackages:
    """A product board, whose pinned foundation Ops installs and proves."""

    kind = PackageManagerKind.APT

    def __init__(self, release: EidolonPiController, profile_id: str) -> None:
        self.release = release
        #: Which reviewed foundation this Host is, reported rather than assumed:
        #: with more than one profile registered, naming the first would be a
        #: report that is wrong on every board but the first.
        self.profile_id = profile_id

    @property
    def capabilities(self) -> frozenset[Capability]:
        return frozenset({Capability.PROVISION})

    def provision(self, *, apply: bool) -> dict[str, object]:
        return self.release.provision(apply=apply)

    def doctor(self) -> dict[str, object]:
        return {**self.release.provision(apply=False), "profile": self.profile_id}
