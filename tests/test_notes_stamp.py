"""The note stamp (generator / note-format / body-sha), note_status() and replace_note(): telling an old note of ours
from one a person edited, and refreshing only the untouched ones, through the Recycle Bin."""

import os
import re
import shutil

import pytest

from redactor_common.core import notes
from redactor_common.core.note_text import credit_footer
from redactor_common.core.notes import (
    CURRENT_NOTE_FORMAT, STATE_CURRENT, STATE_EDITED, STATE_FOREIGN, STATE_LEGACY, STATE_OUTDATED, STATUS_CHANGED,
    STATUS_ERROR, STATUS_EXISTS, STATUS_TOO_LARGE, STATUS_WRITTEN, NoteFrontMatter, find_note, note_hash, note_status,
    parse_front_matter, render_note, replace_note, write_note,
)
from redactor_common.core.trash import TrashError
from tests.test_notes import _real_pluteus, pluteus_front_matter

GENERATOR = "mp3redactor 2026-10-15#08"


def stamped(body="# Ultravox\n\nBand text.", fmt=CURRENT_NOTE_FORMAT, **kw):
    return render_note(NoteFrontMatter(title="Ultravox", generator=GENERATOR, note_format=fmt, **kw), body)


def legacy():
    """What the first releases wrote: no stamp, the closing 'Draft.' footer."""
    return render_note(NoteFrontMatter(title="Ultravox"), "# Ultravox\n\nBand text.\n\n" + credit_footer(["Facts from X"]))


class Bin:
    """The test seam of the Recycle Bin: moves the file into a folder, so a test can see it is recoverable."""

    def __init__(self, folder):
        self.folder, self.calls = folder, []
        os.makedirs(folder, exist_ok=True)

    def __call__(self, path):
        self.calls.append(path)
        shutil.move(path, os.path.join(self.folder, f"{len(self.calls)}-{os.path.basename(path)}"))

    def contents(self):
        return sorted(os.listdir(self.folder))


@pytest.fixture
def bin_(tmp_path):
    return Bin(str(tmp_path / "bin"))


@pytest.fixture
def folder(tmp_path):
    f = tmp_path / "Ultravox"
    f.mkdir()
    return f


# ---- the stamp -----------------------------------------------------------------------------------------------------

def test_a_stamped_note_has_plain_key_value_lines_and_a_hash_of_itself():
    text = stamped()
    meta, body = parse_front_matter(text)
    assert meta["generator"] == GENERATOR and meta["note-format"] == "2" and re.fullmatch(r"[0-9a-f]{16}", meta["body-sha"])
    assert meta["body-sha"] == note_hash(text) and "# Ultravox" in body
    assert text.index("status: draft") < text.index("generator:") < text.index("note-format:") < text.index("body-sha:")
    assert NoteFrontMatter.from_meta(meta).generator == GENERATOR and NoteFrontMatter.from_meta(meta).note_format == 2


def test_an_unstamped_note_is_byte_for_byte_what_it_always_was():
    assert render_note(NoteFrontMatter(title="T"), "x") == "---\ntitle: T\nstatus: draft\n---\n\nx\n"


def test_pluteus_reads_a_stamped_note_and_ignores_the_new_keys():
    text = stamped()
    for parser in (parse_front_matter, pluteus_front_matter, _real_pluteus()):
        if parser is None:
            continue
        meta, body = parser(text)
        assert meta["title"] == "Ultravox" and meta["status"] == "draft"
        assert meta["note-format"] == "2" and meta["generator"] == GENERATOR  # plain keys: read, nothing more
        assert body.startswith("# Ultravox")


def test_the_hash_ignores_only_its_own_line_and_line_endings():
    text = stamped()
    assert note_hash(text) == note_hash(text.replace("\n", "\r\n"))  # an editor saving CRLF is not an edit
    assert note_hash(text) == note_hash(re.sub(r"body-sha: .*\n", "", text))
    for changed in (text.replace("Band text.", "Band text!"), text.replace("status: draft", "status: reviewed"),
                    text.replace("title: Ultravox", "title: Ultravox 2")):
        assert note_hash(changed) != note_hash(text)


# ---- note_status ---------------------------------------------------------------------------------------------------

def put(folder, text, name="artist.md"):
    path = folder / name
    path.write_text(text, encoding="utf-8", newline="\n")
    return str(path)


