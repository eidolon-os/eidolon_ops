"""Making the correct way to reach a Host by hand also the short one.

The transport already connects correctly. A human doing the same thing typed
four options, one of which — `HostKeyAlias` — nobody reaches for unprompted,
because nothing on the command line suggests it is what makes a changed link
stop being a trust decision. So the safe way was the long way, and the short
way was `-o StrictHostKeyChecking=accept-new` into the operator's own
known_hosts: the exact file the profile stopped using, holding a key nobody
checked, under whatever address was typed.

These hold the generated fragment to the transport's own options, so the alias
cannot quietly become a weaker way in than the flags it replaces.
"""

from __future__ import annotations

import dataclasses
import re
from pathlib import Path

import pytest

from eidolon_ops import ssh_config
from eidolon_ops.controller import EidolonPiController
from eidolon_ops.errors import OperationsError

pytestmark = pytest.mark.unit

ALIAS = "eidolon-pi5"


def _host(config, tmp_path: Path):
    """A profile whose known_hosts sits where a real one does: its own directory."""

    return dataclasses.replace(
        config.host,
        hostname="eidolon-pi5.local",
        user="eidolon-pi5",
        port=22,
        known_hosts_file=tmp_path / ".eidolon-ops" / "pi5" / "known_hosts",
    )


def _options(text: str) -> dict[str, str]:
    """The block's options, as ssh would read them — comments are not settings."""

    found: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or line.startswith("Host "):
            continue
        keyword, _, value = line.partition(" ")
        found[keyword] = value.strip()
    return found


def test_the_fragment_carries_the_options_the_transport_uses(config, tmp_path) -> None:
    """Same alias, same file, same strictness — or the alias is a bypass.

    `HostKeyAlias` is the one that has to equal `host.hostname` exactly. The
    transport grants trust under that name; a fragment that used `HostName`
    instead would key the operator's connection by address, which is the
    property the whole design exists to remove.
    """

    host = _host(config, tmp_path)

    options = _options(ssh_config.render(host, alias=ALIAS, generated_by="test"))

    assert options["StrictHostKeyChecking"] == "yes"
    assert options["HostKeyAlias"] == host.hostname
    assert options["UserKnownHostsFile"] == str(host.known_hosts_file)
    assert options["HostName"] == host.hostname
    assert options["User"] == host.user
    assert options["Port"] == str(host.port)
    assert options["IdentityFile"] == str(host.identity_file)
    # Otherwise an agent offers every key it holds first, and a board that
    # accepts one of them lets the operator in as somebody else.
    assert options["IdentitiesOnly"] == "yes"


def test_an_address_as_hostname_still_grants_trust_by_name(config, tmp_path) -> None:
    """The bench profile's shape: HostName is a literal, HostKeyAlias is the name.

    On this bench every flashed board answers at the same point-to-point
    address, so the alias is the only thing telling two boards apart. If the
    fragment derived it from anything but `host.hostname`, `ssh` and Ops would
    disagree about which Host they mean while both looked correct.
    """

    host = dataclasses.replace(_host(config, tmp_path), hostname="10.42.0.2")

    options = _options(ssh_config.render(host, alias="eidolon-opi5max", generated_by="test"))

    assert options["HostName"] == "10.42.0.2"
    assert options["HostKeyAlias"] == "10.42.0.2"


def test_the_block_is_the_alias_and_nothing_broader(config, tmp_path) -> None:
    """A fragment included into ~/.ssh/config must not speak for other hosts."""

    text = ssh_config.render(_host(config, tmp_path), alias=ALIAS, generated_by="test")

    headers = re.findall(r"^Host (.+)$", text, re.MULTILINE)
    assert headers == [ALIAS]
    assert "*" not in headers[0]


def test_a_relative_known_hosts_is_refused_rather_than_written(config, tmp_path) -> None:
    """It would resolve against whatever directory `ssh` was run from.

    Silently, and into a file that does not exist — so `ssh` would offer to
    trust the board on first use, which is the state this whole mechanism is
    built to not have.
    """

    host = dataclasses.replace(config.host, known_hosts_file=Path("../.eidolon-ops/pi5/known_hosts"))

    with pytest.raises(OperationsError, match="absolute path"):
        ssh_config.render(host, alias=ALIAS, generated_by="test")


def test_the_fragment_lands_beside_the_known_hosts_it_points_at(config, tmp_path) -> None:
    """Derived, not configured: one directory holds both, so they move together."""

    host = _host(config, tmp_path)

    assert ssh_config.path_for(host) == host.known_hosts_file.parent / "ssh_config"


def _controller(config, tmp_path):
    controller = EidolonPiController.__new__(EidolonPiController)
    controller.config = dataclasses.replace(config, host=_host(config, tmp_path))
    return controller


