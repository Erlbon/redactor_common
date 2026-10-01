"""
redactor_common/gui/duplicates_dialog.py

The shared "Find Duplicates" REVIEW dialog, promoted from videoredactor's
gui/duplicates_dialog.py (2026-10-01) and generalized: the app finds the
candidate groups (core/duplicates.py's `DuplicateGroup`s), this dialog
shows them and offers the actions.

Duplicates are not errors (see core/duplicates.py): the dialog groups
candidates, says WHY each group matched (a tier label in words plus a
reason sentence -- not just a colour), selects nothing by default, and
lets the user mark a group "not duplicates" so it stops appearing.

Actions (all on the SELECTED member rows; group rows are ignored):
Reveal in Folder, Open (default app), Select These in the List (hands the
items to `on_select_in_list` and closes), "Not duplicates (hide this
group)" and -- only when `allow_trash` -- Move Selected to Recycle
Bin..., behind an explicit confirm that defaults to No, listing the files.
The trash action refuses to remove every member of a group in one go
(at least one must stay), isolates failures per file and never touches a
file that isn't selected. Double-click / Enter on a member opens it.

`run_find_duplicates()` is the whole flow: run the app's `find_fn` on a
worker thread under a cancellable progress dialog, then open this dialog.
"""

from __future__ import annotations

import threading
from typing import Any, Callable, Optional, Sequence

from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtGui import QFont
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QHeaderView,
    QLabel,
    QMessageBox,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from redactor_common.core.duplicates import (
    DismissStore,
    DuplicateGroup,
    DuplicateMember,
    sort_groups,
    tier_label,
)
from redactor_common.core.os_utils import open_with_default_app, reveal_in_file_manager
from redactor_common.core.trash import TrashError, move_to_trash
from redactor_common.gui.background_call import BackgroundCancelled, call_in_background
from redactor_common.gui.progress import ProgressReporter

DEFAULT_TITLE = "Find Duplicates"
MEMBER_ROLE = Qt.ItemDataRole.UserRole
GROUP_ROLE = Qt.ItemDataRole.UserRole + 1

POLICY_TEXT = (
    "Duplicates are not always mistakes: two editions of a book, or one recording on several "
    "releases, can be perfectly legitimate. <b>You decide what to keep</b> -- nothing is "
    "selected, and nothing is changed until you choose an action below."
)
EMPTY_TEXT = "No groups of possible duplicates to review."
_MAX_LISTED = 10   # files named in the confirm / failure messages


def _count(n: int, noun: str) -> str:
    return f"{n} {noun}{'' if n == 1 else 's'}"


