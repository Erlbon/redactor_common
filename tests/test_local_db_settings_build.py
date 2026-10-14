"""gui/local_db_settings_dialog.py's shared build tail (status line, free
space, Save-style Browse, run_build) -- what mp3's MusicBrainz, epub's
Open Library and ISFDB, video's IMDb and cbz's ComicRack dialogs each
repeated. Offscreen; message boxes and file dialogs are stubbed."""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest  # noqa: E402
from PyQt6.QtWidgets import QApplication, QFileDialog, QLabel, QMessageBox  # noqa: E402

from redactor_common.core.dump_import import DumpImportError, ImportCancelled  # noqa: E402
from redactor_common.core.local_db import LocalDatabaseError  # noqa: E402
from redactor_common.gui import local_db_settings_dialog as mod  # noqa: E402
from redactor_common.gui.local_db_settings_dialog import LocalDatabaseSettingsDialog  # noqa: E402

_app = QApplication.instance() or QApplication([])


def _check(path):
    with open(path, encoding="utf-8") as f:
        text = f.read()
    if text != "good":
        raise LocalDatabaseError("not a database")
    return "Looks good: 3 titles"


class Summary:
    def describe(self):
        return "Built 3 titles."


@pytest.fixture
def boxes(monkeypatch):
    """Records QMessageBox calls; `answer` is what question() returns."""
    calls = {"question": [], "information": [], "warning": [], "answer": QMessageBox.StandardButton.Yes}

    def question(parent, title, text, *a, **k):
        calls["question"].append((title, text))
        return calls["answer"]

    monkeypatch.setattr(QMessageBox, "question", staticmethod(question))
    monkeypatch.setattr(QMessageBox, "information", staticmethod(lambda p, t, x, *a, **k: calls["information"].append((t, x))))
    monkeypatch.setattr(QMessageBox, "warning", staticmethod(lambda p, t, x, *a, **k: calls["warning"].append((t, x))))
    return calls


def _dialog(tmp_path, path="", saved=None, **kwargs):
    saved = saved if saved is not None else []
    return LocalDatabaseSettingsDialog(
        title="Test DB", instructions_html="Get it.", path=path, check=_check, save=saved.append,
        **kwargs,
    )


def test_old_api_unchanged(tmp_path):
    dialog = _dialog(tmp_path)
    assert dialog.status_label is None and dialog.space_label is None
    assert dialog.path_edit.placeholderText() == "Path to the database file"
    assert dialog.layout().count() == 4  # intro, path row, actions, buttons -- apps insert at fixed indexes
    assert dialog.destination() == "" and dialog.default_path() == ""
    dialog.refresh_status()  # harmless when nothing is shown


def test_status_line(tmp_path, boxes):
    good = tmp_path / "good.db"
    good.write_text("good", encoding="utf-8")
    bad = tmp_path / "bad.db"
    bad.write_text("junk", encoding="utf-8")
    dialog = _dialog(tmp_path, show_status=True, status_debounce_ms=0)
    assert dialog.status_label.text() == mod.STATUS_NO_PATH
    dialog.path_edit.setText(str(tmp_path / "missing.db"))
    dialog.refresh_status()
    assert dialog.status_label.text() == mod.STATUS_MISSING
    dialog.path_edit.setText(str(good))
    dialog.refresh_status()
    assert dialog.status_label.text() == "Looks good: 3 titles"
    dialog.path_edit.setText(str(bad))
    dialog.refresh_status()
    assert dialog.status_label.text() == "Problem: not a database"
    assert not boxes["warning"]  # the status line never pops up a box
    # status sits between the path row and Check File
    assert dialog.layout().itemAt(2).widget() is dialog.status_label


def test_status_refresh_is_debounced(tmp_path):
    dialog = _dialog(tmp_path, show_status=True, status_debounce_ms=10_000)
    dialog.path_edit.setText(str(tmp_path / "missing.db"))
    assert dialog.status_label.text() == mod.STATUS_NO_PATH  # not yet: typing hasn't paused
    assert dialog._status_timer.isActive()
    dialog._status_timer.timeout.emit()
    assert dialog.status_label.text() == mod.STATUS_MISSING


def test_free_space(tmp_path):
    dialog = _dialog(tmp_path, default_path=str(tmp_path / "sub" / "deeper" / "x.db"),
                     free_space_warning=(1 << 62, " Not enough."))
    text = dialog.space_label.text()
    assert text.startswith("Free space where the database goes:") and text.endswith(" Not enough.")
    assert "Not enough" not in mod.free_space_text(str(tmp_path / "x.db"), 0, " Not enough.")
    assert mod.free_space_text(str(tmp_path / "x.db")).endswith("GB.")
    assert mod.free_space_text("") == ""


def test_default_path_and_save_style_browse(tmp_path, monkeypatch):
    default = str(tmp_path / "default.db")
    seen = {}

    def get_save(parent, caption, start, file_filter, options=None):
        seen.update(caption=caption, start=start, options=options)
        return str(tmp_path / "picked.db"), ""

    monkeypatch.setattr(QFileDialog, "getSaveFileName", staticmethod(get_save))
    monkeypatch.setattr(QFileDialog, "getOpenFileName", staticmethod(lambda *a, **k: pytest.fail("open dialog")))
    dialog = _dialog(tmp_path, default_path=lambda: default, browse_for_destination=True)
    assert dialog.path_edit.placeholderText() == default
    assert dialog.destination() == default
    dialog._browse()
    assert seen["start"] == default and seen["options"] == QFileDialog.Option.DontConfirmOverwrite
    assert dialog.path_edit.text() == str(tmp_path / "picked.db")
    assert dialog.destination() == str(tmp_path / "picked.db")


