"""Which host key a profile trusts, and what it takes to change that.

Trust is granted under the Host's name, so two boards publishing one name
cannot both be trusted — and the operation that resolves that is the only one
allowed to look at a key the strict transport is right to refuse.
"""

from __future__ import annotations

import pytest

from eidolon_ops import host_keys
from eidolon_ops.endpoints import HostEndpoint
from eidolon_ops.errors import OperationsError
from eidolon_ops.process import ProcessResult

pytestmark = pytest.mark.unit

ALIAS = "eidolon-pi5.local"
NEW_KEY = "AAAAC3NzaC1lZDI1NTE5AAAAIIH9XoDPvKWZElKHIYsUdU8Vrysoge17veWDpgjI4QpV"
OLD_KEY = "AAAAC3NzaC1lZDI1NTE5AAAAIGVOU5RtOwpRNL6XpNDgAnI/fKdaogu0yECpYZ8RSgGA"
NEW_PRINT = "SHA256:rGKurdR2TIc8J2A5A5XXstiwfyIK2SnElnSpUtHn7wo"
OLD_PRINT = "SHA256:WD+NlsXU+kbr4SoMfcIvRqLzeU1CnBn8Cj9uA/k5HEo"

WIRED = HostEndpoint(address="169.254.182.252", interface="en7", link="wired")
WIRELESS = HostEndpoint(address="192.168.1.37", interface="en0", link="wireless")


class KeyRunner:
    """ssh-keyscan and ssh-keygen, answering per address and per key."""

    def __init__(self, answers: dict[str, str]) -> None:
        self.answers = answers
        self.scanned: list[str] = []

    def run(self, command, **_kwargs):
        program = command[0]
        if program == "ssh-keyscan":
            address = command[-1]
            self.scanned.append(address)
            key = self.answers.get(address)
            if key is None:
                return ProcessResult(0, "", f"getaddrinfo {address}: nodename nor servname")
            return ProcessResult(0, f"# {address}:22 SSH-2.0\n{address} ssh-ed25519 {key}\n", "")
        if program == "ssh-keygen":
            body = _kwargs.get("input_bytes", b"").decode("utf-8")
            fingerprint = NEW_PRINT if NEW_KEY in body else OLD_PRINT
            return ProcessResult(0, f"256 {fingerprint} no comment (ED25519)\n", "")
        raise AssertionError(f"unexpected program {program}")


def test_the_first_link_that_answers_gives_the_key() -> None:
    """The wire is tried first, but any link carries the same host key.

    ssh-keyscan cannot bind to an interface the way the transport does, and
    does not need to: a link-local address that leaves by the wrong interface
    gets no answer, and reaching the Host by Wi-Fi still reads the Host's key.
    """

    runner = KeyRunner({WIRELESS.address: NEW_KEY})

    host_key, endpoint = host_keys.scan(runner, (WIRED, WIRELESS), port=22, timeout=5)

    assert host_key.fingerprint == NEW_PRINT
    assert host_key.key_type == "ssh-ed25519"
    assert endpoint == WIRELESS
    assert runner.scanned == [WIRED.address, WIRELESS.address]


def test_a_host_that_answers_nowhere_is_not_silently_trusted() -> None:
    runner = KeyRunner({})

    with pytest.raises(OperationsError, match="no ed25519 host key could be read"):
        host_keys.scan(runner, (WIRED,), port=22, timeout=5)


def test_recording_replaces_only_this_alias(tmp_path) -> None:
    """A profile-owned file is still a file, and may name more than one Host."""

    path = tmp_path / "known_hosts"
    path.write_text(
        f"# a comment\nother.local ssh-ed25519 {OLD_KEY}\n{ALIAS} ssh-ed25519 {OLD_KEY}\n",
        encoding="utf-8",
    )
    runner = KeyRunner({WIRED.address: NEW_KEY})
    host_key, _ = host_keys.scan(runner, (WIRED,), port=22, timeout=5)

    host_keys.write(path, ALIAS, host_key)

    lines = path.read_text(encoding="utf-8").splitlines()
    assert f"{ALIAS} ssh-ed25519 {NEW_KEY}" in lines
    assert f"other.local ssh-ed25519 {OLD_KEY}" in lines
    assert "# a comment" in lines
    # The superseded key is gone rather than sitting beside the new one: two
    # trusted keys under one name is two boards this profile would accept.
    assert f"{ALIAS} ssh-ed25519 {OLD_KEY}" not in lines
    assert host_keys.recorded(path, ALIAS) == (f"ssh-ed25519 {NEW_KEY}",)


