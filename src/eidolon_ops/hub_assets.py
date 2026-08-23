"""Render Hub settings around a Host-independent Owner Domain directory.

A Mac source run and a product Host both instantiate Hub's reviewed settings
template with the stable Owner identity and the current Host candidate URI.
TLS issuance lives exclusively in ``owner_domain_assets`` so this renderer
cannot accidentally reintroduce a second trust anchor.

Which file that template is also lives here, because the same two callers need
the same answer and reading it from two places is how the last drift started.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass

from eidolon_ops.host_identity import HostLanIdentity

#: The placeholders a component template carries and Ops instantiates. They are
#: matched exactly once each: a template that stops containing them has changed
#: shape, and rendering it anyway would ship a Hub that answers to a name no
#: device asked for.
_OWNER_ID_PLACEHOLDER = "owner_domain_id: owner-local"
_OWNER_GENERATION_PLACEHOLDER = "owner_domain_generation: 1"
_DESCRIPTOR_URI_PLACEHOLDER = (
    "descriptor_uri: https://eidolon-hub.local/api/device-onboarding/v1/descriptor"
)

#: Which pinned file a Host's Hub settings are rendered from: Hub's own, and the
#: same one Hub's tests load and a local run reads. It lived in eidolon_kernel
#: until this moved, which had two costs worth not repeating — Hub could not
#: change its own deployed defaults without a release of another component, and
#: the settings Hub's suite exercised were not the settings a Host started with.
#: The two had already drifted.
HUB_SETTINGS_TEMPLATE = ("eidolon_hub", "config/settings.yaml")

class HubAssetError(ValueError):
    """Hub TLS material or rendered Hub settings are unsafe or drifted."""


@dataclass(frozen=True, slots=True)
class HubSettingsTemplate:
    """The template one release renders a Host's Hub settings from."""

    source_id: str
    revision: str
    path: str
    text: str

    @property
    def is_legacy(self) -> bool:
        return False


def template_is_renderable(template: str) -> bool:
    """Whether both lines Ops rewrites are present exactly once."""

    return all(
        template.count(placeholder) == 1
        for placeholder in (
            _OWNER_ID_PLACEHOLDER,
            _OWNER_GENERATION_PLACEHOLDER,
            _DESCRIPTOR_URI_PLACEHOLDER,
        )
    )


def hub_settings_template(
    revisions: Mapping[str, str],
    read_exact_file: Callable[[str, str, str], str],
) -> HubSettingsTemplate:
    """Resolve the Hub settings template from the exact commits a release pins.

    Hub's own copy is authoritative. The pre-move address is tried after it and
    only for the same reason it is still named at all: a release is a set of
    exact commits, and one from before the move has nothing to read at the new
    address. A release where neither address answers is refused here, before any
    of it reaches a Host, and the report names every address that was tried.
    """

    source_id, path = HUB_SETTINGS_TEMPLATE
    revision = revisions.get(source_id)
    if revision is None:
        raise HubAssetError(f"{source_id}:{path}: component is not pinned by this release")
    label = f"{source_id}@{revision[:12]}:{path}"
    try:
        text = read_exact_file(source_id, revision, path)
    except Exception as exc:
        raise HubAssetError(f"{label}: no such file at this commit") from exc
    if not template_is_renderable(text):
        raise HubAssetError(f"{label}: does not carry the two lines Ops rewrites, once each")
    return HubSettingsTemplate(source_id=source_id, revision=revision, path=path, text=text)


def render_hub_settings(
    template: str,
    owner_domain_id: str,
    owner_domain_generation: int,
    identity: HostLanIdentity,
    port: int,
) -> str:
    """Bind stable Owner identity and the current Host candidate URI."""

    rendered = _replace_once(
        template,
        _OWNER_ID_PLACEHOLDER,
        f"owner_domain_id: {owner_domain_id}",
        "Owner Domain ID",
    )
    rendered = _replace_once(
        rendered,
        _OWNER_GENERATION_PLACEHOLDER,
        f"owner_domain_generation: {owner_domain_generation}",
        "Owner Domain generation",
    )
    return _replace_once(
        rendered,
        _DESCRIPTOR_URI_PLACEHOLDER,
        "descriptor_uri: "
        + identity.hub_origin(port)
        + "/api/device-onboarding/v1/descriptor",
        "Owner Domain descriptor URI",
    )


def hub_settings_are_bound(
    settings: str,
    owner_domain_id: str,
    owner_domain_generation: int,
    identity: HostLanIdentity,
    port: int,
) -> bool:
    """Whether settings retain Owner identity while routing to this Host."""

    descriptor_uri = (
        "descriptor_uri: "
        + identity.hub_origin(port)
        + "/api/device-onboarding/v1/descriptor"
    )
    return (
        f"owner_domain_id: {owner_domain_id}" in settings
        and f"owner_domain_generation: {owner_domain_generation}" in settings
        and descriptor_uri in settings
        and _OWNER_ID_PLACEHOLDER not in settings
    )

def _replace_once(value: str, old: str, new: str, label: str) -> str:
    if value.count(old) != 1:
        raise HubAssetError(f"{label} template drifted")
    return value.replace(old, new)
