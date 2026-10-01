"""Tests for the shared Preferences framework: core/preferences (specs,
coerce, standard sections, backends) and gui/preferences_dialog."""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest  # noqa: E402
from PyQt6.QtGui import QAction  # noqa: E402
from PyQt6.QtWidgets import (  # noqa: E402
    QApplication,
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QLabel,
    QLineEdit,
    QPushButton,
    QSpinBox,
    QWidget,
)

from redactor_common.core import labels, languages, preferences as prefs  # noqa: E402
from redactor_common.core.preferences import (  # noqa: E402
    KEY_ASCII_FILENAMES,
    KEY_AUTO_NUMBER_PADDING,
    KEY_BLANK_LANGUAGE_ENABLED,
    KEY_DEFAULT_LANGUAGE,
    KEY_ZERO_PAD_NUMBERS,
    KEY_ZERO_PAD_WIDTH,
    CallbackBackend,
    MemoryBackend,
    PrefSection,
    PrefSpec,
    PreferencesBackend,
    coerce,
    filenames_section,
    language_section,
)
from redactor_common.gui import standard_shortcuts as keys  # noqa: E402
from redactor_common.gui.preferences_dialog import PreferencesDialog, preferences_menu_action  # noqa: E402


@pytest.fixture(scope="module", autouse=True)
def _app():
    app = QApplication.instance() or QApplication([])
    yield app


ALL_KINDS = PrefSection("all", "Everything", (
    PrefSpec("b", "Flag", "bool", False),
    PrefSpec("i", "Count", "int", 3, minimum=1, maximum=9, depends_on="b"),
    PrefSpec("f", "Ratio", "float", 0.5, minimum=0.0, maximum=1.0),
    PrefSpec("c", "Mode", "choice", "wrap", choices=("wrap", ("clip", "Clip it"))),
    PrefSpec("s", "Name", "str", "x"),
    PrefSpec("p", "Folder", "path", "", path_mode="folder"),
    PrefSpec("l", "Language", "language", "en", choices=(("en", "English"), ("de", "German"))),
))


def make(sections=(ALL_KINDS,), initial=None, **kw):
    backend = MemoryBackend(initial)
    return PreferencesDialog(list(sections), backend, **kw), backend


# --- core ---------------------------------------------------------------


def test_coerce_bool_tolerant():
    spec = PrefSpec("b", "B", "bool", True)
    assert coerce(spec, "false") is False
    assert coerce(spec, "0") is False
    assert coerce(spec, "1") is True
    assert coerce(spec, 0) is False
    assert coerce(spec, "banana") is True  # default
    assert coerce(spec, None) is True
    assert coerce(spec, 5) is True


def test_coerce_int_range_and_types():
    spec = PrefSpec("i", "I", "int", 4, minimum=1, maximum=9)
    assert coerce(spec, "7") == 7
    assert coerce(spec, 7.0) == 7
    assert coerce(spec, 7.5) == 4
    assert coerce(spec, "x") == 4
    assert coerce(spec, 0) == 4
    assert coerce(spec, 10) == 4
    assert coerce(spec, True) == 4  # a bool is not a count
    assert coerce(spec, float("nan")) == 4


def test_coerce_float_choice_str_path_language():
    f = PrefSpec("f", "F", "float", 0.5, minimum=0, maximum=1)
    assert coerce(f, "0.25") == 0.25
    assert coerce(f, 2) == 0.5
    c = PrefSpec("c", "C", "choice", "a", choices=("a", ("b", "Bee")))
    assert coerce(c, "b") == "b"
    assert coerce(c, "z") == "a"
    assert coerce(c, 3) == "a"
    s = PrefSpec("s", "S", "str", "d")
    assert coerce(s, "hi there") == "hi there"
    assert coerce(s, 5) == "d"
    assert coerce(s, "a\nb") == "d"
    p = PrefSpec("p", "P", "path", "")
    assert coerce(p, "  C:/x  ") == "C:/x"
    lang = PrefSpec("l", "L", "language", "en")
    assert coerce(lang, " fra ") == "fra"
    assert coerce(lang, "  ") == "en"


