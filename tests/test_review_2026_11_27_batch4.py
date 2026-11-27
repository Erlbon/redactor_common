"""Review of 2026-11-27, batch 4: the "check first" cards found real (see the review folder's batch4/findings.md):
64, 65, 67, 71, 75, 76, 77, 82, 84, 86, 91 (docstring only), 100, 102. Card 85 is tested in batch 3."""

import argparse
import os
import sys
import types
import zipfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest  # noqa: E402
from PyQt6.QtWidgets import QApplication, QMessageBox, QTableWidget, QTableWidgetItem  # noqa: E402

_app = QApplication.instance() or QApplication([])


# --- 64: no legacy Recycle Bin on Windows ------------------------------------------------------------------------

def test_64_without_the_modern_back_end_the_file_is_kept_or_goes_to_the_fallback(tmp_path, monkeypatch):
    from redactor_common.core import trash
    monkeypatch.setattr(trash, "trash_is_reliable", lambda path: True)
    monkeypatch.setattr(trash, "windows_bin_is_safe", lambda: False)
    sent = []
    monkeypatch.setitem(sys.modules, "send2trash", types.SimpleNamespace(send2trash=sent.append))
    victim = tmp_path / "big.mkv"
    victim.write_bytes(b"x")
    with pytest.raises(trash.TrashUnavailable, match="pywin32"):
        trash.move_to_trash(str(victim))
    assert victim.exists() and sent == []
    folder = tmp_path / "quarantine"
    trash.move_to_trash(str(victim), str(folder))
    assert not victim.exists() and sent == []
    assert [p.name for p in folder.rglob("*") if p.is_file()] == ["big.mkv"]


def test_64_the_check_looks_for_the_modern_back_end(monkeypatch):
    from redactor_common.core import trash
    monkeypatch.setattr(trash.sys, "platform", "linux")
    assert trash.windows_bin_is_safe() is True
    monkeypatch.setattr(trash.sys, "platform", "win32")
    monkeypatch.setitem(sys.modules, "send2trash.win.modern", None)        # import fails
    assert trash.windows_bin_is_safe() is False


# --- 65: Esc lets go of the file -----------------------------------------------------------------------------------

def test_65_closing_with_esc_releases_the_file(tmp_path):
    # Without building a real player: QtMultimedia crashes this test process (a known offscreen problem).
    import inspect
    from redactor_common.gui import media_preview
    done = inspect.getsource(media_preview.MediaPreviewDialog.done)
    assert "release_file()" in done                         # Esc -> reject() -> done() lets go of the file
    calls = []

    class Player:
        def stop(self):
            calls.append("stop")

        def setSource(self, url):
            calls.append(url.isEmpty())
    path = tmp_path / "a.mp3"
    stub = types.SimpleNamespace(player=Player(), _path=str(path))
    stub.release_file = lambda: media_preview.MediaPreviewDialog.release_file(stub)
    other = types.SimpleNamespace(_path=str(tmp_path / "b.mp3"), release_file=lambda: calls.append("other"))
    media_preview._open_windows.extend([stub, other])
    try:
        media_preview.release_previews_of([str(path)])
    finally:
        media_preview._open_windows.remove(stub)
        media_preview._open_windows.remove(other)
    assert calls == ["stop", True]


# --- 67: the caches' pool is drained at quit -----------------------------------------------------------------------

def test_67_the_cache_pool_is_its_own_and_is_drained(monkeypatch):
    import time
    from PyQt6.QtCore import QRunnable, QThreadPool
    from redactor_common.gui import async_icon_cache

    class Slow(QRunnable):
        def run(self):
            time.sleep(0.2)
    pool = async_icon_cache.cache_pool()
    assert pool is not QThreadPool.globalInstance() and pool is async_icon_cache.cache_pool()
    for _ in range(50):
        pool.start(Slow())
    async_icon_cache.drain_cache_pool()
    assert pool.activeThreadCount() == 0


# --- 71: duplicates trash with progress and Cancel ----------------------------------------------------------------

def test_71_the_duplicates_trash_runs_under_run_with_progress(monkeypatch):
    import inspect
    from redactor_common.gui import duplicates_dialog
    source = inspect.getsource(duplicates_dialog)
    assert "run_with_progress(self, members, step" in source


# --- 75: non-text metadata in the overwrite review ---------------------------------------------------------------

def test_75_numbers_and_trailing_spaces_in_the_overwrite_review():
    from redactor_common.gui import overwrite_review_dialog as ord_
    item = types.SimpleNamespace(path="a.mkv", metadata=types.SimpleNamespace(episode_number=5, title="Same"))
    rows = ord_.build_overwrite_review_rows([item], {0: {"episode_number": "6", "title": "Same "}}, str)
    by_field = {r.item_index[1]: r for r in rows}
    assert by_field["episode_number"].old_value == "5" and not by_field["episode_number"].default_checked
    assert by_field["title"].default_checked
    assert ord_._text(None) == "" and ord_._text(["a", "b"]) == "a; b"


