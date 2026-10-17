"""core/clear_fields.py (pure) and gui/clear_fields_dialog.py (offscreen)."""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from dataclasses import dataclass
from enum import Enum

import pytest
from PyQt6.QtWidgets import QApplication, QHeaderView

from redactor_common.core.clear_fields import FieldGroup, describe_fields, plan_clear
from redactor_common.gui.clear_fields_dialog import WARNING, ClearFieldsDialog
from redactor_common.gui.table_columns import lint_resizable_headers

_app = QApplication.instance() or QApplication([])


class Kind(str, Enum):
    MOVIE = "Movie"
    NONE = ""


@dataclass
class Thing:
    name: str
    title: str = ""
    cast: str = ""
    tmdb_id: str = ""
    kind: object = Kind.NONE
    stamp: str = "SCAN"


GROUPS = [
    FieldGroup("descriptive", "Descriptive fields", "title, cast..."),
    FieldGroup("sources", "Source IDs"),
    FieldGroup("type", "Type", default_checked=False),
]
MEMBERS = {"descriptive": ["title", "cast"], "sources": ["tmdb_id"], "type": ["kind"]}


def fields_of(t):
    return {"title": t.title, "cast": t.cast, "tmdb_id": t.tmdb_id, "kind": t.kind, "stamp": t.stamp}


def things():
    return [Thing("a", title="T", cast="X", tmdb_id="m:1", kind=Kind.MOVIE), Thing("b", title="  "),
            Thing("c", cast="Y"), Thing("d")]


def test_plan_counts_only_values_and_never_unlisted_fields():
    plan = plan_clear(things(), GROUPS, fields_of, MEMBERS)
    totals = plan.totals()
    assert (totals["descriptive"].values, totals["descriptive"].items) == (3, 2)
    assert (totals["sources"].values, totals["sources"].items) == (1, 1)
    assert (totals["type"].values, totals["type"].items) == (1, 1)  # an Enum counts by its value
    assert len(plan.items) == 4 and not plan.items[1] and not plan.items[3]  # blank / empty: nothing to clear
    assert all("stamp" not in p.fields for p in plan.items)  # in no group: never cleared


def test_selected_restricts_to_the_chosen_groups_and_drops_empty_items():
    plan = plan_clear(things(), GROUPS, fields_of, MEMBERS)
    chosen = plan.selected(["sources"])
    assert [p.item.name for p in chosen] == ["a"] and chosen[0].fields == {"tmdb_id": "m:1"}
    both = plan.selected(["descriptive", "sources"])
    assert [p.item.name for p in both] == ["a", "c"] and both[0].count == 3
    assert plan.total_values(["descriptive", "sources", "type"]) == 5
    assert plan.selected([]) == []


def test_describe_fields_limits_the_list():
    assert describe_fields({"a": "1", "b": "2"}) == "a, b"
    assert describe_fields({c: "x" for c in "abcdefgh"}, str.upper, limit=3) == "A, B, C (+5 more)"


def test_none_and_numbers_are_handled():
    plan = plan_clear([Thing("n")], GROUPS, lambda t: {"title": None, "cast": 0, "tmdb_id": 5}, MEMBERS)
    assert plan.items[0].fields == {"cast": "0", "tmdb_id": "5"}


def dialog(items=None):
    plan = plan_clear(items or things(), GROUPS, fields_of, MEMBERS)
    return ClearFieldsDialog(plan, lambda t: t.name, lambda f: f.capitalize())


def test_dialog_group_counts_defaults_and_preview():
    d = dialog()
    assert d.windowTitle() == "Clear fields of 4 file(s)"
    assert d.boxes["descriptive"].text() == "Descriptive fields: 3 values in 2 files"
    assert d.boxes["sources"].text() == "Source IDs: 1 value in 1 file"
    assert d.boxes["descriptive"].isChecked() and d.boxes["sources"].isChecked() and not d.boxes["type"].isChecked()
    assert d.chosen_groups() == ["descriptive", "sources"]
    assert d.table.rowCount() == 2 and d.table.item(0, 0).text() == "a"
    assert d.table.item(0, 1).text() == "Title, Cast, Tmdb_id  (3 values)"
    d.boxes["type"].setChecked(True)
    assert d.table.item(0, 1).text().endswith("(4 values)")
    d.boxes["descriptive"].setChecked(False)
    d.boxes["sources"].setChecked(False)
    d.boxes["type"].setChecked(False)
    assert d.table.rowCount() == 0 and not d.clear_button.isEnabled()  # nothing to clear: Clear is greyed


def test_dialog_warning_and_cancel_is_the_default_button():
    d = dialog()
    assert d.warning_label.text() == WARNING == (
        "This empties the fields in the file(s) when you Save. You can undo before saving.")
    assert "red" in d.warning_label.styleSheet() or "#b00020" in d.warning_label.styleSheet()
    assert d.cancel_button.isDefault() and not d.clear_button.isDefault()
    assert d.clear_button.text() == "Clear" and d.cancel_button.text() == "Cancel"


def test_dialog_accepts_with_the_chosen_groups_and_headers_are_draggable():
    d = dialog()
    d.boxes["sources"].setChecked(False)
    d.clear_button.click()
    assert d.result() == d.DialogCode.Accepted and d.chosen_groups() == ["descriptive"]
    header = d.table.horizontalHeader()
    assert all(header.sectionResizeMode(i) == QHeaderView.ResizeMode.Interactive for i in range(2))
    d2 = dialog()
    d2.cancel_button.click()
    assert d2.result() == d2.DialogCode.Rejected


def test_the_gui_folder_passes_the_resizable_columns_lint():
    from pathlib import Path
    import redactor_common.gui as gui_pkg
    assert lint_resizable_headers(Path(gui_pkg.__file__).parent) == []
