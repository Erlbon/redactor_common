"""Headless (QT_QPA_PLATFORM=offscreen) tests for the gui/ modules that
previously had none: image decoding, AsyncIconCache's loader path,
AsyncPreviewLoader, VisibleRowsWatcher, ProgressReporter, plus smoke
tests of the shared dialogs every app builds on (preview_table-based
overwrite review, LookupDialogBase)."""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import time  # noqa: E402

from PyQt6.QtCore import QBuffer, QByteArray, QIODevice, QSize  # noqa: E402
from PyQt6.QtGui import QColor, QImage  # noqa: E402
from PyQt6.QtWidgets import QApplication, QTableWidget, QTableWidgetItem  # noqa: E402

from redactor_common.gui.async_icon_cache import AsyncIconCache  # noqa: E402
from redactor_common.gui.async_preview import AsyncPreviewLoader  # noqa: E402
from redactor_common.gui.image_decode import decode_scaled  # noqa: E402
from redactor_common.gui.progress import ProgressReporter  # noqa: E402
from redactor_common.gui.visible_rows import VisibleRowsWatcher  # noqa: E402

_app = QApplication.instance() or QApplication([])


def _jpeg(width=400, height=600) -> bytes:
    img = QImage(width, height, QImage.Format.Format_RGB32)
    img.fill(QColor(200, 30, 30))
    data = QByteArray()
    buf = QBuffer(data)
    buf.open(QIODevice.OpenModeFlag.WriteOnly)
    img.save(buf, "JPEG")
    return bytes(data)


