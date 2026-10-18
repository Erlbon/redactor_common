"""
redactor_common/gui/conversion_policy_widget.py

The shared half of every app's Tools > Preferences > Conversion page (core/conversion_policy.py): when a file that needs
converting is opened, what happens to the original afterwards, and the dry-run and recent-file choices. The page of an app
puts this widget first and its own options (bitrate limits, resize, Calibre ...) below it:

    self.policy_widget = ConversionPolicyWidget(load_policy(get, defaults), never_text="...as read-only rows")
    layout.addWidget(self.policy_widget)
    ...
    def save(self):
        save_policy(self.policy_widget.policy(), put)

The widget only edits; the caller reads policy() and saves it when the dialog is accepted.
"""

from __future__ import annotations

from typing import Optional

from PyQt6.QtWidgets import (
    QButtonGroup, QCheckBox, QFileDialog, QFormLayout, QGroupBox, QHBoxLayout, QLabel, QLineEdit, QPushButton,
    QRadioButton, QSpinBox, QVBoxLayout, QWidget,
)

from redactor_common.core.conversion_policy import (
    ON_OPEN_ASK, ON_OPEN_AUTO, ON_OPEN_NEVER, ORIGINALS_QUARANTINE, ORIGINALS_RECYCLE, ConversionPolicy,
)

ON_OPEN_TEXT = {
    ON_OPEN_ASK: ("Ask me", "a dialog lists the files; Convert or Skip, with \"Don't ask again for this folder\""),
    ON_OPEN_AUTO: ("Convert automatically", "no question; the report afterwards says what was done"),
}


class ConversionPolicyWidget(QWidget):
    def __init__(self, policy: ConversionPolicy, *, never_text: str = "the files are not converted",
                 parent: Optional[QWidget] = None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        open_box = QGroupBox("When a file needs converting (on Open, Open Folder, Add)")
        open_layout = QVBoxLayout(open_box)
        self._on_open: dict[str, QRadioButton] = {}
        group = QButtonGroup(self)
        entries = [(ON_OPEN_ASK, *ON_OPEN_TEXT[ON_OPEN_ASK]), (ON_OPEN_AUTO, *ON_OPEN_TEXT[ON_OPEN_AUTO]),
                   (ON_OPEN_NEVER, "Never convert", never_text)]
        for key, title, detail in entries:
            button = QRadioButton(f"{title}: {detail}")
            button.setChecked(policy.on_open == key)
            group.addButton(button)
            open_layout.addWidget(button)
            self._on_open[key] = button
        layout.addWidget(open_box)

        originals_box = QGroupBox("What happens to the original after a verified conversion")
        originals_layout = QVBoxLayout(originals_box)
        self._quarantine = QRadioButton("Move it to a quarantine folder, with an undo log")
        self._recycle = QRadioButton("Send it to the Recycle Bin")
        self._quarantine.setChecked(policy.originals == ORIGINALS_QUARANTINE)
        self._recycle.setChecked(policy.originals == ORIGINALS_RECYCLE)
        originals_group = QButtonGroup(self)
        originals_group.addButton(self._quarantine)
        originals_group.addButton(self._recycle)
        originals_layout.addWidget(self._quarantine)
        originals_layout.addWidget(self._recycle)
        form = QFormLayout()
        self._quarantine_edit = self._folder_row(form, "Quarantine folder (empty: a dated folder beside the file):",
                                                 policy.quarantine_folder)
        self._undo_edit = self._folder_row(form, "Undo log folder (empty: beside the quarantine folder):",
                                           policy.undo_log_folder)
        self._recent = QSpinBox()
        self._recent.setRange(0, 1440)
        self._recent.setValue(policy.recent_minutes)
        self._recent.setSuffix(" minutes")
        form.addRow("Leave files modified in the last:", self._recent)
        originals_layout.addLayout(form)
        note = QLabel("Container fixes and Join Files always use the quarantine: they write an undo log, which the "
                      "Recycle Bin cannot honour.")
        note.setWordWrap(True)
        originals_layout.addWidget(note)
        layout.addWidget(originals_box)
        self._quarantine.toggled.connect(self._sync_enabled)
        self._sync_enabled()

        checking_box = QGroupBox("Checking before converting")
        checking_layout = QVBoxLayout(checking_box)
        self._dry_run = QCheckBox("Dry run first: list what would happen and change nothing (the run's own dialog can change it)")
        self._dry_run.setChecked(policy.dry_run_first)
        checking_layout.addWidget(self._dry_run)
        verify = QLabel("Every result is verified before the original is touched (always on).")
        checking_layout.addWidget(verify)
        layout.addWidget(checking_box)

    def _folder_row(self, form: QFormLayout, label: str, value: str) -> QLineEdit:
        holder = QWidget()
        row = QHBoxLayout(holder)
        row.setContentsMargins(0, 0, 0, 0)
        edit = QLineEdit(value)
        row.addWidget(edit)
        browse = QPushButton("Browse...")
        browse.clicked.connect(lambda _checked=False, e=edit: self._browse(e))
        row.addWidget(browse)
        form.addRow(label, holder)
        return edit

    def _browse(self, edit: QLineEdit) -> None:
        path = QFileDialog.getExistingDirectory(self, "Choose folder", edit.text().strip())
        if path:
            edit.setText(path)

    def _sync_enabled(self) -> None:
        on = self._quarantine.isChecked()
        self._quarantine_edit.setEnabled(on)
        self._undo_edit.setEnabled(on)

    def policy(self) -> ConversionPolicy:
        on_open = next((key for key, button in self._on_open.items() if button.isChecked()), ON_OPEN_ASK)
        return ConversionPolicy(
            on_open=on_open,
            originals=ORIGINALS_QUARANTINE if self._quarantine.isChecked() else ORIGINALS_RECYCLE,
            quarantine_folder=self._quarantine_edit.text().strip(),
            undo_log_folder=self._undo_edit.text().strip(),
            recent_minutes=self._recent.value(),
            dry_run_first=self._dry_run.isChecked(),
        )
