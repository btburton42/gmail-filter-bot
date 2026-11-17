"""Confirmation modals.

Two shapes, both synchronous from the caller's point of view once awaited from a
worker:

  - :class:`Confirm` -- yes/no, for "fire the queue?" and "save the config?".
  - :class:`FireReview` -- the per-item queue review, where every entry can be
    skipped before anything is sent.

The review exists because deferring the work changes the failure mode: quitting
mid-session must not silently drop a queued spam report *or* silently send one.
Stopping at a review screen makes both impossible.
"""

from __future__ import annotations

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import OptionList, Static
from textual.widgets.option_list import Option


class Confirm(ModalScreen[bool]):
    """A yes/no modal; ``Enter`` on "yes" is the fast path, ``Ctrl-C`` says no."""

    BINDINGS = [
        Binding("escape", "no", "no", show=False),
        Binding("n", "no", "no", show=False),
        Binding("y", "yes", "yes", show=False),
    ]

    DEFAULT_CSS = """
    Confirm {
        align: center middle;
        background: $background 60%;
    }
    Confirm > Vertical {
        width: 56;
        max-width: 90%;
        height: auto;
        border: round $accent;
        background: $surface;
        padding: 1 2;
    }
    Confirm #confirm-body {
        height: auto;
    }
    Confirm #confirm-hint {
        color: $text-muted;
        height: auto;
        padding: 1 0 0 0;
    }
    """

    def __init__(self, body: str, *, hint: str = "[y] yes   [n] no") -> None:
        super().__init__()
        self.body = body
        self.hint = hint

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static(self.body, id="confirm-body", markup=False)
            yield Static(self.hint, id="confirm-hint", markup=False)

    def action_yes(self) -> None:
        self.dismiss(True)

    def action_no(self) -> None:
        self.dismiss(False)

    def on_key(self, event) -> None:
        if event.key == "enter":
            self.dismiss(True)
            event.stop()


class FireReview(ModalScreen[list[str] | None]):
    """Review the deferred queue; dismisses with the emails to keep queued.

    ``None`` means "cancel the whole thing" -- the caller then leaves the queue
    untouched, so nothing is fired and nothing is silently discarded.
    """

    BINDINGS = [
        Binding("escape", "cancel", "cancel", show=False),
        Binding("x", "toggle", "skip/keep", show=False),
        Binding("enter", "fire", "fire", show=False),
    ]

    DEFAULT_CSS = """
    FireReview {
        align: center middle;
        background: $background 60%;
    }
    FireReview > Vertical {
        width: 66;
        max-width: 92%;
        height: auto;
        max-height: 80%;
        border: round $warning;
        background: $surface;
        padding: 1 2;
    }
    FireReview Static {
        height: auto;
    }
    FireReview #review-title {
        text-style: bold;
        color: $warning;
    }
    FireReview #review-hint {
        color: $text-muted;
        padding: 1 0 0 0;
    }
    FireReview OptionList {
        height: auto;
        max-height: 16;
        background: transparent;
        border: none;
        padding: 0;
        margin: 1 0;
        scrollbar-size: 1 0;
    }
    """

    def __init__(self, spam: list[str], unsubs: list[str]) -> None:
        super().__init__()
        self.spam = list(spam)
        self.unsubs = list(unsubs)
        self.skipped: set[str] = set()

    def compose(self) -> ComposeResult:
        total = len(self.spam) + len(self.unsubs)
        with Vertical():
            yield Static(
                f"{total} queued item(s) \u00b7 nothing has been sent yet",
                id="review-title",
                markup=False,
            )
            yield OptionList(id="review-list")
            yield Static(
                "[enter] fire them   [x] drop the highlighted one   [esc] cancel",
                id="review-hint",
                markup=False,
            )

    def on_mount(self) -> None:
        self._refresh()
        self.query_one("#review-list", OptionList).focus()

    def _refresh(self) -> None:
        options = self.query_one("#review-list", OptionList)
        keep = options.highlighted
        options.clear_options()
        for email in self.spam:
            state = "skip" if email in self.skipped else "fire"
            options.add_option(Option(Text(f"spam        {email}   [{state}]"), id=f"spam:{email}"))
        for email in self.unsubs:
            state = "skip" if email in self.skipped else "fire"
            options.add_option(Option(Text(f"unsubscribe {email}   [{state}]"), id=f"unsub:{email}"))
        if options.option_count:
            options.highlighted = min(keep or 0, options.option_count - 1)

    def action_toggle(self) -> None:
        option = self.query_one("#review-list", OptionList).highlighted_option
        if option is None or option.id is None:
            return
        email = option.id.split(":", 1)[1]
        if email in self.skipped:
            self.skipped.remove(email)
        else:
            self.skipped.add(email)
        self._refresh()

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        """Enter on a row fires the queue.

        The list is focused, and ``OptionList`` binds ``enter`` to its own
        ``select`` action, so the screen-level binding never sees the key. This
        handler is the actual fast path, not a fallback.
        """
        event.stop()
        self.action_fire()

    def action_fire(self) -> None:
        keep = [e for e in self.spam + self.unsubs if e not in self.skipped]
        self.dismiss(keep)

    def action_cancel(self) -> None:
        self.dismiss(None)
