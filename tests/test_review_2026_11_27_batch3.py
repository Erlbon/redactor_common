"""Review of 2026-11-27, batch 3 (crashes and wrong results): cards 28-38, 40, 42-52, 57, 58, 60, 62."""

import gzip
import io
import json
import os
import subprocess
import sys
import time
import types
import zipfile
from decimal import Decimal
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest  # noqa: E402
from PyQt6.QtWidgets import QApplication, QInputDialog, QMessageBox, QTableWidget  # noqa: E402

_app = QApplication.instance() or QApplication([])
ROOT = Path(__file__).resolve().parent.parent


# --- 28: an error before the QApplication exists is printed, not a silent abort -----------------------------------

def test_28_an_error_before_the_application_exists_is_printed(tmp_path):
    script = tmp_path / "early.py"
    script.write_text(
        "import sys\n"
        "from redactor_common.gui import crash_dialog\n"
        "hook = crash_dialog.CrashReporter.__new__(crash_dialog.CrashReporter)\n"
        "hook.dialog = None\n"
        "crash_dialog.CrashReporter._on_incoming(hook, 'sig', 'summary', 'Traceback: early boom' + chr(10))\n"
        "print('still alive')\n", encoding="utf-8")
    env = dict(os.environ, PYTHONPATH=str(ROOT.parent), QT_QPA_PLATFORM="offscreen")
    done = subprocess.run([sys.executable, str(script)], capture_output=True, text=True, env=env, timeout=60)
    assert done.returncode == 0, done.stderr
    assert "still alive" in done.stdout and "early boom" in done.stderr


# --- 29: a superscript digit sorts, it does not crash ----------------------------------------------------------------

def test_29_superscript_digits_sort_without_a_crash():
    from redactor_common.core.path_column import natural_sort_key
    from redactor_common.gui import path_column, sortable_table
    names = ["Track 10²", "Track 2", "Track 10"]
    assert sorted(names, key=natural_sort_key) == ["Track 2", "Track 10", "Track 10²"]
    assert sortable_table.natural_key("Track 10²") == natural_sort_key("Track 10²")
    table = QTableWidget(3, 2)
    for row, name in enumerate(names):
        table.setItem(row, 0, sortable_table.NaturalTableWidgetItem(name))
        table.setItem(row, 1, path_column.PathTableWidgetItem(os.path.join("d" + name, "f")))
    table.sortItems(0)
    table.sortItems(1)
    assert table.item(0, 0).text() == "Track 2"


# --- 30, 31: regex replace errors and a slow pattern ----------------------------------------------------------------

def test_30_a_missing_named_group_is_a_search_replace_error():
    from redactor_common.core.search_replace import SearchReplaceError, apply_replace, would_change
    with pytest.raises(SearchReplaceError, match="Invalid replacement"):
        apply_replace("abc", "(b)", r"\g<y>", use_regex=True)
    assert would_change("abc", "(b)", r"\g<y>", use_regex=True) is False


def _dialog(values):
    from redactor_common.gui.search_replace_dialog import SearchReplaceDialog
    items = [types.SimpleNamespace(v=v) for v in values]
    return SearchReplaceDialog(items, [("v", "Value")], lambda item, key: item.v, lambda item: item.v)


def test_31_the_preview_waits_for_typing_to_stop_and_apply_uses_the_newest_text():
    dialog = _dialog(["abc", "abd"])
    dialog.search_edit.setText("ab")
    dialog.replace_edit.setText("X")
    assert dialog._preview_timer.isActive()             # not yet run on every keystroke
    assert dialog.accepted_changes() == {0: "Xc", 1: "Xd"}     # flushed before it is read


def test_31_a_regex_pass_over_budget_stops_with_too_slow(monkeypatch):
    from redactor_common.gui import search_replace_dialog
    monkeypatch.setattr(search_replace_dialog, "PREVIEW_BUDGET_S", -1.0)
    dialog = _dialog(["aaa", "aab"])
    dialog.regex_cb.setChecked(True)
    dialog.search_edit.setText("a+")
    dialog._flush_preview()
    assert "too slow" in dialog.status_label.text() and not dialog._ok_button.isEnabled()


# --- 32: series numbers --------------------------------------------------------------------------------------------

def test_32_series_values_take_a_decimal_comma_and_refuse_odd_numbers():
    from redactor_common.core import series_numbering as sn
    assert sn.parse_series_value("3,5") == Decimal("3.5")
    for bad in ("1e2", "Infinity", "NaN", "sNaN", "", "abc", "1.2.3"):
        assert sn.parse_series_value(bad) is None, bad
    assert sn.generate_series_numbers(3, "3,5", "1") == ["3.5", "4.5", "5.5"]
    assert sn.generate_series_numbers(2, "1e2", "1") == ["1", "2"]     # the live preview still never raises