def test_validate_sections_catches_mistakes():
    with pytest.raises(ValueError, match="duplicate"):
        prefs.validate_sections([PrefSection("a", "A", (PrefSpec("x", "X"), PrefSpec("x", "X2")))])
    with pytest.raises(ValueError, match="depends_on"):
        prefs.validate_sections([PrefSection("a", "A", (PrefSpec("x", "X", depends_on="nope"),))])
    with pytest.raises(ValueError, match="depends_on"):
        prefs.validate_sections([PrefSection("a", "A", (
            PrefSpec("n", "N", "int", 1), PrefSpec("x", "X", depends_on="n")))])
    with pytest.raises(ValueError, match="kind"):
        prefs.validate_sections([PrefSection("a", "A", (PrefSpec("x", "X", "blob"),))])
    with pytest.raises(ValueError, match="choices"):
        prefs.validate_sections([PrefSection("a", "A", (PrefSpec("x", "X", "choice", ""),))])
    with pytest.raises(ValueError, match="minimum"):
        prefs.validate_sections([PrefSection("a", "A", (PrefSpec("x", "X", "int", 1, minimum=5, maximum=2),))])
    prefs.validate_sections([ALL_KINDS])


def test_defaults_helper():
    assert prefs.defaults([ALL_KINDS])["i"] == 3


def test_filenames_section_keys_and_subsets():
    full = filenames_section()
    assert [s.key for s in full.specs] == [
        KEY_ASCII_FILENAMES, KEY_ZERO_PAD_NUMBERS, KEY_ZERO_PAD_WIDTH, KEY_AUTO_NUMBER_PADDING]
    by_key = {s.key: s for s in full.specs}
    assert by_key[KEY_ZERO_PAD_WIDTH].depends_on == KEY_ZERO_PAD_NUMBERS
    assert by_key[KEY_ZERO_PAD_WIDTH].default == 2
    assert all(s.help for s in full.specs)
    assert [s.key for s in filenames_section(auto_number=False).specs] == [
        KEY_ASCII_FILENAMES, KEY_ZERO_PAD_NUMBERS, KEY_ZERO_PAD_WIDTH]
    assert [s.key for s in filenames_section(zero_pad=False, ascii_filenames=False).specs] == [
        KEY_AUTO_NUMBER_PADDING]
    prefs.validate_sections([full])


def test_filenames_section_overrides():
    section = filenames_section(overrides={KEY_ZERO_PAD_WIDTH: {"default": 3}}, width_max=10)
    spec = next(s for s in section.specs if s.key == KEY_ZERO_PAD_WIDTH)
    assert spec.default == 3 and spec.maximum == 10
    with pytest.raises(ValueError):
        filenames_section(auto_number=False, overrides={KEY_AUTO_NUMBER_PADDING: {"default": 3}})


def test_language_section_styles():
    a2 = language_section()
    spec = a2.specs[0]
    assert spec.key == KEY_DEFAULT_LANGUAGE and spec.default == "en" and spec.kind == "language"
    assert ("de", "German") in spec.choice_pairs()
    a3 = language_section(style="alpha3")
    assert a3.specs[0].default == "eng"
    assert ("deu", "German") in a3.specs[0].choice_pairs()
    assert ("ger", "German") in language_section(style="alpha3b").specs[0].choice_pairs()
    narrowed = language_section(codes=["nb", "en"])
    assert [v for v, _ in narrowed.specs[0].choice_pairs()] == ["nb", "en"]
    with_switch = language_section(include_enabled=True)
    assert [s.key for s in with_switch.specs] == [KEY_BLANK_LANGUAGE_ENABLED, KEY_DEFAULT_LANGUAGE]
    assert with_switch.specs[1].depends_on == KEY_BLANK_LANGUAGE_ENABLED
    assert with_switch.specs[0].default is True
    prefs.validate_sections([with_switch])
    assert languages.lookup("en") is not None


def test_backends():
    mem = MemoryBackend({"a": 1})
    assert isinstance(mem, PreferencesBackend)
    mem.set_many({"b": 2})
    assert mem.get("a") == 1 and mem.get("b") == 2 and mem.get("zz") is None
    store = {}
    cb = CallbackBackend(store.get, store.update)
    assert isinstance(cb, PreferencesBackend)
    cb.set_many({"k": 1})
    assert cb.get("k") == 1


# --- dialog: widget mapping ------------------------------------------------------


def test_every_kind_gets_the_right_widget():
    dlg, _ = make()
    assert isinstance(dlg.control("b"), QCheckBox)
    assert isinstance(dlg.control("i"), QSpinBox)
    assert isinstance(dlg.control("f"), QDoubleSpinBox)
    combo = dlg.control("c")
    assert isinstance(combo, QComboBox) and not combo.isEditable()
    assert [combo.itemText(i) for i in range(combo.count())] == ["wrap", "Clip it"]
    assert isinstance(dlg.control("s"), QLineEdit)
    path = dlg.control("p")
    assert path.findChild(QLineEdit) is not None
    assert path.findChild(QPushButton).text().startswith("Browse")
    lang = dlg.control("l")
    assert isinstance(lang, QComboBox) and lang.isEditable()
    assert lang.itemText(0) == "English (en)"