def test_open_style_browse_by_default(tmp_path, monkeypatch):
    monkeypatch.setattr(QFileDialog, "getOpenFileName", staticmethod(lambda *a, **k: ("", "")))
    dialog = _dialog(tmp_path, path="keep.db")
    dialog._browse()
    assert dialog.path_edit.text() == "keep.db"  # cancelled: unchanged


def test_add_section_goes_above_the_action_row(tmp_path):
    dialog = _dialog(tmp_path, show_status=True)
    box = QLabel("Build from the dump")
    dialog.add_section(box)
    second = QLabel("more")
    dialog.add_section(second)
    outer = dialog.layout()
    sections = outer.itemAt(3).layout()
    assert sections.itemAt(0).widget() is box and sections.itemAt(1).widget() is second
    assert outer.itemAt(4).layout() is dialog._actions


def test_run_build_success_saves_first_and_fills_in(tmp_path, boxes, monkeypatch):
    forgotten = []
    monkeypatch.setattr(mod, "forget_cached", forgotten.append)
    saved = []
    dest = str(tmp_path / "new.db")
    order = []

    def work(path, progress, cancelled):
        order.append(("work", list(saved)))
        progress(0.5)
        assert cancelled() is False
        with open(path, "w", encoding="utf-8") as f:
            f.write("good")
        return Summary()

    dialog = _dialog(tmp_path, saved=saved, default_path=dest, show_status=True)
    result = dialog.run_build("Build Test Database", "Reading…", work,
                              confirm="Build {name} from the dump?\n\nAt {dest}.")
    assert result == dest
    title, text = boxes["question"][0]
    assert title == "Build Database" and text.startswith("Build new.db from the dump?")
    assert dest in text and mod.REPLACING_NOTE not in text  # nothing to replace yet
    assert order == [("work", [dest])]  # the settings were saved before the build ran
    assert forgotten == [dest]
    assert boxes["information"] == [("Build Test Database", "Built 3 titles.")]
    assert dialog.path_edit.text() == dest and dialog.status_label.text() == "Looks good: 3 titles"


def test_run_build_says_it_replaces_an_existing_file(tmp_path, boxes):
    dest = tmp_path / "old.db"
    dest.write_text("good", encoding="utf-8")
    boxes["answer"] = QMessageBox.StandardButton.No
    saved = []
    dialog = _dialog(tmp_path, path=str(dest), saved=saved)
    assert dialog.run_build("T", "L", lambda *a: pytest.fail("built"), confirm="Build {name}?") is None
    assert boxes["question"][0][1] == "Build old.db?" + mod.REPLACING_NOTE
    assert saved == []  # said no: nothing saved


def test_run_build_without_confirm_or_save(tmp_path, boxes):
    saved = []
    dest = str(tmp_path / "x.db")
    dialog = _dialog(tmp_path, saved=saved)
    result = dialog.run_build("T", "L", lambda d, p, c: "plain result", dest=dest,
                              save_first=False, show_summary=True)
    assert result == dest and saved == [] and not boxes["question"]
    assert boxes["information"] == [("T", "plain result")]  # no describe(): str()


def test_run_build_errors_and_cancel(tmp_path, boxes):
    dialog = _dialog(tmp_path, default_path=str(tmp_path / "x.db"))

    def fails(dest, progress, cancelled):
        raise DumpImportError("The dump is truncated.")

    assert dialog.run_build("Build", "L", fails) is None
    assert boxes["warning"] == [("Build", "The dump is truncated.")]

    class AppError(Exception):
        pass

    def app_fails(dest, progress, cancelled):
        raise AppError("bad options")

    assert dialog.run_build("Build", "L", app_fails, error_types=(AppError,)) is None
    assert boxes["warning"][-1] == ("Build", "bad options")
    with pytest.raises(AppError):  # not listed: a bug, so it propagates
        dialog.run_build("Build", "L", app_fails)

    def cancelled_build(dest, progress, cancelled):
        raise ImportCancelled()

    assert dialog.run_build("Build", "L", cancelled_build) is None
    assert dialog.path_edit.text() == ""  # nothing filled in
    assert not boxes["information"]


def test_run_build_needs_a_destination(tmp_path, boxes):
    dialog = _dialog(tmp_path)
    assert dialog.run_build("T", "L", lambda *a: pytest.fail("built")) is None
    assert boxes["information"] and "Choose where" in boxes["information"][0][1]


def test_run_database_build_standalone(tmp_path, boxes, monkeypatch):
    monkeypatch.setattr(mod, "forget_cached", lambda path: None)
    before = []
    dest = str(tmp_path / "lib.db")
    result = mod.run_database_build(None, "Build ComicRack", "Converting…",
                                    lambda d, p, c: Summary(), dest, before=before.append)
    assert result == dest and before == [dest]
    assert boxes["information"] == [("Build ComicRack", "Built 3 titles.")]


def test_build_button_still_works(tmp_path):
    dialog = _dialog(tmp_path, build=lambda d: str(tmp_path / "made.db"), build_label="Build…")
    dialog.build_button.click()
    assert dialog.path_edit.text() == str(tmp_path / "made.db")