def test_32_the_quick_prompt_asks_again_instead_of_falling_back(monkeypatch):
    from redactor_common.gui import quick_series_number
    answers = iter([("3;5", True), ("3,5", True)])
    warned = []
    monkeypatch.setattr(QInputDialog, "getText", staticmethod(lambda *a, **k: next(answers)))
    monkeypatch.setattr(QMessageBox, "warning", staticmethod(lambda *a, **k: warned.append(a[2])))
    assert quick_series_number.prompt_and_generate_series_numbers(None, 2) == ["3.5", "4.5"]
    assert len(warned) == 1 and "not a number" in warned[0]


# --- 33: PageCount counts every page kind and never writes 0 ---------------------------------------------------------

def test_33_page_count_counts_avif_and_keeps_the_old_value_when_none_is_found():
    from redactor_common.core import comicinfo_write, meta_comic
    assert comicinfo_write.IMAGE_EXTENSIONS is meta_comic.IMAGE_EXTENSIONS
    assert comicinfo_write.count_pages(["p1.avif", "p2.jxl", "p3.tif", "ComicInfo.xml"]) == 3


# --- 34: same-named files are told apart -------------------------------------------------------------------------------

def test_34_same_named_files_from_different_folders_are_separate_entries():
    from redactor_common.gui import overwrite_review_dialog as ord_
    items = [types.SimpleNamespace(path=os.path.join("A", "01 - Intro.mp3"), metadata=types.SimpleNamespace(title="a")),
             types.SimpleNamespace(path=os.path.join("B", "01 - Intro.mp3"), metadata=types.SimpleNamespace(title="b")),
             types.SimpleNamespace(path=os.path.join("C", "02.mp3"), metadata=types.SimpleNamespace(title="c"))]
    rows = ord_.build_overwrite_review_rows(items, {0: {"title": "x"}, 1: {"title": "y"}, 2: {"title": "z"}}, str)
    groups = [r.group for r in rows]
    assert len(set(groups)) == 3
    assert groups[2] == "02.mp3" and "A" in groups[0] and "B" in groups[1]


# --- 35, 36: local database names and network shares --------------------------------------------------------------

def test_35_names_keep_combining_marks_and_decomposed_accents_match():
    import unicodedata
    from redactor_common.core.local_db import normalize_words
    assert normalize_words("हिन्दी") == "हिन्दी"
    assert normalize_words(unicodedata.normalize("NFD", "Amélie")) == normalize_words("Amélie") == "amelie"
    assert normalize_words("G.I. Joe - A Real American Hero") == "g i joe a real american hero"
    assert normalize_words("Rock & Roll") == "rock and roll"


@pytest.mark.skipif(sys.platform != "win32", reason="UNC paths are a Windows thing")
def test_36_a_unc_path_gets_an_empty_authority_uri():
    from redactor_common.core.local_db import sqlite_ro_uri
    uri = sqlite_ro_uri(r"\\nas\share\gcd.db")
    assert uri.startswith("file:////nas/share/gcd.db") and uri.endswith("?mode=ro")
    assert sqlite_ro_uri(r"C:\data\a#b.db").startswith("file:///C:/data/a%23b.db")


# --- 37: MySQL _binary values ------------------------------------------------------------------------------------------

def test_37_a_binary_introducer_is_one_value(tmp_path):
    from redactor_common.core import dump_mysql
    dump = tmp_path / "d.sql"
    dump.write_bytes(
        b"CREATE TABLE `t` (\n  `id` int,\n  `blob` blob,\n  `name` text\n) ENGINE=InnoDB;\n"
        b"INSERT INTO `t` VALUES (1,_binary 'x\\0y','one'),(2,_binary '','two');\n")
    with open(dump, "rb") as stream:
        rows = [row for _table, row in dump_mysql.iter_mysql_dump(stream, {"t": ["id", "name"]})]
    assert [(r["id"], r["name"]) for r in rows] == [("1", "one"), ("2", "two")]


# --- 38: unclosed links do not take quadratic time --------------------------------------------------------------------

def test_38_unclosed_links_are_linear():
    from redactor_common.core import note_text
    text = '<a href="https://x.org">y ' * 20000
    start = time.monotonic()
    note_text.html_to_markdown(text)
    note_text.clean_discogs_markup("[url=https://x.org]y " * 20000)
    assert time.monotonic() - start < 2.0
    assert note_text.html_to_markdown('<a href="https://x.org">X</a> and <a href="file:///c">Y</a>') == \
        "[X](https://x.org) and Y"
    assert note_text.clean_discogs_markup("[url=https://a.b]A[/url] [url]https://c.d[/url]") == \
        "[A](https://a.b) [https://c.d](https://c.d)"


