"""What a freshly flashed card is told, checked with a real YAML parser.

The renderer emits text because this package carries no runtime YAML
implementation. That is only safe if something parses it properly.
"""

from __future__ import annotations

import pytest
import yaml

from eidolon_ops.boot_media import META_DATA, NETWORK_CONFIG, USER_DATA, render
from eidolon_ops.errors import OperationsError

pytestmark = pytest.mark.unit

KEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIKTKp2ZMbC8xKgqcun/TKj2lUDUYR7sghfQD4uRJtAL2 eidolon-pi"


def _parsed(**overrides):
    arguments = {
        "hostname": "eidolon-pi5.local",
        "user": "eidolon-pi5",
        "authorized_key": KEY,
    }
    arguments.update(overrides)
    return {name: yaml.safe_load(text) for name, text in render(**arguments).items()}


def test_the_five_manual_facts_are_all_in_the_payload() -> None:
    """Everything that used to be typed into an imager, derived instead."""

    documents = _parsed()
    config = documents[USER_DATA]

    # Without the suffix Avahi appends for itself.
    assert config["hostname"] == "eidolon-pi5"
    assert config["user"]["name"] == "eidolon-pi5"
    # Non-interactive sudo: the transport cannot elevate without it.
    assert config["user"]["sudo"] == ["ALL=(ALL) NOPASSWD:ALL"]
    assert "sudo" in config["user"]["groups"]
    assert config["user"]["ssh_authorized_keys"] == [KEY]
    # And the wired link, the one that was not written down anywhere.
    passthrough = documents[NETWORK_CONFIG]["network"]["ethernets"]["eth0"]["networkmanager"][
        "passthrough"
    ]
    assert passthrough["ipv4.method"] == "link-local"
    assert passthrough["ipv4.never-default"] == "true"
    # Ops reaches the Host by the name it publishes.
    assert "avahi-daemon" in config["packages"]
    assert ["systemctl", "enable", "--now", "ssh"] in config["runcmd"]
    assert ["systemctl", "enable", "--now", "avahi-daemon"] in config["runcmd"]


def test_the_two_things_that_would_silently_ruin_the_card() -> None:
    """A flapping link and a config netplan will not read.

    `dhcp4: true` on a point-to-point cable is 45 seconds of waiting and then a
    torn-down link, repeatedly. `networkmanager` settings without a renderer
    make netplan refuse the device outright — "networkmanager backend settings
    found but renderer is not NetworkManager" — and the image's global renderer
    file is not in scope when the seed is parsed alone. Either way the operator
    finds out with a board in their hand.
    """

    ethernet = _parsed()[NETWORK_CONFIG]["network"]["ethernets"]["eth0"]

    assert ethernet["dhcp4"] is False
    assert ethernet["dhcp6"] is False
    assert ethernet["renderer"] == "NetworkManager"


def test_the_instance_id_is_derived_so_two_renderings_agree() -> None:
    """Stable, not fresh. On a flashed card the value cannot matter.

    `/var/lib/cloud` is empty there and every module runs regardless, so a
    timestamp would buy nothing for the case this serves while making the
    output irreproducible and turning "reconfigure a card that already booted"
    into the silent default.
    """

    assert _parsed()[META_DATA]["instance-id"] == "eidolon-ops-eidolon-pi5"
    assert render(hostname="a.local", user="u", authorized_key=KEY) == render(
        hostname="a.local", user="u", authorized_key=KEY
    )


def test_a_key_only_host_does_not_also_accept_passwords() -> None:
    config = _parsed()[USER_DATA]

    assert config["ssh_pwauth"] is False
    assert config["user"]["lock_passwd"] is True
    assert "passwd" not in config["user"]


@pytest.mark.parametrize("value", ["not-a-key", f"{KEY}\nssh-ed25519 second"])
def test_an_unusable_key_is_refused_here_not_discovered_on_a_board(value: str) -> None:
    with pytest.raises(OperationsError, match="one OpenSSH public key line"):
        _parsed(authorized_key=value)