class DuplicatesDialog(QDialog):
    """Groups of duplicate candidates for review. After exec(): `to_select`
    holds the items for "Select these in the list" (dialog accepted) and
    `trashed` the items already moved to the Recycle Bin.

    `columns` is a sequence of (key, label); each member row shows
    `member.fields.get(key, "")`. `on_trashed(items)`, when given, is
    called after every trash batch so the app can refresh at once."""

    def __init__(
        self,
        groups: Sequence[DuplicateGroup],
        columns: Sequence[tuple[str, str]],
        parent: Optional[QWidget] = None,
        *,
        title: str = DEFAULT_TITLE,
        dismiss_store: Optional[DismissStore] = None,
        on_select_in_list: Optional[Callable[[list], None]] = None,
        on_trashed: Optional[Callable[[list], None]] = None,
        trash: Callable[[str], None] = move_to_trash,
        allow_trash: bool = True,
        intro_text: str = "",
    ):
        super().__init__(parent)
        self._title = title
        self._columns = list(columns)
        self._store = dismiss_store
        self._on_select = on_select_in_list
        self._on_trashed = on_trashed
        self._trash = trash
        self._allow_trash = allow_trash
        self.to_select: list = []
        self.trashed: list = []
        # Our own copies: trashing shrinks groups, which must not mutate the caller's.
        self._groups: list[DuplicateGroup] = sort_groups(
            DuplicateGroup(g.key, g.tier, g.reason, list(g.members)) for g in groups
        )
        self.setWindowTitle(title)
        self.resize(1000, 480)

        layout = QVBoxLayout(self)
        self.summary_label = QLabel()
        self.summary_label.setWordWrap(True)
        self.summary_label.setTextFormat(Qt.TextFormat.RichText)
        layout.addWidget(self.summary_label)
        self._intro_html = (f"{intro_text} " if intro_text else "") + POLICY_TEXT

        self.show_hidden_check = QCheckBox()
        self.show_hidden_check.toggled.connect(lambda _on: self._rebuild())
        layout.addWidget(self.show_hidden_check)

        self.empty_label = QLabel(EMPTY_TEXT)
        self.empty_label.setWordWrap(True)
        self.empty_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.empty_label)

        self.tree = QTreeWidget()
        self.tree.setColumnCount(len(self._columns))
        self.tree.setHeaderLabels([label for _key, label in self._columns])
        self.tree.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.tree.setRootIsDecorated(True)
        self.tree.setAccessibleName("Groups of possible duplicates")
        header = self.tree.header()
        header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        header.setStretchLastSection(False)
        for col in range(len(self._columns)):
            self.tree.setColumnWidth(col, (260, 300)[col] if col < 2 else 90)
        self.tree.itemSelectionChanged.connect(self._update_buttons)
        self.tree.itemActivated.connect(self._activated)
        layout.addWidget(self.tree, 1)

        buttons = QDialogButtonBox()
        self.reveal_button = buttons.addButton("Reveal in Folder", QDialogButtonBox.ButtonRole.ActionRole)
        self.open_button = buttons.addButton("Open", QDialogButtonBox.ButtonRole.ActionRole)
        self.open_button.setToolTip("Open in the default app")
        self.select_button = buttons.addButton("Select These in the List", QDialogButtonBox.ButtonRole.AcceptRole)
        self.dismiss_button = buttons.addButton("Not Duplicates (Hide This Group)", QDialogButtonBox.ButtonRole.ActionRole)
        self.restore_button = buttons.addButton("Show This Group Again", QDialogButtonBox.ButtonRole.ActionRole)
        self.trash_button = buttons.addButton("Move Selected to Recycle Bin...", QDialogButtonBox.ButtonRole.ActionRole)
        buttons.addButton(QDialogButtonBox.StandardButton.Close)
        self.reveal_button.clicked.connect(self._reveal)
        self.open_button.clicked.connect(self._open)
        self.select_button.clicked.connect(self._select_in_list)
        self.dismiss_button.clicked.connect(self._dismiss)
        self.restore_button.clicked.connect(self._restore)
        self.trash_button.clicked.connect(self._trash_selected)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        # Not offered when there is nothing to back it / the app opted out.
        self.dismiss_button.setVisible(self._store is not None)
        self.restore_button.setVisible(self._can_undismiss())
        self.select_button.setVisible(self._on_select is not None)
        self.trash_button.setVisible(self._allow_trash)
        self._rebuild()

    # --- model -> tree ------------------------------------------------

    def _can_undismiss(self) -> bool:
        """True when the store overrides the optional `undismiss` (duck-typed
        stores that merely have a callable one count too)."""
        undismiss = getattr(self._store, "undismiss", None)
        return callable(undismiss) and getattr(undismiss, "__func__", None) is not DismissStore.undismiss

    def _is_hidden(self, group: DuplicateGroup) -> bool:
        return self._store is not None and self._store.is_dismissed(group.identities)

    def _hidden_count(self) -> int:
        return sum(1 for g in self._groups if self._is_hidden(g))

    def _rebuild(self) -> None:
        show_hidden = self.show_hidden_check.isChecked()
        hidden = self._hidden_count()
        self.show_hidden_check.setText(f"Show {_count(hidden, 'hidden group')}")
        self.show_hidden_check.setVisible(hidden > 0 or show_hidden)
        visible = [g for g in self._groups if show_hidden or not self._is_hidden(g)]

        self.tree.clear()
        bold = QFont(self.tree.font())
        bold.setBold(True)
        for group in visible:
            is_hidden = self._is_hidden(group)
            text = f"{tier_label(group.tier)}: {group.reason} ({_count(len(group.members), 'file')})"
            if is_hidden:
                text += " [hidden: marked not duplicates]"
            parent_item = QTreeWidgetItem([text])
            parent_item.setData(0, GROUP_ROLE, group)
            parent_item.setFont(0, bold)
            parent_item.setToolTip(0, text)
            self.tree.addTopLevelItem(parent_item)
            parent_item.setFirstColumnSpanned(True)
            for member in group.members:
                row = QTreeWidgetItem([member.fields.get(key, "") for key, _label in self._columns])
                row.setData(0, MEMBER_ROLE, member)
                row.setToolTip(0, str(member.path))
                parent_item.addChild(row)
            parent_item.setExpanded(True)

        shown = len(visible)
        if shown:
            lead = f"{_count(shown, 'group')} of possible duplicates. "
        else:
            lead = ""
        self.summary_label.setText(lead + self._intro_html)
        self.empty_label.setVisible(shown == 0)
        self.tree.setVisible(shown > 0)
        if shown == 0 and hidden:
            self.empty_label.setText(f"{EMPTY_TEXT} ({_count(hidden, 'group')} hidden as not duplicates.)")
        else:
            self.empty_label.setText(EMPTY_TEXT)
        self._update_buttons()

    # --- selection helpers --------------------------------------------

    def selected_members(self) -> list[DuplicateMember]:
        out = []
        seen: set[str] = set()
        for item in self.tree.selectedItems():
            member = item.data(0, MEMBER_ROLE)
            # The same file can sit in two groups: act on it once.
            if member is not None and str(member.path) not in seen:
                seen.add(str(member.path))
                out.append(member)
        return out

    def selected_files(self) -> list:
        """The app items of the selected member rows."""
        return [m.item for m in self.selected_members()]

    def _selected_groups(self) -> list[DuplicateGroup]:
        """Groups of the selection: a selected group row, or the parent of a
        selected member row (each group once, in tree order)."""
        found: list[DuplicateGroup] = []
        for g in range(self.tree.topLevelItemCount()):
            top = self.tree.topLevelItem(g)
            picked = top.isSelected() or any(top.child(c).isSelected() for c in range(top.childCount()))
            if picked:
                found.append(top.data(0, GROUP_ROLE))
        return found

    def _update_buttons(self) -> None:
        has_files = bool(self.selected_members())
        for button in (self.reveal_button, self.open_button, self.select_button, self.trash_button):
            button.setEnabled(has_files)
        groups = self._selected_groups()
        self.dismiss_button.setEnabled(any(not self._is_hidden(g) for g in groups))
        self.restore_button.setEnabled(any(self._is_hidden(g) for g in groups))

    # --- actions ------------------------------------------------------

    def _reveal(self) -> None:
        for member in self.selected_members():
            reveal_in_file_manager(str(member.path))

    def _open(self) -> None:
        failed = [str(m.path) for m in self.selected_members() if not open_with_default_app(str(m.path))]
        if failed:
            QMessageBox.warning(self, self._title, "Couldn't open:\n\n" + "\n".join(failed[:_MAX_LISTED]))

    def _activated(self, item: QTreeWidgetItem, _column: int) -> None:
        member = item.data(0, MEMBER_ROLE)
        if member is not None and not open_with_default_app(str(member.path)):
            QMessageBox.warning(self, self._title, f"Couldn't open:\n\n{member.path}")

    def _select_in_list(self) -> None:
        self.to_select = self.selected_files()
        if self.to_select:
            if self._on_select is not None:
                self._on_select(list(self.to_select))
            self.accept()

    def _dismiss(self) -> None:
        if self._store is None:
            return
        for group in self._selected_groups():
            if not self._is_hidden(group):
                self._store.dismiss(group.identities)
        self._rebuild()

    def _restore(self) -> None:
        if not self._can_undismiss():
            return
        undismiss = self._store.undismiss  # type: ignore[union-attr]
        for group in self._selected_groups():
            if self._is_hidden(group):
                undismiss(group.identities)
        self._rebuild()

    def _whole_group_problem(self, doomed_paths: set[str]) -> Optional[DuplicateGroup]:
        """The first group that would be left with no member at all."""
        for group in self._groups:
            if group.members and all(str(m.path) in doomed_paths for m in group.members):
                return group
        return None

    def _trash_selected(self) -> None:
        if not self._allow_trash:
            return
        members = self.selected_members()
        if not members:
            return
        doomed = {str(m.path) for m in members}
        stuck = self._whole_group_problem(doomed)
        if stuck is not None:
            QMessageBox.warning(
                self, self._title,
                "That would move every file of a group to the Recycle Bin "
                f"({tier_label(stuck.tier)}: {stuck.reason}).\n\n"
                "Keep at least one file in each group: deselect one and try again.",
            )
            return
        shown = "\n".join(str(m.path) for m in members[:_MAX_LISTED])
        if len(members) > _MAX_LISTED:
            shown += "\n..."
        answer = QMessageBox.question(
            self, self._title,
            f"Move {_count(len(members), 'file')} to the Recycle Bin?\n\n{shown}\n\n"
            "They can be restored from the Recycle Bin.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        failures: list[str] = []
        moved: list = []
        for member in members:
            try:
                self._trash(str(member.path))
            except (TrashError, OSError) as exc:
                failures.append(f"{member.path}: {exc}")
                continue
            moved.append(member)
        if moved:
            gone = {str(m.path) for m in moved}
            self.trashed.extend(m.item for m in moved)
            for group in self._groups:
                group.members = [m for m in group.members if str(m.path) not in gone]
            # A lone file is no longer a duplicate candidate.
            self._groups = [g for g in self._groups if len(g.members) >= 2]
            self._rebuild()
            if self._on_trashed is not None:
                self._on_trashed([m.item for m in moved])
        if failures:
            QMessageBox.warning(
                self, self._title,
                "Couldn't move to the Recycle Bin:\n\n" + "\n".join(failures[:_MAX_LISTED])
                + ("\n..." if len(failures) > _MAX_LISTED else ""),
            )


FindFn = Callable[[Sequence[Any], Callable[..., None], Callable[[], bool]], Sequence[DuplicateGroup]]


def run_find_duplicates(
    parent: Optional[QWidget],
    items: Sequence[Any],
    find_fn: FindFn,
    columns: Sequence[tuple[str, str]],
    *,
    title: str = DEFAULT_TITLE,
    dismiss_store: Optional[DismissStore] = None,
    on_select_in_list: Optional[Callable[[list], None]] = None,
    on_trashed: Optional[Callable[[list], None]] = None,
    trash: Callable[[str], None] = move_to_trash,
    allow_trash: bool = True,
    intro_text: str = "",
    progress_label: str = "Looking for duplicates...",
    none_found_message: str = "No duplicates found.",
    cancellable: bool = True,
) -> Optional[DuplicatesDialog]:
    """The whole Find Duplicates flow. Runs `find_fn(items, progress,
    cancelled)` on a worker thread under a progress dialog (the window
    keeps painting; Cancel when `cancellable`), then opens the review
    dialog on the groups it returns.

    - `progress(done, total=None, label=None)`: call as work advances;
      thread-safe (the dialog is updated from the GUI thread).
    - `cancelled()`: True once the user clicked Cancel; stop early.
    - `find_fn` returns DuplicateGroups (the dialog sorts them).

    Returns the dialog after it closed (read `.trashed`, `.to_select`),
    or None when cancelled, nothing was found (an info box says so) or
    `find_fn` failed (a warning says so)."""
    items = list(items)
    cancel_event = threading.Event()
    state: dict[str, Any] = {"done": 0, "total": len(items), "label": None}

    def progress(done: int, total: Optional[int] = None, label: Optional[str] = None) -> None:
        state["done"] = done
        if total:
            state["total"] = total
        if label is not None:
            state["label"] = label

    reporter = ProgressReporter(parent, len(items), progress_label, threshold=1, cancellable=cancellable, title=title)
    cancel_signal = None
    if reporter.dialog is not None and cancellable:
        cancel_signal = reporter.dialog.canceled
        cancel_signal.connect(cancel_event.set)

    timer = QTimer()
    timer.setInterval(100)

    def refresh() -> None:
        # Only here, on the GUI thread, is the dialog touched.
        if state["label"] is not None:
            reporter.set_label(state["label"])
        reporter.set_value(min(state["done"], reporter.total), pump=False)

    timer.timeout.connect(refresh)
    timer.start()
    try:
        groups = call_in_background(find_fn, items, progress, cancel_event.is_set, cancel_signal=cancel_signal)
    except BackgroundCancelled:
        return None
    except Exception as exc:  # noqa: BLE001 -- the app's find_fn is arbitrary code
        if reporter.dialog is not None:
            reporter.dialog.close()   # before the warning, so it isn't hidden behind it
        QMessageBox.warning(parent, title, f"Looking for duplicates failed:\n\n{exc}")
        return None
    finally:
        timer.stop()
        user_cancelled = cancel_event.is_set()   # read before close(): closing emits `canceled` too
        if reporter.dialog is not None:
            reporter.dialog.close()
    if user_cancelled:
        return None
    groups = [g for g in (groups or []) if len(g.members) >= 2]
    if not groups:
        QMessageBox.information(parent, title, none_found_message)
        return None
    dialog = DuplicatesDialog(
        groups, columns, parent, title=title, dismiss_store=dismiss_store,
        on_select_in_list=on_select_in_list, on_trashed=on_trashed, trash=trash,
        allow_trash=allow_trash, intro_text=intro_text,
    )
    dialog.exec()
    return dialog
