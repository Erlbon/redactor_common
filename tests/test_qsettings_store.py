"""gui/qsettings_store.py against a real ini file (QSettings IniFormat, no
platform plugin needed). Covers what epub's and cbz's gui/app_settings.py
did, and that a hand-edited value never raises."""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest  # noqa: E402

from redactor_common.gui import qsettings_store as qs  # noqa: E402
from redactor_common.gui.qsettings_store import (  # noqa: E402
    ColumnSettingsStore,
    ManagedListStore,
    PatternHistoryStore,
    QSettingsStore,
)


def _store_with(tmp_path, text: str | None = None) -> QSettingsStore:
    ini = tmp_path / "app_settings.ini"
    if text is not None:
        ini.write_text("[General]\n" + text, encoding="utf-8")
    return QSettingsStore(str(ini))


@pytest.fixture
def store(tmp_path):
    return _store_with(tmp_path)


def test_path_may_be_a_callable_evaluated_on_each_use(tmp_path):
    target = {"path": str(tmp_path / "a.ini")}
    store = QSettingsStore(lambda: target["path"])
    store.set("k", "a")
    target["path"] = str(tmp_path / "b.ini")
    assert store.text("k") == ""
    store.set("k", "b")
    store.sync()
    assert "k=a" in (tmp_path / "a.ini").read_text() and "k=b" in (tmp_path / "b.ini").read_text()


def test_ini_format_at_the_explicit_path(store, tmp_path):
    store.write_many({"files/last_directory": "x", "include_subfolders": False})
    text = (tmp_path / "app_settings.ini").read_text(encoding="utf-8")
    assert "[files]" in text and "last_directory=x" in text and "include_subfolders=false" in text


def test_round_trips(store):
    store.set("t", "hello, world")
    store.set_bool("b", True)
    store.set_int("i", 42)
    store.set("f", 2.5)
    store.set_json("j", {"a": [1, 2]})
    store.set_str_list("l", ["a", "b,c"])
    store.set_string_set("s", {"z", "a"})
    store.set_int_set("n", {3, 1})
    store.set_width_map("w", {"title": 120, 3: 50})
    assert store.text("t") == "hello, world"
    assert store.bool_value("b") is True and store.int_value("i", 0) == 42
    assert store.float_value("f", 0.0) == 2.5
    assert store.json("j") == {"a": [1, 2]}
    assert store.str_list("l") == ["a", "b,c"]
    assert store.string_set("s") == {"a", "z"} and store.raw("s") == '["a", "z"]'  # stored sorted
    assert store.int_set("n") == {1, 3}
    assert store.width_map("w") == {"title": 120, "3": 50}
    assert store.contains("t")
    store.remove("t", sync=True)
    assert not store.contains("t") and store.text("t", "gone") == "gone"


def test_missing_values_give_defaults(store):
    assert store.text("x") == "" and store.text("x", "d") == "d"
    assert store.bool_value("x") is False and store.bool_value("x", True) is True
    assert store.int_value("x", 2) == 2 and store.float_value("x", 1.5) == 1.5
    assert store.json("x", "d") == "d" and store.json_list("x") == [] and store.str_list("x") == []
    assert store.string_set("x") == set() and store.int_set("x") == set() and store.width_map("x") == {}
    assert store.raw("x") is None and store.raw("x", 5) == 5
    assert store.choice("x", ("a", "b"), "a") == "a"


HAND_EDITED = (
    "w=abc\n"
    "f=2.5\n"
    "b=garbage\n"
    "l=[1, 2]\n"
    "j={not json\n"
    "widths=\"{\\\"a\\\": \\\"wide\\\", \\\"b\\\": 80}\"\n"  # as QSettings quotes it
    "typed={\"a\": 1}\n"  # typed by hand, unquoted: QSettings eats the quotes

    "c=bogus\n"
    "h=maybe\n"
)


def test_hand_edited_garbage_never_raises(tmp_path):
    store = _store_with(tmp_path, HAND_EDITED)
    assert store.int_value("w", 1440) == 1440  # QSettings type=int raises TypeError here
    assert store.int_value("f", 3) == 3
    assert store.float_value("w", 1.0) == 1.0
    assert store.bool_value("b", True) is True and store.bool_value("b", False) is False
    assert store.json_list("l") == [1, 2]  # QSettings split the unquoted value at its comma; rejoined
    assert store.json("j", {}) == {} and store.str_list("j") == []
    assert store.width_map("widths") == {"b": 80}
    assert store.width_map("widths", lo=None, strict=True) == {}  # cbz's all-or-nothing
    assert store.width_map("typed") == {} and store.json("typed", "d") == "d"
    assert store.choice("c", ("wrap", "clip"), "wrap") == "wrap"
    assert store.opt_in("h") is False  # opt-in: garbled = off


