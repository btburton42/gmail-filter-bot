"""The triage UI itself, driven headlessly with Textual's ``run_test``.

These replace the old pty tests that scraped escape sequences. ``Pilot`` presses
real keys and clicks real widgets, and the assertions are about widget state and
the returned ``TriageResult`` rather than about bytes on a terminal.
"""

from unittest.mock import MagicMock
from xml.etree import ElementTree

import pytest
from textual.widgets import Static

from gmail_filter_bot import settings
from gmail_filter_bot.config import Config, Credentials, FilterConfig
from gmail_filter_bot.filter_manager import FilterManager
from gmail_filter_bot.gmail_client import Sender, SenderMessage
from gmail_filter_bot.tui.app import DESKTOP_WIDTH, TriageApp
from gmail_filter_bot.tui.confirm import FireReview
from gmail_filter_bot.tui.panel import SidePanel
from gmail_filter_bot.tui.picker import FilterPicker
from gmail_filter_bot.tui.screen import TriageScreen
from gmail_filter_bot.tui.sessions import AUDIT, LABEL, Item, TriageSession
from gmail_filter_bot.tui.state import TriageState

# Two layouts: a laptop-sized terminal with a panel, and a narrow one without.
WIDE = (140, 40)
NARROW = (80, 24)


def sender(name, email, *messages):
    return Sender(
        name,
        email,
        tuple(SenderMessage(s, i) for s, i in messages),
    )


@pytest.fixture(autouse=True)
def isolated_settings(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "user_config_path", lambda: tmp_path / "cfg.yaml")


@pytest.fixture
def config():
    return Config(
        max_entries_per_filter=50,
        filters={"work": FilterConfig("work", "label_and_archive", "Work", [])},
        credentials=Credentials(client_id="x", client_secret="y"),
        allowed=[],
    )


@pytest.fixture
def client():
    client = MagicMock()
    client.report_spam.return_value = 2
    client.archive_message.return_value = True
    client.get_unsubscribe_targets.return_value = None
    client.get_message_preview.return_value = ["line one", "line two"]
    return client


@pytest.fixture
def session(config, client, tmp_path):
    manager = FilterManager(config, client, tmp_path / "filters.yaml")
    state = TriageState(config=config)
    from gmail_filter_bot.tui.controller import TriageController

    controller = TriageController(manager, state)
    items = [
        Item.from_sender(sender("Alice", "a@x.com", ("Hello", "m1"))),
        Item.from_sender(sender("Bob", "b@x.com", ("Hi", "m2"))),
        Item.from_sender(sender("Carol", "c@x.com", ("Yo", "m3"))),
    ]
    return TriageSession(manager, state, controller, kind=LABEL, items=items)


