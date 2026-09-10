"""What Ops requires of a board, and what one platform is told to make it true.

The renderer emits text because this package carries no runtime YAML
implementation. That is only safe if something parses it properly.
"""

from __future__ import annotations

import os
import subprocess
import sys

import pytest
import yaml

from eidolon_ops.bring_up import (
    BOOT_MEDIUM,
    META_DATA,
    NETWORK_CONFIG,
    PLATFORMS,
    SCRIPT,
    SHELL,
    USER_DATA,
    BringUp,
    read_operator_keys,
    render,
)
from eidolon_ops.errors import OperationsError

pytestmark = pytest.mark.unit

KEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIKTKp2ZMbC8xKgqcun/TKj2lUDUYR7sghfQD4uRJtAL2 eidolon-pi"
FOUNDATION = "raspberry-pi-os-debian-arm64-v2"


def _bring_up(**overrides) -> BringUp:
    arguments = {"hostname": "eidolon-pi5.local", "user": "eidolon-pi5", "authorized_keys": (KEY,)}
    arguments.update(overrides)
    return BringUp(**arguments)


def _parsed(**overrides):
    payload = render(_bring_up(**overrides), foundation=FOUNDATION, via=BOOT_MEDIUM)
    return {name: yaml.safe_load(text) for name, text in payload.items()}


def _script(**overrides) -> str:
    return render(_bring_up(**overrides), foundation=FOUNDATION, via=SHELL)[SCRIPT]


def test_the_requirement_names_no_platform() -> None:
    """The split this module exists for.

    `BringUp` is what Ops needs of any board, in Ops's own terms. A file name,
    a partition or an interface appearing in it would be the mechanism leaking
    into the requirement — which is how this ended up first written against a
    first-boot hook the OS had already replaced.
    """

    assert set(BringUp.__dataclass_fields__) == {"hostname", "user", "authorized_keys"}
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

    # Two properties, both measured on a board. Nothing else: `method: auto`
    # restates `dhcp4`, `connection.autoconnect` is already the default, and
    # `dhcp-timeout` only changes how long the first boot waits.
    assert passthrough == {"ipv4.link-local": "4", "ipv6.method": "link-local"}
    assert (ethernet["dhcp4"], ethernet["optional"]) == (True, True)
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
    once = render(_bring_up(), foundation=FOUNDATION, via=BOOT_MEDIUM)
    assert once == render(_bring_up(), foundation=FOUNDATION, via=BOOT_MEDIUM)


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

    assert set(PLATFORMS) == {FOUNDATION}

    for via in (BOOT_MEDIUM, SHELL):
        with pytest.raises(OperationsError, match="no bring-up is declared"):
            render(_bring_up(), foundation="ubuntu-2604-rk3588-arm64-v1", via=via)


@pytest.mark.parametrize("value", ["not-a-key", f"{KEY}\nssh-ed25519 second"])
def test_an_unusable_key_is_refused_here_not_discovered_on_a_board(value: str) -> None:
    with pytest.raises(OperationsError, match="not an OpenSSH public key line"):
        _bring_up(authorized_keys=(value,))


def test_a_host_nobody_can_log_into_is_refused() -> None:
    with pytest.raises(OperationsError, match="accepts no operator keys"):
        _bring_up(authorized_keys=())


# --- the second channel: a board that is already running --------------------


def test_both_channels_carry_the_same_requirement() -> None:
    """One requirement, two expressions — not two designs that drifted.

    A board chooses the channel by its own state: never booted leaves only its
    medium, already running leaves only a shell. Which medium and which shell
    are the operator's business, which is why neither appears here.
    """

    script = _script()
    documents = _parsed()
    config = documents[USER_DATA]

    # The same account, key and name, derived from the same profile.
    assert config["user"]["name"] in script
    assert KEY in script
    assert config["hostname"] in script
    # The same two measured NetworkManager properties.
    passthrough = documents[NETWORK_CONFIG]["network"]["ethernets"]["eth0"]["networkmanager"][
        "passthrough"
    ]
    assert passthrough == {"ipv4.link-local": "4", "ipv6.method": "link-local"}
    assert "ipv4.link-local fallback" in script
    assert "ipv6.method link-local" in script
    # And the same non-interactive sudo the transport cannot elevate without.
    assert "NOPASSWD:ALL" in script


def test_the_script_is_a_shell_script_a_shell_accepts() -> None:
    """Rendered as text, so something has to actually parse it."""

    import subprocess
    import tempfile

    with tempfile.NamedTemporaryFile("w", suffix=".sh") as handle:
        handle.write(_script())
        handle.flush()
        result = subprocess.run(["sh", "-n", handle.name], capture_output=True, text=True)

    assert result.returncode == 0, result.stderr


def test_authorization_is_reconciled_before_the_network_can_disconnect() -> None:
    """Declared keys replace previous ones before network activation."""

    script = _script()

    # The network comes last: `nmcli con up` drops a session running over that
    # interface, and by then everything else has been applied.
    assert script.index("authorized_keys") < script.index("nmcli con up")
    assert script.index("sudoers.d") < script.index("nmcli con up")


