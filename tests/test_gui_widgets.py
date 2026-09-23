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