class TestLayout:
    @pytest.mark.asyncio
    async def test_wide_terminal_shows_the_panel(self, session):
        app = TriageApp(session)
        async with app.run_test(size=WIDE) as pilot:
            await pilot.pause()
            screen = app.screen
            assert screen.has_class("-desktop")
            assert not screen.has_class("-narrow")
            panel = screen.query_one(SidePanel)
            assert panel.display is True
            assert panel.region.width == 38
            sender_pane = screen.query_one("#left")
            assert sender_pane.region.width > 38
            assert sender_pane.region.x < panel.region.x
            assert panel.region.x == sender_pane.region.right

    @pytest.mark.asyncio
    async def test_each_pane_uses_the_terminal_height(self, session):
        """The panes are the point: neither may collapse to a thin strip."""
        app = TriageApp(session)
        async with app.run_test(size=WIDE) as pilot:
            await pilot.pause()
            screen = app.screen
            for selector in ("#left", "#panel", "#sender-body", "#panel-tree"):
                region = screen.query_one(selector).region
                assert region.height >= 30, (selector, region)
            assert screen.query_one("#sender-heading").region.height == 1
            assert screen.query_one("#sender-hint").region.height == 1
            assert screen.query_one("#statusline").region.height == 1

    @pytest.mark.asyncio
    async def test_body_is_visible_and_not_keyboard_focusable(self, session):
        app = TriageApp(session)
        async with app.run_test(size=WIDE) as pilot:
            await pilot.pause()
            body = app.screen.query_one("#sender-body")
            assert body.can_focus is False
            assert body.min_width == 1
            assert "Alice <a@x.com>" in str(app.screen.query_one("#sender-heading").content)
            rendered = "\n".join(line.text for line in body.lines)
            assert "Hello" in rendered

    @pytest.mark.asyncio
    async def test_key_hints_are_literal_and_fit(self, session):
        """Rich markup must not eat [f]/[a] labels or leave clipped keys."""
        for columns in (96, 120):
            app = TriageApp(session)
            async with app.run_test(size=(columns, 34)) as pilot:
                await pilot.pause()
                hint = app.screen.query_one("#sender-hint", Static)
                assert len(str(hint.content)) <= app.screen.query_one("#left").content_region.width
                expected = "[f] filter" if columns >= 120 else "f filter"
                expected_undo = "[z] undo" if columns >= 120 else "z undo"
                assert expected in str(hint.content)
                assert expected_undo in str(hint.content)
                # Check what actually rendered, not only the stored string.
                screen_text = " ".join(
                    ElementTree.fromstring(app.export_screenshot()).itertext()
                ).replace("\u00a0", " ")
                assert expected in screen_text
                assert expected_undo in screen_text

    @pytest.mark.asyncio
    async def test_long_heading_stays_inside_its_pane(self, session):
        app = TriageApp(session)
        async with app.run_test(size=WIDE) as pilot:
            await pilot.pause()
            heading = app.screen.query_one("#sender-heading")
            assert heading.region.x == app.screen.query_one("#left").content_region.x
            assert heading.region.width > 20
            assert heading.region.height == 1

    @pytest.mark.asyncio
    async def test_narrow_terminal_hides_the_panel(self, session):
        app = TriageApp(session)
        async with app.run_test(size=NARROW) as pilot:
            await pilot.pause()
            screen = app.screen
            assert screen.has_class("-narrow")
            panel = screen.query_one(SidePanel)
            assert panel.display is False

    @pytest.mark.asyncio
    async def test_the_statusline_reports_position_and_counters(self, session):
        app = TriageApp(session)
        async with app.run_test(size=WIDE) as pilot:
            await pilot.pause()
            status = app.screen.query_one("#statusline")
            text = str(status.content)
            assert "NORMAL" in text
            assert "1/3" in text
            assert "filtered 0" in text


class TestDecisions:
    @pytest.mark.asyncio
    async def test_filtering_advances_and_updates_the_panel(self, session, config):
        app = TriageApp(session)
        async with app.run_test(size=WIDE) as pilot:
            await pilot.pause()
            await pilot.press("f")
            await pilot.pause()
            assert isinstance(app.screen, FilterPicker)

            await pilot.press("w", "o", "r", "k")
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()

            assert session.pos == 1
            assert "a@x.com" in config.filters["work"].entries
            tree = app.screen.query_one(SidePanel).query_one("Tree")
            labels = [str(node.label) for node in _rows(tree)]
            assert any("work" in label for label in labels)
            assert any("a@x.com" in label for label in labels)

    @pytest.mark.asyncio
    async def test_allow_then_undo(self, session, config):
        app = TriageApp(session)
        async with app.run_test(size=WIDE) as pilot:
            await pilot.pause()
            await pilot.press("a")
            await pilot.pause()
            assert config.allowed == ["a@x.com"]
            assert session.pos == 1

            await pilot.press("z")
            await pilot.pause()
            assert config.allowed == []
            assert session.pos == 0

    @pytest.mark.asyncio
    @pytest.mark.parametrize("key", ["k", "c", ";"])
    async def test_k_skips_and_old_skip_keys_still_work(self, session, config, key):
        app = TriageApp(session)
        async with app.run_test(size=WIDE) as pilot:
            await pilot.pause()
            await pilot.press(key)
            await pilot.pause()
            assert session.pos == 1
            assert session.state.counters["skipped"] == 1
            assert config.allowed == []
            assert config.filters["work"].entries == []

    @pytest.mark.asyncio
    async def test_escape_in_the_picker_changes_nothing(self, session, config):
        app = TriageApp(session)
        async with app.run_test(size=WIDE) as pilot:
            await pilot.pause()
            await pilot.press("f")
            await pilot.pause()
            await pilot.press("escape")
            await pilot.pause()
            assert session.pos == 0
            assert config.filters["work"].entries == []


