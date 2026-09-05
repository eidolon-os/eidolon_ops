from __future__ import annotations

import pytest
import yaml

from eidolon_ops.settings_overlay import (
    OverlayAssignment,
    SettingsOverlayError,
    apply_overlay,
    parse_path,
)

#: Shaped like the pinned templates: nested mappings, a block sequence whose
#: items sit at the key's own indentation, comments between values, and the
#: same leaf name appearing under more than one parent.
TEMPLATE = """\
env: dev
memory:
  discovery_url: http://127.0.0.1:8020/api/discovery
  # Empty until the Host supplies one.
  discovery_token_env: ''
  endpoints:
  - memory_space_id: default.default.default
    mcp_url: http://127.0.0.1:8030/mcp
  - memory_space_id: second.second.second
    mcp_url: http://127.0.0.1:8031/mcp
  recall_timeout_s: 0.5
avatar:
  enabled: true
providers:
  stt_provider: bailian
  tts_provider: bailian
observability:
  enabled: false
  log_level: DEBUG
"""


def _set(path: str, value: str, text: str = TEMPLATE) -> str:
    return apply_overlay(
        text, OverlayAssignment("agent.yaml", parse_path(path, label="agent.yaml"), value)
    )


def _changed_lines(before: str, after: str) -> list[tuple[str, str]]:
    return [
        (old, new)
        for old, new in zip(before.split("\n"), after.split("\n"), strict=True)
        if old != new
    ]


@pytest.mark.parametrize(
    ("path", "value", "expected"),
    [
        ("env", "prod", "env: prod"),
        ("observability.log_level", "INFO", "  log_level: INFO"),
        ("memory.discovery_token_env", "TOKEN", "  discovery_token_env: TOKEN"),
        ("providers.stt_provider", "eidolon_models", "  stt_provider: eidolon_models"),
        ("memory.endpoints[0].mcp_url", "http://x", "    mcp_url: http://x"),
        ("memory.endpoints[1].mcp_url", "http://y", "    mcp_url: http://y"),
    ],
)
def test_one_line_changes_and_it_is_the_addressed_one(path, value, expected) -> None:
    result = _set(path, value)
    assert _changed_lines(TEMPLATE, result) == [
        (TEMPLATE.split("\n")[result.split("\n").index(expected)], expected)
    ]


def test_the_document_still_parses_and_carries_the_new_value() -> None:
    """The overlay edits text; this is what says the text was still YAML."""

    result = _set("providers.tts_provider", "eidolon_models")
    parsed = yaml.safe_load(result)
    assert parsed["providers"]["tts_provider"] == "eidolon_models"
    original = yaml.safe_load(TEMPLATE)
    original["providers"]["tts_provider"] = "eidolon_models"
    assert parsed == original


def test_a_sibling_of_the_same_name_under_another_parent_is_untouched() -> None:
    """`enabled` exists under avatar and under observability."""

    result = yaml.safe_load(_set("avatar.enabled", "false"))
    assert result["avatar"]["enabled"] is False
    assert result["observability"]["enabled"] is False  # was already false
    assert yaml.safe_load(_set("observability.enabled", "true"))["avatar"]["enabled"] is True


def test_comments_and_blank_lines_survive() -> None:
    result = _set("memory.discovery_token_env", "TOKEN")
    assert "  # Empty until the Host supplies one." in result


@pytest.mark.parametrize(
    ("path", "reason"),
    [
        ("nope", "no key 'nope'"),
        ("memory.nope", "no key 'nope'"),
        ("memory", "not a scalar"),
        ("memory.endpoints[9].mcp_url", r"wanted \[9\]"),
        ("memory.endpoints[0]", "may not end at a sequence index"),
    ],
)
def test_a_path_that_does_not_resolve_is_refused(path, reason) -> None:
    with pytest.raises(SettingsOverlayError, match=reason):
        _set(path, "x")


def test_two_siblings_of_one_name_are_refused_rather_than_guessed() -> None:
    malformed = "a:\n  b: 1\n  b: 2\n"
    with pytest.raises(SettingsOverlayError, match="declared 2 times"):
        _set("a.b", "3", malformed)


@pytest.mark.parametrize("path", ["", "1bad", "a..b", "a[x]", "a[]"])
def test_a_malformed_path_is_refused_while_reading_it(path) -> None:
    with pytest.raises(SettingsOverlayError):
        parse_path(path, label="agent.yaml")


def test_the_value_is_written_verbatim() -> None:
    """Ops decides the rendering; the overlay does not quote or coerce."""

    assert '  stt_provider: "quoted"' in _set("providers.stt_provider", '"quoted"')


def test_a_sequence_item_further_out_has_left_the_block() -> None:
    """Same-indent items belong to the key; shallower ones ended it."""

    text = "outer:\n  inner:\n    x: 1\n- stray: 2\nafter:\n  x: 3\n"
    with pytest.raises(SettingsOverlayError, match="no key 'stray'"):
        apply_overlay(
            text,
            OverlayAssignment("a.yaml", parse_path("outer.inner.stray", label="a.yaml"), "9"),
        )
