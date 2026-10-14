"""gui/convert_offer: the Open Folder "these files can be converted, convert them?" question shared by the four
apps (offscreen Qt; the modal box is answered by a patched exec)."""
import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

import pytest  # noqa: E402
from PyQt6.QtWidgets import QApplication, QCheckBox, QMessageBox  # noqa: E402

from redactor_common.gui import convert_offer  # noqa: E402
from redactor_common.gui.convert_offer import (  # noqa: E402
    DONT_ASK_TEXT, NEVER, NO, YES, Remember, details_text, folder_key, offer_conversion, offer_text,
    remembered_folders,
)

_app = QApplication.instance() or QApplication([])

FOUND = [os.path.join("music", "a.flac"), os.path.join("music", "b.wav"), os.path.join("music", "c.flac")]


class Answer:
    """Replaces QMessageBox.exec: records the box, ticks the named checkboxes, clicks the button with
    `button` as its text (None: closes it like Escape does)."""

    def __init__(self, monkeypatch, button, tick=()):
        self.button, self.tick, self.boxes = button, set(tick), []
        monkeypatch.setattr(QMessageBox, "exec", lambda box: self._run(box))

    def _run(self, box):
        self.boxes.append(box)
        # As a set: Qt orders a box's buttons by role and platform, and adds its own "Show Details..."
        self.texts = {b.text() for b in box.buttons()} - {"Show Details..."}
        self.checkboxes = {c.text(): c for c in box.findChildren(QCheckBox)}
        self.default = box.defaultButton().text() if box.defaultButton() else None
        for label in self.tick:
            self.checkboxes[label].setChecked(True)
        if self.button is None:
            box.escapeButton().click()
        else:
            next(b for b in box.buttons() if b.text() == self.button).click()
        return 0

    @property
    def box(self):
        return self.boxes[-1]


def memory():
    store = []
    return store, remembered_folders(lambda: list(store), lambda keys: (store.clear(), store.extend(keys)))


# --- wording -------------------------------------------------------------------------------------------------


def test_text_when_nothing_loaded_and_when_some_did():
    none = offer_text(FOUND, 0, "MP3", "MP3", noun="audio file")
    assert none.startswith("No MP3 files found, but 3 audio file(s) in other formats (FLAC, WAV) that can be converted to MP3.")
    assert none.endswith("Convert them to MP3?")
    some = offer_text(FOUND, 5, "MP4 or MKV", "MP4/MKV", noun="video file")
    assert some.startswith("Also found 3 video file(s) in other formats (FLAC, WAV) that can be converted to MP4/MKV.")


def test_text_with_a_converter_and_own_question():
    text = offer_text(["x.mobi"], 0, "EPUB", "EPUB", converter="Calibre", question="Open Import and Convert?")
    assert "1 file(s) in other formats (MOBI) that Calibre can convert to EPUB." in text
    assert text.endswith("Open Import and Convert?")


def test_details_list_is_capped():
    paths = [f"f{i}.flac" for i in range(205)]
    text = details_text(paths)
    assert text.count("\n") == 200 and text.endswith("... and 5 more")
    assert details_text(paths[:3]) == "f0.flac\nf1.flac\nf2.flac"


# --- the box -------------------------------------------------------------------------------------------------


def test_nothing_found_asks_nothing(monkeypatch):
    answer = Answer(monkeypatch, "Convert")
    assert offer_conversion(None, [], 0, "MP3", "MP3") == NO
    assert answer.boxes == []


def test_yes_no_and_escape(monkeypatch):
    answer = Answer(monkeypatch, "Convert")
    assert offer_conversion(None, FOUND, 0, "MP3", "MP3") == YES
    assert answer.texts == {"Convert", "Not Now"} and answer.default == "Convert"
    assert answer.box.detailedText() == details_text(FOUND)
    assert answer.box.windowTitle() == "Convert to MP3?"
    assert "No MP3 files found" in answer.box.text()
    assert answer.checkboxes == {}  # no remember store: no "Don't ask again"
    Answer(monkeypatch, "Not Now")
    assert offer_conversion(None, FOUND, 2, "MP3", "MP3") == NO
    Answer(monkeypatch, None)
    assert offer_conversion(None, FOUND, 2, "MP3", "MP3") == NO


def test_dont_ask_again_with_no_returns_never_and_is_remembered(monkeypatch, tmp_path):
    store, remember = memory()
    answer = Answer(monkeypatch, "Not Now", tick=[DONT_ASK_TEXT])
    assert offer_conversion(None, FOUND, 0, "MP3", "MP3", remember_key=tmp_path, remember=remember) == NEVER
    assert DONT_ASK_TEXT in answer.checkboxes
    assert store == [folder_key(tmp_path)]
    # The next open of that folder asks nothing.
    again = Answer(monkeypatch, "Convert")
    assert offer_conversion(None, FOUND, 0, "MP3", "MP3", remember_key=str(tmp_path), remember=remember) == NEVER
    assert again.boxes == []
    # Another folder still asks.
    other = tmp_path / "other"
    assert offer_conversion(None, FOUND, 0, "MP3", "MP3", remember_key=other, remember=remember) == YES


def test_dont_ask_again_with_convert_converts_and_remembers(monkeypatch, tmp_path):
    store, remember = memory()
    Answer(monkeypatch, "Convert", tick=[DONT_ASK_TEXT])
    assert offer_conversion(None, FOUND, 3, "MP3", "MP3", remember_key=tmp_path, remember=remember) == YES
    assert store == [folder_key(tmp_path)]