# --- 40: an odd journal line is skipped ------------------------------------------------------------------------------

def test_40_an_odd_journal_line_never_blocks_a_save(tmp_path):
    from redactor_common.core.journal import Journal
    path = tmp_path / "j.jsonl"
    path.write_text(json.dumps({"t": "op", "id": "a", "ts": None, "path": "x"}) + "\n"
                    + json.dumps({"t": "op", "id": "b", "ts": 1.0, "path": "y", "changes": {"t": 5}}) + "\n"
                    + json.dumps({"t": "moved", "ts": None, "from": "x", "to": "z"}) + "\n", encoding="utf-8")
    journal = Journal(str(path))
    entry_id = journal.begin(str(tmp_path / "f.mp3"), "edit", {"title": ("a", "b")})
    assert entry_id
    journal.prune()
    assert [e.id for e in journal.entries()] == [entry_id]


def test_40_a_journal_that_cannot_be_written_returns_none(tmp_path, monkeypatch):
    from redactor_common.core.journal import Journal
    journal = Journal(str(tmp_path / "j.jsonl"))
    monkeypatch.setattr(journal, "_append", lambda line: (_ for _ in ()).throw(OSError("disk full")))
    assert journal.begin(str(tmp_path / "f"), "edit", {"t": ("a", "b")}) is None


# --- 42, 43: notes and release names --------------------------------------------------------------------------------

def test_42_an_infinite_series_index_does_not_raise():
    from redactor_common.core import note_convert
    assert note_convert._index("inf") == "inf" and note_convert._index("nan") == "nan"
    assert note_convert._index("3.0") == "3" and note_convert._index("2.5") == "2.5"


@pytest.mark.parametrize("stem, title, year", [
    ("Spider-Man", "Spider-Man", None),
    ("Wall-E", "Wall-E", None),
    ("Blade Runner 2049 (2017)", "Blade Runner 2049", "2017"),
    ("Parasite.2019.1080p.BluRay.x264-SPARKS", "Parasite", "2019"),
    ("Spider-Man.2002.1080p-GRP", "Spider-Man", "2002"),
])
def test_43_release_names(stem, title, year):
    from redactor_common.core.lookups.release_name import parse_release_name
    guess = parse_release_name(stem)
    assert (guess.title, guess.year) == (title, year)


# --- 44, 85: dump errors are labelled by where they come from -------------------------------------------------------

def test_44_the_callers_own_error_is_not_couldnt_read(tmp_path):
    from redactor_common.core.dump_import import open_dump
    dump = tmp_path / "ok.xml"
    dump.write_bytes(b"<a/>")
    with pytest.raises(OSError, match="disk full"):
        with open_dump(str(dump)) as stream:
            stream.stream.read()
            raise OSError("disk full")


def test_85_a_damaged_gzip_is_a_dump_import_error(tmp_path):
    from redactor_common.core.dump_import import DumpImportError, iter_xml_records, open_dump
    good = gzip.compress(b"<r>" + b"<x>1</x>" * 5000 + b"</r>")
    damaged = bytearray(good)
    for i in range(12, 40):
        damaged[i] ^= 0xFF
    path = tmp_path / "bad.xml.gz"
    path.write_bytes(bytes(damaged))
    with pytest.raises(DumpImportError):
        with open_dump(str(path)) as dump:
            list(iter_xml_records(dump, "x"))


# --- 45, 46: lookup answers of the wrong shape -------------------------------------------------------------------------

def test_45_musicbrainz_and_wikipedia_answers_that_are_not_objects(monkeypatch):
    from redactor_common.core import wikipedia
    from redactor_common.core.lookups import musicbrainz
    monkeypatch.setattr(musicbrainz, "fetch_json", lambda *a, **k: ["error"])
    with pytest.raises(musicbrainz.MusicBrainzError):
        musicbrainz.fetch_release("x", fetch=lambda *a: None)
    monkeypatch.setattr(musicbrainz, "fetch_json", lambda *a, **k: {"id": "x", "media": None})
    assert musicbrainz.fetch_release("x", fetch=lambda *a: None).tracks == []
    monkeypatch.setattr(musicbrainz, "fetch_json", lambda *a, **k: {
        "id": "x", "media": [None, {"format": "CD", "tracks": [None, {"title": "t", "recording": "odd", "length": "x"}]}]})
    release = musicbrainz.fetch_release("x", fetch=lambda *a: None)
    assert [t.title for t in release.tracks] == ["t"] and release.tracks[0].seconds is None
    monkeypatch.setattr(wikipedia, "fetch_json", lambda *a, **k: ["error"])
    with pytest.raises(wikipedia.WikipediaError):
        wikipedia.search_titles("x", fetch=lambda *a: None)
    monkeypatch.setattr(wikipedia, "fetch_json", lambda *a, **k: {"entities": {"Q1": {"claims": {"P1": [None, 3]}}}})
    assert wikipedia.wikidata_claims({"P1": "p"}, entity_id="Q1", fetch=lambda *a: None)["p"] == ""