def test_a_recorded_file_is_private_and_written_whole(tmp_path) -> None:
    path = tmp_path / "nested" / "known_hosts"
    runner = KeyRunner({WIRED.address: NEW_KEY})
    host_key, _ = host_keys.scan(runner, (WIRED,), port=22, timeout=5)

    host_keys.write(path, ALIAS, host_key)

    assert path.stat().st_mode & 0o777 == 0o600
    # A truncated known_hosts is a Host that cannot be reached at all, so the
    # replace is atomic and leaves no partial file behind.
    assert list(path.parent.glob(".known_hosts-*")) == []


def test_an_alias_in_a_comma_list_is_still_this_host(tmp_path) -> None:
    path = tmp_path / "known_hosts"
    path.write_text(f"{ALIAS},169.254.1.2 ssh-ed25519 {OLD_KEY}\n", encoding="utf-8")

    assert host_keys.recorded(path, ALIAS) == (f"ssh-ed25519 {OLD_KEY}",)


def test_a_missing_file_trusts_nothing(tmp_path) -> None:
    assert host_keys.recorded(tmp_path / "absent", ALIAS) == ()


# --- the operation: what it takes to change what is trusted -----------------


def _controller(tmp_path, config, answers: dict[str, str], recorded: str | None):
    """A release controller whose known_hosts and Host answers are both ours."""

    import dataclasses

    from eidolon_ops.controller import EidolonPiController

    class OnlyCandidates:
        """The transport's one job here: which addresses to try, in order.

        Nothing else of it is reachable — the point of this operation is that
        it works on a Host every other command refuses.
        """

        def candidates(self):
            return (WIRED, WIRELESS)

    # Not "known_hosts": the config fixture builds its own SSH material in this
    # same directory, and a name collision would make the fixture's file look
    # like something this operation wrote.
    path = tmp_path / "profile-known-hosts"
    if recorded is not None:
        path.write_text(f"{ALIAS} ssh-ed25519 {recorded}\n", encoding="utf-8")
    host = dataclasses.replace(config.host, hostname=ALIAS, known_hosts_file=path)
    scoped = dataclasses.replace(config, host=host)
    controller = EidolonPiController.__new__(EidolonPiController)
    controller.config = scoped
    controller.runner = KeyRunner(answers)
    controller.transport = OnlyCandidates()
    return controller, path


def test_a_key_never_seen_is_recorded_without_ceremony(tmp_path, config) -> None:
    """Adding a fact this profile did not hold withdraws nothing."""

    controller, path = _controller(tmp_path, config, {WIRED.address: NEW_KEY}, recorded=None)

    planned = controller.trust_host_key()
    assert planned["status"] == "untrusted"
    assert planned["fingerprint"] == NEW_PRINT
    assert host_keys.recorded(path, ALIAS) == (), "a plan must record nothing"

    applied = controller.trust_host_key(apply=True)
    assert applied["status"] == "recorded"
    assert host_keys.recorded(path, ALIAS) == (f"ssh-ed25519 {NEW_KEY}",)


def test_the_key_already_trusted_is_a_read_not_a_write(tmp_path, config) -> None:
    controller, path = _controller(tmp_path, config, {WIRED.address: NEW_KEY}, recorded=NEW_KEY)
    before = path.read_bytes()

    report = controller.trust_host_key(apply=True)

    assert report["status"] == "trusted"
    assert path.read_bytes() == before


def test_a_different_key_is_refused_until_its_fingerprint_is_named(tmp_path, config) -> None:
    """A swapped board and a machine-in-the-middle are the same picture here.

    Nothing on this workstation can tell them apart, so the operator — who can
    read the fingerprint off the Host itself — is the one who states it.
    """

    controller, path = _controller(tmp_path, config, {WIRED.address: NEW_KEY}, recorded=OLD_KEY)

    planned = controller.trust_host_key()
    assert planned["status"] == "differs"
    assert planned["trusted_fingerprints"] == [OLD_PRINT]
    assert f"--replace {NEW_PRINT}" in str(planned["detail"])

    with pytest.raises(OperationsError, match="already trusts a different key"):
        controller.trust_host_key(apply=True)
    assert host_keys.recorded(path, ALIAS) == (f"ssh-ed25519 {OLD_KEY}",)

    # Naming the key that is *not* being presented cannot launder the change:
    # the acknowledgement has to be about this Host, or it is about nothing.
    with pytest.raises(OperationsError, match="but this Host is presenting"):
        controller.trust_host_key(apply=True, replace=OLD_PRINT)
    assert host_keys.recorded(path, ALIAS) == (f"ssh-ed25519 {OLD_KEY}",)

    report = controller.trust_host_key(apply=True, replace=NEW_PRINT)
    assert report["status"] == "recorded"
    assert host_keys.recorded(path, ALIAS) == (f"ssh-ed25519 {NEW_KEY}",)
