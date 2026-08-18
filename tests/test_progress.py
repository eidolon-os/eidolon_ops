"""A phase list that announces itself, and stays the list it was."""

from __future__ import annotations

import pytest

from eidolon_ops.progress import Journal


class Sink:
    def __init__(self) -> None:
        self.events: list[tuple[str, object]] = []

    def phase_began(self, phase: str) -> None:
        self.events.append(("began", phase))

    def phase_recorded(self, entry) -> None:
        self.events.append(("recorded", dict(entry)))


@pytest.mark.unit
def test_journal_without_a_sink_is_the_list_it_replaced() -> None:
    """Every existing caller and test compares this by value."""

    journal = Journal()
    journal.begin("bundle")
    journal.append({"phase": "bundle", "result": {"status": "sealed"}})
    journal.extend(
        [
            {"phase": "upload_guard", "result": {"status": "ready_for_upload"}},
            {"phase": "prepare", "result": {"status": "prepared"}},
        ]
    )

    assert journal == [
        {"phase": "bundle", "result": {"status": "sealed"}},
        {"phase": "upload_guard", "result": {"status": "ready_for_upload"}},
        {"phase": "prepare", "result": {"status": "prepared"}},
    ]
    assert [entry["phase"] for entry in journal] == ["bundle", "upload_guard", "prepare"]


@pytest.mark.unit
def test_journal_announces_a_phase_before_the_work_that_proves_it() -> None:
    sink = Sink()
    journal = Journal(sink)

    journal.begin("prepare")
    journal.append({"phase": "prepare", "result": {"status": "prepared"}})

    assert sink.events == [
        ("began", "prepare"),
        ("recorded", {"phase": "prepare", "result": {"status": "prepared"}}),
    ]


@pytest.mark.unit
def test_extend_announces_every_entry_rather_than_the_batch() -> None:
    """A batch would be the report arriving all at once, which is what this replaced."""

    sink = Sink()
    journal = Journal(sink)

    journal.extend([{"phase": "bundle"}, {"phase": "upload_guard"}])

    assert [name for kind, name in sink.events if kind == "recorded"] == [
        {"phase": "bundle"},
        {"phase": "upload_guard"},
    ]


@pytest.mark.unit
def test_the_open_phase_is_the_one_that_would_be_shown_as_failed() -> None:
    journal = Journal()
    assert journal.open_phase is None

    journal.begin("install")
    assert journal.open_phase == "install"

    journal.append({"phase": "install", "result": {}})
    assert journal.open_phase is None


@pytest.mark.unit
def test_an_entry_without_a_phase_name_is_still_recorded() -> None:
    """Announcement is a side channel; it never edits what a report says."""

    sink = Sink()
    journal = Journal(sink)

    journal.append({"result": "no phase key"})

    assert journal == [{"result": "no phase key"}]
    assert sink.events == [("recorded", {"result": "no phase key"})]


@pytest.mark.unit
def test_a_journal_can_be_seeded_with_entries_that_already_happened() -> None:
    journal = Journal(None, [{"phase": "reset_existing"}])
    journal.append({"phase": "bundle"})

    assert [entry["phase"] for entry in journal] == ["reset_existing", "bundle"]
