"""
redactor_common/gui/settings_bundle_dialogs.py

Qt side of Export/Import Settings (core/settings_bundle.py holds the
format and the logic):

- export_settings(parent, adapter): pick sections (machine-specific ones
  start unticked), pick a file (default `<app>-settings.json`), write it
  atomically.
- import_settings(parent, adapter, on_applied=None): pick a file, parse it,
  show every change as "old -> new" grouped by section with a checkbox per
  section, apply nothing until confirmed, then call `on_applied` and offer
  "Re-detect tools" when the adapter provides `redetect_tools`.
- settings_menu_actions(): MenuAction specs (keys 'export_settings' /
  'import_settings') for the File menu.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from redactor_common.core import settings_bundle as sb
from redactor_common.gui.menu_builder import MenuAction

_FILE_FILTER = "Settings files (*.json);;All files (*)"
MACHINE_SPECIFIC_SUFFIX = " (this computer only)"


def _short(value: Any, limit: int = 80) -> str:
    text = "(not set)" if value is None else (
        value if isinstance(value, str) else json.dumps(value, ensure_ascii=False))
    text = text.replace("\n", " ")
    return text if len(text) <= limit else text[: limit - 1] + "…"


class ExportSectionsDialog(QDialog):
    """Checkbox list of sections; machine-specific ones are unticked."""

    def __init__(self, parent: QWidget | None, adapter: sb.SettingsAdapter) -> None:
        super().__init__(parent)
        self.setWindowTitle("Export Settings")
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel(
            "Choose what to export. Passwords and API keys are never included."))
        self.list = QListWidget()
        for spec in adapter.sections():
            label = spec.label + ("" if spec.portable else MACHINE_SPECIFIC_SUFFIX)
            item = QListWidgetItem(label)
            item.setData(Qt.ItemDataRole.UserRole, spec.key)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(Qt.CheckState.Checked if spec.portable else Qt.CheckState.Unchecked)
            if not spec.portable:
                item.setToolTip("Machine-specific (paths, folders, window layout). "
                                "Usually wrong on another computer.")
            self.list.addItem(item)
        layout.addWidget(self.list)
        self.buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        layout.addWidget(self.buttons)
        self.resize(420, 360)

    def selected_sections(self) -> set[str]:
        return {
            self.list.item(i).data(Qt.ItemDataRole.UserRole)
            for i in range(self.list.count())
            if self.list.item(i).checkState() == Qt.CheckState.Checked
        }


class ImportPreviewDialog(QDialog):
    """Changes grouped by section, a checkbox per section. Nothing is applied here."""

    def __init__(
        self,
        parent: QWidget | None,
        adapter: sb.SettingsAdapter,
        bundle: sb.Bundle,
        changes: list[sb.Change],
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Import Settings")
        layout = QVBoxLayout(self)
        when = f" (exported {bundle.exported})" if bundle.exported else ""
        layout.addWidget(QLabel(
            f"Tick the sections to import{when}. Nothing changes until you press OK."))
        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["Setting", "Current", "New"])
        self.tree.setRootIsDecorated(True)
        specs = {s.key: s for s in adapter.sections()}
        by_section: dict[str, list[sb.Change]] = {}
        for c in changes:
            by_section.setdefault(c.section, []).append(c)
        for key, items in by_section.items():
            spec = specs.get(key)
            label = spec.label if spec else key
            portable = spec.portable if spec else True
            top = QTreeWidgetItem([
                f"{label} ({len(items)} change{'s' if len(items) != 1 else ''})"
                + ("" if portable else MACHINE_SPECIFIC_SUFFIX)])
            top.setData(0, Qt.ItemDataRole.UserRole, key)
            top.setFlags(top.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            top.setCheckState(0, Qt.CheckState.Checked if portable else Qt.CheckState.Unchecked)
            for c in items:
                top.addChild(QTreeWidgetItem([c.key, _short(c.old), _short(c.new)]))
            self.tree.addTopLevelItem(top)
            top.setExpanded(True)
        self.tree.resizeColumnToContents(0)
        layout.addWidget(self.tree)
        self.buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        layout.addWidget(self.buttons)
        self.resize(640, 460)

    def selected_sections(self) -> set[str]:
        out = set()
        for i in range(self.tree.topLevelItemCount()):
            top = self.tree.topLevelItem(i)
            if top.checkState(0) == Qt.CheckState.Checked:
                out.add(top.data(0, Qt.ItemDataRole.UserRole))
        return out


def export_settings(parent: QWidget | None, adapter: sb.SettingsAdapter) -> Path | None:
    """Runs the export flow. Returns the written path, or None if cancelled/failed."""
    picker = ExportSectionsDialog(parent, adapter)
    if picker.exec() != QDialog.DialogCode.Accepted:
        return None
    chosen = picker.selected_sections()
    if not chosen:
        QMessageBox.information(parent, "Export Settings", "Nothing was selected to export.")
        return None
    path, _ = QFileDialog.getSaveFileName(
        parent, "Export Settings", f"{adapter.app_slug}-settings.json", _FILE_FILTER)
    if not path:
        return None
    try:
        bundle = sb.build_bundle(adapter, chosen)
        sb.write_text_atomic(path, sb.dump_bundle(bundle))
    except Exception as exc:
        QMessageBox.warning(parent, "Export Settings", f"Couldn't write the settings file:\n{exc}")
        return None
    QMessageBox.information(
        parent, "Export Settings",
        f"Exported {len(bundle.sections)} section(s) to:\n{path}")
    return Path(path)


def import_settings(
    parent: QWidget | None,
    adapter: sb.SettingsAdapter,
    on_applied: Callable[[sb.ApplyResult], None] | None = None,
) -> sb.ApplyResult | None:
    """Runs the import flow. Returns the ApplyResult, or None if nothing was applied."""
    path, _ = QFileDialog.getOpenFileName(parent, "Import Settings", "", _FILE_FILTER)
    if not path:
        return None
    try:
        text = Path(path).read_text(encoding="utf-8-sig")
    except (OSError, UnicodeDecodeError) as exc:
        QMessageBox.warning(parent, "Import Settings", f"Couldn't read the file:\n{exc}")
        return None
    try:
        bundle = sb.parse_bundle(text, adapter.app_slug)
    except sb.SettingsBundleError as exc:
        QMessageBox.warning(parent, "Import Settings", str(exc))
        return None
    changes = sb.diff_bundle(adapter, bundle)
    if not changes:
        QMessageBox.information(
            parent, "Import Settings", "This file doesn't change anything: the settings already match.")
        return None
    preview = ImportPreviewDialog(parent, adapter, bundle, changes)
    if preview.exec() != QDialog.DialogCode.Accepted:
        return None
    chosen = preview.selected_sections()
    if not chosen:
        return None
    result = sb.apply_bundle(adapter, bundle, chosen)
    if result.failed:
        details = "\n".join(f"{k}: {v}" for k, v in result.failed.items())
        QMessageBox.warning(
            parent, "Import Settings",
            f"These sections could not be applied (the others were):\n{details}")
    if result.applied and on_applied is not None:
        on_applied(result)
    redetect = getattr(adapter, "redetect_tools", None)
    if result.applied and callable(redetect):
        answer = QMessageBox.question(
            parent, "Import Settings",
            "Settings imported. Re-detect external tools on this computer now?")
        if answer == QMessageBox.StandardButton.Yes:
            redetect()
    elif result.applied and not result.failed:
        QMessageBox.information(parent, "Import Settings", "Settings imported.")
    return result


def settings_menu_actions(
    export_slot: Callable[[], None], import_slot: Callable[[], None]
) -> list[MenuAction]:
    """MenuAction specs for the File menu, same vocabulary as gui/menu_builder."""
    return [
        MenuAction("export_settings", "&Export Settings...", export_slot,
                   tooltip="Save portable settings to a file"),
        MenuAction("import_settings", "I&mport Settings...", import_slot,
                   tooltip="Load settings from a file exported by this program"),
    ]
