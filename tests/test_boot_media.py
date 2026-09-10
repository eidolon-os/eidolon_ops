"""What Ops requires of a board, and what one platform is told to make it true.

The renderer emits text because this package carries no runtime YAML
implementation. That is only safe if something parses it properly.
"""

from __future__ import annotations

import pytest
import yaml

from eidolon_ops.boot_media import (
    META_DATA,
    NETWORK_CONFIG,
    PAYLOADS,
    USER_DATA,
    BringUp,
    render,
)
from eidolon_ops.errors import OperationsError

pytestmark = pytest.mark.unit

KEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIKTKp2ZMbC8xKgqcun/TKj2lUDUYR7sghfQD4uRJtAL2 eidolon-pi"
FOUNDATION = "raspberry-pi-os-debian-arm64-v2"


def _bring_up(**overrides) -> BringUp:
    arguments = {"hostname": "eidolon-pi5.local", "user": "eidolon-pi5", "authorized_key": KEY}
    arguments.update(overrides)
    return BringUp(**arguments)


def _parsed(**overrides):
    payload = render(_bring_up(**overrides), foundation=FOUNDATION)
    return {name: yaml.safe_load(text) for name, text in payload.items()}


def test_the_requirement_names_no_platform() -> None:
    """The split this module exists for.

    `BringUp` is what Ops needs of any board, in Ops's own terms. A file name,
    a partition or an interface appearing in it would be the mechanism leaking
    into the requirement — which is how this ended up first written against a
    first-boot hook the OS had already replaced.
    """

    assert set(BringUp.__dataclass_fields__) == {"hostname", "user", "authorized_key"}
    assert _bring_up().short_hostname == "eidolon-pi5"


def test_the_five_manual_facts_are_all_in_the_payload() -> None:
    """Everything that used to be typed into an imager, derived instead."""

    documents = _parsed()
    config = documents[USER_DATA]

    assert config["hostname"] == "eidolon-pi5"
    assert config["user"]["name"] == "eidolon-pi5"
    # Non-interactive sudo: the transport cannot elevate without it.
    assert config["user"]["sudo"] == ["ALL=(ALL) NOPASSWD:ALL"]
    assert "sudo" in config["user"]["groups"]
    assert config["user"]["ssh_authorized_keys"] == [KEY]
    # Ops reaches the Host by the name it publishes.
    assert "avahi-daemon" in config["packages"]
    assert ["systemctl", "enable", "--now", "ssh"] in config["runcmd"]
    assert ["systemctl", "enable", "--now", "avahi-daemon"] in config["runcmd"]
    assert "eth0" in documents[NETWORK_CONFIG]["network"]["ethernets"]


def test_one_wired_configuration_serves_both_of_its_jobs() -> None:
    """A lease on a real network, a link-local address on the bench cable.

    `auto` alone flaps on a point-to-point cable: nothing answers DHCP, so
    NetworkManager tears the connection down and starts again, which is what a
    Host answering in bursts was. `link-local` alone can never carry the route
    out that `provision` needs. Fallback is both — measured on the board:
    169.254/16 after the timeout, connection still activated, 30 probes over
    90 seconds with none lost.
    """

    ethernet = _parsed()[NETWORK_CONFIG]["network"]["ethernets"]["eth0"]
    passthrough = ethernet["networkmanager"]["passthrough"]

    assert ethernet["dhcp4"] is True
    assert passthrough["ipv4.method"] == "auto"
    # NetworkManager's "fallback": try DHCP, assign link-local if unanswered.
    assert passthrough["ipv4.link-local"] == "4"
    # A link-local address has no gateway and cannot take a default route, so
    # never-default costs nothing there — and setting it would stop the cable
    # being the route out on a real network, the case this exists for.
    assert "ipv4.never-default" not in passthrough
    # netplan refuses a device with `networkmanager` settings and no renderer,
    # and the image's global renderer file is not in scope for a lone seed.
    assert ethernet["renderer"] == "NetworkManager"


def test_the_instance_id_is_derived_so_two_renderings_agree() -> None:
    """Stable, not fresh: on a flashed card the value cannot matter.

    `/var/lib/cloud` is empty there and every module runs regardless, so a
    timestamp would buy nothing while making the output irreproducible and
    turning "reconfigure a card that already booted" into the silent default.
    """

    assert _parsed()[META_DATA]["instance-id"] == "eidolon-ops-eidolon-pi5"
    assert render(_bring_up(), foundation=FOUNDATION) == render(_bring_up(), foundation=FOUNDATION)


def test_a_key_only_host_does_not_also_accept_passwords() -> None:
    config = _parsed()[USER_DATA]

    assert config["ssh_pwauth"] is False
    assert config["user"]["lock_passwd"] is True
    assert "passwd" not in config["user"]


def test_a_foundation_with_no_declared_payload_is_refused() -> None:
    """The capability lives on the SSH/systemd adapter, which several boards use.

    The rk3588 profile reached this command and would have been handed `eth0`
    for an interface called `enP3p49s0` — a card that boots, comes up with no
    wired link, and says nothing about why. A platform gets its own renderer
    declared beside it rather than another's guessed at.
    """

    assert set(PAYLOADS) == {FOUNDATION}

    with pytest.raises(OperationsError, match="no first-boot payload is declared"):
        render(_bring_up(), foundation="ubuntu-2604-rk3588-arm64-v1")


@pytest.mark.parametrize("value", ["not-a-key", f"{KEY}\nssh-ed25519 second"])
def test_an_unusable_key_is_refused_here_not_discovered_on_a_board(value: str) -> None:
    with pytest.raises(OperationsError, match="one OpenSSH public key line"):
        _bring_up(authorized_key=value)