# --- 76: blank lines inside a cue ---------------------------------------------------------------------------------

def test_76_a_blank_line_inside_a_cue_does_not_split_it():
    from redactor_common.core import srt
    text = srt.format_srt([srt.Cue(1.0, 2.0, "A\n\nB"), srt.Cue(3.0, 4.0, "C")])
    back = srt.parse_srt(text) if hasattr(srt, "parse_srt") else None
    assert "A\nB\n" in text and "\n\n\n" not in text
    if back is not None:
        assert [c.text for c in back] == ["A\nB", "C"]


# --- 77: the reader picks the entry the writer rewrites -----------------------------------------------------------

def test_77_read_and_write_pick_the_same_comicinfo(tmp_path):
    from redactor_common.core import comicinfo_write, meta_comic
    path = tmp_path / "two.cbz"
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("b/ComicInfo.xml", "<ComicInfo><Title>B</Title></ComicInfo>")
        z.writestr("__MACOSX/ComicInfo.xml", "<ComicInfo><Title>M</Title></ComicInfo>")
        z.writestr("a/ComicInfo.xml", "<ComicInfo><Title>A</Title></ComicInfo>")
        z.writestr("a/p1.jpg", b"x")
        names = z.namelist()
    assert meta_comic._find_member(names) == comicinfo_write.find_comicinfo_member(names) == "a/ComicInfo.xml"


# --- 82: one LRCLIB pace per process -------------------------------------------------------------------------------

def test_82_default_lrclib_clients_share_one_throttle(monkeypatch):
    from redactor_common.core import lrclib
    seen = []
    monkeypatch.setattr(lrclib, "throttled", lambda fetch, throttle: seen.append(throttle) or fetch)
    lrclib.LrclibClient()
    lrclib.LrclibClient()
    lrclib.LrclibClient(interval=5.0)
    assert seen[0] is seen[1] is lrclib._SHARED_THROTTLE and seen[2] is not seen[0]


# --- 84: a group that works resets the failure run ------------------------------------------------------------------

def test_84_a_working_group_resets_the_run_of_single_failures():
    from redactor_common.core import translate
    from redactor_common.core.srt import Cue
    cues = [Cue(i, i + 1, f"line {i}") for i in range(12)]
    bad = {0, 1, 6, 7}           # pairs of 2 fail in a group of 2 -> one by one; a good group sits between

    class Engine(translate.Translator):
        def translate_text(self, text, source, target):
            parts = text.split(translate.DEFAULT_SEPARATOR) if hasattr(translate, "DEFAULT_SEPARATOR") else [text]
            if any(p.strip() in {f"line {i}" for i in bad} for p in parts):
                raise translate.TranslateError("no")
            return text.upper()
    result = translate.translate_cues(cues, "en", "fr", Engine(), group_size=2, sleep=lambda s: None, retries=0)
    assert not result.error and set(result.failed) == bad


# --- 86: MusicBrainz busy waits watch Cancel -----------------------------------------------------------------------

def test_86_a_cancelled_lookup_stops_waiting_for_a_busy_musicbrainz(monkeypatch):
    from redactor_common.core.lookups import musicbrainz
    slept = []
    monkeypatch.setattr(musicbrainz, "_sleep", slept.append)
    calls = []

    def busy(*a, **k):
        calls.append(1)
        raise musicbrainz.MusicBrainzError("MusicBrainz is busy (rate limit)")
    monkeypatch.setattr(musicbrainz, "_get", busy)
    fetch = types.SimpleNamespace(should_cancel=lambda: len(slept) >= 2)
    with pytest.raises(musicbrainz.MusicBrainzError, match="Cancelled"):
        musicbrainz._get_retrying("release", fetch)
    assert len(calls) == 1 and sum(slept) <= 0.5


# --- 100: only the rows the filter shows ----------------------------------------------------------------------------

def test_100_selected_visible_rows_skips_hidden_rows():
    from redactor_common.gui.column_filter import ColumnFilterHeader
    table = QTableWidget(5, 1)
    for row in range(5):
        table.setItem(row, 0, QTableWidgetItem(str(row)))
    header = ColumnFilterHeader(table)
    table.setHorizontalHeader(header)
    table.setRowHidden(1, True)
    table.setRowHidden(3, True)
    table.selectAll()
    assert header.selected_visible_rows() == [0, 2, 4]


# --- 102: --output that is also an input -----------------------------------------------------------------------------

def test_102_output_onto_an_input_is_refused_and_the_file_kept(tmp_path):
    from redactor_common.cli import CliError, make_output
    data = tmp_path / "a.mp3"
    data.write_bytes(b"x" * 800)
    with pytest.raises(CliError, match="also an input"):
        make_output(argparse.Namespace(output=str(data), paths=[str(data)], dry_run=True, json=False, quiet=False))
    assert data.stat().st_size == 800
    out = make_output(argparse.Namespace(output=str(tmp_path / "report.txt"), paths=[str(data)], json=False,
                                         quiet=False))
    out.close() if hasattr(out, "close") else None
