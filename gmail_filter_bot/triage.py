"""Shared helpers for the interactive triage commands (``label`` and ``audit``).

Both commands walk a list of items one at a time and let the user act on
each. A finger slip (pressing the wrong key) is recoverable with ``undo``:
the command records every applied action on a :class:`TriageHistory` stack,
and ``undo`` pops the most recent one, reverses it, and re-presents the item
it applied to.

The stack stores ``(index, undo_record)`` pairs where ``index`` is the
position in the item list the action was taken on, so a rewind can move the
walk pointer back to that item. The *contents* of ``undo_record`` are
command-specific; this module only owns the stack mechanics.
"""

from __future__ import annotations


class TriageHistory:
    """A bounded undo stack for interactive triage.

    Stores ``(index, undo_record)`` pairs. ``undo()`` pops the most recent
    entry and returns it, or ``None`` when there is nothing to rewind.
    """

    def __init__(self) -> None:
        self._stack: list[tuple[int, object]] = []

    def push(self, index: int, undo_record: object) -> None:
        """Record an action taken on the item at ``index``."""
        self._stack.append((index, undo_record))

    def undo(self) -> tuple[int, object] | None:
        """Pop and return the most recent ``(index, undo_record)``, or ``None``."""
        if not self._stack:
            return None
        return self._stack.pop()

    @property
    def empty(self) -> bool:
        return not self._stack

    def __len__(self) -> int:
        return len(self._stack)
