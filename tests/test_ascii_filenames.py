"""Tests for the ASCII-safe filenames option: rename_pattern.to_ascii,
render_filename(ascii_only=...) and the Rename/Export dialog checkbox."""

import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication  # noqa: E402

from redactor_common.core.rename_pattern import render_filename, to_ascii  # noqa: E402

_app = QApplication.instance() or QApplication(sys.argv)


def test_to_ascii_transliterates():
    assert to_ascii("Pokémon Café Crème Brûlée") == "Pokemon Cafe Creme Brulee"
    assert to_ascii("Tromsø Blåbær Æsop Øl") == "Tromso Blabaer AEsop Ol"
    assert to_ascii("Straße Łódź Þór Œuvre") == "Strasse Lodz Thor OEuvre"
    assert to_ascii("“Quoted” — it’s… done") == "\"Quoted\" - it's... done"
    assert to_ascii("Señor Niño Ångström") == "Senor Nino Angstrom"
    assert to_ascii("Naruto ナルト ☆ 🎬") == "Naruto   "  # no ASCII form: dropped


def test_render_filename_ascii_only():
    values = {"author": "Jo Nesbø", "title": "Snømannen – Harry Hole ☆"}
    assert render_filename(values, "%author% - %title%") == "Jo Nesbø - Snømannen – Harry Hole ☆"
    # Dropped characters leave no doubled spaces or dangling separators.
    assert render_filename(values, "%author% - %title%", ascii_only=True) == "Jo Nesbo - Snomannen - Harry Hole"
    # Nothing ASCII left: the fallback name, not an empty one.
    assert render_filename({"title": "ナルト"}, "%title%", ascii_only=True) == "untitled"


def test_dialog_checkbox_changes_the_preview_and_reports_back(tmp_path):
    from redactor_common.gui.rename_pattern_dialog import RenamePatternDialog

    item = str(tmp_path / "old.mkv")
    changes = []
    dialog = RenamePatternDialog(
        [item], [("title", "Title")], lambda _i: {"title": "Amélie Poulain"}, lambda i: i,
        pattern_history=[], default_pattern="%title%", on_ascii_only_changed=changes.append,
    )
    assert os.path.basename(dialog.planned_renames()[0][2]) == "Amélie Poulain.mkv"
    dialog.ascii_cb.setChecked(True)
    assert os.path.basename(dialog.planned_renames()[0][2]) == "Amelie Poulain.mkv"
    assert changes == [True] and dialog.ascii_only()

    remembered = RenamePatternDialog(
        [item], [("title", "Title")], lambda _i: {"title": "Amélie"}, lambda i: i,
        pattern_history=[], default_pattern="%title%", ascii_only=True,
    )
    assert remembered.ascii_cb.isChecked() and os.path.basename(remembered.planned_renames()[0][2]) == "Amelie.mkv"