def _pump_until(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while not predicate() and time.monotonic() < deadline:
        _app.processEvents()
        time.sleep(0.01)
    return predicate()


class _Item:
    """Weak-referenceable stand-in for a model object."""


# -- image_decode -----------------------------------------------------------------

def test_decode_scaled_downscales_and_never_upscales():
    image = decode_scaled(_jpeg(400, 600), QSize(100, 100))
    assert (image.width(), image.height()) == (67, 100) or image.height() == 100
    small = decode_scaled(_jpeg(40, 60), QSize(100, 100))
    assert (small.width(), small.height()) == (40, 60)


def test_decode_scaled_bad_or_empty_data_is_null():
    assert decode_scaled(b"not an image", QSize(10, 10)).isNull()
    assert decode_scaled(None, QSize(10, 10)).isNull()


# -- AsyncIconCache ----------------------------------------------------------------

def test_icon_cache_loader_path_and_equality_source():
    cache = AsyncIconCache(QSize(32, 32))
    item = _Item()
    ready = []
    cache.icon_ready.connect(lambda key, icon: ready.append((key, icon)))
    calls = []

    def loader():
        calls.append(1)
        return _jpeg()

    cache.request(item, ("a.cbz", 1.0), loader=loader)
    cache.request(item, ("a.cbz", 1.0), loader=loader)  # in flight: dropped
    assert _pump_until(lambda: ready)
    assert len(calls) == 1
    # an equal (not identical) tuple still hits the cache
    assert cache.get_cached_icon(item, ("a.cbz", 1.0)) is not None
    assert cache.get_cached_icon(item, ("a.cbz", 2.0)) is None


def test_icon_cache_bytes_source_compared_by_identity():
    cache = AsyncIconCache(QSize(32, 32))
    item = _Item()
    data = _jpeg()
    ready = []
    cache.icon_ready.connect(lambda key, icon: ready.append(icon))
    cache.request(item, data, data)
    assert _pump_until(lambda: ready)
    assert cache.get_cached_icon(item, data) is not None
    assert cache.get_cached_icon(item, bytes(bytearray(data))) is None


def test_icon_cache_loader_exception_gives_blank_icon():
    cache = AsyncIconCache(QSize(32, 32))
    item = _Item()
    ready = []
    cache.icon_ready.connect(lambda key, icon: ready.append(icon))

    def broken():
        raise OSError("archive gone")

    cache.request(item, "v1", loader=broken)
    assert _pump_until(lambda: ready)
    assert ready[0].isNull()


# -- AsyncPreviewLoader ----------------------------------------------------------------

def test_preview_loader_delivers_only_the_latest_request():
    loader = AsyncPreviewLoader(QSize(100, 100), debounce_ms=10000)
    results = []
    loader.image_ready.connect(lambda token, image: results.append((token, image)))
    loader.request("first", _jpeg)
    loader.request("second", _jpeg)  # replaces "first" before it starts
    loader.wait_for_done()
    assert _pump_until(lambda: results)
    assert [t for t, _ in results] == ["second"]
    assert results[0][1].height() == 100


def test_preview_loader_drops_a_superseded_running_load(tmp_path):
    path = tmp_path / "thumb.jpg"
    path.write_bytes(_jpeg())
    loader = AsyncPreviewLoader(QSize(50, 50), debounce_ms=0)
    results = []
    loader.image_ready.connect(lambda token, image: results.append(token))

    def slow():
        time.sleep(0.3)
        return str(path)  # a path is read and decoded too

    loader.request("slow", slow)
    loader.wait_for_done(0)  # starts it
    loader.request("fast", lambda: str(path))
    loader.wait_for_done()
    _pump_until(lambda: "fast" in results, timeout=3)
    _app.processEvents()
    assert results == ["fast"]


def test_preview_loader_failure_emits_null_image():
    loader = AsyncPreviewLoader(debounce_ms=0)
    results = []
    loader.image_ready.connect(lambda token, image: results.append(image))
    loader.request("x", lambda: None)
    loader.wait_for_done()
    assert _pump_until(lambda: results)
    assert results[0].isNull()


# Deleting a loader's owner while a load runs used to deadlock:
# ~QThreadPool() waited for the load with the GIL held, and the load
# (Python code) needed the GIL to finish. The app hung on quit if a
# thumbnail was still loading; pytest hung at exit once failing tests
# kept windows alive until its final gc. Run in a child process, since
# the failure is a hang, not an exception.
_DELETE_WHILE_LOADING = """
import gc, sys, time
from PyQt6.QtCore import QObject
from PyQt6.QtWidgets import QApplication
from redactor_common.gui.async_preview import AsyncPreviewLoader

app = QApplication([])
started = []

def slow():
    started.append(1)
    time.sleep(0.3)  # drops the GIL; returning needs it back
    return None

for how in ("refcount", "gc", "exit"):
    started.clear()
    owner = QObject()
    owner.loader = AsyncPreviewLoader(debounce_ms=0, parent=owner)
    owner.loader.request(how, slow)
    owner.loader.wait_for_done(0)  # starts it without waiting
    while not started:
        time.sleep(0.01)
    if how == "exit":
        break  # the app quitting with the load still running
    if how == "gc":
        owner.cycle = owner
    del owner
    gc.collect()
    deadline = time.monotonic() + 1
    while time.monotonic() < deadline:  # the late result reaches a deleted loader
        app.processEvents()
print("ok", flush=True)
"""


def test_preview_loader_owner_deleted_while_loading_does_not_hang():
    import subprocess
    import sys
    from pathlib import Path

    import redactor_common

    env = dict(os.environ, QT_QPA_PLATFORM="offscreen")
    # The child must import the same redactor_common as this process.
    env["PYTHONPATH"] = os.pathsep.join(
        p for p in (str(Path(redactor_common.__file__).parent.parent), env.get("PYTHONPATH")) if p)
    result = subprocess.run([sys.executable, "-c", _DELETE_WHILE_LOADING], env=env,
                            capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "ok"


def test_preview_loader_shutdown_drops_pending_and_waits_for_running():
    loader = AsyncPreviewLoader(debounce_ms=0)
    finished, results = [], []
    loader.image_ready.connect(lambda token, image: results.append(token))

    def slow():
        time.sleep(0.2)
        finished.append(1)

    loader.request("running", slow)
    loader.wait_for_done(0)
    loader.request("pending", slow)  # debounced, not started yet
    assert loader.shutdown()
    assert finished == [1]  # the running load finished before shutdown() returned
    _pump_until(lambda: False, timeout=0.3)
    assert results == []  # and neither result is delivered

# -- VisibleRowsWatcher ------------------------------------------------------------------

def test_visible_rows_watcher_reports_viewport_plus_buffer():
    table = QTableWidget(500, 1)
    for row in range(500):
        table.setItem(row, 0, QTableWidgetItem(str(row)))
    table.resize(300, 300)
    table.show()
    _app.processEvents()
    seen = []
    watcher = VisibleRowsWatcher(table, seen.append, buffer_rows=5, debounce_ms=10)
    watcher.check_now()
    rows = seen[-1]
    assert rows[0] == 0 and len(rows) < 100
    table.setRowHidden(2, True)
    table.verticalScrollBar().setValue(200)
    assert _pump_until(lambda: len(seen) >= 2 and seen[-1][0] > 0)
    assert 2 not in seen[-1]
    assert min(seen[-1]) >= 190
    table.close()


def test_visible_rows_watcher_empty_table_no_callback():
    table = QTableWidget(0, 1)
    seen = []
    VisibleRowsWatcher(table, seen.append).check_now()
    assert seen == []


# -- ProgressReporter ----------------------------------------------------------------------

def test_progress_reporter_below_threshold_has_no_dialog():
    with ProgressReporter(None, total=2, label="x", threshold=3) as reporter:
        assert reporter.dialog is None
        reporter.on_progress(1, 2)
        assert reporter.should_cancel() is False


def test_progress_reporter_dialog_fixed_width_and_cancel():
    cancelled = []
    with ProgressReporter(None, total=10, label="Working", threshold=3) as reporter:
        dialog = reporter.dialog
        assert dialog is not None
        assert dialog.minimumWidth() == dialog.maximumWidth()
        reporter.set_label("x" * 500)
        assert len(dialog.labelText()) <= 70
        reporter.connect_cancel(lambda: cancelled.append(True))
        dialog.cancel()
        assert reporter.should_cancel() is True
    assert cancelled == [True]
    assert reporter.dialog is None  # closed on exit


# -- shared dialog smoke tests -----------------------------------------------------------------

class _Meta:
    def __init__(self, **values):
        self.__dict__.update(values)


class _Book:
    def __init__(self, path, **values):
        self.path = path
        self.metadata = _Meta(**values)


def test_overwrite_review_rows_blank_ticked_overwrite_unticked():
    from redactor_common.gui.overwrite_review_dialog import build_overwrite_review_rows

    book = _Book("dir/a.cbz", title="Old", series="")
    rows = build_overwrite_review_rows(
        [book], {0: {"title": "New", "series": "S"}}, lambda attr: attr.capitalize()
    )
    by_attr = {row.item_index[1]: row for row in rows}
    assert by_attr["title"].default_checked is False  # real overwrite starts unticked
    assert by_attr["series"].default_checked is True  # filling a blank starts ticked
    assert by_attr["title"].old_value == "Old" and by_attr["title"].group == "a.cbz"


def test_lookup_dialog_base_constructs():
    from redactor_common.gui import lookup_dialog

    assert hasattr(lookup_dialog, "LookupDialogBase")
    assert hasattr(lookup_dialog, "LookupResult")


def test_parse_filename_dialog_uses_field_patterns_and_normalizers():
    from redactor_common.core.filename_parser import (
        MONTH_FIELD_PATTERN, SERIES_INDEX_FIELD_PATTERN, normalize_month,
    )
    from redactor_common.gui.parse_filename_dialog import ParseFilenameDialog

    items = ["Saga 01-03 - Title Jan.epub", "Other 2 - Book Mar.epub"]
    dialog = ParseFilenameDialog(
        items, [("series", "Series"), ("series_index", "#"), ("title", "Title"), ("month", "Month")],
        lambda item: item,
        pattern_history=["%series% %series_index% - %title% %month%"],
        default_pattern="%title%",
        valid_field_keys={"series", "series_index", "title", "month"},
        strip_leading_zeros_fields={"series_index"},
        field_patterns={"series_index": SERIES_INDEX_FIELD_PATTERN, "month": MONTH_FIELD_PATTERN},
        normalizers={"month": normalize_month},
    )
    changes = dialog.accepted_changes()
    assert changes[0] == {"series": "Saga", "series_index": "1-3", "title": "Title", "month": "1"}
    assert changes[1]["month"] == "3"


def test_rename_pattern_dialog_plans_unique_paths(tmp_path):
    from redactor_common.gui.rename_pattern_dialog import RenamePatternDialog

    items = [str(tmp_path / "a.cbz"), str(tmp_path / "b.cbz")]
    dialog = RenamePatternDialog(
        items, [("title", "Title")], lambda item: {"title": "Same"}, lambda item: item,
        pattern_history=["%title%"], default_pattern="%title%",
    )
    new_names = sorted(os.path.basename(new) for _item, _old, new in dialog.planned_renames())
    assert new_names == ["Same (2).cbz", "Same.cbz"]


def test_icon_cache_accepts_unhashable_keys_and_drops_dead_ones():
    import dataclasses
    import gc

    from redactor_common.gui.async_icon_cache import IdentityWeakDict

    @dataclasses.dataclass
    class Book:  # eq=True -> unhashable, like cbz's CbzBook
        path: str

    a, b = Book("x"), Book("x")  # equal but distinct
    d = IdentityWeakDict()
    d[a] = 1
    assert d.get(a) == 1 and d.get(b) is None and len(d) == 1
    del a
    gc.collect()
    assert len(d) == 0

    cache = AsyncIconCache(QSize(16, 16))
    ready = []
    cache.icon_ready.connect(lambda key, icon: ready.append(key))
    book = Book("y")
    cache.request(book, "v1", loader=_jpeg)
    assert _pump_until(lambda: ready)
    assert cache.get_cached_icon(book, "v1") is not None


# -- image_pane / AspectRatioImageLabel resizing -------------------------------------------------

def test_image_label_can_shrink_after_growing():
    from PyQt6.QtGui import QPixmap

    from redactor_common.gui.image_label import AspectRatioImageLabel

    label = AspectRatioImageLabel()
    label.set_original_pixmap(QPixmap.fromImage(QImage.fromData(_jpeg(400, 600))))
    label.show()  # a hidden widget gets no resize events
    label.resize(400, 600)
    _app.processEvents()
    # A plain QLabel would now report the 400x600 pixmap as its minimum.
    assert label.minimumSizeHint().height() < 50
    label.resize(100, 150)
    _app.processEvents()
    assert label.pixmap().height() <= 150
    label.close()


def test_image_panel_splitter_lets_the_image_pane_be_resized():
    from PyQt6.QtWidgets import QLabel

    from redactor_common.gui.image_pane import ImagePanelSplitter, ImagePreviewBox

    box = ImagePreviewBox("A Very Long Group Box Title That Is Wide", placeholder="No cover")
    assert box.image_label.text() == "No cover"
    splitter = ImagePanelSplitter(QLabel("fields"), box)
    splitter.resize(300, 800)
    splitter.show()
    _app.processEvents()

    splitter.setSizes([200, 600])
    _app.processEvents()
    big = splitter.sizes()[1]
    splitter.setSizes([700, 100])
    _app.processEvents()
    assert splitter.sizes()[1] < big  # the image pane shrinks again after growing
    # The group box's long title doesn't set the pane's minimum width.
    assert splitter.image_container.minimumSizeHint().width() < 120

    box.show_image(QImage.fromData(_jpeg()))
    assert box.has_image()
    box.show_image(None, "Unreadable")
    assert not box.has_image() and box.image_label.text() == "Unreadable"

    splitter.set_image_visible(False)
    assert splitter.image_container.isHidden()
    splitter.close()


def test_message_box_width_rule_leaves_the_icon_alone():
    """Only the text labels are capped: capping the icon's label too
    pushed the text away from the icon when buttons were wide."""
    from PyQt6.QtWidgets import QApplication, QLabel, QMessageBox

    from redactor_common.gui.qmessagebox_style import apply_message_box_style

    app = QApplication.instance()
    saved = app.styleSheet()
    try:
        app.setStyleSheet("")
        apply_message_box_style(app)
        box = QMessageBox(QMessageBox.Icon.Warning, "T", "Some text")
        for name in ("Open a Very Long Download Page", "Another Wide Button", "Locate Manually..."):
            box.addButton(name, QMessageBox.ButtonRole.ActionRole)
        box.setInformativeText(
            "You can still use features that don't need them, but anything relying on a missing tool "
            "will fail until it's installed."
        )
        box.show()
        for _ in range(5):  # the filter finishes the layout on the next event-loop turn
            app.processEvents()
        text = box.findChild(QLabel, "qt_msgbox_label")
        info = box.findChild(QLabel, "qt_msgbox_informativelabel")
        # Nothing clipped: each label is tall enough for its wrapped text.
        assert info.height() >= info.heightForWidth(info.width())
        icon = box.findChild(QLabel, "qt_msgboxex_icon_label")
        assert "qt_msgbox_label" in app.styleSheet() and "QMessageBox QLabel {" not in app.styleSheet()
        assert text.geometry().left() - icon.geometry().right() < 60  # text right next to the icon
        box.close()
    finally:
        app.setStyleSheet(saved)


def test_lookup_dialog_names_its_rows(tmp_path):
    """A row can be a folder (mp3redactor: one album per folder), not a file."""
    from redactor_common.gui.lookup_dialog import LookupDialogBase, LookupResult

    def build(**extra):
        return LookupDialogBase(
            ["a", "b"], None, window_title="T", info_text="i", search_label="s", item_label=str,
            search_one=lambda item, _q: LookupResult(fields={"x": item}) if item == "a" else LookupResult(),
            **extra,
        )

    default = build()
    assert default.table.horizontalHeaderItem(0).text() == "File"
    assert "1 of 2 file(s)" in default.status_label.text()
    folders = build(item_noun="folder")
    assert folders.table.horizontalHeaderItem(0).text() == "Folder"
    assert "1 of 2 folder(s)" in folders.status_label.text()


# -- build_menu_bar ----------------------------------------------------------------------

def _menu_specs():
    from redactor_common.gui.menu_builder import MenuAction
    specs = {name: [] for name in ("File", "Import", "Operations", "Settings", "Help")}
    specs["File"] = [MenuAction("load", "&Load...", lambda: None)]
    return specs


def test_build_menu_bar_places_extra_menus():
    from PyQt6.QtWidgets import QMainWindow

    from redactor_common.gui.menu_builder import MenuAction, build_menu_bar

    window = QMainWindow()
    actions = build_menu_bar(window, _menu_specs(),
                             extra_menus=[("Collection", 3, [MenuAction("scan", "&Scan...", lambda: None)])])
    assert [a.text() for a in window.menuBar().actions()] == [
        "&File", "&Import", "&Operations", "&Collection", "&Settings", "&Help"]
    assert set(actions) == {"load", "scan"}


def test_build_menu_bar_rejects_an_unknown_menu_key():
    # cbzredactor's Collection menu was a specs key and silently never appeared.
    import pytest
    from PyQt6.QtWidgets import QMainWindow

    from redactor_common.gui.menu_builder import build_menu_bar

    specs = _menu_specs()
    specs["Collection"] = []
    with pytest.raises(ValueError, match="extra_menus"):
        build_menu_bar(QMainWindow(), specs)


def test_rename_pattern_dialog_restores_and_reports_zero_pad(tmp_path):
    from redactor_common.gui.rename_pattern_dialog import RenamePatternDialog

    seen = []
    dialog = RenamePatternDialog(
        [str(tmp_path / "a.cbz")], [("number", "Number")], lambda item: {"number": "1"}, lambda item: item,
        pattern_history=[], default_pattern="%number%", zero_pad_field="number",
        zero_pad_initial=(True, 3), on_zero_pad_changed=lambda on, w: seen.append((on, w)),
    )
    assert dialog.zero_pad_cb.isChecked()
    assert dialog.zero_pad_width_combo.currentData() == 3
    assert seen == []  # restoring the saved state must not re-save it
    dialog.zero_pad_width_combo.setCurrentIndex(0)
    dialog.zero_pad_cb.setChecked(False)
    assert seen == [(True, 2), (False, 2)]


def test_auto_numbering_dialog_restores_and_reports_padding():
    from redactor_common.gui.auto_numbering_dialog import AutoNumberingDialog

    seen = []
    dialog = AutoNumberingDialog(
        ["x"], [("number", "Number", True)], lambda item, key: "", lambda item: item,
        padding=4, on_padding_changed=seen.append,
    )
    assert dialog.padding_spin.value() == 4
    assert seen == []
    dialog.padding_spin.setValue(3)
    assert seen == [3]