def test_defaults_and_ranges_shown():
    dlg, _ = make()
    assert dlg.value("b") is False and dlg.value("i") == 3 and dlg.value("f") == 0.5
    assert dlg.value("c") == "wrap" and dlg.value("s") == "x" and dlg.value("l") == "en"
    spin = dlg.control("i")
    assert (spin.minimum(), spin.maximum()) == (1, 9)
    dspin = dlg.control("f")
    assert (dspin.minimum(), dspin.maximum()) == (0.0, 1.0)


def test_stored_values_loaded_and_bad_ones_fall_back():
    dlg, _ = make(initial={"b": "true", "i": "7", "f": "oops", "c": "clip", "s": "hello", "l": "de"})
    assert dlg.value("b") is True and dlg.value("i") == 7
    assert dlg.value("f") == 0.5  # bad stored value -> default
    assert dlg.value("c") == "clip" and dlg.value("s") == "hello" and dlg.value("l") == "de"
    assert dlg.changed_values() == {}  # a fallback is not an edit


def test_out_of_range_stored_int_falls_back():
    dlg, _ = make(initial={"i": 99})
    assert dlg.value("i") == 3


def test_language_free_text_kept_as_typed():
    dlg, backend = make()
    dlg.control("l").setEditText("  nb-custom ")
    assert dlg.value("l") == "nb-custom"
    dlg.accept()
    assert backend.values["l"] == "nb-custom"


def test_language_pick_returns_code():
    dlg, backend = make()
    combo = dlg.control("l")
    combo.setCurrentIndex(combo.findData("de"))
    assert dlg.value("l") == "de"


def test_path_row_edit_is_a_change():
    dlg, backend = make()
    dlg.control("p").findChild(QLineEdit).setText("/data")
    assert dlg.changed_values() == {"p": "/data"}


def test_help_text_and_restart_note_shown():
    section = PrefSection("s", "S", (
        PrefSpec("a", "A", "bool", False, help="Plain words here.", restart_note="Restart needed."),
    ))
    dlg, _ = make([section])
    texts = [w.text() for w in dlg.findChildren(QLabel)]
    assert "Plain words here. Restart needed." in texts


# --- depends_on ---------------------------------------------------------------------


def test_depends_on_greys_and_ungreys():
    dlg, _ = make()
    assert dlg.is_row_enabled("i") is False
    dlg.control("b").setChecked(True)
    assert dlg.is_row_enabled("i") is True
    dlg.control("b").setChecked(False)
    assert dlg.is_row_enabled("i") is False


def test_depends_on_chain_and_forward_reference():
    section = PrefSection("s", "S", (
        PrefSpec("c3", "C3", "bool", False, depends_on="c2"),
        PrefSpec("c2", "C2", "bool", True, depends_on="c1"),
        PrefSpec("c1", "C1", "bool", False),
    ))
    dlg, _ = make([section])
    assert not dlg.is_row_enabled("c2") and not dlg.is_row_enabled("c3")
    dlg.control("c1").setChecked(True)
    assert dlg.is_row_enabled("c2") and dlg.is_row_enabled("c3")
    dlg.control("c1").setChecked(False)
    assert not dlg.is_row_enabled("c3")


def test_standard_zero_pad_width_follows_checkbox():
    dlg, _ = make([filenames_section()])
    assert dlg.is_row_enabled(KEY_ZERO_PAD_WIDTH) is False
    dlg.control(KEY_ZERO_PAD_NUMBERS).setChecked(True)
    assert dlg.is_row_enabled(KEY_ZERO_PAD_WIDTH) is True


# --- writing -------------------------------------------------------------------------


def test_only_changed_keys_are_written_once():
    dlg, backend = make(initial={"s": "keep"})
    dlg.control("s").setText("new")
    dlg.control("b").setChecked(True)
    dlg.accept()
    assert backend.writes == [{"s": "new", "b": True}]
    assert dlg.result_values() == {"s": "new", "b": True}


def test_ok_without_changes_writes_nothing():
    dlg, backend = make()
    dlg.accept()
    assert backend.writes == []
    assert dlg.result_values() == {}


def test_cancel_writes_nothing():
    dlg, backend = make()
    dlg.control("s").setText("changed")
    dlg.reject()
    assert backend.writes == []


def test_apply_writes_then_rebases():
    dlg, backend = make()
    assert dlg.apply_button.isEnabled() is False
    dlg.control("s").setText("one")
    assert dlg.apply_button.isEnabled() is True
    dlg.apply_button.click()
    assert backend.writes == [{"s": "one"}]
    assert dlg.apply_button.isEnabled() is False
    dlg.accept()  # nothing new
    assert backend.writes == [{"s": "one"}]
    assert dlg.result_values() == {"s": "one"}


