"""The filter picker: a typeahead modal, replacing ``questionary``.

Why a modal instead of the old prompt: the triage app owns the screen, so a
``questionary`` prompt would have to tear down the alternate screen and draw a
raw ``input()`` line into the middle of a live layout. A ``ModalScreen`` is just
another screen -- the layout stays mounted underneath, the mouse works, and the
fuzzy matching comes from Textual (``textual.fuzzy``) rather than hand-rolled
subsequence matching.

The picker is also where the "archive" sentinel lives: typing ``archive`` (or
picking it) returns ``ARCHIVE_CHOICE`` and the caller knows to archive instead
of filtering. That sentinel behaviour was in the old prompt and callers depend
on it, so it is preserved rather than re-invented.
"""

from __future__ import annotations

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.fuzzy import Matcher
from textual.screen import ModalScreen
from textual.widgets import Input, OptionList, Static
from textual.widgets.option_list import Option

ARCHIVE_CHOICE = "archive"
"""Returned when the user chooses to archive the sender instead of filtering."""


class FilterPicker(ModalScreen[str | None]):
    """Pick (or create) a filter name. Dismisses with the name, or ``None``."""

    BINDINGS = [
        Binding("escape", "cancel", "cancel", show=False),
    ]

    DEFAULT_CSS = """
    FilterPicker {
        align: center middle;
        background: $background 60%;
    }
    FilterPicker > Vertical {
        width: 60;
        max-width: 90%;
        height: auto;
        max-height: 80%;
        border: round $accent;
        background: $surface;
        padding: 1 1;
    }
    FilterPicker #picker-title {
        text-style: bold;
        color: $accent;
        height: auto;
    }
    FilterPicker #picker-hint {
        color: $text-muted;
        height: auto;
        padding: 0 0 1 0;
    }
    FilterPicker Input {
        border: none;
        background: $panel;
        padding: 0 1;
    }
    FilterPicker OptionList {
        height: auto;
        max-height: 14;
        margin: 1 0 0 0;
        background: transparent;
        border: none;
        scrollbar-size: 1 0;
    }
    """

    def __init__(
        self,
        names: list[str],
        *,
        title: str = "filter this into which filter?",
        current: str = "",
    ) -> None:
        super().__init__()
        self.names = sorted(names)
        self.title_text = title
        self.current = current

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static(self.title_text, id="picker-title", markup=False)
            yield Static(
                "type to search \u00b7 enter picks \u00b7 esc cancels \u00b7 "
                "unknown names create a new filter",
                id="picker-hint",
                markup=False,
            )
            yield Input(placeholder="filter name", value=self.current, id="picker-input")
            yield OptionList(id="picker-options")

    def on_mount(self) -> None:
        self._refresh_options("")
        self.query_one("#picker-input", Input).focus()

    # -- search ------------------------------------------------------------
    def on_input_changed(self, event: Input.Changed) -> None:
        self._refresh_options(event.value)

    def _refresh_options(self, query: str) -> None:
        options = self.query_one("#picker-options", OptionList)
        options.clear_options()

        matches = _match(self.names, query)
        for name in matches:
            options.add_option(Option(name, id=name))
        if query:
            options.add_option(Option(f"+ create '{query}'", id=f"__new__{query}"))
        options.add_option(Option("archive (keep out of the inbox)", id=ARCHIVE_CHOICE))
        if matches:
            options.highlighted = 0

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        self.dismiss(_resolve(event.option_id))

    def on_input_submitted(self, event: Input.Submitted) -> None:
        """Enter in the input: take the highlighted option, else the typed text."""
        options = self.query_one("#picker-options", OptionList)
        option = options.highlighted_option
        if option is not None and option.id is not None:
            self.dismiss(_resolve(option.id))
            return
        self.dismiss(event.value.strip() or None)

    def on_key(self, event) -> None:
        """Arrows move the list even while the text box has focus."""
        if event.key not in ("up", "down"):
            return
        options = self.query_one("#picker-options", OptionList)
        if options.option_count == 0:
            return
        if event.key == "down":
            options.action_cursor_down()
        else:
            options.action_cursor_up()
        event.stop()

    # -- actions -----------------------------------------------------------
    def action_cancel(self) -> None:
        self.dismiss(None)


def _resolve(option_id: str) -> str:
    """Turn an option id into the value the caller wants."""
    if option_id.startswith("__new__"):
        return option_id[len("__new__"):]
    return option_id


def _match(names: list[str], query: str) -> list[str]:
    """Fuzzy-filter ``names``; empty query means "show everything"."""
    if not query:
        return names
    matcher = Matcher(query.lower())
    scored = []
    for name in names:
        score = matcher.match(name.lower())
        if score > 0:
            scored.append((score, name))
    scored.sort(key=lambda pair: (-pair[0], pair[1]))
    return [name for _score, name in scored]
