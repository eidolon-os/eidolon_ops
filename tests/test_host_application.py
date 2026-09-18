from __future__ import annotations

import os
from ipaddress import IPv4Address

import pytest

from eidolon_ops.environment import EnvironmentFileError
from eidolon_ops.host_application import (
    HostApplicationError,
    HostApplicationMaterializer,
)
from eidolon_ops.hub_assets import HubAssetError
from eidolon_ops.install_inputs import (
    HOST_RENDERED_ENV_KEYS,
    InstallInputError,
    host_rendered_fields,
)
from eidolon_ops.owner_domain_assets import HostAuthority
from eidolon_ops.paths import AppAccess

#: A Host holding nothing, with the state id fixed so two renders compare equal.
FRESH = HostAuthority(owner_domain_generation=1, state_id="authority-state_test", established=False)

HUB_TEMPLATE = """\
onboarding:
  owner_domain_id: owner-local
  owner_domain_generation: 1
  trust_epoch: 1
  descriptor_uri: https://eidolon-hub.local/api/device-onboarding/v1/descriptor
  descriptor_path: /etc/eidolon/owner-domain/owner_domain_descriptor.json
  owner_root_certificate_path: /etc/eidolon/owner-domain/owner_domain_root_ca.pem
  authority_signing_certificate_path: /etc/eidolon/owner-domain/authority_signing_certificate.pem
  retrieval_window_seconds: 1800
discovery:
  mdns:
    enabled: true
channel_provider:
  contract_url: http://127.0.0.1:8767/v1
persistence:
  path: /var/lib/eidolon/hub/eidolon-hub.sqlite3
"""


def _app(address: str) -> AppAccess:
    return AppAccess(
        lan_ipv4=IPv4Address(address),
        hub_https_port=8443,
        allow_insecure_livekit=True,
    )


def test_pi_application_assets_are_stable_and_owner_scoped(config) -> None:
    identity_path = config.install_files["host_identity"]
    identity_path.write_bytes(b"a" * 32)
    identity_path.chmod(0o600)
    materializer = HostApplicationMaterializer(config, _app("192.168.100.15"), b"runtime", host_authority=FRESH)

    first = materializer.prepare(HUB_TEMPLATE)
    second = materializer.prepare(HUB_TEMPLATE)

    assert first.identity == second.identity
    assert first.files["hub.crt"] == second.files["hub.crt"]
    assert first.files["hub.key"] == second.files["hub.key"]
    settings = first.files["hub.generated.yaml"].decode()
    assert f"owner_domain_id: {first.owner_domain_id}" in settings
    assert f"descriptor_uri: {first.identity.hub_origin(8443)}/api/device-onboarding/v1/descriptor" in settings
    assert "owner_domain_id: owner-local" not in settings
    assert "path: /var/lib/eidolon/hub/eidolon-hub.sqlite3" in settings
    assert b"--listen-port 8443" in first.files["hub-ingress.service"]
    assert b"/etc/eidolon/generated/hub.yaml" in first.files["hub-service-override.conf"]
    assert "owner-domain-root.key.pem" not in first.files
    assert "authority-signing.key.pem" not in first.files
    # No per-device file is installed at all now, and no settings stanza turns
    # one on: the Host signs commissioning vouchers with the management secret
    # it already holds, so there is nothing here to drift from a release.
    assert "commissioning-secrets.json" not in first.files
    assert "setup_secret_registry_path" not in settings


def test_host_replacement_keeps_owner_contract_and_changes_observation(config) -> None:
    identity_path = config.install_files["host_identity"]
    identity_path.write_bytes(b"a" * 32)
    identity_path.chmod(0o600)
    first = HostApplicationMaterializer(config, _app("192.168.100.15"), b"runtime", host_authority=FRESH)
    first_contract = first.public_contract()

    identity_path.write_bytes(b"b" * 32)
    second = HostApplicationMaterializer(config, _app("192.168.100.16"), b"runtime")
    second_contract = second.public_contract()

    assert first_contract["host_id"] != second_contract["host_id"]
    assert first_contract["owner_domain_id"] == second_contract["owner_domain_id"]
    assert first_contract["hub_hostname"] != second_contract["hub_hostname"]


def test_pi_environment_targets_the_same_host_bound_hub(config) -> None:
    identity_path = config.install_files["host_identity"]
    identity_path.write_bytes(b"a" * 32)
    identity_path.chmod(0o600)
    materializer = HostApplicationMaterializer(config, _app("192.168.100.15"), b"runtime", host_authority=FRESH)
    owner = materializer.prepare(HUB_TEMPLATE)
    local_api = materializer.render_environment(
        "local-api.env",
        "EIDOLON_LOCAL_API_ADMIN_BASE_URL=http://127.0.0.1:9000\n"
        "EIDOLON_LOCAL_API_ADMIN_SERVICE_TOKEN=test-token\n",
    )
    # The seed carries credentials and nothing else. Both Host-bound fields
    # below are added by the render, not overwritten in it: an input set that
    # stated where devices reach LiveKit would be answering a question only a
    # binding can answer.
    channel = materializer.render_environment(
        "channel.env",
        "PAIRING_JWT_SECRET=test\n",
    )

    assert f"EIDOLON_LOCAL_API_OWNER_DOMAIN_ID={owner.owner_domain_id}" in local_api
    assert owner.identity.hub_origin(8443) in local_api
    assert "EIDOLON_LOCAL_API_OWNER_DOMAIN_DESCRIPTOR=/etc/eidolon/owner-domain/owner_domain_descriptor.json" in local_api
    assert "EIDOLON_LIVEKIT_CLIENT_URL=ws://192.168.100.15:7880" in channel
    assert "EIDOLON_CHANNEL_PROVIDER_ALLOW_INSECURE_LAN_CLIENT_URL=1" in channel