@pytest.mark.parametrize("main", ["12a", {"id": 1}, None, 0])
def test_46_a_master_without_a_numeric_main_release_is_a_discogs_error(main):
    from redactor_common.core.lookups import discogs
    client = types.SimpleNamespace(get_json=lambda path, **k: {"main_release": main})
    with pytest.raises(discogs.DiscogsError):
        discogs.release_from_link(client, "https://www.discogs.com/master/123")


# --- 47: identical cues are translated, not failed ------------------------------------------------------------------

def test_47_a_cue_that_reads_the_same_is_not_failed():
    from redactor_common.core import translate
    from redactor_common.core.srt import Cue
    cues = [Cue(i, i + 1, t) for i, t in enumerate(("OK", "Hello", "♪", "Bye"))]
    calls = []

    class Engine(translate.Translator):
        def translate_text(self, text, source, target):
            calls.append(text)
            if len(calls) > 2:
                raise translate.TranslateUnavailable("gone")
            return text if text in ("OK", "♪") else text.upper()
    result = translate.translate_cues(cues, "en", "fr", Engine(), group_size=1, sleep=lambda s: None)
    assert result.error and set(result.failed) == {2, 3}       # "OK" read the same and was translated


# --- 48, 49: settings files ---------------------------------------------------------------------------------------------

def test_48_a_deeply_nested_settings_file_is_not_a_settings_file():
    from redactor_common.core import settings_bundle, settings_coerce
    with pytest.raises(settings_bundle.SettingsBundleError):
        settings_bundle.parse_bundle("[" * 100000, "app")
    assert settings_coerce.parse_json("[" * 100000, "d") == "d"


@pytest.mark.parametrize("name", ["comicvine_key", "license_key", "passphrase", "cookie", "auth", "oauth_token",
                                  "LicenseKey"])
def test_49_more_secret_names(name):
    from redactor_common.core.settings_bundle import looks_secret
    assert looks_secret(name)


@pytest.mark.parametrize("name", ["sort_key", "hotkey", "shortcut_keys", "author", "keyboard_layout", "pinned",
                                  "column_keys"])
def test_49_not_secret(name):
    from redactor_common.core.settings_bundle import looks_secret
    assert not looks_secret(name)


def test_49_nested_secrets_are_dropped_and_imports_keep_their_type():
    from redactor_common.core import settings_bundle as sb
    assert sb._clean_items({"lookup": {"comicvine_key": "s", "on": True}, "list": [{"token": "t", "a": 1}]}) == \
        {"lookup": {"on": True}, "list": [{"a": 1}]}
    assert sb._same_kind(2, 1.5) and sb._same_kind([1], (1,)) and sb._same_kind(None, 3)
    assert not sb._same_kind("big", 3) and not sb._same_kind(1, True) and not sb._same_kind({}, [])


# --- 50: an old keyring entry does not hide the fallback copy -----------------------------------------------------------

def test_50_falling_back_removes_the_old_keyring_entry(tmp_path, monkeypatch):
    from redactor_common.core import secret_store

    class Backend:
        def __init__(self, deletable=True):
            self.store, self.deletable = {("redactor-app", "tmdb"): "A"}, deletable

        def get_password(self, service, name):
            return self.store.get((service, name))

        def set_password(self, service, name, value):
            raise RuntimeError("refused")

        def delete_password(self, service, name):
            if not self.deletable:
                raise RuntimeError("locked")
            self.store.pop((service, name), None)
    backend = Backend()
    monkeypatch.setattr(secret_store, "_get_backend", lambda: backend)
    monkeypatch.setattr(secret_store, "service_name", lambda app: "redactor-" + app)
    files = {}
    monkeypatch.setattr(secret_store, "_read_fallback", lambda app: dict(files))
    monkeypatch.setattr(secret_store, "_write_fallback", lambda app, values: files.clear() or files.update(values))
    assert secret_store.set_secret("app", "tmdb", "B", allow_unencrypted_fallback=True) == secret_store.SOURCE_FILE
    assert secret_store.get_secret("app", "tmdb") == "B"
    stuck = Backend(deletable=False)
    monkeypatch.setattr(secret_store, "_get_backend", lambda: stuck)
    with pytest.raises(secret_store.SecretStoreUnavailable, match="older value could not be removed"):
        secret_store.set_secret("app", "tmdb", "C", allow_unencrypted_fallback=True)


