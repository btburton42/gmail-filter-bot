"""The Textual app wrapper: breakpoints, theme, and the run entry point.

The app is deliberately thin. It exists to own three app-level concerns:

  - **Breakpoints.** ``HORIZONTAL_BREAKPOINTS`` applies ``-narrow`` / ``-desktop``
    classes to the screen, and one CSS rule hides the panel below 96 columns.
    That replaces the old width arithmetic (``_terminal_width()``,
    ``_display_rows()``, ``_erase_rows()``) entirely -- there is exactly one
    place that decides whether the panel fits, and it is a number.

  - **Theme.** ``gfb config set theme <name>`` picks any built-in Textual theme;
    the default is ``textual-dark``.

  - **Return value.** ``TriageApp.run()`` returns a :class:`TriageResult`, so the
    caller (``cli.py``) can print the plain-text summary, save the config, and
    fire the deferred queue *after* the alternate screen is gone.
"""

from __future__ import annotations

import contextlib
from dataclasses import dataclass

from textual.app import App

from ..settings import get_theme_name
from .screen import TriageScreen
from .sessions import TriageSession

DESKTOP_WIDTH = 96
"""Below this many columns the right-hand panel is hidden."""

PANEL_WIDTH = 38


@dataclass
class TriageResult:
    """What happened during a session, handed back to the caller."""

    session: TriageSession
    completed: bool = False
    """True when the walk reached the last item rather than being quit early."""
    keep: list[str] | None = None
    """Emails the user chose to fire at the queue review; ``None`` means the
    review was cancelled or never shown, and nothing should be sent."""
    reviewed: bool = False

    @property
    def should_fire(self) -> bool:
        return self.reviewed and bool(self.keep)


class TriageApp(App):
    """The triage UI."""

    CSS_PATH = "styles.tcss"
    TITLE = "gfb triage"

    # One breakpoint per layout, low to high. The first is the plain
    # narrow-terminal layout; the second gives the panel a column.
    HORIZONTAL_BREAKPOINTS = [(0, "-narrow"), (DESKTOP_WIDTH, "-desktop")]

    def __init__(self, session: TriageSession, *, theme: str | None = None) -> None:
        super().__init__()
        self.session = session
        self.theme_name = theme or get_theme_name()

    def on_mount(self) -> None:
        # A theme name from an older config (or a typo) must not break triage;
        # fall back to the default rather than refusing to run.
        with contextlib.suppress(Exception):
            self.theme = self.theme_name
        self.push_screen(TriageScreen(self.session))


def run_triage(session: TriageSession, *, theme: str | None = None) -> TriageResult:
    """Run the app and return what happened.

    Split out from the CLI so tests can drive the *screen* with ``run_test``
    without constructing an ``App`` themselves, and so ``cli.py`` stays free of
    Textual imports.
    """
    result = TriageApp(session, theme=theme).run()
    if isinstance(result, TriageResult):
        return result
    # ``run()`` returns None if something exits outside ``app.exit(value)``.
    return TriageResult(session=session, completed=False)