def test_int_range_and_choice_setter(store):
    store.set("i", "500")
    assert store.int_value("i", 2, 1, 10) == 2 and store.int_value("i", 2) == 500
    assert store.set_choice("mode", "clip", ("wrap", "clip")) is True
    assert store.set_choice("mode", "bogus", ("wrap", "clip")) is False
    assert store.text("mode") == "clip"


def test_bool_other_is_false_matches_cbz_history(store):
    store.set("history/enabled", "yes")
    assert store.opt_in() is True
    store.set("history/enabled", "nonsense")
    assert store.opt_in() is False and store.bool_value("history/enabled", True) is True
    assert store.bool_value("history/enabled", True, other_is_false=True) is False


def test_rename_defaults_and_include_subfolders(store):
    assert store.include_subfolders() is True  # ON unless turned off
    store.set_include_subfolders(False)
    assert store.include_subfolders() is False
    assert store.ascii_filenames() is False
    store.set_ascii_filenames(True)
    assert store.ascii_filenames() is True and store.text(qs.ASCII_FILENAMES_KEY) == "true"
    assert store.rename_zero_pad() == (False, 2)
    store.set_rename_zero_pad(True, 3)
    assert store.rename_zero_pad() == (True, 3)
    store.set(qs.ZERO_PAD_WIDTH_KEY, "three")
    assert store.rename_zero_pad() == (True, 2)  # epub's type=int raised here


def test_last_directory(store, tmp_path):
    folder = tmp_path / "books"
    folder.mkdir()
    book = folder / "a.epub"
    book.write_text("x")
    assert store.last_directory() == ""
    assert store.remember_directory(str(book)) is True  # a file: its folder
    assert store.last_directory() == str(folder)
    assert store.remember_directory(str(folder)) is True  # a folder: itself
    assert store.text(qs.LAST_DIRECTORY_KEY) == str(folder)
    assert store.remember_directory(str(tmp_path / "gone" / "x.epub")) is False  # nothing stored
    assert store.remember_directory("") is False
    assert store.text(qs.LAST_DIRECTORY_KEY) == str(folder)
    store.set(qs.LAST_DIRECTORY_KEY, str(tmp_path / "unplugged"))
    assert store.last_directory() == ""  # gone = let Qt choose
    assert store.remember_directory(str(book), key="other/dir") and store.last_directory("other/dir") == str(folder)


def test_pure_directory_helpers(tmp_path):
    f = tmp_path / "a.mp3"
    f.write_text("x")
    assert qs.resolve_start_directory(str(tmp_path)) == str(tmp_path)
    assert qs.resolve_start_directory(str(tmp_path / "nope")) == ""
    assert qs.resolve_start_directory("") == "" and qs.resolve_start_directory(None) == ""
    assert qs.directory_for(str(f)) == str(tmp_path)  # mp3's directory_for: a file -> its folder
    assert qs.directory_for(str(tmp_path)) == str(tmp_path)
    assert qs.directory_for(str(tmp_path / "missing" / "x.mp3")) == str(tmp_path / "missing")  # no check
    assert qs.remembered_directory(str(tmp_path / "missing" / "x.mp3")) == ""


def test_existing_paths(store, tmp_path):
    exe = tmp_path / "sigil.exe"
    exe.write_text("x")
    assert store.set_existing_path("calibre", str(tmp_path)) is True
    assert store.set_existing_path("calibre", str(tmp_path / "nope")) is False
    assert store.existing_path("calibre") == str(tmp_path)
    assert store.set_existing_path("sigil", str(tmp_path), kind="file") is False
    assert store.set_existing_path("sigil", str(exe), kind="file") is True
    assert store.existing_path("sigil", kind="file") == str(exe)
    exe.unlink()
    assert store.existing_path("sigil", kind="file") == ""  # moved/reinstalled elsewhere
    store.set("db", str(tmp_path / "not-built.db"))
    assert store.existing_path("db", kind="any") == str(tmp_path / "not-built.db")
    assert store.set_existing_path("x", "") is False


GENRES = ["Action", "Horror", "Comedy"]
LANGUAGES = [("en", "English"), ("de", "German")]


