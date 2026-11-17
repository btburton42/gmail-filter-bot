"""Tests for the TriageState model: decisions, queues, undo, panel sections.

No Textual import happens in the module under test, so none happens here.
"""

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from gmail_filter_bot.config import Config, Credentials, FilterConfig
from gmail_filter_bot.filter_manager import FilterManager
from gmail_filter_bot.tui.controller import TriageController
from gmail_filter_bot.tui.sessions import AUDIT, LABEL, Item, TriageSession
from gmail_filter_bot.tui.state import (
    SPAM_FILTER,
    SpamItem,
    TriageState,
)


@pytest.fixture(autouse=True)
def isolated_settings(monkeypatch, tmp_path):
    """Keep `touch_filter_access` out of the real user config directory."""
    from gmail_filter_bot import settings

    monkeypatch.setattr(settings, "user_config_path", lambda: tmp_path / "cfg.yaml")


@pytest.fixture
def config():
    return Config(
        max_entries_per_filter=50,
        filters={
            "work": FilterConfig("work", "label_and_archive", "Work", ["a@x.com"]),
            "newsletters": FilterConfig(
                "newsletters", "label_only", "Newsletters", ["n@x.com"]
            ),
        },
        credentials=Credentials(client_id="x", client_secret="y"),
        allowed=["friend@x.com"],
    )


@pytest.fixture
def state(config):
    return TriageState(config=config)


@pytest.fixture
def session(config, state, tmp_path):
    """A session over two senders, for the panel-op tests."""
    client = MagicMock()
    client.report_spam.return_value = 1
    client.get_unsubscribe_targets.return_value = None
    manager = FilterManager(config, client, tmp_path / "filters.yaml")
    controller = TriageController(manager, state)
    items = [
        Item(email="b@x.com", name="Bob", messages=(("hi", "m1"),)),
        Item(email="c@x.com", name="Carol", messages=(("yo", "m2"),)),
    ]
    return TriageSession(manager, state, controller, kind=LABEL, items=items)


class TestFilter:
    def test_adds_the_entry_and_records_the_change(self, state, config):
        state.add_filter("new@x.com", "work")
        assert "new@x.com" in config.filters["work"].entries
        assert state.changes == {"work": ["new@x.com"]}
        assert state.counters["filtered"] == 1

    def test_creates_an_unknown_filter(self, state, config):
        state.add_filter("new@x.com", "brand-new")
        assert config.filters["brand-new"].entries == ["new@x.com"]

    def test_adding_twice_does_not_duplicate(self, state, config):
        state.add_filter("new@x.com", "work")
        state.add_filter("new@x.com", "work")
        assert config.filters["work"].entries.count("new@x.com") == 1
        assert state.changes["work"] == ["new@x.com"]
        # Both decisions are still tallied: the user did press the key twice.
        assert state.counters["filtered"] == 2


class TestUndo:
    def test_undoing_a_filter_entry(self, state, config):
        record = state.add_filter("new@x.com", "work")
        state.record(0, record)
        index, undone = state.undo()

        assert index == 0
        assert undone["action"] == "filter"
        assert "new@x.com" not in config.filters["work"].entries
        assert state.changes == {}
        assert state.counters["filtered"] == 0

    def test_undo_removes_a_filter_it_created(self, state, config):
        record = state.add_filter("new@x.com", "brand-new")
        state.record(0, record)
        state.undo()
        assert "brand-new" not in config.filters

    def test_undo_keeps_a_pre_existing_filter(self, state, config):
        record = state.add_filter("new@x.com", "work")
        state.record(0, record)
        state.undo()
        assert "work" in config.filters
        assert config.filters["work"].entries == ["a@x.com"]

    def test_undoing_allow(self, state, config):
        record = state.allow("spammy@x.com")
        state.record(0, record)
        state.undo()
        assert "spammy@x.com" not in config.allowed
        assert state.allowed_added == []
        assert state.counters["allowed"] == 0

    def test_undoing_a_skip_only_decrements(self, state):
        record = state.skip()
        state.record(0, record)
        state.undo()
        assert state.counters["skipped"] == 0

    def test_empty_history(self, state):
        assert state.undo() is None