def test_unticked_answers_remember_nothing(monkeypatch, tmp_path):
    store, remember = memory()
    Answer(monkeypatch, "Not Now")
    assert offer_conversion(None, FOUND, 0, "MP3", "MP3", remember_key=tmp_path, remember=remember) == NO
    assert store == []


def test_remember_as_a_plain_pair_of_callables(monkeypatch, tmp_path):
    declined = set()
    Answer(monkeypatch, "Not Now", tick=[DONT_ASK_TEXT])
    pair = (lambda key: key in declined, declined.add)
    assert offer_conversion(None, FOUND, 0, "EPUB", "EPUB", remember_key=tmp_path, remember=pair) == NEVER
    assert declined == {folder_key(tmp_path)}


def test_no_key_means_no_dont_ask_box(monkeypatch):
    _store, remember = memory()
    answer = Answer(monkeypatch, "Convert")
    offer_conversion(None, FOUND, 0, "MP3", "MP3", remember=remember)
    assert answer.checkboxes == {}


def test_three_buttons_own_labels_default_and_options(monkeypatch, tmp_path):
    # cbzredactor's shape: Convert Now / Add Unconverted / Skip, its own checkbox, Add Unconverted the default.
    recycle = QCheckBox("Move the originals to the Recycle Bin after a successful conversion")
    answer = Answer(monkeypatch, "Add Unconverted", tick=[recycle.text()])
    result = offer_conversion(
        None, ["x.cbr"], 0, "CBZ", "CBZ", title="Convert to CBZ?", yes_text="Convert Now", no_text="Skip",
        extra_buttons=[("Add Unconverted", "unconverted")], default="unconverted", options=[recycle],
    )
    assert result == "unconverted"
    assert answer.texts == {"Convert Now", "Add Unconverted", "Skip"}
    assert answer.default == "Add Unconverted"
    assert recycle.isChecked()  # the caller reads its own widgets afterwards


def test_extra_button_with_dont_ask_ticked_is_not_remembered(monkeypatch, tmp_path):
    store, remember = memory()
    Answer(monkeypatch, "Later", tick=[DONT_ASK_TEXT])
    result = offer_conversion(None, FOUND, 0, "MP3", "MP3", remember_key=tmp_path, remember=remember,
                              extra_buttons=[("Later", "later")])
    assert result == "later" and store == []


# --- the remember store --------------------------------------------------------------------------------------


def test_remembered_folders_normalises_dedupes_and_caps(tmp_path):
    store = []
    remember = remembered_folders(lambda: store, lambda keys: (store.clear(), store.extend(keys)), limit=3)
    for name in ["a", "b", "a", "c", "d"]:
        remember.decline(str(tmp_path / name))
    # a, b, a (moves to the end), c, d -> b falls off the front
    assert store == [folder_key(tmp_path / n) for n in ["a", "c", "d"]]
    assert not remember.is_declined(str(tmp_path / "b"))  # the oldest fell off
    assert remember.is_declined(str(tmp_path / "d" / ".." / "d"))


def test_remembered_folders_tolerates_an_empty_store():
    remember = remembered_folders(lambda: None, lambda keys: None)
    assert remember.is_declined("anything") is False
    assert isinstance(remember, Remember)


@pytest.mark.skipif(os.path.normcase("A") == "A", reason="case-insensitive paths only")
def test_folder_key_is_case_insensitive_where_the_os_is(tmp_path):
    assert folder_key(str(tmp_path).upper()) == folder_key(str(tmp_path).lower())


# --- find_convertible --------------------------------------------------------------------------------------


def test_find_convertible_runs_the_walk_under_run_busy(monkeypatch, tmp_path):
    (tmp_path / "a.flac").write_bytes(b"x")
    (tmp_path / ".scratch.flac").write_bytes(b"x")
    (tmp_path / "b.mp3").write_bytes(b"x")
    labels = []

    def fake_busy(parent, label, fn, *args, **kwargs):
        labels.append(label)
        return fn(*args, **kwargs)

    monkeypatch.setattr(convert_offer, "run_busy", fake_busy)
    found = convert_offer.find_convertible(None, [tmp_path], [".flac"], skip=lambda n: n.startswith(".scratch"))
    assert [os.path.basename(p) for p in found] == ["a.flac"]
    assert labels and "convert" in labels[0]


def test_full_text_override_replaces_the_message_but_keeps_the_details(monkeypatch):
    answer = Answer(monkeypatch, "Convert")
    offer_conversion(None, FOUND, 0, "CBZ", "CBZ", text="3 file(s) are not real CBZ files:\n  a\n  b\n\nConvert them?")
    assert answer.box.text().startswith("3 file(s) are not real CBZ files:")
    assert FOUND[0] in answer.box.detailedText()


def test_several_folders_are_remembered_together_and_skipped_only_when_all_were_declined(monkeypatch, tmp_path):
    store, remember = memory()
    folders = [str(tmp_path / "a"), str(tmp_path / "b")]
    answer = Answer(monkeypatch, "Not Now", tick=["Don't ask again for these folders"])
    assert offer_conversion(None, FOUND, 0, "MP3", "MP3", remember_key=folders, remember=remember) == NEVER
    assert len(store) == 2  # every folder recorded
    # both declined: nothing is shown any more
    quiet = Answer(monkeypatch, "Convert")
    assert offer_conversion(None, FOUND, 0, "MP3", "MP3", remember_key=folders, remember=remember) == NEVER
    assert quiet.boxes == []
    # one new folder among them: the offer comes back (the declined one is not enough)
    again = Answer(monkeypatch, "Convert")
    offer_conversion(None, FOUND, 0, "MP3", "MP3", remember_key=[folders[0], str(tmp_path / "c")], remember=remember)
    assert again.boxes and "Don't ask again for these folders" in again.checkboxes