def test_applied_signal_carries_written_keys():
    dlg, _ = make()
    seen = []
    dlg.applied.connect(seen.append)
    dlg.control("i").setValue(5)
    dlg.apply()
    assert seen == [{"i": 5}]


def test_values_written_are_typed():
    dlg, backend = make()
    dlg.control("i").setValue(8)
    dlg.control("f").setValue(0.75)
    dlg.control("c").setCurrentIndex(1)
    dlg.accept()
    assert backend.values["i"] == 8 and isinstance(backend.values["i"], int)
    assert backend.values["f"] == 0.75
    assert backend.values["c"] == "clip"


# --- reset ------------------------------------------------------------------------------


def test_reset_page_restores_defaults_without_saving():
    other = PrefSection("o", "Other", (PrefSpec("z", "Z", "int", 1, minimum=0, maximum=9),))
    dlg, backend = make([ALL_KINDS, other], initial={"s": "stored", "i": 8, "z": 6, "b": True})
    dlg.set_current_page(0)
    dlg.reset_button.click()
    assert dlg.value("s") == "x" and dlg.value("i") == 3 and dlg.value("b") is False
    assert dlg.value("z") == 6  # other page untouched
    assert backend.writes == []  # not saved yet
    dlg.accept()
    assert backend.writes and backend.writes[0]["s"] == "x" and "z" not in backend.writes[0]


def test_reset_disabled_on_extra_page():
    dlg, _ = make(extra_pages=[("Tools", lambda: QWidget())])
    dlg.set_current_page(0)
    assert dlg.reset_button.isEnabled()
    dlg.set_current_page(1)
    assert not dlg.reset_button.isEnabled()


# --- pages -----------------------------------------------------------------------------


def test_extra_pages_come_after_sections_and_are_not_written():
    marker = QWidget()
    marker.setObjectName("mine")
    dlg, backend = make([filenames_section(), language_section()], extra_pages=[("Tools", lambda: marker)])
    assert dlg.page_titles() == ["Filenames", "Language", "Tools"]
    assert dlg.findChild(QWidget, "mine") is marker
    dlg.accept()
    assert backend.writes == []


def test_many_pages_switch_to_list_navigation():
    sections = [PrefSection(f"s{i}", f"Page {i}", (PrefSpec(f"k{i}", f"K{i}", "bool", False),)) for i in range(8)]
    dlg, backend = make(sections)
    assert dlg.page_titles() == [f"Page {i}" for i in range(8)]
    dlg.set_current_page(3)
    assert dlg.current_page() == 3
    dlg.control("k5").setChecked(True)
    dlg.accept()
    assert backend.writes == [{"k5": True}]


def test_tab_mnemonics_unique_and_reset_letter_reserved():
    sections = [PrefSection(f"s{i}", t, ()) for i, t in enumerate(["Filenames", "Files", "Folders"])]
    dlg, _ = make(sections)
    tabs = dlg._tabs
    letters = [tabs.tabText(i).split("&")[1][0].lower() for i in range(tabs.count()) if "&" in tabs.tabText(i)]
    assert len(letters) == len(set(letters)) and "r" not in letters


def test_control_mnemonics_unique_across_pages():
    dlg, _ = make([filenames_section(), language_section(include_enabled=True)])
    letters = []
    for label in dlg.findChildren(QLabel):
        if label.buddy() is not None and "&" in label.text():
            letters.append(label.text().split("&")[1][0].lower())
    for box in dlg.findChildren(QCheckBox):
        if "&" in box.text():
            letters.append(box.text().split("&")[1][0].lower())
    assert letters and len(letters) == len(set(letters))
    assert "r" not in letters


def test_accessible_names_set():
    dlg, _ = make([filenames_section()])
    assert dlg.control(KEY_ASCII_FILENAMES).accessibleName() == "ASCII-safe filenames"
    assert dlg.control(KEY_ASCII_FILENAMES).accessibleDescription()


def test_invalid_sections_rejected_by_dialog():
    with pytest.raises(ValueError):
        PreferencesDialog([PrefSection("a", "A", (PrefSpec("x", "X", depends_on="missing"),))], MemoryBackend())


# --- menu action -------------------------------------------------------------------------------


def test_preferences_menu_action():
    called = []
    action = preferences_menu_action(lambda: called.append(1))
    assert action.key == "preferences"
    assert action.text == labels.PREFERENCES
    assert action.shortcut == keys.PREFERENCES == "Ctrl+,"
    assert action.role == QAction.MenuRole.PreferencesRole
    action.slot()
    assert called == [1]