class TestDeferredFiring:
    @pytest.mark.asyncio
    async def test_quit_with_an_empty_queue_does_not_review(self, session):
        app = TriageApp(session)
        async with app.run_test(size=WIDE) as pilot:
            await pilot.pause()
            await pilot.press("q")
            await pilot.pause()
        result = app.return_value
        assert result.reviewed is False
        assert result.keep is None
        assert result.completed is False

    @pytest.mark.asyncio
    async def test_spam_is_queued_then_approved_at_the_end(self, session, client):
        app = TriageApp(session)
        async with app.run_test(size=WIDE) as pilot:
            await pilot.pause()
            await pilot.press("s")
            await pilot.pause()
            # Queued, not sent.
            assert [item.email for item in session.state.spam_queue] == ["a@x.com"]
            assert not client.report_spam.called

            await pilot.press("q")
            await pilot.pause()
            assert isinstance(app.screen, FireReview)
            await pilot.press("enter")
            await pilot.pause()

        result = app.return_value
        assert result.reviewed is True
        assert result.keep == ["a@x.com"]
        assert result.should_fire is True
        # Still nothing sent: firing is the CLI's job, after the app is gone.
        assert not client.report_spam.called

    @pytest.mark.asyncio
    async def test_skipping_at_the_review_drops_the_item(self, session):
        app = TriageApp(session)
        async with app.run_test(size=WIDE) as pilot:
            await pilot.pause()
            await pilot.press("s")
            await pilot.pause()
            await pilot.press("q")
            await pilot.pause()
            assert isinstance(app.screen, FireReview)
            captured = " ".join(
                ElementTree.fromstring(app.export_screenshot()).itertext()
            ).replace("\u00a0", " ")
            assert "[enter] fire them" in captured
            assert "[fire]" in captured
            await pilot.press("x")  # mark the row as skip
            await pilot.pause()
            review_text = " ".join(
                ElementTree.fromstring(app.export_screenshot()).itertext()
            ).replace("\u00a0", " ")
            assert "[skip]" in review_text
            await pilot.press("enter")

        result = app.return_value
        assert result.keep == []
        assert result.should_fire is False

    @pytest.mark.asyncio
    async def test_cancelling_the_review_fires_nothing(self, session):
        app = TriageApp(session)
        async with app.run_test(size=WIDE) as pilot:
            await pilot.pause()
            await pilot.press("s")
            await pilot.pause()
            await pilot.press("q")
            await pilot.pause()
            await pilot.press("escape")
            await pilot.pause()

        result = app.return_value
        assert result.reviewed is False
        assert result.keep is None

    @pytest.mark.asyncio
    async def test_finishing_the_walk_still_stops_at_the_review(self, session):
        """Walking off the end must not exit past the queue review."""
        app = TriageApp(session)
        async with app.run_test(size=WIDE) as pilot:
            await pilot.pause()
            await pilot.press("s")       # item 1
            await pilot.pause()
            await pilot.press("c")       # item 2, skip
            await pilot.pause()
            await pilot.press("c")       # item 3 -> end of the walk
            await pilot.pause()
            assert isinstance(app.screen, FireReview)
            await pilot.press("enter")
            await pilot.pause()

        result = app.return_value
        assert result.completed is True
        assert result.keep == ["a@x.com"]