def test_a_plan_writes_nothing_and_an_apply_writes_privately(config, tmp_path) -> None:
    controller = _controller(config, tmp_path)
    target = ssh_config.path_for(controller.config.host)

    planned = controller.ssh_config(alias=ALIAS, invocation="./eidolon pi5 ssh-config --apply")
    assert planned["status"] == "rendered"
    assert not target.exists(), "a plan must write nothing"
    # The plan shows the fragment, so the operator reads the options before
    # anything is placed where ssh will act on them.
    assert "StrictHostKeyChecking yes" in str(planned["fragment"])

    applied = controller.ssh_config(
        alias=ALIAS, invocation="./eidolon pi5 ssh-config --apply", apply=True
    )
    assert applied["status"] == "written"
    assert target.read_text(encoding="utf-8") == str(planned["fragment"])
    # ssh refuses a config file others can write, and a half-written fragment
    # is an `ssh` that silently stops applying StrictHostKeyChecking.
    assert target.stat().st_mode & 0o777 == 0o600
    assert list(target.parent.glob(".ssh_config-*")) == []


def test_the_report_says_how_to_make_the_alias_take_effect(config, tmp_path) -> None:
    """A fragment nobody includes is a fragment that changes nothing."""

    controller = _controller(config, tmp_path)
    target = ssh_config.path_for(controller.config.host)

    report = controller.ssh_config(alias=ALIAS, invocation="./eidolon pi5 ssh-config --apply")

    assert report["include"] == f"Include {target}"
    assert f"ssh {ALIAS}" in str(report["next"])
    # Ordering matters and is easy to get wrong: ssh keeps the first value it
    # is given, so an Include below a `Host *` block is quietly overridden.
    assert "Host *" in str(report["next"])


def test_rewriting_replaces_the_previous_fragment_whole(config, tmp_path) -> None:
    """A stale block is the one that can disagree with the transport."""

    controller = _controller(config, tmp_path)
    target = ssh_config.path_for(controller.config.host)
    controller.ssh_config(
        alias=ALIAS, invocation="./eidolon pi5 ssh-config --apply", apply=True
    )

    moved = dataclasses.replace(
        controller.config,
        host=dataclasses.replace(controller.config.host, hostname="eidolon-pi5-b.local"),
    )
    controller.config = moved
    controller.ssh_config(
        alias=ALIAS, invocation="./eidolon pi5 ssh-config --apply", apply=True
    )

    options = _options(target.read_text(encoding="utf-8"))
    assert options["HostKeyAlias"] == "eidolon-pi5-b.local"
    assert "eidolon-pi5.local" not in target.read_text(encoding="utf-8")


def test_the_alias_is_the_host_id_an_operator_already_types() -> None:
    """`ssh eidolon-pi5` and `./eidolon eidolon-pi5 status` name one Host.

    Not `host.hostname`, which on the bench profile is `10.42.0.2` — an alias
    nobody would type, for a Host whose whole point is that its address is not
    its identity.
    """

    from types import SimpleNamespace

    from eidolon_ops.host_controller import HostController
    from eidolon_ops.model import Capability, Outcome

    seen: dict[str, object] = {}

    class Release:
        def ssh_config(self, *, alias, invocation, apply):
            seen.update(alias=alias, invocation=invocation, apply=apply)
            return {"status": "written" if apply else "rendered"}

    class Adapter:
        def require_release(self, capability):
            seen["capability"] = capability
            return Release()

    controller = HostController.__new__(HostController)
    controller.profile = SimpleNamespace(
        host_id="eidolon-opi5max", path=Path("config/hosts/rk3588.toml")
    )
    controller._adapter = Adapter()

    evidence = controller.ssh_config(apply=True)

    assert seen["alias"] == "eidolon-opi5max"
    # The command names the Host the other way round, and has to: the wrapper
    # selects a profile by filename, and ids are not unique — `pi5.toml` and
    # `pi5-device-management-hil.toml` both declare `eidolon-pi5`.
    assert seen["invocation"] == "./eidolon rk3588 ssh-config --apply"
    assert seen["capability"] is Capability.SSH_CONFIG
    assert evidence.plan.operation == "ssh-config"
    assert evidence.outcome is Outcome.APPLIED


def test_the_cli_exposes_the_operation() -> None:
    """A generated fragment nobody can generate is documentation."""

    from eidolon_ops import host_cli

    assert "ssh-config" in host_cli.OPERATIONS
    parsed = host_cli._parser().parse_args(["--config", "host.toml", "ssh-config", "--apply"])
    assert parsed.operation == "ssh-config"
    assert parsed.apply is True