def test_the_five_states(folder):
    assert note_status(put(folder, stamped())).kind == STATE_CURRENT
    assert note_status(put(folder, stamped(fmt=1))).kind == STATE_OUTDATED  # stamped with an older shape, untouched
    assert note_status(put(folder, stamped().replace("Band text.", "My own words."))).kind == STATE_EDITED
    assert note_status(put(folder, legacy())).kind == STATE_LEGACY
    assert note_status(put(folder, "---\ntitle: Mine\n---\n\nwritten by me\n")).kind == STATE_FOREIGN


def test_edge_cases_are_never_replaceable_by_mistake(folder):
    # a person marked it reviewed: front matter edits count
    assert note_status(put(folder, stamped().replace("status: draft", "status: reviewed"))).kind == STATE_EDITED
    # a stamp without a hash, or with a wrong one
    assert note_status(put(folder, re.sub(r"body-sha: .*\n", "", stamped()))).kind == STATE_EDITED
    assert note_status(put(folder, re.sub(r"body-sha: \w+", "body-sha: 0000000000000000", stamped()))).kind == STATE_EDITED
    # a draft by hand (no footer), a footer without status: draft, a note from a NEWER format
    assert note_status(put(folder, "---\ntitle: T\nstatus: draft\n---\n\ntext\n")).kind == STATE_FOREIGN
    assert note_status(put(folder, "---\ntitle: T\n---\n\ntext\n\n---\n*Draft. Facts.*\n")).kind == STATE_FOREIGN
    assert note_status(put(folder, stamped(fmt=CURRENT_NOTE_FORMAT + 3))).kind == STATE_CURRENT
    # unreadable, binary, oversized, missing
    (folder / "bad.md").write_bytes(b"\xff\xfe\x00\x01")
    assert note_status(str(folder / "bad.md")).kind == STATE_FOREIGN
    (folder / "big.md").write_text("x" * (notes.MAX_NOTE_BYTES + 5), encoding="utf-8")
    assert note_status(str(folder / "big.md")).kind == STATE_FOREIGN
    assert note_status(str(folder / "missing.md")).kind == STATE_FOREIGN


def test_current_format_is_a_parameter(folder):
    path = put(folder, stamped(fmt=3))
    assert note_status(path).kind == STATE_CURRENT
    assert note_status(path, current_format=4).kind == STATE_OUTDATED


def test_find_note_is_case_insensitive(folder):
    put(folder, stamped(), "Artist.MD")
    assert os.path.basename(find_note(folder, "artist")) == "Artist.MD" and find_note(folder, "album") is None


# ---- replace_note --------------------------------------------------------------------------------------------------

def new_text():
    return stamped("# Ultravox\n\nBetter text.")


def test_an_outdated_untouched_note_is_replaced_and_the_old_one_is_recoverable(folder, bin_):
    path = put(folder, stamped(fmt=1))
    state = note_status(path)
    result = replace_note(folder, "artist", new_text(), expect=state, trash=bin_)
    assert result.status == STATUS_WRITTEN and result.ok and result.replaced == path
    assert "Better text." in (folder / "artist.md").read_text(encoding="utf-8")
    assert note_status(str(folder / "artist.md")).kind == STATE_CURRENT
    assert len(bin_.contents()) == 1 and "Band text." in open(os.path.join(bin_.folder, bin_.contents()[0]), encoding="utf-8").read()
    assert [n for n in os.listdir(folder) if n.startswith(".")] == []  # no temp file left


def test_a_legacy_note_is_replaced_the_same_way_and_an_expected_hash_string_works(folder, bin_):
    path = put(folder, legacy(), "Artist.md")  # a different letter case
    result = replace_note(folder, "artist", new_text(), expect=note_status(path).sha, trash=bin_)
    assert result.ok and [n.lower() for n in os.listdir(folder)] == ["artist.md"]
    assert "Better text." in open(os.path.join(folder, "artist.md"), encoding="utf-8").read()


@pytest.mark.parametrize("make", [
    lambda: stamped().replace("Band text.", "My own words."),  # edited
    lambda: "---\ntitle: Mine\n---\n\nmine\n",  # foreign
    lambda: stamped(),  # current
])
def test_edited_foreign_and_current_notes_are_never_replaced(folder, bin_, make):
    path = put(folder, make())
    before = open(path, encoding="utf-8").read()
    result = replace_note(folder, "artist", new_text(), expect=note_status(path), trash=bin_)
    assert result.status == STATUS_EXISTS and not result.ok
    assert open(path, encoding="utf-8").read() == before and bin_.calls == []