class TestPanelInteraction:
    @pytest.mark.asyncio
    async def test_tab_moves_the_keymap_to_the_panel(self, session):
        app = TriageApp(session)
        async with app.run_test(size=WIDE) as pilot:
            await pilot.pause()
            screen = app.screen
            assert screen.active_pane == "left"
            await pilot.press("tab")
            await pilot.pause()
            assert screen.active_pane == "panel"
            assert "PANEL" in str(screen.query_one("#statusline").content)
            await pilot.press("tab")
            await pilot.pause()
            assert screen.active_pane == "left"

    @pytest.mark.asyncio
    async def test_dropping_a_row_from_the_panel(self, session, config):
        app = TriageApp(session)
        async with app.run_test(size=WIDE) as pilot:
            await pilot.pause()
            await pilot.press("a")  # allowed -> a panel row exists
            await pilot.pause()
            assert config.allowed == ["a@x.com"]

            await pilot.press("tab")
            await pilot.pause()
            _select_panel_row(app.screen, "a@x.com")
            await pilot.press("x")
            await pilot.pause()

            assert config.allowed == []
            assert session.state.counters["allowed"] == 0

    @pytest.mark.asyncio
    async def test_moving_a_row_from_the_panel(self, session, config):
        app = TriageApp(session)
        async with app.run_test(size=WIDE) as pilot:
            await pilot.pause()
            await pilot.press("f")
            await pilot.pause()
            await pilot.press("w", "o", "r", "k", "enter")
            await pilot.pause()

            await pilot.press("tab")
            await pilot.pause()
            _select_panel_row(app.screen, "a@x.com")
            await pilot.press("m")
            await pilot.pause()
            assert isinstance(app.screen, FilterPicker)
            await pilot.press("n", "e", "w", "enter")
            await pilot.pause()

            assert "a@x.com" in config.filters["new"].entries
            assert "a@x.com" not in config.filters["work"].entries
            assert session.state.counters["filtered"] == 1

    @pytest.mark.asyncio
    async def test_filter_heading_is_not_an_entry(self, session):
        app = TriageApp(session)
        async with app.run_test(size=WIDE) as pilot:
            await pilot.pause()
            await pilot.press("f", "w", "o", "r", "k", "enter")
            await pilot.pause()
            await pilot.press("tab")
            await pilot.pause()
            tree = app.screen.query_one("#panel-tree")
            for node in _rows(tree):
                if node.data and node.data.get("kind") == "filter":
                    tree.move_cursor(node)
                    break
            await pilot.press("s")
            await pilot.pause()
            assert session.state.spam_queue == []
            assert "" not in session.state.config.allowed
            assert "filter heading" in str(app.screen.query_one("#statusline").content)

    @pytest.mark.asyncio
    async def test_dropping_a_spam_entry_clears_the_queue_too(self, session, config):
        """The FILTERS and SPAM sections show one decision, not two."""
        app = TriageApp(session)
        async with app.run_test(size=WIDE) as pilot:
            await pilot.pause()
            await pilot.press("s")
            await pilot.pause()
            await pilot.press("tab")
            await pilot.pause()
            _select_panel_row(app.screen, "a@x.com")  # first match is FILTERS
            await pilot.press("x")
            await pilot.pause()
            assert session.state.spam_queue == []
            assert session.state.counters["spammed"] == 0
            assert "spam" not in config.filters

    @pytest.mark.asyncio
    async def test_dropping_an_entry_updates_the_counter(self, session):
        app = TriageApp(session)
        async with app.run_test(size=WIDE) as pilot:
            await pilot.pause()
            await pilot.press("f", "w", "o", "r", "k", "enter")
            await pilot.pause()
            await pilot.press("tab")
            await pilot.pause()
            _select_panel_row(app.screen, "a@x.com")
            await pilot.press("x")
            await pilot.pause()
            assert session.state.counters["filtered"] == 0
            assert session.state.changes == {}

    @pytest.mark.asyncio
    async def test_panel_ops_need_a_row(self, session):
        app = TriageApp(session)
        async with app.run_test(size=WIDE) as pilot:
            await pilot.pause()
            await pilot.press("tab")
            await pilot.pause()
            await pilot.press("x")
            await pilot.pause()
            status = str(app.screen.query_one("#statusline").content)
            assert "pick a row" in status