def test_host_application_rejects_unsafe_or_drifting_material(config) -> None:
    identity_path = config.install_files["host_identity"]
    identity_path.write_bytes(b"a" * 32)
    identity_path.chmod(0o600)
    materializer = HostApplicationMaterializer(config, _app("192.168.100.15"), b"runtime", host_authority=FRESH)
    assert materializer.render_environment("data.env", "KEY=value\n") == "KEY=value\n"
    with pytest.raises(EnvironmentFileError, match="Host application environment is invalid"):
        materializer.render_environment("local-api.env", "invalid")
    with pytest.raises(HubAssetError, match="template drifted"):
        materializer.prepare(
            HUB_TEMPLATE.replace("owner_domain_id: owner-local", "owner_domain_id: bad")
        )

    first = materializer.prepare(HUB_TEMPLATE)
    identity_path.write_bytes(b"b" * 32)
    second = materializer.prepare(HUB_TEMPLATE)
    assert second.owner_domain_id == first.owner_domain_id
    assert second.identity != first.identity
    assert second.files["hub.crt"] != first.files["hub.crt"]


def test_host_application_rejects_incomplete_and_unsafe_paths(config) -> None:
    identity_path = config.install_files["host_identity"]
    identity_path.write_bytes(b"a" * 32)
    identity_path.chmod(0o600)
    materializer = HostApplicationMaterializer(config, _app("192.168.100.15"), b"runtime", host_authority=FRESH)
    root = materializer.material_root
    root.mkdir(mode=0o700)
    (root / "hub.crt").write_text("partial", encoding="utf-8")
    (root / "hub.crt").chmod(0o600)
    with pytest.raises(HostApplicationError, match="incomplete"):
        materializer.prepare(HUB_TEMPLATE)

    (root / "hub.crt").unlink()
    os.chmod(root, 0o755)
    with pytest.raises(HostApplicationError, match="directory is unsafe"):
        materializer.prepare(HUB_TEMPLATE)

    os.chmod(root, 0o700)
    (root / "unexpected").write_text("drift", encoding="utf-8")
    with pytest.raises(HostApplicationError, match="extra files"):
        materializer.prepare(HUB_TEMPLATE)
    (root / "unexpected").unlink()

    identity_path.unlink()
    identity_path.symlink_to(root / "missing")
    with pytest.raises(HostApplicationError, match="identity input"):
        materializer.identity()


def test_starting_the_hub_opens_the_lan_with_it(config) -> None:
    """PartOf carries stop and restart downward, never start.

    Activation stopped the Hub, took the ingress down with it, started the Hub
    again and left port 8443 closed. The App gate read that as a refused
    connection and rolled the whole release back, so an update could never
    finish. The two directives have to be stated as a pair.
    """

    identity_path = config.install_files["host_identity"]
    identity_path.write_bytes(b"a" * 32)
    identity_path.chmod(0o600)
    materializer = HostApplicationMaterializer(config, _app("192.168.100.15"), b"runtime", host_authority=FRESH)

    assets = materializer.prepare(HUB_TEMPLATE)

    override = assets.files["hub-service-override.conf"].decode()
    ingress = assets.files["hub-ingress.service"].decode()
    assert "Wants=eidolon-hub-ingress.service" in override
    assert "PartOf=eidolon-hub.service" in ingress
    # Wants= carries no ordering, and the ingress orders itself after the Hub,
    # so the pair cannot deadlock systemd.
    assert "After=network-online.target eidolon-hub.service" in ingress


def test_a_renderer_cannot_write_a_field_the_table_does_not_declare() -> None:
    """The declaration is checked, not merely written down.

    ``EIDOLON_CHANNEL_PROVIDER_ALLOW_INSECURE_LAN_CLIENT_URL`` reached every
    Host this product has ever had while appearing in no declaration anywhere in
    Ops; the only way to learn a Host had it was to read the file on the Host.
    Both renderers pass their replacements through here, so the next such field
    fails where it is added rather than arriving unannounced.
    """

    declared = HOST_RENDERED_ENV_KEYS["channel.env"]
    assert host_rendered_fields("channel.env", dict.fromkeys(declared, "x")) == dict.fromkeys(
        declared, "x"
    )
    with pytest.raises(InstallInputError, match="EIDOLON_CHANNEL_PROVIDER_NEW_FIELD"):
        host_rendered_fields(
            "channel.env",
            {**dict.fromkeys(declared, "x"), "EIDOLON_CHANNEL_PROVIDER_NEW_FIELD": "1"},
        )
    # And a field dropped from a renderer is caught the same way: a Host would
    # otherwise keep whatever the last release wrote.
    with pytest.raises(InstallInputError, match="EIDOLON_LIVEKIT_CLIENT_URL"):
        host_rendered_fields(
            "channel.env",
            {"EIDOLON_CHANNEL_PROVIDER_ALLOW_INSECURE_LAN_CLIENT_URL": "1"},
        )
