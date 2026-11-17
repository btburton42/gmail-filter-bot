"""The session: one triage walk, independent of how it is drawn.

``label`` and ``audit`` are the same walk over different items -- a cursor, a
left pane showing one thing, a right pane showing what you have decided so far,
and an undo stack. They differ in key vocabulary, in which decisions exist, and
in what the panel sections are called, so they share one class with a ``kind``
flag rather than two nearly-identical ones.

The session is the seam the tests aim at: everything here is synchronous and
touches the outside world only through the injected manager/controller.

Note the split of responsibility with the controller: the controller performs a
decision and hands back an *undo record*; the session records it against the
cursor position. That way the index in the history is always the real one, and
the controller never has to know where the cursor is.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..filter_manager import FilterManager
from ..gmail_client import Sender
from ..hand_mode import get_audit_key_map, get_key_map
from .controller import ActionResult, TriageController
from .state import ARCHIVE_FILTER, SPAM_FILTER, PanelSection, TriageState

LABEL = "label"
AUDIT = "audit"

#: Panel rows are labelled by what they *look* like ("spam", "unsub"); the
#: session dispatches on what it can *do* ("spam_item", "unsub_item"). One dict,
#: applied in one place, so a row can never be silently unroutable.
_PANEL_KIND = {"spam": "spam_item", "unsub": "unsub_item"}


def _kind(data: dict) -> str:
    """The operation name for a panel row's data."""
    kind = str(data.get("kind", ""))
    return _PANEL_KIND.get(kind, kind)


@dataclass(frozen=True)
class Item:
    """One step of the walk."""

    email: str
    name: str = ""
    messages: tuple[tuple[str, str], ...] = ()
    """``(subject, message_id)`` pairs, newest first."""
    filter_name: str = ""
    """``audit`` only: the filter this entry currently lives in."""

    @classmethod
    def from_sender(cls, sender: Sender) -> Item:
        return cls(
            email=sender.email,
            name=sender.name,
            messages=tuple((msg.subject, msg.message_id) for msg in sender.messages),
        )

    @property
    def subject(self) -> str:
        return self.messages[0][0] if self.messages else ""

    def message_id(self, index: int) -> str:
        if not self.messages:
            return ""
        index = max(0, min(index, len(self.messages) - 1))
        return self.messages[index][1]

    @property
    def title(self) -> str:
        return f"{self.name} <{self.email}>" if self.name else self.email


