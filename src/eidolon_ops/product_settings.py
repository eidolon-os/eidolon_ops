"""Instantiate the pinned component settings for a product Host.

Each assignment names where a value lives rather than what it currently says.
The path must resolve, so a component that removes or renames the setting still
breaks the build here, where it is visible — that check is why the previous
implementation counted string matches. What no longer breaks the build is a
component editing the comment above a value, or changing the value itself.

Two overlays are applied in order. The first is what being a product means and
is the same on every Host. The second comes from the Host's own operations
config, and is how two Hosts built from identical commits can differ — one
reaching a cloud provider for speech, another running the models itself.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from eidolon_ops.component_artifacts import capability_artifact, host_artifact_root
from eidolon_ops.config import OperationsConfig
from eidolon_ops.errors import InstallInputError
from eidolon_ops.settings_overlay import (
    OverlayAssignment,
    SettingsOverlayError,
    apply_overlay,
    parse_path,
)

#: The documents Ops renders, and the component each is read from.
SETTINGS_DOCUMENTS: tuple[tuple[str, str], ...] = (
    ("agent.yaml", "eidolon_agent"),
    ("channel.yaml", "eidolon_channel"),
    ("memory.yaml", "eidolon_memory"),
)

#: What every product Host gets. Not a Host's choice: a Host that wanted any of
#: these differently would not be a product Host.
PRODUCT_OVERLAY: tuple[tuple[str, str, str], ...] = (
    ("agent.yaml", "env", "prod"),
    # The Agent's own template debugs itself: at DEBUG it logs every sqlite
    # cursor close and rollback, which measured 52 KB/s on an *idle* Host —
    # 4.5 GB/day, onto a Pi's SD card. `env: prod` above is the same decision
    # about the same file; a product Host's log level belongs beside it rather
    # than left at whatever value happens to help while developing the Agent.
    ("agent.yaml", "observability.log_level", "INFO"),
    ("agent.yaml", "memory.endpoints[0].mcp_url", "http://127.0.0.1:10030/mcp"),
    ("agent.yaml", "memory.discovery_token_env", "EIDOLON_MEMORY_MCP_TOKEN"),
    ("channel.yaml", "avatar.enabled", "false"),
)

# Memory needs no product overlay: its settings resolve paths from the Host path
# contract this deployer already exports, and the Host's encoder is chosen
# through EIDOLON_MEMORY_EMBEDDING_MODEL in memory.env.


def product_settings(
    config: OperationsConfig,
    read_exact_file: Callable[[str, str, str], str],
) -> dict[str, str]:
    documents = {
        name: read_exact_file(source, config.sources[source].revision, "config/settings.yaml")
        for name, source in SETTINGS_DOCUMENTS
    }
    speaker = capability_artifact(
        config,
        read_exact_file,
        component_id="eidolon_models",
        capability="local_speaker_verification",
    )
    model_path = ()
    if speaker is not None:
        if any(
            a.document == "channel.yaml" and a.display == "voiceprint.model_dir"
            for a in config.settings_overlay
        ):
            raise InstallInputError(
                "voiceprint.model_dir is derived from the Models artifact, not a Host overlay"
            )
        for assignment in config.settings_overlay:
            if (
                assignment.document == "channel.yaml"
                and assignment.display == "voiceprint.calibration_path"
                and assignment.value
                and not Path(assignment.value).is_absolute()
                and assignment.value not in {file.path for file in speaker.files}
            ):
                raise InstallInputError(
                    "relative voiceprint.calibration_path must be a file in the selected Models artifact"
                )
        model_path = (
            OverlayAssignment(
                "channel.yaml",
                parse_path("voiceprint.model_dir", label="model asset"),
                str(host_artifact_root(speaker)),
            ),
        )
    assignments = (
        *(
            OverlayAssignment(document, parse_path(path, label=document), value)
            for document, path, value in PRODUCT_OVERLAY
        ),
        *config.settings_overlay,
        *model_path,
    )
    sources = dict(SETTINGS_DOCUMENTS)
    for assignment in assignments:
        if assignment.document == "hub.yaml":
            # Hub's settings are rendered from Hub's own template by hub_assets,
            # which applies these assignments itself.
            continue
        if assignment.document not in documents:
            raise InstallInputError(
                f"settings overlay names an unrendered document: {assignment.document}"
            )
        try:
            documents[assignment.document] = _apply(documents[assignment.document], assignment)
        except InstallInputError as exc:
            source = sources[assignment.document]
            revision = config.sources[source].revision
            raise InstallInputError(
                f"{exc}; template={source}@{revision}:config/settings.yaml. "
                "Update the component template and select its Git revision; "
                "uncommitted files and settings.example.yaml are not deployment inputs."
            ) from exc
    return documents


def _apply(text: str, assignment: OverlayAssignment) -> str:
    try:
        return apply_overlay(text, assignment)
    except SettingsOverlayError as exc:
        raise InstallInputError(f"pinned settings template drifted: {exc}") from exc
