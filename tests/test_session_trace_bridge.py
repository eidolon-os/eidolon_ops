from eidolon_ops.source_assets import PORTS, admin_services_yaml

import yaml


def test_source_host_trace_bridge_has_the_provider_address_and_credential():
    services = yaml.safe_load(admin_services_yaml())["services"]
    provider = next(s for s in services if s["id"] == "channel-provider")
    assert provider["integration"] == "proxy"
    assert provider["base_url"] == f"http://127.0.0.1:{PORTS['channel_provider']}"
    assert provider["auth"] == {"type": "bearer", "token_env": "EIDOLON_CHANNEL_PROVIDER_TOKEN"}
