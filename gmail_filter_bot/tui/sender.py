"""The left pane: the sender currently being triaged.

Heading (who this is), a scrollable read-only body, and the key hints for the
active hand mode. The body never takes keyboard focus: Tab switches between
this pane and the session panel, rather than stopping on every scrolling
widget. Mouse-wheel scrolling still works without focus.
"""

from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import Vertical
from textual.widgets import RichLog, Static


class SenderPane(Vertical, can_focus=True):
    """Heading + body + key hints."""

    def compose(self) -> ComposeResult:
        yield Static("", id="sender-heading", markup=False)
        # RichLog's default content width is 78 columns. That would force a
        # horizontal scrollbar whenever this pane is narrower, so opt out.
        body = RichLog(
            id="sender-body", wrap=True, markup=False, auto_scroll=False, min_width=1
        )
        body.can_focus = False
        yield body
        yield Static("", id="sender-hint", markup=False)

    def show(self, heading: str, hint: str) -> None:
        self.query_one("#sender-heading", Static).update(heading)
        if self.app.size.width >= 120:
            hint = hint.replace("unsubscribe", "unsub").replace("continue", "skip")
            hint = hint.replace(" / ", " ")
        else:
            # Keep every important key visible in the narrower 58-column pane.
            # Message expansion is already advertised beside the subject list.
            parts = [part for part in hint.split(" / ") if not part.endswith(" expand")]
            hint = " ".join(part.replace("] ", "]") for part in parts)
            hint = hint.replace("[", "").replace("]", " ")
            hint = hint.replace("unsubscribe", "unsub").replace("continue", "skip")
        self.query_one("#sender-hint", Static).update(hint)

    def set_body(self, lines: list[str]) -> None:
        body = self.query_one("#sender-body", RichLog)
        body.clear()
        for line in lines:
            body.write(line)

    def append_body(self, line: str) -> None:
        self.query_one("#sender-body", RichLog).write(line)

    def clear_body(self) -> None:
        self.query_one("#sender-body", RichLog).clear()

    def scroll_body(self, amount: int) -> None:
        body = self.query_one("#sender-body", RichLog)
        body.scroll_relative(y=amount, animate=False)