def test_a_note_edited_after_it_was_looked_at_is_left_alone(folder, bin_):
    path = put(folder, stamped(fmt=1))
    state = note_status(path)  # what the person saw in the review
    put(folder, stamped(fmt=1).replace("Band text.", "They typed something meanwhile."))  # the race
    result = replace_note(folder, "artist", new_text(), expect=state, trash=bin_)
    assert result.status == STATUS_CHANGED and bin_.calls == []
    assert "typed something" in open(path, encoding="utf-8").read()


def test_the_note_changing_between_the_temp_write_and_the_move_is_caught_too(folder, bin_, monkeypatch):
    path = put(folder, stamped(fmt=1))
    state = note_status(path)
    real = notes.note_status
    calls = {"n": 0}

    def sneaky(p, *a, **k):
        calls["n"] += 1
        if calls["n"] == 2:  # the second look: the last one before the old note goes
            put(folder, "---\ntitle: Mine\n---\n\nchanged in that instant\n")
        return real(p, *a, **k)

    monkeypatch.setattr(notes, "note_status", sneaky)
    result = replace_note(folder, "artist", new_text(), expect=state, trash=bin_)
    assert result.status == STATUS_CHANGED and bin_.calls == []
    assert "changed in that instant" in open(path, encoding="utf-8").read()
    assert [n for n in os.listdir(folder) if n.startswith(".")] == []


def test_a_note_that_is_gone_or_a_missing_expectation_is_refused(folder, bin_):
    assert replace_note(folder, "artist", new_text(), expect="abc", trash=bin_).status == STATUS_CHANGED
    path = put(folder, stamped(fmt=1))
    assert replace_note(folder, "artist", new_text(), expect="", trash=bin_).status == STATUS_CHANGED
    assert replace_note(folder, "artist", new_text(), expect="deadbeef", trash=bin_).status == STATUS_CHANGED
    assert replace_note(folder, "bogus", new_text(), expect="x", trash=bin_).status == STATUS_ERROR
    assert os.path.exists(path) and bin_.calls == []


def test_a_failing_recycle_bin_keeps_the_old_note_and_writes_nothing(folder):
    path = put(folder, stamped(fmt=1))

    def broken(p):
        raise TrashError("the bin is full")

    result = replace_note(folder, "artist", new_text(), expect=note_status(path), trash=broken)
    assert result.status == STATUS_ERROR and "kept" in result.message
    assert "Band text." in open(path, encoding="utf-8").read()
    assert os.listdir(folder) == ["artist.md"]


def test_dry_run_too_large_and_empty(folder, bin_):
    path = put(folder, stamped(fmt=1))
    state = note_status(path)
    dry = replace_note(folder, "artist", new_text(), expect=state, trash=bin_, dry_run=True)
    assert dry.status == STATUS_WRITTEN and dry.dry_run and bin_.calls == [] and "Band text." in open(path).read()
    big = replace_note(folder, "artist", "x" * (notes.MAX_NOTE_BYTES + 1), expect=state, trash=bin_)
    assert big.status == STATUS_TOO_LARGE and bin_.calls == []
    assert replace_note(folder, "artist", "  ", expect=state, trash=bin_).status == STATUS_ERROR


def test_a_new_note_that_appears_at_the_target_is_never_overwritten(folder, bin_, monkeypatch):
    path = put(folder, stamped(fmt=1))
    state = note_status(path)
    real = notes._link_or_reserve

    def appears(temp, target):
        put(folder, "---\ntitle: Mine\n---\n\nappeared\n")
        return real(temp, target)

    monkeypatch.setattr(notes, "_link_or_reserve", appears)
    result = replace_note(folder, "artist", new_text(), expect=state, trash=bin_)
    assert result.status == STATUS_EXISTS and "appeared" in open(folder / "artist.md", encoding="utf-8").read()


def test_the_real_recycle_bin_function_is_used_by_default(folder, monkeypatch):
    """No seam given: trash_with_retries() -> move_to_trash, which tests replace (never the user's real bin)."""
    from redactor_common.core import trash

    moved = []

    def fake_move(path, fallback=None):
        moved.append(path)
        os.remove(path)

    monkeypatch.setattr(trash, "move_to_trash", fake_move)
    path = put(folder, stamped(fmt=1))
    assert replace_note(folder, "artist", new_text(), expect=note_status(path)).ok and moved == [path]


def test_write_note_still_never_overwrites_and_a_stamped_note_written_by_it_is_current(folder):
    assert write_note(folder, "artist", stamped()).ok
    assert write_note(folder, "artist", new_text()).status == STATUS_EXISTS
    assert note_status(str(folder / "artist.md")).kind == STATE_CURRENT
