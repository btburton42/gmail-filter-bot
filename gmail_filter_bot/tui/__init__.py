"""The interactive triage UI (Textual).

The package is split so that the *model* has no idea it is being rendered:

  - ``state``      -- what the user decided (pure; no Textual import)
  - ``controller`` -- the Gmail/config side effects for those decisions
  - ``sessions``   -- binds a controller + cursor to the widget layer
  - ``app``        -- Textual App/Screen, breakpoints, theme
  - ``sender``     -- the left pane (the thing being triaged)
  - ``panel``      -- the right pane (what has been decided so far)
  - ``picker``     -- the filter typeahead, as a modal screen

See ``docs/adr/0001-textual-for-the-triage-ui.md`` for why the UI is a
Textual app rather than a hand-rolled ANSI renderer.
"""
