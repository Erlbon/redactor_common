"""
redactor_common/gui/redact_dialog.py

Qt side of the "Redact" automation (core/pipeline.py holds the engine):

- run_redact(): runs a recipe over the items under run_with_progress()
  (cancellable, per-file label), then shows the results dialog.
- RedactResultsDialog: the report text (Save report... / Copy) plus a
  "Needs review" tab listing every guess that was below the confidence
  threshold and therefore NOT applied.
- RecipeEditorDialog: checkable, reorderable step list, per-step
  option widgets built from each step's declared OptionSpecs (nothing
  app-specific here), and the confidence threshold.
- redact_menu_action()/edit_recipe_menu_action(): MenuAction specs with
  the family shortcut, for the Operations menu.
"""

from __future__ import annotations

import time
from typing import Any, Callable, Iterable

from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtGui import QFontDatabase
from PyQt6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSpinBox,
    QTabWidget,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from redactor_common.core import labels
from redactor_common.core.pipeline import (
    FileReport,
    OptionSpec,
    RedactReport,
    Recipe,
    Step,
    begin_run,
    effective_option_source,
    ordered_keys,
    run_recipe_on_item,
)
from redactor_common.gui.menu_builder import MenuAction
from redactor_common.gui.progress import run_with_progress
from redactor_common.gui.standard_shortcuts import REDACT


# --- results ----------------------------------------------------------------