def test_managed_names(store):
    genres = ManagedListStore(store, "genres/custom", "genres/hidden_defaults", GENRES)
    assert genres.merged() == GENRES
    genres.add_custom("Noir")
    genres.add_custom("noir")  # case-insensitive dupe
    genres.add_custom("  ")
    assert genres.custom() == ["Noir"]
    genres.hide("horror")
    genres.hide("Horror")
    assert genres.hidden() == ["horror"]
    assert genres.visible_defaults() == ["Action", "Comedy"]
    assert genres.merged() == ["Action", "Comedy", "Noir"]
    genres.remove_custom("NOIR")
    genres.restore_defaults()
    assert genres.merged() == GENRES
    genres.save_custom(["X", "Y"])
    genres.save_hidden(["Action"])
    assert genres.merged() == ["Horror", "Comedy", "X", "Y"]
    # the same keys and JSON the apps wrote
    assert store.raw("genres/custom") == '["X", "Y"]' and store.raw("genres/hidden_defaults") == '["Action"]'


def test_managed_pairs_and_lazy_defaults(store):
    calls = []

    def defaults():
        calls.append(1)
        return LANGUAGES

    languages = ManagedListStore(store, "languages/custom", "languages/hidden_defaults", defaults, pairs=True)
    assert not calls  # evaluated on use, not construction (epub imports its list lazily)
    languages.add_custom("nb", "Norwegian")
    languages.add_custom("nb", "Norsk bokmål")  # same code replaces
    languages.add_custom("", "Blank")  # ignored
    assert languages.custom() == [("nb", "Norsk bokmål")]
    languages.hide("de")
    languages.hide("de")
    assert languages.hidden() == ["de"]
    assert languages.merged() == [("en", "English"), ("nb", "Norsk bokmål")]
    languages.remove_custom("nb")
    assert languages.merged() == [("en", "English")] and calls
    assert store.raw("languages/hidden_defaults") == '["de"]'


def test_managed_list_survives_garbage(tmp_path):
    store = _store_with(tmp_path, "genres/custom=not json\nlanguages/custom=[[1, 2]]\n")
    assert ManagedListStore(store, "genres/custom", "genres/hidden", GENRES).merged() == GENRES
    assert ManagedListStore(store, "languages/custom", "languages/hidden", LANGUAGES, pairs=True).merged() == LANGUAGES


def test_pattern_history(store):
    history = PatternHistoryStore(store, "patterns/history", max_history=3)
    assert history.load() == [] and history.last("%title%") == "%title%"
    for pattern in ("a", "b", "c", "a", "  ", "d"):
        history.record(pattern)
    assert history.load() == ["d", "a", "c"]
    assert history.last() == "d"
    history.save(["x, y", "z", "w", "v"])  # commas survive (JSON), trimmed to max
    assert history.load() == ["x, y", "z", "w"]
    store.set("patterns/history", "p1\x1fp2")  # video's unit-separator form still reads
    assert history.load() == ["p1", "p2"]
    assert PatternHistoryStore(store, "k").max_history == 15


def test_column_settings_cbz_style(store):
    columns = ColumnSettingsStore(store, "table/column_widths", "table/hidden_columns", "table/column_order",
                                  min_width=None, strict_widths=True)
    assert columns.widths() == {} and columns.hidden() == set() and columns.order() == []
    assert columns.has_hidden_preference() is False
    columns.save_hidden(set())
    assert columns.has_hidden_preference() is True  # "show everything" is a saved choice
    columns.save_widths({"title": 120, "zero": 0})
    columns.save_order(["title", "series"])
    assert columns.widths() == {"title": 120, "zero": 0}
    assert columns.order() == ["title", "series"]
    store.set("table/column_widths", '{"title": 120, "bad": "x"}')
    assert columns.widths() == {}


def test_column_settings_epub_migration(store):
    keys = ["path", "filename", "status", "title"]
    store.set("table/column_widths", '{"0": 100, "3": 250, "9": 40, "1": 0}')
    store.set("table/hidden_columns", "[2, 3, 99]")
    columns = ColumnSettingsStore(store, "table/column_widths_by_key", "table/hidden_column_keys",
                                  legacy_widths_key="table/column_widths", legacy_hidden_key="table/hidden_columns")
    assert columns.widths(keys) == {"path": 100, "title": 250}
    assert columns.hidden(keys) == {"status", "title"}
    assert store.contains("table/column_widths_by_key") and store.contains("table/hidden_column_keys")
    store.set("table/column_widths", '{"0": 1}')  # migrated once: the old keys no longer matter
    assert columns.widths(keys) == {"path": 100, "title": 250}


def test_column_settings_epub_migration_edge_cases(store):
    columns = ColumnSettingsStore(store, "w_by_key", "hidden_keys",
                                  legacy_widths_key="w_old", legacy_hidden_key="hidden_old")
    assert columns.widths(["a"]) == {} and not store.contains("w_by_key")  # nothing to migrate: not written
    assert columns.hidden(["a"]) == set() and store.contains("hidden_keys")  # written even when empty (epub)
    assert columns.save_order(["x"]) is None and columns.order() == []  # no order key: no-op
