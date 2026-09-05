"""Assign one scalar in a component's settings, addressed by where it lives.

Ops renders each component's shipped settings template into the document a
product Host installs. It used to do that by replacing exact strings — the old
value had to be written out, and matched an expected number of times, so a
component that improved the line being matched broke the deployer. The
coupling was to a component's prose, which components are free to change.

Addressing the assignment by key path instead couples Ops to the *shape* of a
component's settings, which is its interface, and not to the text. A path that
no longer resolves still fails loudly — that check is the reason the old code
counted matches — but editing a comment above a value, or changing the value
itself, no longer breaks anything.

Not a YAML implementation. Ops carries these documents as exact Git objects and
has no YAML dependency at runtime, so this walks the text and rewrites one
scalar in place, leaving every other byte — comments and formatting included —
as the component wrote it. What it understands is what the settings templates
use: nested block mappings, and block sequences addressed by index.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

__all__ = ["OverlayAssignment", "SettingsOverlayError", "apply_overlay", "parse_path"]


class SettingsOverlayError(ValueError):
    """A path does not resolve, or resolves to something that is not a scalar."""


#: ``key`` or ``key[3]``. Keys are what the templates use: a leading letter or
#: underscore, then word characters, dots and dashes.
_STEP = re.compile(r"^([A-Za-z_][\w.-]*)(?:\[(\d+)\])?$")
_MAPPING_LINE = re.compile(r"^(?P<indent> *)(?P<key>[A-Za-z_][\w.-]*):(?P<rest>.*)$")
_SEQUENCE_LINE = re.compile(r"^(?P<indent> *)- (?P<rest>.*)$")


@dataclass(frozen=True, slots=True)
class OverlayAssignment:
    """One scalar to set, and the document to set it in."""

    document: str
    path: tuple[tuple[str, int | None], ...]
    value: str

    @property
    def display(self) -> str:
        return ".".join(key if index is None else f"{key}[{index}]" for key, index in self.path)


def parse_path(text: str, *, label: str) -> tuple[tuple[str, int | None], ...]:
    """``"memory.endpoints[0].mcp_url"`` into steps, or refuse."""

    if not text:
        raise SettingsOverlayError(f"{label}: path is empty")
    steps: list[tuple[str, int | None]] = []
    for raw in text.split("."):
        matched = _STEP.fullmatch(raw)
        if matched is None:
            raise SettingsOverlayError(f"{label}: path segment is invalid: {raw!r}")
        key, index = matched.group(1), matched.group(2)
        steps.append((key, None if index is None else int(index)))
    return tuple(steps)


def apply_overlay(text: str, assignment: OverlayAssignment) -> str:
    """Return ``text`` with one scalar replaced, or raise.

    The block a step owns is every following line indented deeper than the step
    itself, which is how a block mapping nests. Blank lines and comments belong
    to whatever block they sit in and are skipped rather than ended on, so a
    commented value is still reachable.
    """

    lines = text.split("\n")
    start, stop, indent = 0, len(lines), 0
    for depth, (key, index) in enumerate(assignment.path):
        last = depth == len(assignment.path) - 1
        line_no = _find_key(lines, start, stop, indent, key, assignment)
        if index is not None:
            start, stop, indent = _descend(lines, line_no, stop)
            line_no = _find_item(lines, start, stop, indent, index, assignment)
            if last:
                # Nothing addresses a bare sequence item today. Refusing beats
                # shipping a rewrite rule no template exercises.
                raise _refuse(assignment, "a path may not end at a sequence index")
            start, stop, indent = _descend_item(lines, line_no, stop)
            continue
        if last:
            return _rewrite_mapping(lines, line_no, assignment)
        start, stop, indent = _descend(lines, line_no, stop)
    raise SettingsOverlayError(f"{assignment.document}: path is empty")


def _refuse(assignment: OverlayAssignment, detail: str) -> SettingsOverlayError:
    return SettingsOverlayError(f"{assignment.document}: {assignment.display}: {detail}")


def _find_key(
    lines: list[str],
    start: int,
    stop: int,
    indent: int,
    key: str,
    assignment: OverlayAssignment,
) -> int:
    """The single line declaring ``key`` at ``indent`` in ``[start, stop)``."""

    found: list[int] = []
    for number in range(start, stop):
        matched = _MAPPING_LINE.match(lines[number])
        if matched is None or len(matched.group("indent")) != indent:
            continue
        if matched.group("key") == key:
            found.append(number)
    if not found:
        raise _refuse(assignment, f"no key {key!r} at indent {indent}")
    if len(found) > 1:
        # Two siblings of the same name is malformed YAML, and silently taking
        # the first would install whichever the component happened to write.
        raise _refuse(assignment, f"key {key!r} is declared {len(found)} times")
    return found[0]


def _find_item(
    lines: list[str],
    start: int,
    stop: int,
    indent: int,
    index: int,
    assignment: OverlayAssignment,
) -> int:
    items = [
        number
        for number in range(start, stop)
        if (matched := _SEQUENCE_LINE.match(lines[number])) is not None
        and len(matched.group("indent")) == indent
    ]
    if index >= len(items):
        raise _refuse(assignment, f"sequence has {len(items)} items, wanted [{index}]")
    return items[index]


def _block_end(
    lines: list[str], line_no: int, stop: int, indent: int, *, sequence_belongs: bool
) -> int:
    """First line after ``line_no`` that leaves the block opened at ``indent``.

    ``sequence_belongs`` is the difference between the two kinds of block. A
    key whose value is a block sequence has its items written at the key's own
    indentation, not deeper, so those lines are inside it. Inside one item,
    the same line at the same indentation is the *next* item and ends this one.
    """

    for number in range(line_no + 1, stop):
        line = lines[number]
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if len(line) - len(line.lstrip(" ")) > indent:
            continue
        # A sequence item at exactly this indentation belongs to the key; one
        # further out has left the block regardless of what it is.
        if (
            sequence_belongs
            and len(line) - len(line.lstrip(" ")) == indent
            and _SEQUENCE_LINE.match(line) is not None
        ):
            continue
        return number
    return stop


def _descend(lines: list[str], line_no: int, stop: int) -> tuple[int, int, int]:
    matched = _MAPPING_LINE.match(lines[line_no])
    assert matched is not None
    indent = len(matched.group("indent"))
    end = _block_end(lines, line_no, stop, indent, sequence_belongs=True)
    child = _first_indent(lines, line_no + 1, end, indent)
    return line_no + 1, end, child


def _descend_item(lines: list[str], line_no: int, stop: int) -> tuple[int, int, int]:
    """Inside ``- key: value``, the item's keys align after the dash."""

    matched = _SEQUENCE_LINE.match(lines[line_no])
    assert matched is not None
    indent = len(matched.group("indent"))
    end = _block_end(lines, line_no, stop, indent, sequence_belongs=False)
    return line_no, end, indent + 2


def _first_indent(lines: list[str], start: int, stop: int, parent: int) -> int:
    for number in range(start, stop):
        line = lines[number]
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        return len(line) - len(line.lstrip(" "))
    return parent + 2


def _rewrite_mapping(lines: list[str], line_no: int, assignment: OverlayAssignment) -> str:
    matched = _MAPPING_LINE.match(lines[line_no])
    assert matched is not None
    _require_scalar(matched.group("rest"), assignment)
    updated = list(lines)
    updated[line_no] = f"{matched.group('indent')}{matched.group('key')}: {assignment.value}"
    return "\n".join(updated)


def _require_scalar(rest: str, assignment: OverlayAssignment) -> None:
    """Refuse to overwrite a block with a scalar.

    ``key:`` with nothing after it opens a mapping or a sequence. Replacing that
    line with ``key: value`` would delete every child, which is a large silent
    change to make on behalf of a one-line assignment.
    """

    body = rest.split(" #", 1)[0].strip()
    if not body:
        raise _refuse(assignment, "path resolves to a block, not a scalar")
