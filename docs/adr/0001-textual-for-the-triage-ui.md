# Build the triage UI on Textual instead of hand-rolled ANSI

`gfb label` and `gfb audit` used to draw themselves: a `_TriageView` that
computed terminal width, rewrote rows with escape sequences, erased exactly what
it had painted, and read raw keys with a `cbreak` wrapper (falling back to
`input()` when stdin was not a TTY). That worked, but every new idea — a
right-hand pane, a clickable row, a status line that does not scroll away,
undoing a queued action — meant another round of terminal arithmetic and another
set of tests that had to allocate a pty to assert anything. We replaced it with
[Textual](https://textual.textualize.io/): the layout is CSS, the panes are
focusable widgets, the mouse works, blocking calls run in thread workers, and
the whole thing is testable headlessly with `run_test()` and `Pilot`. The
hand-rolled renderer is deleted rather than kept as a fallback, because two
implementations of the same interaction is how they drift apart. The cost is
real — Textual is a large dependency for a CLI, and the narrow-terminal case now
lives behind a breakpoint (`HORIZONTAL_BREAKPOINTS`) instead of simply not
drawing a panel — but the alternative was writing a worse version of a layout
engine by hand, which is not what this tool is for.

## Consequences

Deferring the irreversible work came with the move, and is mostly a consequence
of having a panel to show it in: `report_spam` and the unsubscribe requests are
queued during the session and fired once, in the normal terminal, after the app
exits. That is what makes them undoable, and it is why quitting mid-session
stops at a review screen instead of firing behind the user's back.