class TestBody:
    @pytest.mark.asyncio
    async def test_expanding_reads_the_body(self, session, client):
        app = TriageApp(session)
        async with app.run_test(size=WIDE) as pilot:
            await pilot.pause()
            await pilot.press("e")
            await pilot.pause()
            client.get_message_preview.assert_called_once()
            body = app.screen.query_one("#sender-body").lines
            text = "\n".join(str(line) for line in body)
            assert "line one" in text
            assert "collapses" in str(app.screen.query_one("#statusline").content)

    @pytest.mark.asyncio
    async def test_a_slow_body_fetch_cannot_overwrite_the_next_sender(self, session, client):
        import threading

        started = threading.Event()
        release = threading.Event()

        def slow_preview(*args, **kwargs):
            started.set()
            release.wait(timeout=5)
            return ["STALE BODY"]

        client.get_message_preview.side_effect = slow_preview
        app = TriageApp(session)
        async with app.run_test(size=WIDE) as pilot:
            await pilot.pause()
            await pilot.press("e")
            for _ in range(20):
                if started.is_set():
                    break
                await pilot.pause(0.05)
            assert started.is_set()
            await pilot.press("a")  # allow Alice and advance to Bob
            await pilot.pause()
            release.set()
            await pilot.pause(0.2)
            body = "\n".join(str(line) for line in app.screen.query_one("#sender-body").lines)
            assert "STALE BODY" not in body
            assert "b@x.com" in body


class TestMouse:
    @pytest.mark.asyncio
    async def test_clicking_a_panel_row_activates_its_keymap(self, session):
        app = TriageApp(session)
        async with app.run_test(size=WIDE) as pilot:
            await pilot.pause()
            await pilot.press("a")
            await pilot.pause()
            await pilot.click("#panel-tree")
            await pilot.pause()
            assert app.screen.active_pane == "panel"
            assert app.focused.id == "panel-tree"


class TestAuditScreen:
    @pytest.mark.asyncio
    async def test_audit_vocabulary_and_panel(self, config, client, tmp_path):
        from gmail_filter_bot.tui.controller import TriageController

        config.filters["newsletters"] = FilterConfig(
            "newsletters", "label_only", "Newsletters", ["a@x.com", "b@x.com"]
        )
        manager = FilterManager(config, client, tmp_path / "filters.yaml")
        state = TriageState(config=config)
        controller = TriageController(manager, state)
        items = [
            Item(email="a@x.com", filter_name="newsletters"),
            Item(email="b@x.com", filter_name="newsletters"),
        ]
        session = TriageSession(manager, state, controller, kind=AUDIT, items=items)

        app = TriageApp(session)
        async with app.run_test(size=WIDE) as pilot:
            await pilot.pause()
            heading = str(app.screen.query_one(SidePanel).query_one("#panel-heading").content)
            assert heading == "AUDIT"

            await pilot.press("f")  # audit: move
            await pilot.pause()
            await pilot.press("w", "o", "r", "k", "enter")
            await pilot.pause()

            assert session.pos == 1
            assert "a@x.com" in config.filters["work"].entries
            labels = [str(node.label) for node in _rows(app.screen.query_one(SidePanel).query_one("Tree"))]
            assert any("MOVED" in label for label in labels)

            await pilot.press("a")  # audit: remove
            await pilot.pause()
            assert config.filters["newsletters"].entries == []


def _rows(tree):
    """Depth-first over the panel's tree nodes."""
    def walk(node):
        for child in node.children:
            yield child
            yield from walk(child)

    return list(walk(tree.root))


def _select_panel_row(screen, email):
    """Move the panel cursor onto the row for ``email``."""
    tree = screen.query_one(SidePanel).query_one("Tree")
    for node in _rows(tree):
        data = node.data if isinstance(node.data, dict) else {}
        if data.get("email") == email:
            tree.move_cursor(node)
            return
    raise AssertionError(f"no panel row for {email}")


def test_desktop_width_is_a_single_number():
    """The layout decision lives in one constant, not in arithmetic."""
    assert DESKTOP_WIDTH == 96


