"""The triage screen: layout, key dispatch, and the worker plumbing.

Layout is two panes and a status line, which is exactly the nvim shape:

    +---------------------------+------------+
    | [3/12] Name <email>       | SESSION    |
    |                           | FILTERS 2  |
    |  subjects / message body  |   spam 1   |
    |                           +------------+
    |                           | 2 spam     |
    +---------------------------+ 1 unsub    |
    | f filter / a allow / ...  |            |
    +---------------------------+------------+
    | NORMAL  left  3/12  filtered 4  queued 3 |
    +------------------------------------------+

Panes are *focused*, not just drawn: the active pane decides what the keys mean,
the way a vim window does. That is the only way ``s`` can mean "spam this sender"
in the left pane and "spam the selected row" in the right one without a prefix
key nobody would remember.

Blocking work (reading a body, resolving an unsubscribe header) runs in thread
workers; the UI stays live and the status line says what is happening.
"""

from __future__ import annotations

from textual import work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal
from textual.screen import Screen
from textual.widgets import Static, Tree

from ..hand_mode import audit_prompt_hint, prompt_hint
from .confirm import FireReview
from .controller import ActionResult
from .panel import SidePanel
from .picker import ARCHIVE_CHOICE, FilterPicker
from .sender import SenderPane
from .sessions import TriageSession

LEFT = "left"
PANEL = "panel"

#: Panel-only keys. Fixed, regardless of hand-mode, because they act on the
#: right-hand pane rather than on the sender under the cursor.
PANEL_ACTIONS = {
    "x": "drop",
    "m": "move",
    "s": "spam",
    "a": "allow",
    "u": "unqueue",
    "enter": "toggle",
}


