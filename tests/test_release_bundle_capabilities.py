"""What a bundle carries when the Host can do something extra.

The release contract is validated on the Host, before Ops' config exists there,
so a capability has to travel *in* the bundle rather than be inferred from it.
These tests pin the two halves of that: the sources a sealing command names, and
the capabilities it declares.
"""

from __future__ import annotations

from dataclasses import replace
from itertools import pairwise
from pathlib import Path

import pytest

from eidolon_ops.config import SOURCE_IDS, SourceConfig
from eidolon_ops.release_bundle import BundleTransfer

pytestmark = pytest.mark.unit


class _Runner:
    def __init__(self) -> None:
        self.commands: list[list[str]] = []

    def run(self, command, *, env=None, timeout=None):
        self.commands.append([str(item) for item in command])

        class _Result:
            returncode = 0
            stdout = '{"status": "bundled", "manifest": "m", "notes": []}'
            stderr = ""

        return _Result()


class _Resolver:
    def revision(self, source_id: str) -> str:
        return "0" * 40


def _transfer(config, runner) -> BundleTransfer:
    return BundleTransfer(config, runner, transport=object(), sources=_Resolver())


def _with_models(config):
    sources = dict(config.sources)
    sources["eidolon_models"] = SourceConfig(path=Path("/tmp/eidolon_models"))
    return replace(
        config,
        sources=sources,
        capabilities=frozenset({"rknpu2", "local_asr"}),
    )


def test_a_host_that_declares_nothing_seals_exactly_the_baseline(pinned_config) -> None:
    runner = _Runner()
    transfer = _transfer(pinned_config, runner)

    assert transfer._sealed_source_ids() == SOURCE_IDS


def test_a_capability_adds_its_repository_after_the_baseline(pinned_config) -> None:
    """Ordered so two Hosts differing only in what they can do produce bundle
    source records in the same order for everything they share."""

    config = _with_models(pinned_config)
    transfer = _transfer(config, _Runner())

    assert transfer._sealed_source_ids() == (*SOURCE_IDS, "eidolon_models")


def test_the_sealing_command_declares_the_capabilities_and_names_the_repository(
    pinned_config, tmp_path, monkeypatch
) -> None:
    config = _with_models(pinned_config)
    runner = _Runner()
    transfer = _transfer(config, runner)
    monkeypatch.setattr(
        BundleTransfer, "_workstation_uv", lambda self: Path("/usr/bin/uv")
    )

    transfer._seal(tmp_path / "out", "r1", cutover_mode="reversible")

    command = runner.commands[0]
    pairs = list(pairwise(command))
    assert ("--capability", "local_asr") in pairs
    assert ("--capability", "rknpu2") in pairs
    assert ("--models-repo", "/tmp/eidolon_models") in pairs
    assert ("--models-revision", "0" * 40) in pairs


def test_a_baseline_command_declares_no_capability_at_all(
    pinned_config, tmp_path, monkeypatch
) -> None:
    """Most Hosts. Nothing extra is named, so nothing extra is expected of the
    release, and the descriptor it seals is the one every Host had before."""

    runner = _Runner()
    transfer = _transfer(pinned_config, runner)
    monkeypatch.setattr(
        BundleTransfer, "_workstation_uv", lambda self: Path("/usr/bin/uv")
    )

    transfer._seal(tmp_path / "out", "r1", cutover_mode="reversible")

    command = runner.commands[0]
    assert "--capability" not in command
    assert "--models-repo" not in command
