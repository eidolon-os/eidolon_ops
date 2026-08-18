"""The Host-bound Hub assets, derived once for whichever Host asks.

A Mac source run and a product Host need the same two things: a TLS identity
that matches the Host identity, and a Hub settings file that names this Host
rather than the template's placeholder. Both existed twice, and the two copies
had already diverged on when material is replaced and on how a drifted template
is reported.

Which file that template is also lives here, because the same two callers need
the same answer and reading it from two places is how the last drift started.
"""

from __future__ import annotations

import os
import stat
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

from eidolon_ops.host_identity import (
    HostIdentityError,
    HostLanIdentity,
    generate_hub_tls_identity,
    validate_hub_tls_identity,
)
from eidolon_ops.private_files import atomic_private_file

#: The placeholders a component template carries and Ops instantiates. They are
#: matched exactly once each: a template that stops containing them has changed
#: shape, and rendering it anyway would ship a Hub that answers to a name no
#: device asked for.
_HUB_ID_PLACEHOLDER = "hub_id: eidolon-hub-local"
_HUB_ORIGIN_PLACEHOLDER = "public_base_url: https://eidolon-hub.local"

#: Which pinned file a Host's Hub settings are rendered from: Hub's own, and the
#: same one Hub's tests load and a local run reads. It lived in eidolon_kernel
#: until this moved, which had two costs worth not repeating — Hub could not
#: change its own deployed defaults without a release of another component, and
#: the settings Hub's suite exercised were not the settings a Host started with.
#: The two had already drifted.
HUB_SETTINGS_TEMPLATE = ("eidolon_hub", "config/settings.yaml")

#: The same file at the address it had before it moved. A release pins every
#: component at an exact commit, so deploying or refreshing a release from
#: before the move still has to work: this is read only when the pinned Hub
#: carries nothing renderable, and which one answered is recorded in the release
#: evidence rather than left to be inferred.
LEGACY_HUB_SETTINGS_TEMPLATE = ("eidolon_kernel", "config/hub.systemd.example.yaml")


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
        return (self.source_id, self.path) == LEGACY_HUB_SETTINGS_TEMPLATE


def template_is_renderable(template: str) -> bool:
    """Whether both lines Ops rewrites are present exactly once."""

    return all(
        template.count(placeholder) == 1
        for placeholder in (_HUB_ID_PLACEHOLDER, _HUB_ORIGIN_PLACEHOLDER)
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

    rejected: list[str] = []
    for source_id, path in (HUB_SETTINGS_TEMPLATE, LEGACY_HUB_SETTINGS_TEMPLATE):
        revision = revisions.get(source_id)
        if revision is None:
            rejected.append(f"{source_id}:{path}: component is not pinned by this release")
            continue
        label = f"{source_id}@{revision[:12]}:{path}"
        try:
            text = read_exact_file(source_id, revision, path)
        except Exception:
            # The address is the diagnosis here; the reader's stderr is not, and
            # the systemd gate beside this one is careful not to repeat it.
            rejected.append(f"{label}: no such file at this commit")
            continue
        if not template_is_renderable(text):
            rejected.append(f"{label}: does not carry the two lines Ops rewrites, once each")
            continue
        return HubSettingsTemplate(
            source_id=source_id, revision=revision, path=path, text=text
        )
    raise HubAssetError(
        "no pinned Hub settings template can be rendered for this Host:\n"
        + "\n".join(rejected)
    )


def render_hub_settings(template: str, identity: HostLanIdentity, port: int) -> str:
    """Instantiate the Hub settings template for one Host."""

    rendered = _replace_once(
        template,
        _HUB_ID_PLACEHOLDER,
        f"hub_id: {identity.hub_id}",
        "Hub ID",
    )
    return _replace_once(
        rendered,
        _HUB_ORIGIN_PLACEHOLDER,
        f"public_base_url: {identity.hub_origin(port)}",
        "Hub public base URL",
    )


def hub_settings_are_bound(settings: str, identity: HostLanIdentity, port: int) -> bool:
    """Whether rendered settings name this Host and not the template."""

    return (
        f"hub_id: {identity.hub_id}" in settings
        and f"public_base_url: {identity.hub_origin(port)}" in settings
        and _HUB_ID_PLACEHOLDER not in settings
    )


def ensure_hub_tls_identity(
    certificate_path: Path,
    private_key_path: Path,
    identity: HostLanIdentity,
    *,
    mode: int = 0o600,
    rotate_on_mismatch: bool = False,
) -> tuple[bytes, bytes]:
    """Return the Hub TLS pair for ``identity``, creating it if it is absent.

    ``rotate_on_mismatch`` is the one deliberate difference between a source
    run and a product Host: a workstation profile re-derives its material when
    the Host identity underneath it changes, while a product Host refuses,
    because there the mismatch means material was replaced and an operator has
    to decide what happened rather than have it quietly rewritten.
    """

    existing = (certificate_path.is_file(), private_key_path.is_file())
    if any(existing) and not all(existing):
        raise HubAssetError("Hub TLS identity is incomplete")
    if all(existing):
        for path in (certificate_path, private_key_path):
            if path.is_symlink() or not path.is_file():
                raise HubAssetError("Hub TLS identity is unsafe")
            if stat.S_IMODE(path.stat().st_mode) != mode:
                os.chmod(path, mode)
        certificate = certificate_path.read_bytes()
        private_key = private_key_path.read_bytes()
        try:
            validate_hub_tls_identity(certificate, private_key, identity)
        except HostIdentityError as exc:
            if not rotate_on_mismatch:
                raise HubAssetError(
                    "existing Hub TLS identity does not match the Host identity"
                ) from exc
        else:
            return certificate, private_key
    certificate, private_key = generate_hub_tls_identity(identity)
    # Prove the new pair before either half of it lands, so a failure cannot
    # leave a Host holding one file from this attempt and one from the last.
    try:
        validate_hub_tls_identity(certificate, private_key, identity)
    except HostIdentityError as exc:
        raise HubAssetError("generated Hub TLS identity is invalid") from exc
    atomic_private_file(certificate_path, certificate, mode=mode)
    try:
        atomic_private_file(private_key_path, private_key, mode=mode)
    except OSError:
        certificate_path.unlink(missing_ok=True)
        raise
    return certificate, private_key


def _replace_once(value: str, old: str, new: str, label: str) -> str:
    if value.count(old) != 1:
        raise HubAssetError(f"{label} template drifted")
    return value.replace(old, new)
