import json

from eidolon_ops import lan_observation
from eidolon_ops.process import ProcessResult


class LinuxNetwork:
    def __init__(self, routes=None):
        self.routes = routes if routes is not None else [{"dev": "wlan0"}]
        self.calls = []

    def run(self, command, **kwargs):
        self.calls.append(command)
        if command[0] == "getent":
            return ProcessResult(0, "192.168.1.37 STREAM host.local\n192.168.1.37 DGRAM\n", "")
        if "route" in command:
            return ProcessResult(0, json.dumps(self.routes), "")
        entries = [
            {"ifname": "lo", "addr_info": [{"family": "inet", "local": "127.0.0.1"}]},
            {"ifname": "wlan0", "addr_info": [{"family": "inet", "local": "192.168.1.37"}]},
            {"ifname": "usb0", "addr_info": [{"family": "inet", "local": "10.42.0.2"}]},
        ]
        if "dev" in command:
            entries = [entry for entry in entries if entry["ifname"] == command[-1]]
        return ProcessResult(0, json.dumps(entries), "")


def test_linux_observes_current_default_interface_and_resolver(monkeypatch):
    monkeypatch.setattr(lan_observation.platform, "system", lambda: "Linux")
    runner = LinuxNetwork()
    addresses = lan_observation.interface_addresses(runner)
    assert addresses == {"127.0.0.1", "192.168.1.37", "10.42.0.2"}
    assert lan_observation.observed_lan_address(runner, addresses) == "192.168.1.37"
    assert lan_observation.name_resolves_to(runner, "host.local", "192.168.1.37")
    assert not lan_observation.name_resolves_to(runner, "host.local", "10.42.0.2")
    assert all(command[0] in {"ip", "getent"} for command in runner.calls)


def test_linux_does_not_guess_between_multiple_default_interfaces(monkeypatch):
    monkeypatch.setattr(lan_observation.platform, "system", lambda: "Linux")
    runner = LinuxNetwork(routes=[{"dev": "wlan0"}, {"dev": "usb0"}])
    assert lan_observation.observed_lan_address(runner, {"192.168.1.37", "10.42.0.2"}) == ""