class TestSpamQueue:
    def test_queues_without_touching_the_network(self, state, config):
        record = state.queue_spam("bad@x.com", "m1")
        assert [item.email for item in state.spam_queue] == ["bad@x.com"]
        assert record["created_spam"] is True
        # The local filter is the visible half; the report is deferred.
        assert "bad@x.com" in config.filters[SPAM_FILTER].entries
        assert state.counters["spammed"] == 1

    def test_spam_filter_is_not_recreated(self, state, config):
        state.queue_spam("one@x.com")
        record = state.queue_spam("two@x.com")
        assert record["created_spam"] is False
        assert config.filters[SPAM_FILTER].entries == ["one@x.com", "two@x.com"]

    def test_undo_drops_the_queue_item_and_the_filter(self, state, config):
        record = state.queue_spam("bad@x.com", "m1")
        state.record(0, record)
        state.undo()
        assert state.spam_queue == []
        assert SPAM_FILTER not in config.filters
        assert state.counters["spammed"] == 0

    def test_undo_only_removes_its_own_item(self, state):
        first = state.queue_spam("one@x.com")
        state.record(0, first)
        state.queue_spam("two@x.com")
        state.undo()
        assert [item.email for item in state.spam_queue] == ["two@x.com"]

    def test_drop_queued_removes_the_last_match(self, state):
        state.spam_queue[:] = [SpamItem("a@x.com"), SpamItem("b@x.com")]
        state.drop_queued(state.spam_queue, "a@x.com")
        assert [item.email for item in state.spam_queue] == ["b@x.com"]


class TestUnsubQueue:
    def test_pending_item_is_described_as_queued(self, state):
        state.queue_unsubscribe("list@x.com", "m1")
        assert state.unsub_queue[0].describe() == "queued"
        assert state.unsub_queue[0].glyph == "\u00b7"

    def test_status_and_detail_come_from_the_header_read(self, state):
        state.queue_unsubscribe(
            "list@x.com", "m1", status="one_click", detail="lists.example"
        )
        item = state.unsub_queue[0]
        assert item.describe() == "lists.example"
        assert item.glyph == "\u2713"

    def test_undo_reverses_an_unsubscribe(self, state):
        record = state.queue_unsubscribe("list@x.com", "m1")
        state.record(0, record)
        state.undo()
        assert state.unsub_queue == []
        assert state.counters["unsubscribed"] == 0


class TestCounters:
    def test_pending_count_sums_both_queues(self, state):
        state.queue_spam("bad@x.com")
        state.queue_unsubscribe("list@x.com", "m1")
        assert state.pending_count == 2

    def test_changed_anything_tracks_real_changes(self, state):
        assert state.changed_anything is False
        state.skip()
        assert state.changed_anything is False  # a skip changes nothing
        state.allow("new@x.com")
        assert state.changed_anything is True

    def test_decrement_never_goes_negative(self, state):
        state.decrement("spammed")
        assert state.counters["spammed"] == 0


class TestSections:
    def test_label_panel_groups_decisions(self, state):
        state.add_filter("new@x.com", "work")
        state.queue_spam("bad@x.com")
        state.queue_unsubscribe("list@x.com", "m1", status="one_click", detail="h")
        state.allow("ok@x.com")

        sections = {section.title: section for section in state.sections()}
        assert list(sections) == ["FILTERS", "SPAM", "UNSUBSCRIBE", "ALLOWED"]
        # "spam" is a filter too, so it shows up here as well as in its own
        # section -- the panel is honest about where the entry went.
        assert sections["FILTERS"].heading == "FILTERS 2"
        assert [node.label for node in sections["FILTERS"].nodes] == ["spam", "work"]
        work = sections["FILTERS"].nodes[1]
        assert work.detail == "+1"
        assert work.children[0].label == "new@x.com"
        assert sections["SPAM"].nodes[0].label == "bad@x.com"
        assert sections["UNSUBSCRIBE"].nodes[0].detail.startswith("\u2713")
        assert sections["ALLOWED"].nodes[0].label == "ok@x.com"

    def test_audit_panel_shows_moves_and_removals(self, state):
        state.record_move("a@x.com", "newsletters", "work")
        state.record_removal("n@x.com", "newsletters")
        sections = {section.title: section for section in state.sections(audit=True)}

        assert "FILTERS" not in sections
        assert sections["MOVED"].nodes[0].detail == "to work"
        assert sections["REMOVED"].nodes[0].detail == "from newsletters"

    def test_rows_are_self_describing(self, state):
        """Every panel row carries a kind the session can dispatch on."""
        state.add_filter("new@x.com", "work")
        state.queue_spam("bad@x.com")
        state.queue_unsubscribe("list@x.com", "m1")
        state.allow("ok@x.com")

        kinds = set()
        for section in state.sections():
            for node in section.nodes:
                kinds.add(node.kind)
        assert kinds == {"filter", "spam", "unsub", "allowed"}


class TestSummary:
    def test_label_summary_lists_every_counter(self, state):
        state.add_filter("new@x.com", "work")
        state.queue_spam("bad@x.com")
        lines = "\n".join(state.summary_lines())
        assert "Filtered:" in lines
        assert "Queued:" in lines  # the pending queue is never hidden

    def test_audit_summary_uses_audit_words(self, state):
        state.record_keep()
        lines = "\n".join(state.summary_lines(audit=True))
        assert "Kept:" in lines
        assert "Removed:" in lines
        assert "Archived:" not in lines


