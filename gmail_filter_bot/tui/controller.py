"""The side effects behind a triage decision.

Split from :mod:`gmail_filter_bot.tui.state` on purpose: state answers *what did
the user decide*, the controller answers *what does that cost*. Everything here
may block on the network, so the widget layer calls it from a thread worker.

The only blocking call on the hot path is :meth:`TriageController.unsubscribe`,
which reads one message's ``List-Unsubscribe`` headers so the panel can show
whether an unsubscribe is even possible before the session ends. Everything
irreversible (reporting spam, sending the unsubscribe requests) is queued by
state and fired by :meth:`TriageController.fire_queue` at the end.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from .. import settings
from ..filter_manager import FilterManager
from ..unsubscribe import UnsubscribeResult, UnsubscribeTargets, unsubscribe
from .state import SPAM_FILTER, TriageState


@dataclass
class ActionResult:
    """What the UI should do after an action."""

    advance: bool = True
    """Move to the next sender (False for a cancel or an invalid action)."""
    message: str = ""
    """One line for the vim-ish status line."""
    quit: bool = False
    cancelled: bool = False
    refresh: bool = True
    """Rebuild the right-hand panel."""
    index: int | None = None
    """Where to rewind the cursor to (undo)."""
    record: dict | None = None
    """Undo record for this decision; the *session* files it against the cursor
    position, so the controller never needs to know where the cursor is."""


@dataclass
class FireReport:
    """The result of firing the deferred queue."""

    spam_queued: int = 0
    spam_reported: int = 0
    spam_error: str = ""
    unsubs: list[tuple[str, str, str]] = field(default_factory=list)
    """``(email, status, message)`` per unsubscribe attempt."""

    @property
    def unsubscribed(self) -> int:
        return sum(1 for _email, status, _msg in self.unsubs if status in ("one_click", "url"))

    @property
    def clean(self) -> bool:
        return not self.spam_error


class TriageController:
    """Applies triage decisions to the config, and (later) to Gmail."""

    def __init__(self, manager: FilterManager, state: TriageState) -> None:
        self.manager = manager
        self.state = state

    # -- label decisions ---------------------------------------------------
    def filter(self, email: str, filter_name: str) -> ActionResult:
        """Bucket a sender into a filter."""
        record = self.state.add_filter(email, filter_name)
        settings.touch_filter_access(filter_name)
        return ActionResult(message=f"added to '{filter_name}'", record=record)

    def archive_now(self, email: str, msg_id: str) -> ActionResult:
        """Archive this message *and* future ones from the sender.

        The config entry is cheap; archiving the message already in the inbox is
        a Gmail call, so it happens here and its failure is reported rather than
        swallowed.
        """
        record = self.state.archive(email)
        settings.touch_filter_access("archive")
        try:
            if self.manager.client.archive_message(msg_id):
                return ActionResult(
                    message="archived; future mail goes to archive", record=record
                )
        except Exception as e:  # noqa: BLE001 - surfaced to the user, not swallowed
            self.state.notes.append(f"could not archive message {msg_id}: {e}")
        return ActionResult(
            message="added to 'archive' (message not archived)", record=record
        )

    def allow(self, email: str) -> ActionResult:
        return ActionResult(
            message="allowed; delivery left alone", record=self.state.allow(email)
        )

    def skip(self) -> ActionResult:
        return ActionResult(
            message="skipped; it will come back next time", record=self.state.skip()
        )

    def spam(self, email: str, msg_id: str, *, current_filter: str | None = None) -> ActionResult:
        """Queue a sender for spam. Nothing is sent until the session ends."""
        if current_filter and current_filter in self.manager.config.filters:
            entries = self.manager.config.filters[current_filter].entries
            if email in entries:
                entries.remove(email)
        record = self.state.queue_spam(email, msg_id)
        return ActionResult(
            message=f"queued for spam ({len(self.state.spam_queue)} queued)",
            record=record,
        )

    def unsubscribe(self, email: str, msg_id: str) -> ActionResult:
        """Queue an unsubscribe, resolving its target now so the panel can show it.

        Reading the header is one metadata call; sending the request is deferred
        to :meth:`fire_queue`. If the header cannot be read we still queue the
        sender, marked ``failed``, so the decision is not silently lost.
        """
        try:
            targets = self.manager.client.get_unsubscribe_targets(msg_id)
        except Exception as e:  # noqa: BLE001 - reported, not raised
            record = self.state.queue_unsubscribe(
                email, msg_id, None, status="failed", detail=str(e)
            )
            return ActionResult(
                message=f"unsubscribe queued (header read failed: {e})", record=record
            )

        status = "pending"
        detail = ""
        if targets is None:
            status, detail = "unsupported", "no List-Unsubscribe header"
        elif targets.one_click:
            status, detail = "one_click", _host(targets)
        elif targets.https_urls:
            status, detail = "url", _host(targets)
        elif targets.mailtos:
            status, detail = "manual", "email only"
        else:
            status, detail = "unsupported", "nothing actionable"

        record = self.state.queue_unsubscribe(email, msg_id, targets, status=status, detail=detail)
        return ActionResult(
            message=f"unsubscribe queued ({detail}) \u00b7 "
            f"{len(self.state.unsub_queue)} queued",
            record=record,
        )

    def undo(self) -> ActionResult:
        popped = self.state.undo()
        if popped is None:
            return ActionResult(advance=False, message="nothing to undo", refresh=False)
        index, record = popped
        note = ""
        if record["action"] in ("filter", "spam", "archive"):
            note = " (config only)"
        return ActionResult(
            advance=False,
            message=f"undid {record['action']}{note}",
            index=index,
        )

    # -- deferred queue ----------------------------------------------------
    def fire_queue(self, progress: Callable[[str], None] | None = None) -> FireReport:
        """Send every deferred spam report and unsubscribe request.

        Called once, after the app has exited and the terminal is normal again,
        so the output is plain and greppable.
        """
        report = FireReport(spam_queued=len(self.state.spam_queue))

        if self.state.spam_queue:
            if progress:
                progress(f"reporting {len(self.state.spam_queue)} sender(s) as spam...")
            addresses = [item.email for item in self.state.spam_queue]
            try:
                reported = self.manager.client.report_spam(addresses)
                report.spam_reported = reported
                for item in self.state.spam_queue:
                    item.status = "reported"
                    item.detail = f"{reported} message(s) moved"
            except Exception as e:  # noqa: BLE001 - reported in the summary
                report.spam_error = str(e)
                for item in self.state.spam_queue:
                    item.status = "failed"
                    item.detail = str(e)[:60]

        for item in self.state.unsub_queue:
            if progress:
                progress(f"unsubscribing {item.email}...")
            result: UnsubscribeResult = unsubscribe(item.targets)
            item.status = result.status
            item.detail = result.message
            report.unsubs.append((item.email, result.status, result.message))

        return report

    # -- persistence -------------------------------------------------------
    def save(self) -> str:
        """Persist the local config; returns the path written."""
        self.manager.config.save(self.manager.config_path)
        return str(self.manager.config_path)

    def summary_lines(self, *, audit: bool = False) -> list[str]:
        return self.state.summary_lines(audit=audit)

    def change_lines(self) -> list[str]:
        """``filter: +n`` rows plus the queued senders, for the closing summary."""
        lines: list[str] = []
        if self.state.changes:
            lines.append("Filters updated:")
            for name in sorted(self.state.changes):
                emails = self.state.changes[name]
                lines.append(f"  {name}: +{len(emails)} sender(s)")
                lines.extend(f"    - {email}" for email in emails)
        if self.state.spam_queue:
            lines.append("Queued for spam (sent at the end):")
            lines.extend(f"    - {item.email}" for item in self.state.spam_queue)
        if self.state.unsub_queue:
            lines.append("Queued for unsubscribe (sent at the end):")
            lines.extend(
                f"    - {item.email}  [{item.describe()}]" for item in self.state.unsub_queue
            )
        if self.state.allowed_added:
            lines.append("Allowed (never filtered again):")
            lines.extend(f"    - {email}" for email in self.state.allowed_added)
        return lines


def _host(targets: UnsubscribeTargets) -> str:
    """Host of the best available unsubscribe URL, for display."""
    urls = targets.https_urls
    if not urls:
        return "email only"
    url = urls[0]
    return url.split("//", 1)[-1].split("/", 1)[0] or url


def entry_actions(config, email: str) -> list[str]:
    """Filters that already cover ``email`` (exact or wildcard).

    Used by the panel to explain *why* an entry is showing, and by ``audit`` to
    keep an entry from being moved to where it already is.
    """
    hits: list[str] = []
    for name, filter_config in sorted(config.filters.items()):
        if email in filter_config.entries:
            hits.append(name)
    return hits


def spam_target(config) -> str:
    return SPAM_FILTER if SPAM_FILTER in config.filters else "spam"