class TriageSession:
    """A walk over ``items`` with a decision history."""

    def __init__(
        self,
        manager: FilterManager,
        state: TriageState,
        controller: TriageController,
        *,
        kind: str = LABEL,
        items: list[Item] | None = None,
    ) -> None:
        self.manager = manager
        self.state = state
        self.controller = controller
        self.kind = kind
        self.items: list[Item] = items or []
        self.pos = 0
        self.quitting = False
        self._body_cache: dict[tuple[str, str], tuple[str, ...]] = {}

    # -- queries -----------------------------------------------------------
    @property
    def audit(self) -> bool:
        return self.kind == AUDIT

    @property
    def key_map(self) -> dict[str, tuple[str, str]]:
        return get_audit_key_map() if self.audit else get_key_map()

    @property
    def current(self) -> Item | None:
        if 0 <= self.pos < len(self.items):
            return self.items[self.pos]
        return None

    @property
    def position(self) -> str:
        return f"{min(self.pos + 1, len(self.items))}/{len(self.items)}"

    def sections(self) -> tuple[PanelSection, ...]:
        return self.state.sections(audit=self.audit)

    def counters_line(self) -> str:
        counters = self.state.counters
        if self.audit:
            pairs = [("kept", "kept"), ("moved", "moved"), ("spammed", "spam"),
                     ("removed", "gone")]
        else:
            pairs = [
                ("filtered", "filtered"),
                ("allowed", "allowed"),
                ("spammed", "spam"),
                ("unsubscribed", "unsub"),
                ("skipped", "skipped"),
                ("archived", "archived"),
            ]
        shown = [f"{label} {counters[key]}" for key, label in pairs]
        if self.state.pending_count:
            shown.append(f"queued {self.state.pending_count}")
        return "  \u00b7  ".join(shown)

    def _record(self, result: ActionResult) -> ActionResult:
        """Attach the controller's undo record to the current cursor position."""
        if result.record is not None:
            self.state.record(self.pos, result.record)
        return result

    # -- decisions ---------------------------------------------------------
    def filter_item(self, item: Item, filter_name: str) -> ActionResult:
        return self._record(self.controller.filter(item.email, filter_name))

    def archive_item(self, item: Item, msg_index: int) -> ActionResult:
        return self._record(
            self.controller.archive_now(item.email, item.message_id(msg_index))
        )

    def allow_item(self, item: Item) -> ActionResult:
        return self._record(self.controller.allow(item.email))

    def skip_item(self, item: Item) -> ActionResult:
        return self._record(self.controller.skip())

    def spam_item(self, item: Item, msg_index: int) -> ActionResult:
        if self.audit:
            # Audit entries are filter entries, not senders with messages, and
            # audit has never contacted Gmail: queueing a report_spam here would
            # invent a side effect the command did not have.
            if item.filter_name == SPAM_FILTER:
                return self.keep_item(item)
            record = self.state.record_move(
                item.email, item.filter_name, SPAM_FILTER, counter="spammed"
            )
            return self._record(
                ActionResult(message=f"moved '{item.email}' to spam", record=record)
            )
        return self._record(
            self.controller.spam(
                item.email,
                item.message_id(msg_index),
                current_filter=item.filter_name or None,
            )
        )

    def unsubscribe_item(self, item: Item, msg_index: int) -> ActionResult:
        return self._record(
            self.controller.unsubscribe(item.email, item.message_id(msg_index))
        )

    def keep_item(self, item: Item) -> ActionResult:
        return self._record(ActionResult(message="kept", record=self.state.record_keep()))

    def remove_item(self, item: Item) -> ActionResult:
        if not self.audit:
            return ActionResult(advance=False, message="remove is an audit action")
        record = self.state.record_removal(item.email, item.filter_name)
        return self._record(ActionResult(message=f"removed from '{item.filter_name}'",
                                         record=record))

    def move_item(self, item: Item, target: str) -> ActionResult:
        if not self.audit:
            return self.filter_item(item, target)
        if target == item.filter_name:
            return self.keep_item(item)
        record = self.state.record_move(item.email, item.filter_name, target)
        return self._record(ActionResult(message=f"moved '{item.email}' to '{target}'",
                                         record=record))

    def undo(self) -> ActionResult:
        return self.controller.undo()

    # -- panel operations --------------------------------------------------
    #
    # The panel is a flat list of rows, each carrying a ``data`` dict that says
    # what it is. Every operation reduces to: take the email out of wherever it
    # currently sits, then put it somewhere else (or nowhere).
    def panel_drop(self, data: dict) -> ActionResult:
        """``x``: take the row out of the session entirely."""
        email, kind = data.get("email", ""), _kind(data)
        if kind == "entry":
            if data["filter"] == SPAM_FILTER:
                # Spam entries also have a SPAM queue row. Both views describe
                # the same decision, so dropping either must remove both.
                self._unqueue_spam(email)
                return ActionResult(advance=False, message=f"unqueued {email} for spam")
            return self._drop_entry(email, data["filter"])
        if kind == "allowed":
            self._unallow(email)
            return ActionResult(advance=False, message=f"un-allowed {email}")
        if kind == "spam_item":
            self._unqueue_spam(email)
            return ActionResult(
                advance=False,
                message=f"dropped {email} from the spam queue "
                f"({len(self.state.spam_queue)} queued)",
            )
        if kind == "unsub_item":
            self._unqueue_unsub(email)
            return ActionResult(
                advance=False,
                message=f"dropped {email} from the unqueue "
                f"({len(self.state.unsub_queue)} queued)",
            )
        return ActionResult(advance=False, message="nothing to drop here", refresh=False)

    def _drop_entry(self, email: str, filter_name: str) -> ActionResult:
        entries = self.state.config.filters.get(filter_name)
        if entries is None or email not in entries.entries:
            return ActionResult(advance=False, message="entry already gone", refresh=False)
        entries.entries.remove(email)
        if filter_name in self.state.changes:
            changed = self.state.changes[filter_name]
            if email in changed:
                changed.remove(email)
            if not changed:
                del self.state.changes[filter_name]
        self.state.decrement("archived" if filter_name == ARCHIVE_FILTER else "filtered")
        if not entries.entries and filter_name in (SPAM_FILTER, ARCHIVE_FILTER):
            del self.state.config.filters[filter_name]
        return ActionResult(advance=False, message=f"dropped {email} from '{filter_name}'")

    def _unallow(self, email: str) -> None:
        if self.state.config.allowed and email in self.state.config.allowed:
            self.state.config.allowed.remove(email)
        if email in self.state.allowed_added:
            self.state.allowed_added.remove(email)
        self.state.decrement("allowed")

    def _unqueue_spam(self, email: str) -> None:
        before = len(self.state.spam_queue)
        self.state.drop_queued(self.state.spam_queue, email)
        if len(self.state.spam_queue) != before:
            self.state.decrement("spammed")
        entries = self.state.config.filters.get(SPAM_FILTER)
        if entries is not None and email in entries.entries:
            entries.entries.remove(email)
            if SPAM_FILTER in self.state.changes:
                changed = self.state.changes[SPAM_FILTER]
                if email in changed:
                    changed.remove(email)
                if not changed:
                    del self.state.changes[SPAM_FILTER]
            if not entries.entries:
                del self.state.config.filters[SPAM_FILTER]

    def _unqueue_unsub(self, email: str) -> None:
        before = len(self.state.unsub_queue)
        self.state.drop_queued(self.state.unsub_queue, email)
        if len(self.state.unsub_queue) != before:
            self.state.decrement("unsubscribed")

    def panel_move(self, data: dict, target: str) -> ActionResult:
        """``m``: re-bucket a row into a different filter."""
        email, kind = data.get("email", ""), _kind(data)
        if kind == "entry":
            if data["filter"] == SPAM_FILTER:
                self._unqueue_spam(email)
            elif data["filter"] == target:
                return ActionResult(advance=False, message="already in that filter")
            else:
                self._drop_entry(email, data["filter"])
        elif kind == "spam_item":
            self._unqueue_spam(email)
        elif kind == "unsub_item":
            self._unqueue_unsub(email)
        elif kind == "allowed":
            self._unallow(email)
        else:
            return ActionResult(advance=False, message="pick a row to move", refresh=False)

        record = self.state.add_filter(email, target)
        self.state.record(self.pos, record)
        return ActionResult(advance=False, message=f"moved {email} to '{target}'")

    def panel_spam(self, data: dict) -> ActionResult:
        """``s``: queue a row for spam."""
        email, kind = data.get("email", ""), _kind(data)
        if any(item.email == email for item in self.state.spam_queue):
            return ActionResult(
                advance=False, message=f"{email} is already queued for spam"
            )
        if kind == "entry":
            self._drop_entry(email, data["filter"])
        elif kind == "allowed":
            self._unallow(email)
        elif kind == "unsub_item":
            self._unqueue_unsub(email)
        elif kind not in ("filter", "spam_item"):
            return ActionResult(advance=False, message="pick a row to spam", refresh=False)

        record = self.state.queue_spam(email)
        self.state.record(self.pos, record)
        return ActionResult(
            advance=False,
            message=f"queued {email} for spam ({len(self.state.spam_queue)} queued)",
        )

    def panel_allow(self, data: dict) -> ActionResult:
        """``a``: stop filtering a row and leave its delivery alone."""
        email, kind = data.get("email", ""), _kind(data)
        if kind == "entry":
            if data["filter"] == SPAM_FILTER:
                self._unqueue_spam(email)
            else:
                self._drop_entry(email, data["filter"])
        elif kind == "spam_item":
            self._unqueue_spam(email)
        elif kind == "unsub_item":
            self._unqueue_unsub(email)
        elif kind not in ("filter", "allowed"):
            return ActionResult(advance=False, message="pick a row to allow", refresh=False)
        record = self.state.allow(email)
        self.state.record(self.pos, record)
        return ActionResult(advance=False, message=f"allowed {email}")

    def panel_unqueue(self, data: dict) -> ActionResult:
        """``u``: only meaningful for rows that are waiting to be fired."""
        email, kind = data.get("email", ""), _kind(data)
        if kind == "spam_item":
            self._unqueue_spam(email)
            return ActionResult(
                advance=False,
                message=f"dropped {email} from the spam queue "
                f"({len(self.state.spam_queue)} queued)",
            )
        if kind == "unsub_item":
            self._unqueue_unsub(email)
            return ActionResult(
                advance=False,
                message=f"dropped {email} from the unqueue "
                f"({len(self.state.unsub_queue)} queued)",
            )
        return ActionResult(
            advance=False,
            message="unqueue applies to queued spam/unsubscribe rows (x drops)",
            refresh=False,
        )

    # -- bodies ------------------------------------------------------------
    def read_body(self, item: Item, msg_index: int) -> list[str]:
        """Fetch a message body for the left pane (blocking; call in a worker)."""
        msg_id = item.message_id(msg_index)
        if not msg_id:
            return []
        cache_key = (item.email, msg_id)
        if cache_key in self._body_cache:
            return list(self._body_cache[cache_key])
        try:
            lines = self.manager.client.get_message_preview(
                msg_id, max_lines=300, max_chars=12000
            )
        except Exception as e:  # noqa: BLE001 - shown in the body pane
            return [f"(could not read message body: {e})"]
        self._body_cache[cache_key] = tuple(lines)
        return lines
