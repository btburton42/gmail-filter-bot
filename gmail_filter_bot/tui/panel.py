"""The right pane: everything decided so far.

One ``Tree`` with a section per decision kind, because the alternative --
``TabbedContent`` -- hides half the answer behind a tab and makes the mouse do
more work than the keyboard.

The tree is rebuilt wholesale on every decision. That is deliberate: the panel
is small (tens of rows), rebuilding is O(rows), and incremental updates would
mean holding a widget-per-entry mapping and getting it wrong on undo. The
cursor is restored by remembering the selected email, not the node.

Rows carry ``data`` dicts (``{"kind": ..., "email": ..., "filter": ...}``) that
the screen turns into session operations. Keeping that translation in the data
rather than in the widget is what lets the panel ops be unit-tested without a
terminal.
"""

from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import Vertical
from textual.widgets import Static, Tree

from .state import PanelNode, PanelSection


class SidePanel(Vertical):
    """Titled tree of decisions, plus a one-line queue summary."""

    # Geometry lives in styles.tcss so there is one layout, not four.
    DEFAULT_CSS = """
    SidePanel {
        width: 38;
        height: 1fr;
    }
    """

    def compose(self) -> ComposeResult:
        yield Static("SESSION", id="panel-heading", markup=False)
        yield Tree("decisions", id="panel-tree")
        yield Static("", id="panel-queue", markup=False)

    def on_mount(self) -> None:
        tree = self.query_one("#panel-tree", Tree)
        tree.show_root = False
        tree.guide_depth = 2
        tree.root.expand()

    # -- rendering ---------------------------------------------------------
    def render_sections(self, sections: tuple[PanelSection, ...], *, heading: str = "SESSION") -> None:
        tree = self.query_one("#panel-tree", Tree)
        selected = self.selected_key()

        tree.clear()
        for section in sections:
            if section.count == 0:
                continue
            branch = tree.root.add(
                _row(f"{section.heading}", "section"),
                data={"kind": "section", "title": section.title},
                expand=True,
            )
            self._add_nodes(branch, section.nodes)

        self.query_one("#panel-heading", Static).update(heading)
        self._restore(selected)

    def _add_nodes(self, parent, nodes: tuple[PanelNode, ...]) -> None:
        for node in nodes:
            data = dict(node.data or {})
            # `kind` is the node's own type; the session ops dispatch on it, so
            # stamping it here keeps every row self-describing.
            data.setdefault("kind", node.kind)
            label = node.text if node.detail else node.label
            if node.children:
                # Expanded, not collapsed: the whole point of the panel is
                # seeing *which* senders went where without another keypress.
                branch = parent.add(_row(label, node.kind), data=data, expand=True)
                self._add_nodes(branch, node.children)
            else:
                parent.add_leaf(_row(label, node.kind), data=data)

    def set_queue_line(self, text: str) -> None:
        self.query_one("#panel-queue", Static).update(text)

    # -- selection ---------------------------------------------------------
    def selected_data(self) -> dict:
        tree = self.query_one("#panel-tree", Tree)
        node = tree.cursor_node
        if node is None or not isinstance(node.data, dict):
            return {}
        return node.data

    def selected_key(self) -> tuple[str, str] | None:
        data = self.selected_data()
        if not data:
            return None
        return (str(data.get("kind", "")), str(data.get("email", "")))

    def _restore(self, key: tuple[str, str] | None) -> None:
        """Put the cursor back on the same row after a rebuild, if it survived."""
        tree = self.query_one("#panel-tree", Tree)
        first_leaf = None
        for node in _descend(tree.root):
            if not isinstance(node.data, dict):
                continue
            if first_leaf is None and not node.children:
                first_leaf = node
            if key is not None and (
                str(node.data.get("kind", "")), str(node.data.get("email", ""))
            ) == key:
                tree.move_cursor(node)
                return
        # The row is gone (typically: someone just removed it). Fall back to
        # the first leaf, which is the nearest thing that still exists.
        tree.move_cursor(first_leaf or tree.root)

    def move_cursor(self, delta: int) -> None:
        tree = self.query_one("#panel-tree", Tree)
        tree.action_cursor_up() if delta < 0 else tree.action_cursor_down()

    def focus_tree(self) -> None:
        self.query_one("#panel-tree", Tree).focus()


def _descend(node):
    """Depth-first over a ``TreeNode`` tree (Textual has no ``walk``)."""
    for child in node.children:
        yield child
        yield from _descend(child)


def _row(label: str, kind: str) -> str:
    """Prefix a row by kind, the way a vim filetree marks node types."""
    marker = {
        "section": "",
        "filter": "",
        "entry": "  ",
        "spam": "  ",
        "unsub": "  ",
        "allowed": "  ",
        "move": "  ",
        "remove": "  ",
    }.get(kind, "  ")
    return f"{marker}{label}"
