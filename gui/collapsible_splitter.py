"""
redactor_common/gui/collapsible_splitter.py

Promoted from epub's toggle_tag_panel()/_sync_tag_panel_collapsed_
indicator()/_on_splitter_moved() plus TagPanel's own collapse button --
video had the same 2-pane horizontal splitter (side panel + table)
but no collapse mechanism at all; mp3 has no side panel to begin with.

Two pieces, deliberately separate: the owning window resizes the
splitter (SplitterPaneCollapser -- resizing is the window's job, since
it's the one that knows the splitter), the panel widget itself only
ever asks to be toggled and displays whether it currently is
(CollapseToggleButton) -- same split of responsibility the original
had ("the panel doesn't control its own width -- MainWindow does").

Usage (2-pane horizontal splitter: a side panel + the main table):
    from redactor_common.gui.collapsible_splitter import SplitterPaneCollapser

    self._panel_collapser = SplitterPaneCollapser(
        self.splitter, pane_index=0, collapsed_width=32, default_width=340,
    )
    self.tag_panel.collapseToggleRequested.connect(self._toggle_panel)
    self.splitter.splitterMoved.connect(lambda *_: self._sync_panel_indicator())

    def _toggle_panel(self) -> None:
        self._panel_collapser.toggle()
        self._sync_panel_indicator()

    def _sync_panel_indicator(self) -> None:
        self.tag_panel.set_collapsed_indicator(self._panel_collapser.is_collapsed())

And inside the panel widget itself:
    from redactor_common.gui.collapsible_splitter import CollapseToggleButton

    self.collapse_toggle_btn = CollapseToggleButton()
    self.collapse_toggle_btn.clicked.connect(self.collapseToggleRequested.emit)
    ...
    def set_collapsed_indicator(self, collapsed: bool) -> None:
        self.collapse_toggle_btn.set_collapsed(collapsed)
"""

from __future__ import annotations

from PyQt6.QtWidgets import QPushButton, QSplitter


class SplitterPaneCollapser:
    """Owns the collapse/restore behavior for one pane of a 2-pane
    QSplitter. Collapsing shrinks the pane to `collapsed_width` -- not
    0 -- so a toggle button living inside that pane stays reachable to
    expand it again (the splitter's own drag handle can still reach a
    genuine 0 width by hand; this is only about the *toggle button's*
    affordance). The width right before collapsing is remembered and
    restored on the next toggle back.
    """

    def __init__(
        self,
        splitter: QSplitter,
        pane_index: int,
        collapsed_width: int,
        default_width: int,
        min_restored_width: int = 200,
    ):
        self.splitter = splitter
        self.pane_index = pane_index
        self.other_index = 1 - pane_index
        self.collapsed_width = collapsed_width
        self.min_restored_width = min_restored_width
        self._last_width = default_width

    def is_collapsed(self) -> bool:
        sizes = self.splitter.sizes()
        pane_width = sizes[self.pane_index] if sizes else 0
        return pane_width <= self.collapsed_width + 10

    def toggle(self) -> None:
        sizes = self.splitter.sizes()
        pane_width = sizes[self.pane_index] if sizes else 0
        if not self.is_collapsed():
            self._last_width = pane_width
            freed = pane_width - self.collapsed_width
            sizes[self.pane_index] = self.collapsed_width
            sizes[self.other_index] = sizes[self.other_index] + freed
        else:
            restored = max(self._last_width, self.min_restored_width)
            taken = restored - pane_width
            sizes[self.other_index] = max(sizes[self.other_index] - taken, 100)
            sizes[self.pane_index] = restored
        self.splitter.setSizes(sizes)


class CollapseToggleButton(QPushButton):
    """The small "◀"/"▶" button a collapsible splitter pane uses to ask
    to be toggled -- the pane itself doesn't resize anything (see
    SplitterPaneCollapser); this only emits `clicked` and displays
    whichever state it's told about via set_collapsed()."""

    def __init__(self, width: int = 26, parent=None):
        super().__init__("◀", parent)
        self.setMaximumWidth(width)

    def set_collapsed(self, collapsed: bool) -> None:
        self.setText("▶" if collapsed else "◀")
        self.setToolTip("Restore this panel" if collapsed else "Minimize this panel")