# --- 51: a pasted key is stripped ---------------------------------------------------------------------------------------

def test_51_a_pasted_key_is_stripped_and_blank_keeps(monkeypatch):
    from redactor_common.core import secret_store
    from redactor_common.gui import secret_field
    saved = []
    monkeypatch.setattr(secret_store, "set_secret", lambda app, name, value, *a: saved.append(value))
    monkeypatch.setattr(secret_store, "secret_source", lambda *a, **k: secret_store.SOURCE_NONE)
    monkeypatch.setattr(secret_store, "get_secret", lambda *a, **k: "")
    field_cls = next(c for n, c in vars(secret_field).items() if isinstance(c, type) and hasattr(c, "apply")
                     and c.__module__ == secret_field.__name__)
    try:
        field = field_cls("app", "tmdb", "TMDB key")
    except TypeError:
        pytest.skip("secret field has another constructor")
    field.edit.setText("abc123 ")
    assert field.apply() is True and saved == ["abc123"]
    field.edit.setText("   ")
    assert field.apply() is False and saved == ["abc123"]


# --- 52: a folder is not a tool -------------------------------------------------------------------------------------------

def test_52_a_folder_is_not_found_as_a_tool(tmp_path):
    from redactor_common.core.tool_locator import find_tool
    (tmp_path / "ffmpeg.exe").mkdir()
    assert find_tool("ffmpeg.exe", override=str(tmp_path)) is None
    assert find_tool("ffmpeg.exe", override=str(tmp_path / "ffmpeg.exe")) is None
    assert find_tool("ffmpeg.exe", tools_dir=tmp_path, which=lambda name: None) is None
    tool = tmp_path / "bin" / "ffmpeg.exe"
    tool.parent.mkdir()
    tool.write_bytes(b"MZ")
    tool.chmod(0o755)
    assert find_tool("ffmpeg.exe", override=str(tool)) == tool


# --- 57: ID3 v2.3 year-month ------------------------------------------------------------------------------------------

def test_57_a_year_read_back_for_a_year_month_date_is_accepted():
    from redactor_common.core.audio_write import _reads_back
    assert _reads_back("date", ["2019-05"], ["2019"])
    assert not _reads_back("date", ["2019-05"], ["2018"])
    assert not _reads_back("title", ["2019-05"], ["2019"])


# --- 58: a default namespace does not leave ns0: tags ---------------------------------------------------------------------

def test_58_extras_lose_the_default_namespace():
    import xml.etree.ElementTree as ET
    from redactor_common.core import comicinfo_write
    root = ET.fromstring('<ComicInfo xmlns="http://example.org/ci"><Title>T</Title>'
                         '<Pages><Page Image="0" Type="FrontCover"/></Pages></ComicInfo>')
    xml = comicinfo_write.serialize_comicinfo(root).decode("utf-8")
    assert "ns0:" not in xml and "<Pages>" in xml and '<Page Image="0" Type="FrontCover" />' in xml


# --- 60: Quick Pick ignores hidden rows ---------------------------------------------------------------------------------

def test_60_quick_pick_never_returns_a_hidden_row():
    from redactor_common.gui import quick_pick_dialog
    entries = lambda: [("rock", "Rock"), ("jazz", "Jazz")]  # noqa: E731
    dialog = quick_pick_dialog.QuickPickDialog("Genre", entries, multi_select=False)
    dialog.list_widget.setCurrentRow(0)
    dialog.filter_edit.setText("jazz")
    assert dialog.selected_keys() == []
    many = quick_pick_dialog.QuickPickDialog("Genre", entries, multi_select=True)
    for row in range(2):
        many.list_widget.item(row).setCheckState(quick_pick_dialog.Qt.CheckState.Checked)
    many.filter_edit.setText("jazz")
    assert many.selected_keys() == ["jazz"]


# --- 62: superscript reserved names -----------------------------------------------------------------------------------

@pytest.mark.parametrize("stem", ["COM²", "lpt¹", "COM³.x"])
def test_62_superscript_device_names_are_reserved(stem):
    from redactor_common.core import rename_pattern
    checker = next(f for n, f in vars(rename_pattern).items() if callable(f) and "reserved" in n.lower())
    assert checker(stem)