class RedactResultsDialog(QDialog):
    """Report text + Needs-review list. Non-blocking helpers
    (save_report_to, copy_report) are separate from the buttons so they
    can be tested without a file dialog.

    Optional `header` (a line or paragraph shown above the tabs) and
    `extra_notes` (lines shown below them) let an app add its own context
    -- "3 files had unsaved edits and were skipped" -- without a subclass.
    Neither is part of the saved/copied report text."""

    def __init__(
        self,
        report: RedactReport,
        parent: QWidget | None = None,
        title: str = "Redact results",
        header: str = "",
        extra_notes: list[str] | None = None,
    ):
        super().__init__(parent)
        self.report = report
        self.setWindowTitle(title)
        self.resize(720, 520)

        self.report_view = QPlainTextEdit(report.to_text())
        self.report_view.setReadOnly(True)
        self.report_view.setFont(QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont))

        self.review_tree = QTreeWidget()
        self.review_tree.setHeaderLabels(["File", "Step", "Suggestion", "Confidence", "Reason"])
        self.review_tree.setRootIsDecorated(False)
        self.review_tree.setUniformRowHeights(True)
        self.review_tree.setAlternatingRowColors(True)
        n_review = 0
        for entry in report.needs_review():
            for r in entry.review:
                n_review += 1
                self.review_tree.addTopLevelItem(
                    QTreeWidgetItem(
                        [entry.file, r.step_label, str(r.value), f"{r.confidence:.0%}", r.reason]
                    )
                )
        for col in range(4):
            self.review_tree.resizeColumnToContents(col)

        self.tabs = QTabWidget()
        self.tabs.addTab(self.report_view, "Report")
        self.tabs.addTab(self.review_tree, f"Needs review ({n_review})")
        if n_review:
            self.tabs.setCurrentIndex(1)

        self.save_button = QPushButton("Save report...")
        self.save_button.clicked.connect(self._save_clicked)
        self.copy_button = QPushButton("Copy")
        self.copy_button.clicked.connect(self.copy_report)
        close = QPushButton("Close")
        close.setDefault(True)
        close.clicked.connect(self.accept)
        buttons = QHBoxLayout()
        buttons.addWidget(self.save_button)
        buttons.addWidget(self.copy_button)
        buttons.addStretch(1)
        buttons.addWidget(close)

        self.header_label: QLabel | None = None
        self.notes_label: QLabel | None = None
        layout = QVBoxLayout(self)
        if header:
            self.header_label = QLabel(header)
            self.header_label.setWordWrap(True)
            layout.addWidget(self.header_label)
        layout.addWidget(self.tabs)
        notes = [n for n in (extra_notes or []) if n]
        if notes:
            self.notes_label = QLabel("\n".join(notes))
            self.notes_label.setWordWrap(True)
            layout.addWidget(self.notes_label)
        layout.addLayout(buttons)

    def copy_report(self) -> None:
        QApplication.clipboard().setText(self.report.to_text())

    def save_report_to(self, path: str) -> None:
        with open(path, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(self.report.to_text())

    def _save_clicked(self) -> None:
        path, _ = QFileDialog.getSaveFileName(
            self, "Save report", "redact-report.txt", "Text files (*.txt);;All files (*)"
        )
        if not path:
            return
        try:
            self.save_report_to(path)
        except OSError as exc:
            QMessageBox.warning(self, "Save report", f"Couldn't save the report:\n{exc}")


def run_redact(
    parent: QWidget | None,
    items: Iterable[Any],
    recipe: Recipe,
    catalogue: Iterable[Step],
    make_context: Callable[[Any], Any],
    describe: Callable[[Any], str] = str,
    title: str = "Redact",
    show_results: bool = True,
    finalize: Callable[[Any, FileReport], Any] | None = None,
    finalize_label: str = "Final save",
    header: str = "",
    extra_notes: list[str] | None = None,
    env: Any = None,
) -> RedactReport | None:
    """Run `recipe` on `items` under a cancellable progress dialog (one
    label per file), then show the results dialog. Returns the report,
    or None when there was nothing to do. Cancelling stops between
    files; files already done stay done and appear in the report.

    `header` / `extra_notes` go to the results dialog (see
    RedactResultsDialog). `env` is handed to every enabled step's
    prepare(items, env) once, before the first file (Step.prepare)."""
    items = list(items)
    if not items:
        return None
    catalogue = list(catalogue)
    resolved = recipe.resolve(catalogue)
    report = RedactReport(confidence_threshold=recipe.confidence_threshold)
    started = time.monotonic()
    run = begin_run(items, resolved, env)
    report.run_notes = run.notes

    def do_one(item: Any, _index: int) -> None:
        report.entries.append(
            run_recipe_on_item(
                item, resolved, recipe.confidence_threshold, make_context, describe, finalize, finalize_label,
                run=run,
            )
        )

    finished = run_with_progress(
        parent,
        items,
        do_one,
        label=f"{title}...",
        threshold=1,
        cancellable=True,
        label_for=lambda item: f"{title}: {_safe_describe(describe, item)}",
    )
    report.duration = time.monotonic() - started
    if not finished:
        report.cancelled = True
        report.not_processed = len(items) - len(report.entries)
    if show_results:
        RedactResultsDialog(
            report, parent, title=f"{title} results", header=header, extra_notes=extra_notes
        ).exec()
    return report


def _safe_describe(describe: Callable[[Any], str], item: Any) -> str:
    try:
        return describe(item)
    except Exception:
        return repr(item)


# --- recipe editor ------------------------------------------------------------


class RecipeEditorDialog(QDialog):
    """Edit a Recipe against a catalogue: tick/untick and reorder steps
    (drag, or Move up/down), tweak the selected step's options, and set
    the confidence threshold. recipe() returns the edited result; the
    caller persists it (Recipe.to_json())."""

    def __init__(self, catalogue: Iterable[Step], recipe: Recipe, parent: QWidget | None = None):
        super().__init__(parent)
        self.setWindowTitle("Redact recipe")
        self.resize(620, 460)
        catalogue = list(catalogue)
        self._steps = {s.key: s for s in catalogue}
        # Hidden (internal) steps are never listed, stored or edited here.
        self._visible = [s for s in catalogue if not s.hidden]
        # Same ordering rules as Recipe.resolve(): stored order, new
        # catalogue steps at their catalogue position, position groups
        # ("first" on top ... "after_save" at the bottom) pinned and each
        # step's `after` constraints honoured.
        keys = [k for k in ordered_keys(recipe.order, catalogue) if not self._steps[k].hidden]
        self._enabled = {k: recipe.enabled.get(k, self._steps[k].default_enabled) for k in keys}
        self._options: dict[str, dict[str, Any]] = {}
        for k in keys:
            stored = recipe.options.get(k, {})
            self._options[k] = {o.key: o.coerce(stored.get(o.key, o.default)) for o in self._steps[k].options}
        self._widgets: dict[str, QWidget] = {}
        self._shown_key: str | None = None

        self.list = QListWidget()
        self.list.setDragDropMode(QListWidget.DragDropMode.InternalMove)
        self.list.setDefaultDropAction(Qt.DropAction.MoveAction)
        for k in keys:
            step = self._steps[k]
            item = QListWidgetItem(step.label or k)
            item.setData(Qt.ItemDataRole.UserRole, k)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(Qt.CheckState.Checked if self._enabled[k] else Qt.CheckState.Unchecked)
            self.list.addItem(item)
        self.list.currentItemChanged.connect(lambda cur, _prev: self._show_step(cur))
        # A drag may drop a step outside its position group or above a step
        # it must run after; put things back once the drop has settled.
        self.list.model().rowsMoved.connect(lambda *_: QTimer.singleShot(0, self._repin))

        up = QPushButton("Move up")
        up.clicked.connect(lambda: self.move_current(-1))
        down = QPushButton("Move down")
        down.clicked.connect(lambda: self.move_current(1))
        left_buttons = QHBoxLayout()
        left_buttons.addWidget(up)
        left_buttons.addWidget(down)
        left = QVBoxLayout()
        left.addWidget(QLabel("Steps run top to bottom:"))
        left.addWidget(self.list, 1)
        left.addLayout(left_buttons)

        self.description = QLabel()
        self.description.setWordWrap(True)
        self.description.setAlignment(Qt.AlignmentFlag.AlignTop)
        self.options_form = QFormLayout()
        self.options_host = QWidget()
        self.options_host.setLayout(self.options_form)
        right = QVBoxLayout()
        right.addWidget(self.description)
        right.addWidget(self.options_host)
        right.addStretch(1)

        self.threshold = QDoubleSpinBox()
        self.threshold.setRange(0.0, 1.0)
        self.threshold.setSingleStep(0.05)
        self.threshold.setDecimals(2)
        self.threshold.setValue(recipe.confidence_threshold)
        self.threshold.setToolTip(
            "A guess (language, series, lookup match...) is applied automatically only at or above "
            "this confidence; anything below is listed as 'Needs review' and left alone."
        )
        threshold_row = QHBoxLayout()
        threshold_row.addWidget(QLabel("Apply guesses automatically at confidence >="))
        threshold_row.addWidget(self.threshold)
        threshold_row.addStretch(1)

        reset = QPushButton("Reset to defaults")
        reset.clicked.connect(self._reset)
        box = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        box.accepted.connect(self.accept)
        box.rejected.connect(self.reject)
        bottom = QHBoxLayout()
        bottom.addWidget(reset)
        bottom.addStretch(1)
        bottom.addWidget(box)

        body = QHBoxLayout()
        body.addLayout(left, 1)
        body.addLayout(right, 1)
        layout = QVBoxLayout(self)
        layout.addLayout(body, 1)
        layout.addLayout(threshold_row)
        layout.addLayout(bottom)

        if self.list.count():
            self.list.setCurrentRow(0)

    # -- state ---------------------------------------------------------------

    def _commit_shown(self) -> None:
        """Pull the visible option widgets' values into self._options."""
        if self._shown_key is None:
            return
        for spec in self._steps[self._shown_key].options:
            w = self._widgets.get(spec.key)
            if w is None:
                continue
            if isinstance(w, QCheckBox):
                value: Any = w.isChecked()
            elif isinstance(w, QSpinBox):
                value = w.value()
            elif isinstance(w, QDoubleSpinBox):
                value = w.value()
            elif isinstance(w, QComboBox):
                value = w.currentText()
            elif isinstance(w, QLineEdit):
                value = w.text()
            else:
                continue
            self._options[self._shown_key][spec.key] = value

    def _show_step(self, item: QListWidgetItem | None) -> None:
        self._commit_shown()
        while self.options_form.rowCount():
            self.options_form.removeRow(0)
        self._widgets = {}
        if item is None:
            self._shown_key = None
            self.description.setText("")
            return
        key = item.data(Qt.ItemDataRole.UserRole)
        self._shown_key = key
        step = self._steps[key]
        text = step.description or ""
        if step.required:
            text += ("\n\n" if text else "") + "If this step fails, the file is left untouched."
        self.description.setText(text)
        for spec in step.options:
            value = self._options[key][spec.key]
            if spec.kind == "str" and spec.suggestions is not None:
                widget, row = self._make_pattern_row(spec, value)
                self._widgets[spec.key] = widget
                self.options_form.addRow(spec.label, row)
                continue
            widget = self._make_widget(spec, value)
            self._widgets[spec.key] = widget
            self.options_form.addRow(spec.label, widget)

    @staticmethod
    def _pattern_choices(spec: OptionSpec) -> list[str]:
        """The suggestion list for the dropdown: de-duplicated, newest
        first, filename patterns before path patterns (those with '/')."""
        try:
            raw = list(spec.suggestions()) if spec.suggestions else []
        except Exception:
            raw = []
        seen: set[str] = set()
        unique = [p for p in raw if isinstance(p, str) and p and not (p in seen or seen.add(p))]
        return [p for p in unique if "/" not in p] + [p for p in unique if "/" in p]

    @staticmethod
    def _make_pattern_row(spec: OptionSpec, value: Any) -> tuple[QComboBox, QWidget]:
        """The "pattern trail" for a str option with suggestions: an
        editable combo (recent patterns), a 'Use fallback' button, a live
        'In effect: ... -- source' caption and an optional preview line.
        Returns (the combo holding the value, the row widget to lay out)."""
        combo = QComboBox()
        combo.setEditable(True)
        combo.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        combo.addItems(RecipeEditorDialog._pattern_choices(spec))
        edit = combo.lineEdit()
        if spec.max_length is not None:
            edit.setMaxLength(int(spec.max_length))
        if spec.fallback is not None:
            edit.setPlaceholderText("(follow " + (spec.fallback_label or "the app's current setting") + ")")
        combo.setCurrentText(str(value))
        if spec.tooltip:
            combo.setToolTip(spec.tooltip)

        clear = QPushButton("Use fallback")
        clear.setToolTip("Clear the pattern so this step follows the fallback again")
        clear.setEnabled(spec.fallback is not None)
        clear.clicked.connect(lambda: combo.setCurrentText(""))
        top = QHBoxLayout()
        top.setContentsMargins(0, 0, 0, 0)
        top.addWidget(combo, 1)
        top.addWidget(clear)

        caption = QLabel()
        caption.setWordWrap(True)
        caption.setObjectName("pattern_caption")
        preview = QLabel()
        preview.setWordWrap(True)
        preview.setObjectName("pattern_preview")
        preview.setVisible(spec.preview is not None)

        def refresh() -> None:
            effective, source = effective_option_source(spec, combo.currentText())
            caption.setText(f"In effect: {effective} — {source}")
            if spec.preview is not None:
                try:
                    shown = spec.preview(effective)
                except Exception as exc:
                    shown = f"(no preview: {exc})"
                preview.setText(f"Preview: {shown}")

        combo.editTextChanged.connect(lambda _t: refresh())
        refresh()

        row = QWidget()
        col = QVBoxLayout(row)
        col.setContentsMargins(0, 0, 0, 0)
        col.addLayout(top)
        col.addWidget(caption)
        col.addWidget(preview)
        # Handles for tests / callers that want the pieces.
        combo.setProperty("pattern_trail", True)
        row.caption = caption  # type: ignore[attr-defined]
        row.preview = preview  # type: ignore[attr-defined]
        row.use_fallback = clear  # type: ignore[attr-defined]
        return combo, row

    @staticmethod
    def _make_widget(spec: OptionSpec, value: Any) -> QWidget:
        if spec.kind == "int":
            w: QWidget = QSpinBox()
            w.setRange(int(spec.minimum) if spec.minimum is not None else -1_000_000,
                       int(spec.maximum) if spec.maximum is not None else 1_000_000)
            w.setValue(int(value))
        elif spec.kind == "float":
            w = QDoubleSpinBox()
            w.setDecimals(3)
            w.setRange(spec.minimum if spec.minimum is not None else -1e9,
                       spec.maximum if spec.maximum is not None else 1e9)
            w.setValue(float(value))
        elif spec.kind == "choice":
            w = QComboBox()
            w.addItems(list(spec.choices))
            w.setCurrentText(str(value))
        elif spec.kind == "str":
            w = QLineEdit(str(value))
            if spec.max_length is not None:
                w.setMaxLength(int(spec.max_length))
        else:
            w = QCheckBox()
            w.setChecked(bool(value))
        if spec.tooltip:
            w.setToolTip(spec.tooltip)
        return w

    def move_current(self, delta: int) -> None:
        row = self.list.currentRow()
        new = row + delta
        if row < 0 or not 0 <= new < self.list.count():
            return
        if not self.can_move(row, delta):
            return  # pinned position groups / `after` constraints stay true
        self._commit_shown()
        item = self.list.takeItem(row)
        self.list.insertItem(new, item)
        self.list.setCurrentRow(new)

    def can_move(self, row: int, delta: int) -> bool:
        """Whether moving the step at `row` by `delta` (+-1) keeps the list
        legal: same position group, and not above a step it must run after
        (or below a step that must run after it)."""
        new = row + delta
        if row < 0 or not 0 <= new < self.list.count():
            return False
        keys = self._keys()
        keys[row], keys[new] = keys[new], keys[row]
        return ordered_keys(keys, self._visible) == keys

    def _keys(self) -> list[str]:
        return [self._key_at(r) for r in range(self.list.count())]

    def _key_at(self, row: int) -> str:
        return self.list.item(row).data(Qt.ItemDataRole.UserRole)

    def _repin(self) -> None:
        """Restore the legal order after a drag (stable: position groups
        pinned, `after` constraints honoured; keeps check states and the
        current selection)."""
        rows = [self.list.item(r) for r in range(self.list.count())]
        keys = [i.data(Qt.ItemDataRole.UserRole) for i in rows]
        legal = ordered_keys(keys, self._visible)
        if legal == keys:
            return
        by_key = dict(zip(keys, rows))
        wanted = [by_key[k] for k in legal]
        current = self.list.currentItem().data(Qt.ItemDataRole.UserRole) if self.list.currentItem() else None
        self.list.blockSignals(True)
        for r in range(len(rows) - 1, -1, -1):
            self.list.takeItem(r)
        for item in wanted:
            self.list.addItem(item)
        if current is not None:
            self.list.setCurrentRow(_row_of(self.list, current))
        self.list.blockSignals(False)

    def _reset(self) -> None:
        defaults = Recipe.default_for(self._visible)
        self._shown_key = None  # don't commit stale widget values over the defaults
        for k, step in self._steps.items():
            self._options[k] = {o.key: o.default for o in step.options}
        self.list.blockSignals(True)
        for key in defaults.order:
            item = self.list.takeItem(_row_of(self.list, key))
            self.list.addItem(item)
            item.setCheckState(Qt.CheckState.Checked if defaults.enabled[key] else Qt.CheckState.Unchecked)
        self.list.blockSignals(False)
        self._repin()
        self.threshold.setValue(defaults.confidence_threshold)
        self.list.setCurrentRow(0)
        self._show_step(self.list.currentItem())

    def recipe(self) -> Recipe:
        self._commit_shown()
        order, enabled = [], {}
        for row in range(self.list.count()):
            item = self.list.item(row)
            key = item.data(Qt.ItemDataRole.UserRole)
            order.append(key)
            enabled[key] = item.checkState() == Qt.CheckState.Checked
        return Recipe(
            order=order,
            enabled=enabled,
            options={k: dict(v) for k, v in self._options.items() if k in enabled},
            confidence_threshold=round(self.threshold.value(), 2),
        )


def _row_of(lst: QListWidget, key: str) -> int:
    for r in range(lst.count()):
        if lst.item(r).data(Qt.ItemDataRole.UserRole) == key:
            return r
    raise KeyError(key)


# --- menu specs ---------------------------------------------------------------


def redact_menu_action(slot: Callable[[], None], text: str = labels.REDACT) -> MenuAction:
    """The standard "Redact" item (Ctrl+Shift+E) for the Operations menu."""
    return MenuAction(
        "redact",
        text,
        slot,
        shortcut=REDACT,
        tooltip="Run the Redact recipe on the selected files: fix what can be fixed automatically",
    )


def edit_recipe_menu_action(slot: Callable[[], None], text: str = labels.EDIT_REDACT_RECIPE) -> MenuAction:
    """Companion item opening RecipeEditorDialog (no shortcut)."""
    return MenuAction("redact_recipe", text, slot, tooltip="Choose and order the Redact steps")