class TriageScreen(Screen):
    """The whole triage interaction, as a screen."""

    # All visual rules live in styles.tcss. Duplicating them here previously
    # gave two sources of truth for heights, breakpoints, and focus styling.
    DEFAULT_CSS = """
    TriageScreen {
        background: $background;
    }
    """

    # The panel is a luxury: below 96 columns the layout is the plain terminal
    # version, two panes wide above it.
    BINDINGS = [
        Binding("tab", "toggle_pane", "pane", show=True),
        Binding("ctrl+c", "quit_session", "quit", show=True, priority=True),
    ]

    def __init__(self, session: TriageSession) -> None:
        super().__init__()
        self.session = session
        self.active_pane = LEFT
        self.expanded = False
        self.message = ""
        self.msg_index = 0
        self._unsubscribe_busy = False
        """Which of the sender's messages is on screen. Every decision that
        needs a message id (spam, unsubscribe, archive, expand) uses this, so
        "the message you are looking at" is one piece of state, not an argument
        passed around by every caller."""

    # -- composition -------------------------------------------------------
    def compose(self) -> ComposeResult:
        with Horizontal(id="main"):
            yield SenderPane(id="left")
            yield SidePanel(id="panel")
        yield Static("", id="statusline", markup=False)

    def on_mount(self) -> None:
        self._focus_left()
        self.refresh_view()

    def on_descendant_focus(self, event) -> None:
        """Keep ``active_pane`` honest when focus moves for any reason.

        Tab, a mouse click on the tree, and ``SidePanel.focus_tree()`` all end up
        here, so this is the single place that decides which keymap is live. If
        it only lived in the tab action, clicking the panel with the mouse would
        leave the keyboard talking to the left pane -- which is exactly the bug
        this handler exists to avoid.
        """
        widget = event.widget
        if widget is None:
            return
        in_panel = False
        node = widget
        while node is not None:
            if isinstance(node, SidePanel):
                in_panel = True
                break
            node = node.parent
        self.active_pane = PANEL if in_panel else LEFT
        self._set_status(self.message)

    # -- rendering ---------------------------------------------------------
    def refresh_view(self) -> None:
        """Redraw everything from the session (cheap; the panel is small)."""
        item = self.session.current
        if item is None:
            self._finish()
            return

        hint = (
            audit_prompt_hint(self.session.key_map)
            if self.session.audit
            else prompt_hint(self.session.key_map)
        )
        self.query_one(SenderPane).show(f"[{self.session.position}] {item.title}", hint)

        if not self.expanded:
            self._show_subjects(item)

        kwargs = {"heading": "AUDIT" if self.session.audit else "SESSION"}
        self.query_one(SidePanel).render_sections(self.session.sections(), **kwargs)
        self.query_one(SidePanel).set_queue_line(self._queue_line())
        self._set_status(self.message)

    def _show_subjects(self, item) -> None:
        pane = self.query_one(SenderPane)
        lines = [f"{item.email}"]
        if item.filter_name:
            lines.append(f"currently in filter: {item.filter_name}")
        if item.messages:
            lines.append("")
            lines.append("messages (\u2190/\u2192 pick one, e opens it):")
            for index, (subject, _msg_id) in enumerate(item.messages[:12]):
                # The marker does the same job the old view's "\u25b8" did: it says
                # which message the next keypress will act on.
                marker = "\u25b8" if index == self.msg_index else "\u2022"
                lines.append(f"  {marker} {subject or '(no subject)'}")
            if len(item.messages) > 12:
                lines.append(f"  \u2026 {len(item.messages) - 12} more")
        else:
            lines.append("")
            lines.append("(no message metadata for this sender)")
        pane.set_body(lines)

    def _queue_line(self) -> str:
        spam, unsub = len(self.session.state.spam_queue), len(self.session.state.unsub_queue)
        if not spam and not unsub:
            return "0 queued. suspicious."
        return f"{spam} spam \u00b7 {unsub} unsub queued"

    def _set_status(self, message: str) -> None:
        status = self.query_one("#statusline", Static)
        pane = self.active_pane
        parts = [
            "PANEL" if pane == PANEL else "NORMAL",
            pane,
            self.session.position,
            self.session.counters_line(),
        ]
        if message:
            parts.append(message)
        status.update("  \u00b7  ".join(parts))
        status.set_class(bool(message), "-alert")

    def _say(self, message: str) -> None:
        self.message = message
        self._set_status(message)

    # -- focus -------------------------------------------------------------
    def _focus_left(self) -> None:
        self.active_pane = LEFT
        self.query_one(SenderPane).focus()

    def _focus_panel(self) -> None:
        self.active_pane = PANEL
        self.query_one(SidePanel).focus_tree()

    def action_toggle_pane(self) -> None:
        if self.active_pane == LEFT:
            self._focus_panel()
        else:
            self._focus_left()
        self._set_status(self.message)

    # -- key dispatch ------------------------------------------------------
    def on_key(self, event) -> None:
        key = event.key
        if key == "tab":
            self.action_toggle_pane()
            event.stop()
            return

        if self.active_pane == PANEL:
            if key in PANEL_ACTIONS:
                event.stop()
                self._panel_action(PANEL_ACTIONS[key])
                return
            if key in ("up", "down"):
                return  # the Tree handles these natively
            if key in ("left", "escape"):
                event.stop()
                self._focus_left()
                self._set_status(self.message)
                return
            if key == "e":
                event.stop()
                self._panel_action("toggle")
                return
        else:
            if key in ("up", "down"):
                event.stop()
                self.query_one(SenderPane).scroll_body(-1 if key == "up" else 1)
                return
            if key in ("left", "right"):
                event.stop()
                self._step_message(-1 if key == "left" else 1)
                return

        action = self._action_for(key)
        if action is None:
            return
        event.stop()
        self._run_action(action)

    def _action_for(self, key: str) -> str | None:
        """Map a key to an action using the active hand-mode plus the always-on keys."""
        for action, (bound_key, _word) in self.session.key_map.items():
            if key == bound_key.lower():
                return action
        # Keep the original skip/keep bindings working for existing users.
        # k is now the advertised key in both hand modes.
        if key in ("c", ";", "semicolon"):
            return "keep" if self.session.audit else "skip"
        return {"q": "quit", "z": "undo", "e": "expand"}.get(key)

    def _run_action(self, action: str) -> None:
        session = self.session
        item = session.current
        if item is None:
            return
        if self._unsubscribe_busy:
            self._say("still checking unsubscribe headers; wait a second")
            return

        if action in ("quit",):
            self.action_quit_session()
            return
        if action == "undo":
            self._apply(session.undo())
            return
        if action == "expand":
            self._toggle_expand(item)
            return

        if action in ("filter", "move"):
            self._pick_filter(action, item)
            return
        if action == "allow":
            self._apply(session.allow_item(item))
            return
        if action == "skip":
            self._apply(session.skip_item(item))
            return
        if action == "keep":
            self._apply(session.keep_item(item))
            return
        if action == "remove":
            self._apply(session.remove_item(item))
            return
        if action == "spam":
            self._apply(session.spam_item(item, self.msg_index))
            return
        if action == "unsubscribe":
            self._unsubscribe_busy = True
            self._unsubscribe(item)
            return

    # -- actions that need a modal ----------------------------------------
    @work(exclusive=True, group="modal")
    async def _pick_filter(self, action: str, item) -> None:
        names = list(self.session.manager.config.filters)
        title = (
            f"move {item.email} to which filter?"
            if action == "move"
            else f"filter {item.email} into which filter?"
        )
        choice = await self.app.push_screen_wait(
            FilterPicker(names, title=title, current=item.filter_name)
        )
        if choice is None:
            self._say("no change")
            return
        if choice == ARCHIVE_CHOICE:
            self._apply(self.session.archive_item(item, self.msg_index))
            return
        if action == "move":
            self._apply(self.session.move_item(item, choice))
        else:
            self._apply(self.session.filter_item(item, choice))

    @work(thread=True, exclusive=True, group="gmail")
    def _unsubscribe(self, item) -> None:
        """Resolve + queue the unsubscribe off the UI thread, then apply it."""
        self.app.call_from_thread(self._say, f"reading unsubscribe headers for {item.email}...")
        try:
            result = self.session.unsubscribe_item(item, self.msg_index)
        except Exception as exc:
            self.app.call_from_thread(self._unsubscribe_failed, exc)
            return
        self.app.call_from_thread(self._apply, result)

    def _unsubscribe_failed(self, exc: Exception) -> None:
        self._unsubscribe_busy = False
        self._say(f"unsubscribe lookup failed: {exc}")

    # -- panel operations --------------------------------------------------
    @work(exclusive=True, group="modal")
    async def _panel_action(self, name: str) -> None:
        panel = self.query_one(SidePanel)
        data = panel.selected_data()
        if not data or data.get("kind") == "section":
            self._say("pick a row first (J/K or arrows)")
            return
        session = self.session
        kind = data.get("kind")
        # Filter branches are containers, not entries. Acting on one used to
        # create empty-address spam/allow rows because it has no email field.
        if name != "toggle" and not data.get("email"):
            self._say("pick an entry row, not a filter heading")
            return

        if name == "toggle":
            if kind == "filter":
                node = self.query_one(SidePanel).query_one(Tree).cursor_node
                if node is not None:
                    node.toggle()
            else:
                self._say(data.get("email", ""))
            return

        if name == "move":
            choice = await self.app.push_screen_wait(
                FilterPicker(list(session.manager.config.filters),
                             title=f"move {data.get('email')} to which filter?")
            )
            if choice is None:
                self._say("no change")
                return
            self._apply(session.panel_move(data, choice))
            return

        handler = {
            "drop": session.panel_drop,
            "spam": session.panel_spam,
            "allow": session.panel_allow,
            "unqueue": session.panel_unqueue,
        }.get(name)
        if handler is None:
            return
        self._apply(handler(data))

    # -- applying a result -------------------------------------------------
    def _apply(self, result: ActionResult) -> None:
        self._unsubscribe_busy = False
        if result.index is not None:
            self.session.pos = max(0, min(result.index, len(self.session.items) - 1))
        if result.advance:
            self.expanded = False
            self.msg_index = 0
            self.session.pos += 1
            if self.session.pos >= len(self.session.items):
                # Finishing the walk is still a quit as far as the deferred
                # queue is concerned: it has to stop at the review screen, not
                # fire behind the user's back.
                self.action_quit_session()
                return
        self.message = result.message
        if result.refresh:
            self.refresh_view()
        else:
            self._set_status(result.message)

    # -- messages ----------------------------------------------------------
    def _step_message(self, delta: int) -> None:
        """Walk the sender's messages (the old \u2190/\u2192 bindings)."""
        item = self.session.current
        if item is None or not item.messages:
            self._say("no other messages for this sender")
            return
        count = len(item.messages)
        if count == 1:
            self._say("only one message from this sender")
            return
        self.msg_index = (self.msg_index + delta) % count
        if self.expanded:
            # Navigating is not collapsing: keep showing a body, now for the
            # newly selected message. _show_body rejects stale responses when
            # another arrow key arrives before this worker finishes.
            self._say(f"message {self.msg_index + 1}/{count} · reading message...")
            self._load_body(item, self.msg_index)
            return
        self._show_subjects(item)
        self._say(f"message {self.msg_index + 1}/{count}")

    # -- body loading ------------------------------------------------------
    def _toggle_expand(self, item) -> None:
        if self.expanded:
            self.expanded = False
            self._show_subjects(item)
            self._say("collapsed")
            return
        self.expanded = True
        self._say("reading message...")
        self._load_body(item, self.msg_index)

    @work(thread=True, exclusive=True, group="body")
    def _load_body(self, item, msg_index: int) -> None:
        lines = self.session.read_body(item, msg_index)
        self.app.call_from_thread(self._show_body, item, msg_index, lines)

    def _show_body(self, item, msg_index: int, lines: list[str]) -> None:
        # The user may have moved on while Gmail was responding. An older
        # response must never overwrite the body of the next sender.
        if self.session.current is not item or self.msg_index != msg_index or not self.expanded:
            return
        pane = self.query_one(SenderPane)
        header = [item.title, ""]
        pane.set_body(header + (lines or ["(empty message body)"]))
        self._say("e collapses")

    # -- finishing ---------------------------------------------------------
    def action_quit_session(self) -> None:
        if self._unsubscribe_busy:
            self._say("still checking unsubscribe headers; wait a second")
            return
        if self.session.state.pending_count:
            self._review_queue()
        else:
            self._finish()

    @work(exclusive=True, group="modal")
    async def _review_queue(self) -> None:
        state = self.session.state
        keep = await self.app.push_screen_wait(
            FireReview(
                [item.email for item in state.spam_queue],
                [item.email for item in state.unsub_queue],
            )
        )
        self._finish(keep=keep)

    def _finish(self, keep: list[str] | None = None) -> None:
        from .app import TriageResult

        self.session.quitting = True
        self.app.exit(
            TriageResult(
                session=self.session,
                completed=self.session.pos >= len(self.session.items),
                keep=keep,
                reviewed=keep is not None,
            )
        )