class TestPanelOperations:
    """The right pane's ops, driven through the session (no widgets)."""

    def _data(self, session, kind, **extra):
        data = {"kind": kind, **extra}
        return data

    def test_drop_removes_a_filter_entry(self, session, config):
        session.filter_item(session.current, "work")
        result = session.panel_drop({"kind": "entry", "filter": "work", "email": "b@x.com"})
        assert "dropped" in result.message
        assert "b@x.com" not in config.filters["work"].entries

    def test_drop_un_allows(self, session, config):
        session.allow_item(session.current)
        session.panel_drop({"kind": "allowed", "email": "b@x.com"})
        assert "b@x.com" not in config.allowed
        assert session.state.counters["allowed"] == 0

    def test_drop_un_queues_spam(self, session):
        """Regression: panel rows say "spam", the session dispatches on "spam_item"."""
        session.spam_item(session.current, 0)
        result = session.panel_drop({"kind": "spam", "email": "b@x.com"})
        assert "spam queue" in result.message
        assert session.state.spam_queue == []

    def test_drop_un_queues_an_unsubscribe(self, session):
        session.unsubscribe_item(session.current, 0)
        session.panel_drop({"kind": "unsub", "email": "b@x.com"})
        assert session.state.unsub_queue == []

    def test_drop_on_a_section_says_so(self, session):
        result = session.panel_drop({"kind": "section"})
        assert "nothing to drop" in result.message

    def test_move_re_buckets_a_row(self, session, config):
        session.filter_item(session.current, "work")
        result = session.panel_move(
            {"kind": "entry", "filter": "work", "email": "b@x.com"}, "newsletters"
        )
        assert "moved" in result.message
        assert "b@x.com" not in config.filters["work"].entries
        assert "b@x.com" in config.filters["newsletters"].entries

    def test_move_a_queued_spam_row_into_a_filter(self, session, config):
        session.spam_item(session.current, 0)
        session.panel_move({"kind": "spam", "email": "b@x.com"}, "work")
        assert session.state.spam_queue == []
        assert "b@x.com" in config.filters["work"].entries

    def test_spam_a_row(self, session):
        session.filter_item(session.current, "work")
        result = session.panel_spam(
            {"kind": "entry", "filter": "work", "email": "b@x.com"}
        )
        assert "queued" in result.message
        assert [item.email for item in session.state.spam_queue] == ["b@x.com"]

    def test_spamming_the_same_row_twice_is_refused(self, session):
        session.spam_item(session.current, 0)
        result = session.panel_spam({"kind": "spam", "email": "b@x.com"})
        assert "already queued" in result.message
        assert len(session.state.spam_queue) == 1

    def test_allow_a_row(self, session, config):
        session.panel_allow({"kind": "filter", "filter": "work", "email": "b@x.com"})
        assert "b@x.com" in config.allowed

    def test_unqueue_only_applies_to_queues(self, session):
        result = session.panel_unqueue({"kind": "entry", "filter": "work", "email": "b@x.com"})
        assert "unqueue applies" in result.message

    def test_panel_ops_are_undoable(self, session):
        session.filter_item(session.current, "work")
        session.panel_spam({"kind": "entry", "filter": "work", "email": "b@x.com"})
        session.undo()
        assert session.state.spam_queue == []


class TestItem:
    def test_title_and_message_ids(self):
        item = Item(email="a@x.com", name="Alice", messages=(("hi", "m1"), ("old", "m0")))
        assert item.title == "Alice <a@x.com>"
        assert item.subject == "hi"
        assert item.message_id(1) == "m0"
        # Out-of-range indices clamp rather than explode.
        assert item.message_id(50) == "m0"

    def test_sender_without_messages(self):
        item = Item(email="a@x.com")
        assert item.title == "a@x.com"
        assert item.message_id(0) == ""
        assert item.subject == ""

    def test_position_is_one_based_and_capped(self, session):
        assert session.position == "1/2"
        session.pos = 1
        assert session.position == "2/2"
        session.pos = 99
        assert session.position == "2/2"


class TestKeyMaps:
    def test_audit_keeps_its_own_vocabulary(self, config, state, tmp_path):
        client = MagicMock()
        manager = FilterManager(config, client, Path("filters.yaml"))
        controller = TriageController(manager, state)
        audit = TriageSession(manager, state, controller, kind=AUDIT, items=[])
        label = TriageSession(manager, state, controller, kind=LABEL, items=[])
        assert audit.audit is True
        assert label.audit is False
        assert "keep" in audit.key_map
        assert "allow" in label.key_map