class TestMessageCursor:
    """←/→ pick which of the sender's messages an action applies to."""

    @pytest.fixture
    def multi(self, config, client, tmp_path):
        from gmail_filter_bot.tui.controller import TriageController

        manager = FilterManager(config, client, tmp_path / "filters.yaml")
        state = TriageState(config=config)
        controller = TriageController(manager, state)
        items = [
            Item.from_sender(
                sender("Alice", "a@x.com", ("Newest", "m-new"), ("Older", "m-old"))
            )
        ]
        return TriageSession(manager, state, controller, kind=LABEL, items=items)

    @pytest.mark.asyncio
    async def test_right_moves_the_marker(self, multi):
        app = TriageApp(multi)
        async with app.run_test(size=WIDE) as pilot:
            await pilot.pause()
            assert app.screen.msg_index == 0
            await pilot.press("right")
            await pilot.pause()
            assert app.screen.msg_index == 1
            body = "\n".join(str(line) for line in app.screen.query_one("#sender-body").lines)
            assert "\u25b8 Older" in body
            await pilot.press("right")  # wraps back around
            await pilot.pause()
            assert app.screen.msg_index == 0

    @pytest.mark.asyncio
    async def test_actions_use_the_selected_message(self, multi, client):
        app = TriageApp(multi)
        async with app.run_test(size=WIDE) as pilot:
            await pilot.pause()
            await pilot.press("right")
            await pilot.pause()
            await pilot.press("e")
            await pilot.pause()
            client.get_message_preview.assert_called_once_with(
                "m-old", max_lines=300, max_chars=12000
            )

    @pytest.mark.asyncio
    async def test_expansion_follows_message_navigation(self, multi, client):
        client.get_message_preview.side_effect = lambda msg_id, **kwargs: [f"BODY {msg_id}"]
        app = TriageApp(multi)
        async with app.run_test(size=WIDE) as pilot:
            await pilot.pause()
            await pilot.press("e", "right")
            await pilot.pause()
            assert app.screen.expanded is True
            assert app.screen.msg_index == 1
            body = "\n".join(str(line) for line in app.screen.query_one("#sender-body").lines)
            assert "BODY m-old" in body
            assert "BODY m-new" not in body

            await pilot.press("left")
            await pilot.pause()
            body = "\n".join(str(line) for line in app.screen.query_one("#sender-body").lines)
            assert "BODY m-new" in body
            # Each body is fetched once, then served from the session cache.
            assert client.get_message_preview.call_count == 2
            requested = [call.args[0] for call in client.get_message_preview.call_args_list]
            assert requested == ["m-new", "m-old"]

    @pytest.mark.asyncio
    async def test_unsubscribe_uses_the_displayed_message(self, multi, client):
        app = TriageApp(multi)
        async with app.run_test(size=WIDE) as pilot:
            await pilot.pause()
            await pilot.press("right", "u")
            await pilot.pause()
            client.get_unsubscribe_targets.assert_called_once_with("m-old")
            await pilot.press("escape")
            await pilot.pause()

    @pytest.mark.asyncio
    async def test_quit_waits_for_an_unsubscribe_lookup(self, multi, client):
        import threading

        import gmail_filter_bot.tui.controller as controller_module

        started = threading.Event()
        release = threading.Event()

        def slow_targets(_message_id):
            started.set()
            release.wait(timeout=5)
            return controller_module.UnsubscribeTargets(one_click="https://example.test/u")

        client.get_unsubscribe_targets.side_effect = slow_targets
        app = TriageApp(multi)
        async with app.run_test(size=WIDE) as pilot:
            await pilot.pause()
            await pilot.press("u")
            for _ in range(20):
                if started.is_set():
                    break
                await pilot.pause(0.05)
            assert started.is_set()
            await pilot.press("q")
            await pilot.pause()
            assert isinstance(app.screen, TriageScreen)
            assert multi.state.pending_count == 0
            release.set()
            for _ in range(20):
                if app.screen.__class__.__name__ == "FireReview":
                    break
                await pilot.pause(0.05)
            assert app.screen.__class__.__name__ == "FireReview"
            assert multi.state.pending_count == 1
            await pilot.press("escape")
            await pilot.pause()

    @pytest.mark.asyncio
    async def test_a_single_message_says_so(self, session):
        app = TriageApp(session)
        async with app.run_test(size=WIDE) as pilot:
            await pilot.pause()
            await pilot.press("right")
            await pilot.pause()
            assert "only one message" in str(app.screen.query_one("#statusline").content)
