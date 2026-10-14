"""gui/tool_path_row.py: mp3's deferred, read-only rows and video's
save-on-edit rows, offscreen; the file dialog is stubbed."""

import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest  # noqa: E402
from PyQt6.QtWidgets import QApplication, QFileDialog  # noqa: E402

from redactor_common.gui import tool_path_row as tpr  # noqa: E402
from redactor_common.gui.colors import FOUND_TEXT_COLOR, MISSING_TEXT_COLOR  # noqa: E402
from redactor_common.gui.tool_path_row import ToolPathRow, ToolPathsWidget  # noqa: E402

_app = QApplication.instance() or QApplication([])


@pytest.fixture
def exe(tmp_path):
    path = tmp_path / "tool.exe"
    path.write_text("x")
    return str(path)


def _pick(monkeypatch, result):
    seen = {}

    def get_open(parent, caption, start, file_filter, *a, **k):
        seen.update(caption=caption, start=start, filter=file_filter)
        return result, ""

    monkeypatch.setattr(QFileDialog, "getOpenFileName", staticmethod(get_open))
    return seen


def test_immediate_commit_like_video(monkeypatch, exe):
    stored = {"ffmpeg": ""}
    row = ToolPathRow("ffmpeg:", "ffmpeg", stored["ffmpeg"],
                      probe=lambda _path: bool(stored["ffmpeg"]),  # video reads the stored override
                      on_change=lambda path: stored.__setitem__("ffmpeg", path), status_width=60)
    assert row.status_label.text() == "\u2717 missing"
    assert MISSING_TEXT_COLOR in row.status_label.styleSheet()
    assert row.status_label.width() == 60 or row.status_label.maximumWidth() == 60
    assert not row.path_edit.isReadOnly()

    seen = _pick(monkeypatch, exe)
    row.browse()
    assert stored["ffmpeg"] == exe  # stored before the probe ran
    assert row.status_label.text() == "\u2713 found" and FOUND_TEXT_COLOR in row.status_label.styleSheet()
    assert seen["caption"] == "Locate ffmpeg"

    row.path_edit.setText("  C:/typed/ffmpeg.exe  ")
    assert stored["ffmpeg"] == exe  # not until editing finishes
    row.path_edit.editingFinished.emit()
    assert stored["ffmpeg"] == "C:/typed/ffmpeg.exe"

    row.clear()
    assert stored["ffmpeg"] == "" and row.current_path() == ""
    assert row.status_label.text() == "\u2717 missing"


def test_unchanged_edit_does_not_commit(exe):
    commits = []
    row = ToolPathRow("x:", "x", exe, probe=lambda p: True, on_change=commits.append)
    row.path_edit.editingFinished.emit()
    assert commits == []
    row.set_path(exe)
    assert commits == []


def test_browse_cancel_leaves_path(monkeypatch, exe):
    commits = []
    row = ToolPathRow("x:", "x", exe, probe=lambda p: True, on_change=commits.append)
    seen = _pick(monkeypatch, "")
    row.browse()
    assert row.current_path() == exe and commits == []
    assert seen["start"] == os.path.dirname(exe)


def test_deferred_read_only_like_mp3(monkeypatch, exe):
    commits = []
    emitted = []
    row = ToolPathRow("mp3val:", "mp3val.exe", "", on_change=commits.append,
                      commit="deferred", read_only=True, missing_text="\u2717 not found")
    row.path_changed.connect(emitted.append)
    assert row.path_edit.isReadOnly()
    _pick(monkeypatch, exe)
    row.browse()
    assert commits == [] and emitted == [exe]  # nothing stored by the row
    assert row.current_path() == exe
    assert row.status_label.text() == "\u2713 found"  # default probe: find_tool with the field as override
    row.clear()
    assert emitted == [exe, ""]


def test_default_probe_uses_the_typed_override(tmp_path, exe):
    row = ToolPathRow("x:", "definitely-not-a-real-tool-xyz", "", commit="deferred")
    assert row.is_found() is False
    row.set_path(str(tmp_path / "missing.exe"))
    assert row.is_found() is False
    row.set_path(exe)
    assert row.is_found() is True


def test_raising_probe_counts_as_missing():
    row = ToolPathRow("x:", "x", "", probe=lambda p: 1 / 0)
    assert row.status_label.text() == "\u2717 missing"


def test_bad_commit_policy_rejected():
    with pytest.raises(ValueError):
        ToolPathRow("x:", "x", commit="sometimes")


def test_file_filter_is_platform_aware(monkeypatch):
    seen = _pick(monkeypatch, "")
    ToolPathRow("x:", "x", probe=lambda p: False).browse()
    if sys.platform == "win32":
        assert seen["filter"].startswith("Executables (*.exe)")
    else:
        assert seen["filter"] == "All files (*)"
    seen = _pick(monkeypatch, "")
    ToolPathRow("x:", "x", probe=lambda p: False, file_filter="Tools (*)").browse()
    assert seen["filter"] == "Tools (*)"


def test_paths_widget(exe):
    rows = [
        ToolPathRow("ffmpeg:", "ffmpeg", exe, probe=lambda p: bool(p)),
        ToolPathRow("ffprobe:", "ffprobe", "", probe=lambda p: bool(p)),
    ]
    page = ToolPathsWidget(rows, "Point at the tools here.",
                           links_html='Downloads: <a href="https://ffmpeg.org/">ffmpeg</a>')
    assert page.intro_label.text() == "Point at the tools here."
    assert page.paths() == {"ffmpeg": exe, "ffprobe": ""}
    assert list(page.path_edits) == ["ffmpeg", "ffprobe"]
    assert page.status_labels["ffmpeg"].text() == "\u2713 found"
    assert page.status_labels["ffprobe"].text() == "\u2717 missing"
    assert page.row("ffprobe") is rows[1]
    rows[1].path_edit.setText(exe)
    page.refresh_all()
    assert page.status_labels["ffprobe"].text() == "\u2713 found"
    assert tpr.ToolPathsWidget([]).paths() == {}
