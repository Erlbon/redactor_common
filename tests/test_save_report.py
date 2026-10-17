"""core/save_report: the Save All result in words; run_with_progress(on_error=...) keeps a batch going."""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PyQt6.QtWidgets import QApplication, QWidget

from redactor_common.core import save_report as sr
from redactor_common.gui.progress import run_with_progress


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def test_nothing_to_save_names_the_reason():
    assert sr.nothing_to_save_reason(0, []) == "Nothing to save: no file has unsaved changes"
    reason = sr.nothing_to_save_reason(3, ["a.mkv", "b.mkv", "c.mkv"])
    assert reason.startswith("Nothing to save: all 3 changed file(s) have load errors") and "a.mkv, b.mkv, c.mkv" in reason
    many = sr.nothing_to_save_reason(8, [f"f{i}.mkv" for i in range(8)])
    assert "f0.mkv" in many and "and 3 more" in many and "f7.mkv" not in many


def test_status_line_and_problem_lines():
    report = sr.SaveReport(saved=2, failed=[("b.mp4", "disk says no")], skipped_load_errors=["c.mp4"],
                           still_dirty=["d.mp4", "e.mp4"])
    assert report.status_line() == ("Saved 2 file(s), 1 failed, 1 skipped (load errors), 2 still marked unsaved (a bug)")
    lines = report.problem_lines()
    assert lines[0] == "b.mp4: disk says no"
    assert lines[1].startswith("c.mp4: skipped")
    assert lines[2] == "2 file(s) are still marked unsaved after saving: d.mp4, e.mp4"
    assert report.has_problems and report.attempted == 3
    ok = sr.SaveReport(saved=3)
    assert ok.status_line() == "Saved 3 file(s)" and ok.problem_lines() == [] and not ok.has_problems
    assert sr.SaveReport(nothing=sr.NOTHING_NO_CHANGES).status_line() == sr.NOTHING_NO_CHANGES
    crashed = sr.SaveReport(saved=1, crashed="ValueError: x", not_attempted=2)
    assert "stopped by an unexpected error: ValueError: x" in crashed.status_line()
    assert "cancelled, 2 not attempted" in crashed.status_line()


class F:
    def __init__(self, name, dirty, error=""):
        self.name, self.dirty, self.error = name, dirty, error


def test_files_still_dirty_is_the_invariant_check():
    files = [F("ok", False), F("bug", True), F("failed", True, "disk says no")]
    assert sr.files_still_dirty(files, lambda f: f.dirty, lambda f: bool(f.error), lambda f: f.name) == ["bug"]


def test_log_is_opt_in_and_never_has_file_names(tmp_path):
    report = sr.SaveReport(saved=2, failed=[("secret name.mp4", "x")])
    assert sr.append_save_log(tmp_path, False, report) is False
    assert not (tmp_path / "save_log.txt").exists()
    assert sr.append_save_log(tmp_path, True, report) is True
    text = (tmp_path / "save_log.txt").read_text(encoding="utf-8")
    assert "saved=2" in text and "failed=1" in text and "secret name" not in text and text.count("\n") == 1
    sr.append_save_log(tmp_path, True, sr.SaveReport(nothing=sr.NOTHING_NO_CHANGES))
    assert (tmp_path / "save_log.txt").read_text(encoding="utf-8").count("\n") == 2


def test_run_with_progress_without_on_error_still_stops_at_the_first_exception(qapp):
    seen = []

    def step(item, _i):
        if item == 2:
            raise ValueError("boom")
        seen.append(item)

    with pytest.raises(ValueError):
        run_with_progress(QWidget(), [1, 2, 3], step, "x", threshold=99)
    assert seen == [1]


def test_run_with_progress_on_error_isolates_each_item(qapp):
    seen, errors = [], []

    def step(item, _i):
        if item == 2:
            raise ValueError("boom")
        seen.append(item)

    done = run_with_progress(QWidget(), [1, 2, 3], step, "x", threshold=99,
                             on_error=lambda item, exc: errors.append((item, str(exc))))
    assert done is True and seen == [1, 3] and errors == [(2, "boom")]