def _execute_with_fake_board(tmp_path, *, failure="", keys=(KEY,)):
    """Execute the generated shell with temp paths and fake system commands."""
    board = tmp_path / "board"
    board.mkdir(exist_ok=True)
    (board / "sudoers.d").mkdir(exist_ok=True)
    (board / "hosts").touch()
    home = board / "home"
    home.mkdir(exist_ok=True)
    binaries = tmp_path / "bin"
    binaries.mkdir(exist_ok=True)
    stub = binaries / "stub"
    stub.write_text(f"#!{sys.executable}\n" + '''
import os, sys
from pathlib import Path
name = Path(sys.argv[0]).name
args = sys.argv[1:]
failure = os.environ.get("BOARD_FAILURE", "")
if name == "id": print("0" if args[0] == "-u" else "operator")
elif name == "getent": print("operator:x:1000:1000::" + os.environ["BOARD_HOME"] + ":/bin/sh")
elif name == "install": Path(args[-1]).mkdir(parents=True, exist_ok=True)
elif name == "su":
    assert args == ["-s", "/bin/sh", "eidolon-pi5", "-c", "sudo -n -u root true"]
    sys.exit(1 if failure == "sudo" else 0)
elif name == "nmcli":
    if "DEVICE,TYPE" in args: print("eth0:ethernet")
    elif "NAME,DEVICE" in args: print("wired:eth0")
    elif args[:2] == ["con", "up"]: sys.exit(1 if failure == "activation" else 0)
elif name == "systemctl" and args[0] == "is-active":
    sys.exit(1 if failure == "service" else 0)
elif name == "ip":
    if failure != "address": print("2: eth0 inet 169.254.1.2/16 scope link eth0")
elif name == "hostname": print("eidolon-pi5")
''')
    stub.chmod(0o755)
    for name in ("id", "getent", "install", "su", "nmcli", "systemctl", "ip", "hostname",
                 "hostnamectl", "useradd", "usermod", "chown", "chmod", "visudo", "avahi-daemon"):
        path = binaries / name
        if not path.exists():
            path.symlink_to(stub)
    script = _script(authorized_keys=keys).replace("/etc/hosts", str(board / "hosts"))
    script = script.replace("/etc/sudoers.d", str(board / "sudoers.d"))
    result = subprocess.run(
        ["/bin/sh"], input=script, text=True, capture_output=True,
        env={**os.environ, "PATH": f"{binaries}:/usr/bin:/bin", "BOARD_HOME": str(home), "BOARD_FAILURE": failure},
    )
    return result, home / ".ssh/authorized_keys"


@pytest.mark.parametrize("failure,exit_code", [("activation", 75), ("sudo", 1), ("service", 1), ("address", 1)])
def test_shell_reports_failed_postconditions_in_its_exit_status(tmp_path, failure, exit_code):
    result, _ = _execute_with_fake_board(tmp_path, failure=failure)
    assert result.returncode == exit_code, result.stderr


def test_shell_applies_and_revokes_the_declared_keys_and_can_be_repeated(tmp_path):
    result, authorized = _execute_with_fake_board(tmp_path, keys=(KEY, SECOND))
    assert result.returncode == 0, result.stderr
    assert authorized.read_text().splitlines() == [KEY, SECOND]
    result, authorized = _execute_with_fake_board(tmp_path, keys=(SECOND,))
    assert result.returncode == 0, result.stderr
    assert authorized.read_text().splitlines() == [SECOND]
    assert "removing key not in the declaration" in result.stdout
    result, _ = _execute_with_fake_board(tmp_path, keys=(SECOND,))
    assert result.returncode == 0, result.stderr


def test_a_channel_a_board_does_not_have_is_refused() -> None:
    with pytest.raises(KeyError):
        PLATFORMS["no-such-foundation"]


# --- who may operate this Host, declared rather than implied ----------------

SECOND = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIKHhyMtoqOaGzBaKm4ldkGggu8LWslWg4n0s second@elsewhere"


def test_every_declared_operator_reaches_both_channels() -> None:
    """Nobody needs anybody else's private key.

    `host.identity_file` was carrying two facts: which private key this
    machine connects with, and which key the board trusts. While they were one
    field the board could trust exactly one key, so a second operator had to
    be handed a private one — the thing least worth copying, and it takes the
    audit trail with it. Declared separately, each operator keeps their own.
    """

    both = _bring_up(authorized_keys=(KEY, SECOND))

    medium = render(both, foundation=FOUNDATION, via=BOOT_MEDIUM)
    assert yaml.safe_load(medium[USER_DATA])["user"]["ssh_authorized_keys"] == [KEY, SECOND]

    script = render(both, foundation=FOUNDATION, via=SHELL)[SCRIPT]
    assert KEY in script and SECOND in script


def test_the_board_is_reconciled_to_the_declaration_so_revocation_works() -> None:
    """Append-only was safe with one key and made removal impossible.

    A line deleted from the declaration has to reach the board, or the
    declaration is not what decides who can log in.
    """

    script = _script()

    assert 'printf \'%s\\n\' "$KEYS" > "$AUTHORIZED"' in script, "made equal, not added to"
    assert "removing key not in the declaration" in script
    # Still last, because it drops a session running over that interface.
    assert script.index("AUTHORIZED") < script.index("nmcli con up")


def test_the_declaration_is_read_as_the_format_the_board_wants() -> None:
    """authorized_keys format: comments and blanks skipped, order kept."""

    assert read_operator_keys(
        f"# who can reach this Host\n{KEY}\n\n{SECOND}\n", label="operators"
    ) == (KEY, SECOND)

    with pytest.raises(OperationsError, match="operators:2 is not an OpenSSH public key"):
        read_operator_keys(f"{KEY}\nnot-a-key\n", label="operators")
    with pytest.raises(OperationsError, match="declares no operator keys"):
        read_operator_keys("# nobody yet\n", label="operators")
