"""Session state for interactive triage: decisions, not pixels.

Nothing in this module imports Textual. That is the point: the queue semantics
(defer, fire, undo) are the part worth testing, and they are much easier to
test when they do not need a terminal.

Two decisions shape this model:

  - **Spam and unsubscribe are deferred.** Deciding to spam a sender records a
    :class:`SpamItem`; deciding to unsubscribe records an :class:`UnsubItem`.
    Neither talks to the network. They are fired in one batch when the session
    ends, which is also why ``undo`` can reverse them here — nothing has been
    sent yet. (Before deferral, both were one-way: undoing only re-presented
    the sender and told the user to live with it.)

  - **Local config mutation happens immediately.** A filtered sender is in
    ``filters.yaml`` the moment you press the key. Only the irreversible
    Gmail side effects wait.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..config import Config, FilterConfig
from ..triage import TriageHistory
from ..unsubscribe import UnsubscribeTargets

SPAM_FILTER = "spam"
ARCHIVE_FILTER = "archive"

#: Every action a session can tally. ``label`` uses the first group, ``audit``
#: the second; keeping one table means the status line never has to guess.
COUNTERS = (
    "filtered",
    "allowed",
    "spammed",
    "unsubscribed",
    "skipped",
    "archived",
    "moved",
    "removed",
    "kept",
)

#: How far an unsubscribe got, rendered in the panel. Filled in when the
#: decision is made (the header is cheap to read) — firing happens later.
UNSUB_STATUS_DETAIL = {
    "pending": "queued",
    "one_click": "one-click",
    "url": "opened",
    "manual": "email only",
    "unsupported": "no header",
    "failed": "failed",
}


@dataclass
class SpamItem:
    """A sender queued for ``report_spam`` at the end of the session."""

    email: str
    msg_id: str | None = None
    created_filter: bool = False
    """True when this session created the ``spam`` filter (so undo can drop it)."""
    status: str = "queued"
    """``queued`` before firing, then ``reported`` or ``failed``."""
    detail: str = ""

    def describe(self) -> str:
        return self.detail or ("queued" if self.status == "queued" else self.status)


@dataclass
class UnsubItem:
    """A sender queued for an unsubscribe request at the end of the session."""

    email: str
    msg_id: str
    targets: UnsubscribeTargets | None = None
    status: str = "pending"
    detail: str = ""

    @property
    def glyph(self) -> str:
        return {"pending": "\u00b7", "one_click": "\u2713", "url": "~"}.get(self.status, "!")

    def describe(self) -> str:
        label = UNSUB_STATUS_DETAIL.get(self.status, self.status)
        if self.detail and self.status not in ("pending",):
            return self.detail
        return label


@dataclass(frozen=True)
class PanelNode:
    """One row of the right-hand panel, before it becomes a ``Tree`` node."""

    label: str
    detail: str = ""
    kind: str = ""
    """``section`` / ``filter`` / ``entry`` / ``spam`` / ``unsub`` / ``allowed``."""
    data: Any = None
    children: tuple[PanelNode, ...] = ()

    @property
    def text(self) -> str:
        """The single line a tree row shows."""
        if not self.detail:
            return self.label
        return f"{self.label}  {self.detail}"


@dataclass(frozen=True)
class PanelSection:
    """A titled group of rows in the right-hand panel."""

    title: str
    nodes: tuple[PanelNode, ...] = ()
    count: int = 0
    """Count shown in the section header; kept separate from ``len(nodes)``
    because filter sections count additions across nested filter nodes."""

    @property
    def heading(self) -> str:
        return f"{self.title} {self.count}" if self.count else self.title


@dataclass
class TriageState:
    """Everything decided so far this session.

    Holds a reference to the ``Config`` because a filter addition *is* a config
    mutation — pretending otherwise just means copying lists around and then
    throwing the copies away.
    """

    config: Config
    changes: dict[str, list[str]] = field(default_factory=dict)
    spam_queue: list[SpamItem] = field(default_factory=list)
    unsub_queue: list[UnsubItem] = field(default_factory=list)
    allowed_added: list[str] = field(default_factory=list)
    counters: dict[str, int] = field(
        default_factory=lambda: dict.fromkeys(COUNTERS, 0)
    )
    history: TriageHistory = field(default_factory=TriageHistory)
    notes: list[str] = field(default_factory=list)
    """Free-form lines for the closing summary (e.g. an archive that failed)."""
    audit_moves: list[tuple[str, str]] = field(default_factory=list)
    """``(email, destination filter)`` for ``audit`` -- unused by ``label``."""
    audit_removals: list[tuple[str, str]] = field(default_factory=list)
    """``(email, source filter)`` for ``audit`` -- unused by ``label``."""

    # -- decisions ---------------------------------------------------------
    def add_filter(self, email: str, filter_name: str) -> dict:
        """Bucket ``email`` into ``filter_name``, creating the filter if needed."""
        created = filter_name not in self.config.filters
        if created:
            self.config.filters[filter_name] = FilterConfig(
                name=filter_name,
                action="label_and_archive",
                label=filter_name,
                entries=[],
            )
        entries = self.config.filters[filter_name].entries
        if email not in entries:
            entries.append(email)
            self.changes.setdefault(filter_name, []).append(email)
        self.counters["filtered"] += 1
        return {
            "action": "filter",
            "filter_name": filter_name,
            "email": email,
            "created_filter": created,
        }

    def allow(self, email: str) -> dict:
        """Add ``email`` to the allowed list so triage stops offering it."""
        if self.config.allowed is None:
            self.config.allowed = []
        if email not in self.config.allowed:
            self.config.allowed.append(email)
        if email not in self.allowed_added:
            self.allowed_added.append(email)
        self.counters["allowed"] += 1
        return {"action": "allow", "email": email}

    def skip(self) -> dict:
        """Leave the sender for another day."""
        self.counters["skipped"] += 1
        return {"action": "skip"}

    def queue_spam(self, email: str, msg_id: str | None = None) -> dict:
        """Record a spam decision (local config now, ``report_spam`` later)."""
        created = SPAM_FILTER not in self.config.filters
        if created:
            self.config.filters[SPAM_FILTER] = FilterConfig(
                name=SPAM_FILTER, action="delete", entries=[]
            )
        entries = self.config.filters[SPAM_FILTER].entries
        if email not in entries:
            entries.append(email)
            self.changes.setdefault(SPAM_FILTER, []).append(email)
        item = SpamItem(email=email, msg_id=msg_id, created_filter=created)
        self.spam_queue.append(item)
        self.counters["spammed"] += 1
        return {
            "action": "spam",
            "email": email,
            "created_spam": created,
            "queue_index": len(self.spam_queue) - 1,
        }

    def queue_unsubscribe(
        self,
        email: str,
        msg_id: str,
        targets: UnsubscribeTargets | None = None,
        *,
        status: str = "pending",
        detail: str = "",
    ) -> dict:
        """Record an unsubscribe decision; the request goes out at session end."""
        item = UnsubItem(
            email=email, msg_id=msg_id, targets=targets, status=status, detail=detail
        )
        self.unsub_queue.append(item)
        self.counters["unsubscribed"] += 1
        return {
            "action": "unsubscribe",
            "email": email,
            "queue_index": len(self.unsub_queue) - 1,
        }

    def archive(self, email: str) -> dict:
        """Record bucketing a sender into the ``archive`` filter."""
        created = ARCHIVE_FILTER not in self.config.filters
        if created:
            self.config.filters[ARCHIVE_FILTER] = FilterConfig(
                name=ARCHIVE_FILTER, action="archive", entries=[]
            )
        entries = self.config.filters[ARCHIVE_FILTER].entries
        if email not in entries:
            entries.append(email)
            self.changes.setdefault(ARCHIVE_FILTER, []).append(email)
        self.counters["archived"] += 1
        return {
            "action": "archive",
            "email": email,
            "created_archive": created,
        }

    # -- history -----------------------------------------------------------
    def decrement(self, counter: str) -> None:
        """Take one back off a tally (used when a panel row is dropped)."""
        if self.counters.get(counter, 0) > 0:
            self.counters[counter] -= 1

    def record(self, index: int, undo_record: dict) -> None:
        self.history.push(index, undo_record)

    def undo(self) -> tuple[int, dict] | None:
        """Reverse the most recent decision, returning ``(index, record)``."""
        popped = self.history.undo()
        if popped is None:
            return None
        index, record = popped
        record = dict(record)  # type: ignore[arg-type]
        counter = self._reverse(record)
        if counter and self.counters.get(counter, 0) > 0:
            self.counters[counter] -= 1
        return index, record

    def _reverse(self, record: dict) -> str | None:
        action = record["action"]
        email = record.get("email", "")

        if action == "filter":
            self._drop_entry(record["filter_name"], email, record.get("created_filter"))
            return "filtered"

        if action == "archive":
            self._drop_entry(ARCHIVE_FILTER, email, record.get("created_archive"))
            return "archived"

        if action == "spam":
            self._drop_entry(SPAM_FILTER, email, record.get("created_spam"))
            self.drop_queued(self.spam_queue, email)
            return "spammed"

        if action == "unsubscribe":
            # Deferral makes this genuinely reversible: nothing was sent yet.
            self.drop_queued(self.unsub_queue, email)
            self.drop_queued(self.spam_queue, email)
            return "unsubscribed"

        if action == "allow":
            if self.config.allowed and email in self.config.allowed:
                self.config.allowed.remove(email)
            if email in self.allowed_added:
                self.allowed_added.remove(email)
            return "allowed"

        if action == "move":
            # Audit: put the entry back where it came from, and take the move
            # off the panel -- an undone move should not still be listed.
            self._drop_entry(record["to"], email, record.get("created_target"))
            self._unlist(self.audit_moves, email, record["to"])
            target = self.config.filters.get(record["from"])
            if target is not None and email not in target.entries:
                target.entries.append(email)
            return record.get("counter", "moved")

        if action == "remove":
            self._unlist(self.audit_removals, email, record["filter"])
            target = self.config.filters.get(record["filter"])
            if target is not None and email not in target.entries:
                target.entries.append(email)
            return "removed"

        if action == "keep":
            return "kept"

        if action == "skip":
            return "skipped"

        return None

    def _drop_entry(self, filter_name: str, email: str, created: bool | None) -> None:
        """Remove ``email`` from a filter, and the filter itself if we made it."""
        target = self.config.filters.get(filter_name)
        if target is not None and email in target.entries:
            target.entries.remove(email)
        if filter_name in self.changes and email in self.changes[filter_name]:
            self.changes[filter_name].remove(email)
            if not self.changes[filter_name]:
                del self.changes[filter_name]
        if (
            created
            and filter_name in self.config.filters
            and not self.config.filters[filter_name].entries
        ):
            del self.config.filters[filter_name]

    @staticmethod
    def _unlist(pairs: list[tuple[str, str]], email: str, where: str) -> None:
        """Drop a ``(email, filter)`` pair from an audit listing, last first."""
        for position in range(len(pairs) - 1, -1, -1):
            if pairs[position] == (email, where):
                del pairs[position]
                return

    @staticmethod
    def drop_queued(queue: list, email: str) -> None:
        for position in range(len(queue) - 1, -1, -1):
            if queue[position].email == email:
                del queue[position]
                return

    # -- views -------------------------------------------------------------
    @property
    def pending_count(self) -> int:
        return len(self.spam_queue) + len(self.unsub_queue)

    @property
    def changed_anything(self) -> bool:
        return bool(
            self.changes
            or self.allowed_added
            or self.spam_queue
            or self.unsub_queue
            or self.notes
            or self.audit_moves
            or self.audit_removals
        )

    def sections(self, *, audit: bool = False) -> tuple[PanelSection, ...]:
        """Build the right-hand panel model.

        ``label`` shows filters / spam / unsubscribe / allowed. ``audit`` shows
        the same spam queue (it is shared) plus what has been moved or removed
        from the filters under review.
        """
        sections: list[PanelSection] = []

        if audit:
            moved = [
                PanelNode(
                    label=email,
                    detail=f"to {target}",
                    kind="move",
                    data={"action": "move", "email": email, "to": target},
                )
                for email, target in self.audit_moves
            ]
            removed = [
                PanelNode(
                    label=email,
                    detail=f"from {source}",
                    kind="remove",
                    data={"action": "remove", "email": email, "filter": source},
                )
                for email, source in self.audit_removals
            ]
            sections.append(PanelSection("MOVED", tuple(moved), len(moved)))
            sections.append(PanelSection("REMOVED", tuple(removed), len(removed)))
        else:
            filters: list[PanelNode] = []
            for name in sorted(self.changes):
                emails = self.changes[name]
                children = tuple(
                    PanelNode(
                        label=email,
                        kind="entry",
                        data={"action": "entry", "filter": name, "email": email},
                    )
                    for email in emails
                )
                filters.append(
                    PanelNode(
                        label=name,
                        detail=f"+{len(emails)}",
                        kind="filter",
                        data={"action": "branch", "filter": name},
                        children=children,
                    )
                )
            sections.append(
                PanelSection("FILTERS", tuple(filters), len(self.changes))
            )

        spam = tuple(
            PanelNode(
                label=item.email,
                detail=item.describe(),
                kind="spam",
                data={"action": "spam_item", "email": item.email},
            )
            for item in self.spam_queue
        )
        sections.append(PanelSection("SPAM", spam, len(spam)))

        unsubs = tuple(
            PanelNode(
                label=item.email,
                detail=f"{item.glyph} {item.describe()}",
                kind="unsub",
                data={"action": "unsub_item", "email": item.email},
            )
            for item in self.unsub_queue
        )
        sections.append(PanelSection("UNSUBSCRIBE", unsubs, len(unsubs)))

        if not audit:
            allowed = tuple(
                PanelNode(
                    label=email,
                    kind="allowed",
                    data={"action": "allowed_item", "email": email},
                )
                for email in self.allowed_added
            )
            sections.append(PanelSection("ALLOWED", allowed, len(allowed)))

        return tuple(sections)

    def record_move(
        self, email: str, source: str, target: str, counter: str = "moved"
    ) -> dict:
        """Audit: recategorise an entry from ``source`` to ``target``.

        Sending an entry to the ``spam`` filter is also a move -- the entry is
        the same, only the destination differs -- but it is tallied as
        ``spammed``, so the counter travels in the undo record and ``undo``
        decrements whichever one was actually used.
        """
        self._drop_entry(source, email, created=False)
        created = target not in self.config.filters
        if created:
            self.config.filters[target] = FilterConfig(
                name=target, action="label_and_archive", label=target, entries=[]
            )
        if email not in self.config.filters[target].entries:
            self.config.filters[target].entries.append(email)
        self.audit_moves.append((email, target))
        self.counters[counter] += 1
        return {
            "action": "move",
            "email": email,
            "from": source,
            "to": target,
            "counter": counter,
            "created_target": created,
        }

    def record_removal(self, email: str, source: str) -> dict:
        """Audit: drop an entry from a filter entirely."""
        self._drop_entry(source, email, created=False)
        self.audit_removals.append((email, source))
        self.counters["removed"] += 1
        return {"action": "remove", "email": email, "filter": source}

    def record_keep(self) -> dict:
        self.counters["kept"] += 1
        return {"action": "keep"}

    def summary_lines(self, *, audit: bool = False) -> list[str]:
        """The closing summary, printed after the app exits."""
        if audit:
            rows = [("Kept", "kept"), ("Moved", "moved"), ("Spammed", "spammed"),
                    ("Removed", "removed")]
        else:
            rows = [
                ("Filtered", "filtered"),
                ("Allowed", "allowed"),
                ("Spammed", "spammed"),
                ("Unsubscribed", "unsubscribed"),
                ("Skipped", "skipped"),
                ("Archived", "archived"),
            ]
        width = max(len(label) for label, _ in rows) + 1
        lines = [f"  {label + ':':<{width}} {self.counters.get(key, 0)}" for label, key in rows]
        if self.pending_count:
            lines.append(
                f"  {'Queued:':<{width}} {len(self.spam_queue)} spam, "
                f"{len(self.unsub_queue)} unsubscribe (not sent yet)"
            )
        return lines
