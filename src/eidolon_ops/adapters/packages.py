"""Who installs the non-Eidolon foundation a release assumes is already there."""

from __future__ import annotations

from eidolon_ops.controller import EidolonPiController
from eidolon_ops.foundation import FOUNDATION_PROFILE
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

    def __init__(self, release: EidolonPiController) -> None:
        self.release = release

    @property
    def capabilities(self) -> frozenset[Capability]:
        return frozenset({Capability.PROVISION})

    def provision(self, *, apply: bool) -> dict[str, object]:
        return self.release.provision(apply=apply)

    def doctor(self) -> dict[str, object]:
        return {**self.release.provision(apply=False), "profile": FOUNDATION_PROFILE}
