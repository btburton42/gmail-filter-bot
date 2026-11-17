"""Tests for the filter picker modal (replaces the questionary typeahead)."""

import pytest

from gmail_filter_bot.tui.picker import ARCHIVE_CHOICE, FilterPicker, _match, _resolve

NAMES = ["newsletters", "newsletters/tech", "receipts", "work"]


class TestMatch:
    """Fuzzy ranking, without a terminal."""

    def test_empty_query_shows_everything(self):
        assert _match(NAMES, "") == NAMES

    def test_subsequence_matches(self):
        assert _match(NAMES, "nl") == ["newsletters", "newsletters/tech"]

    def test_contiguous_prefix_ranks_first(self):
        # "work" should beat any looser subsequence match.
        assert _match(NAMES, "work")[0] == "work"

    def test_no_match_is_empty(self):
        assert _match(NAMES, "zzz") == []

    def test_ranking_is_stable_for_ties(self):
        names = ["b/x", "a/x"]
        assert _match(names, "/x") == sorted(names)


class TestResolve:
    """Option ids are turned into the value the caller wants."""

    def test_plain_name_passes_through(self):
        assert _resolve("work") == "work"

    def test_create_row_yields_the_typed_name(self):
        assert _resolve("__new__brand-new") == "brand-new"

    def test_archive_sentinel_is_itself(self):
        assert _resolve(ARCHIVE_CHOICE) == "archive"


class TestFilterPickerModal:
    """The modal itself, driven by Pilot."""

    @pytest.mark.asyncio
    async def test_typing_filters_the_list(self):
        from textual.app import App

        class Host(App):
            def on_mount(self):
                self.push_screen(FilterPicker(NAMES))

        app = Host()
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("w", "o", "r")
            await pilot.pause()
            picker = app.screen
            assert isinstance(picker, FilterPicker)
            options = picker.query_one("#picker-options")
            ids = [options.get_option_at_index(i).id for i in range(options.option_count)]
            assert "work" in ids
            # A create row for the typed query, plus the archive sentinel.
            assert "__new__wor" in ids
            assert ARCHIVE_CHOICE in ids

    @pytest.mark.asyncio
    async def test_enter_takes_the_highlighted_filter(self):
        from textual.app import App

        results = []

        class Host(App):
            def on_mount(self):
                self.push_screen(FilterPicker(NAMES), callback=results.append)

        app = Host()
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("w", "o", "r")
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()
        assert results == ["work"]

    @pytest.mark.asyncio
    async def test_escape_cancels(self):
        from textual.app import App

        results = []

        class Host(App):
            def on_mount(self):
                self.push_screen(FilterPicker(NAMES), callback=results.append)

        app = Host()
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("escape")
            await pilot.pause()
        assert results == [None]
