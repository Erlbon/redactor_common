import os
import sys

import pytest

from redactor_common.core import notes, temp_names
from redactor_common.core.note_text import credit_footer, discogs_credit, musicbrainz_credit, wikipedia_credit
from redactor_common.core.notes import (NoteFrontMatter, existing_note, existing_sidecar, note_file_name, note_path,
                                        parse_front_matter, render_note, write_note)


# ---- Pluteus compatibility: its parser, copied verbatim (pluteus/markdown.py front_matter) -------------------------
import re

_FRONT_KEY = re.compile(r"^([A-Za-z_][\w-]{0,40})\s*:\s*(.*)$")
_FRONT_ITEM = re.compile(r"^\s*-\s+(.*)$")


def _p_unquote(value):
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        value = value[1:-1]
    return value


def pluteus_front_matter(text):
    text = text.lstrip("\ufeff")
    if not text.startswith("---"):
        return {}, text
    lines = text.split("\n", 80)[:80]
    if lines[0].rstrip() != "---":
        return {}, text
    meta, current = {}, None
    for number, line in enumerate(lines[1:], start=1):
        line = line.rstrip("\r")
        if line.strip() in ("---", "..."):
            return meta, "\n".join(text.split("\n")[number + 1:]).lstrip("\n")
        item = _FRONT_ITEM.match(line)
        if item and current is not None:
            value = meta[current]
            if isinstance(value, str):
                value = meta[current] = [value] if value else []
            value.append(_p_unquote(item.group(1)))
            continue
        found = _FRONT_KEY.match(line)
        if not found:
            if line.strip():
                return {}, text
            continue
        current = found.group(1).lower()
        value = found.group(2).strip()
        if value.startswith("[") and value.endswith("]"):
            meta[current] = [_p_unquote(v) for v in value[1:-1].split(",") if v.strip()]
        else:
            meta[current] = _p_unquote(value)
    return {}, text


def _real_pluteus():
    """The real pluteus parser when its source is around and importable, else None."""
    src = os.path.join(os.environ.get("PLUTEUS_SRC", r"C:\Dev\pluteus"), "pluteus", "markdown.py")
    try:
        with open(src, encoding="utf-8") as handle:
            code = handle.read()
        # only the front matter part of the module is executed (the rest needs mistune and the pluteus package)
        start, end = code.index("_FRONT_KEY"), code.index("@dataclass\nclass Rendered")
        namespace = {"re": re}
        exec(code[start:end], namespace)
        return namespace["front_matter"]
    except Exception:
        return None


TRICKY_TITLES = [
    "Ultravox", "Cristina (Cristina Monet Palaci)", "Vol. 2: The End", "Re:Zero", "Track #1", "C# minor", "a #b",
    "[Untitled]", "- dash first", "-", "---", "...", 'He said "hi"', "It's", 'Both "double" and \'single\'',
    "Ünïcödé \u2013 Ångström \u5ddd", "trailing colon:", "  padded  ", "multi\nline", "*star", "&amp", "@home", "%x", "'quoted'",
    '"dq"', "{curly}", "a, b", "https://example.org/a:b", "50/50", "tab\there",
]


def test_front_matter_defaults_and_order():
    block = NoteFrontMatter(title="Ultravox", years="1976-", genres=["art rock", "pop"], links=["http://a.example/"],
                            tags=["band"]).render()
    assert block == ("---\ntitle: Ultravox\nyears: 1976-\ngenres: [art rock, pop]\ntags: [band]\n"
                     "links: [http://a.example/]\nstatus: draft\n---\n")


def test_empty_front_matter_is_just_a_draft_status():
    assert NoteFrontMatter().render() == "---\nstatus: draft\n---\n"
    assert NoteFrontMatter(status="").render() == "---\nstatus: draft\n---\n"
    meta, body = parse_front_matter(NoteFrontMatter(genres=[], tags=[" "]).render() + "text")
    assert meta == {"status": "draft"} and body == "text"


@pytest.mark.parametrize("title", TRICKY_TITLES)
def test_title_round_trips_through_pluteus(title):
    expected = re.sub(r"\s+", " ", title).strip()
    if notes._needs_quotes(expected) and '"' in expected and "'" in expected:
        expected = expected.replace('"', "\u201d")
    text = render_note(NoteFrontMatter(title=title), "# Body\n")
    for parser in (parse_front_matter, pluteus_front_matter):
        meta, body = parser(text)
        assert meta["title"] == expected, (title, text)
        assert meta["status"] == "draft"
        assert body == "# Body\n"
    real = _real_pluteus()
    if real is not None:
        assert real(text)[0]["title"] == expected


@pytest.mark.parametrize("item", ["plain", "a, b", "[x]", "- y", "k: v", "x #y", "#tag", "it's", 'say "x"', "https://a.example/p?q=1,2"])
def test_list_round_trips_incl_awkward_items(item):
    front = NoteFrontMatter(title="T", genres=["first", item, "last"], tags=[item], links=[item])
    text = render_note(front, "x")
    clean = re.sub(r"\s+", " ", item).strip()
    for parser in (parse_front_matter, pluteus_front_matter):
        meta, _ = parser(text)
        assert meta["genres"] == ["first", clean, "last"], text
        assert meta["tags"] == [clean] and meta["links"] == [clean]
    back = NoteFrontMatter.from_meta(parse_front_matter(text)[0])
    assert back.genres == ["first", clean, "last"] and back.title == "T"


def test_simple_lists_stay_inline_and_awkward_ones_become_blocks():
    assert "genres: [a, b]" in NoteFrontMatter(genres=["a", "b"]).render()
    block = NoteFrontMatter(genres=["a, b", "c"]).render()
    assert "genres:\n  - a, b\n  - c\n" in block


def test_duplicate_items_are_dropped_and_all_keys_round_trip():
    front = NoteFrontMatter(title="T", years="1980", genres=["x", "x", "y"], tags=["t"], links=["https://e.org/"],
                            audience="kids", status="draft")
    meta = pluteus_front_matter(front.render())[0]
    assert meta == {"title": "T", "years": "1980", "genres": ["x", "y"], "tags": ["t"], "links": ["https://e.org/"],
                    "audience": "kids", "status": "draft"}


def test_parse_front_matter_leaves_unclean_text_alone():
    assert parse_front_matter("no front matter") == ({}, "no front matter")
    assert parse_front_matter("---\nnot yaml at all\n---\nbody")[0] == {}
    assert parse_front_matter("---\ntitle: open\nbody")[1].startswith("---")
    assert parse_front_matter("\ufeff---\ntitle: A\n---\nB") == ({"title": "A"}, "B")


def test_render_note_shape_and_newlines():
    text = render_note(NoteFrontMatter(title="A"), "\r\n# A\r\n\r\nbody\r\n\r\n")
    assert text == "---\ntitle: A\nstatus: draft\n---\n\n# A\n\nbody\n"
    assert render_note(NoteFrontMatter(title="A"), "") == "---\ntitle: A\nstatus: draft\n---\n"


# ---- byte for byte against the spike samples -----------------------------------------------------------------------

def test_album_sample_is_reproducible_byte_for_byte():
    front = NoteFrontMatter(title="Vienna", years="1980", genres=["art pop", "new romantic", "new wave", "pop", "rock"])
    body = ("# Vienna\n\n**Artist:** Ultravox  \n**Released:** 1980-07-11  \n**Label:** Chrysalis\n\n## Tracklist\n\n"
            "1. Astradyne 7:07\n\n## In this folder\n\n*(the app compares the files here with the tracklist and lists "
            "what is missing or extra)*\n\n" + credit_footer([musicbrainz_credit(), "No Wikipedia article found"]))
    text = render_note(front, body)
    assert text.startswith("---\ntitle: Vienna\nyears: 1980\ngenres: [art pop, new romantic, new wave, pop, rock]\n"
                           "status: draft\n---\n\n# Vienna\n")
    assert text.endswith("---\n*Draft. Facts from [MusicBrainz](https://musicbrainz.org) (CC0). "
                         "No Wikipedia article found.*\n")


def test_credit_lines_match_the_samples():
    assert wikipedia_credit("Ultravox") == (
        "Introduction from Wikipedia ([Ultravox](https://en.wikipedia.org/wiki/Ultravox), CC BY-SA 4.0)")
    assert credit_footer([musicbrainz_credit(), wikipedia_credit("Ultravox")]) == (
        "---\n*Draft. Facts from [MusicBrainz](https://musicbrainz.org) (CC0). Introduction from Wikipedia "
        "([Ultravox](https://en.wikipedia.org/wiki/Ultravox), CC BY-SA 4.0).*")
    assert credit_footer([discogs_credit("artist", 55646), "Facts: MusicBrainz"]) == (
        "---\n*Draft. Profile from [Discogs](https://www.discogs.com/artist/55646) (user-contributed). "
        "Facts: MusicBrainz.*")


# ---- file names, finding notes -------------------------------------------------------------------------------------------

def test_names_and_paths(tmp_path):
    assert notes.NOTE_KINDS == ("artist", "author", "series", "album", "about")
    assert note_file_name("album") == "album.md"
    assert note_path(tmp_path, "about") == os.path.join(str(tmp_path), "about.md")
    with pytest.raises(ValueError):
        note_file_name("readme")


@pytest.mark.parametrize("name", ["artist.md", "Artist.MD", "ARTIST.md", "album.md", "About.Md"])
def test_existing_note_is_case_insensitive(tmp_path, name):
    (tmp_path / name).write_text("x")
    assert os.path.basename(existing_note(tmp_path)) == name


def test_existing_note_priority_and_ignored_files(tmp_path):
    assert existing_note(tmp_path) is None
    for name in ("README.md", "index.md", "notes.txt", "artist.nfo", "metadata.opf", "album.markdown"):
        (tmp_path / name).write_text("x")
    assert existing_note(tmp_path) is None
    (tmp_path / "about.md").write_text("x")
    (tmp_path / "Album.md").write_text("x")
    assert os.path.basename(existing_note(tmp_path)) == "Album.md"
    (tmp_path / "ARTIST.MD").write_text("x")
    assert os.path.basename(existing_note(tmp_path)) == "ARTIST.MD"
    assert existing_note(tmp_path / "missing") is None


def test_a_folder_named_like_a_note_is_not_a_note(tmp_path):
    (tmp_path / "artist.md").mkdir()
    assert existing_note(tmp_path) is None


def test_existing_sidecar(tmp_path):
    assert existing_sidecar(tmp_path) is None
    (tmp_path / "Metadata.OPF").write_text("x")
    assert os.path.basename(existing_sidecar(tmp_path)) == "Metadata.OPF"
    (tmp_path / "artist.nfo").write_text("x")
    assert os.path.basename(existing_sidecar(tmp_path)) == "artist.nfo"


# ---- the safe writer ----------------------------------------------------------------------------------------------------------

def _leftovers(folder):
    return [n for n in os.listdir(folder) if temp_names.is_app_temp_name(n)]


def test_write_exact_bytes_and_no_temp_left(tmp_path):
    result = write_note(tmp_path, "artist", "---\r\ntitle: Åse\r\n---\r\n\r\n# Åse \u2013 tak\r\nline\rend\n\n\n")
    assert result.status == "written" and result.ok
    assert result.path == str(tmp_path / "artist.md")
    data = (tmp_path / "artist.md").read_bytes()
    assert data == "---\ntitle: Åse\n---\n\n# Åse \u2013 tak\nline\nend\n".encode("utf-8")
    assert not data.startswith(b"\xef\xbb\xbf") and b"\r" not in data
    assert os.listdir(tmp_path) == ["artist.md"]


def test_bom_is_stripped(tmp_path):
    write_note(tmp_path, "about", "\ufeff# Hi")
    assert (tmp_path / "about.md").read_bytes() == b"# Hi\n"


def test_never_overwrites_same_name(tmp_path):
    (tmp_path / "artist.md").write_bytes(b"mine")
    result = write_note(tmp_path, "artist", "# new")
    assert result.status == "exists" and result.path == str(tmp_path / "artist.md") and not result.ok
    assert (tmp_path / "artist.md").read_bytes() == b"mine"
    assert os.listdir(tmp_path) == ["artist.md"]


@pytest.mark.parametrize("existing", ["Artist.MD", "ARTIST.md", "about.md", "Album.md", "series.md"])
def test_never_overwrites_case_variants_or_other_note_names(tmp_path, existing):
    (tmp_path / existing).write_bytes(b"mine")
    result = write_note(tmp_path, "artist", "# new")
    assert result.status == "exists" and os.path.basename(result.path) == existing
    assert sorted(os.listdir(tmp_path)) == [existing]
    assert (tmp_path / existing).read_bytes() == b"mine"


def test_nfo_and_opf_do_not_block_a_write(tmp_path):
    (tmp_path / "artist.nfo").write_text("<artist/>")
    (tmp_path / "metadata.opf").write_text("<package/>")
    (tmp_path / "README.md").write_text("readme")
    assert write_note(tmp_path, "artist", "# new").status == "written"
    assert (tmp_path / "artist.nfo").read_text() == "<artist/>"


def test_a_directory_with_the_note_name_is_not_overwritten(tmp_path):
    (tmp_path / "artist.md").mkdir()
    result = write_note(tmp_path, "artist", "# new")
    assert result.status == "exists"
    assert (tmp_path / "artist.md").is_dir()


def test_dry_run_writes_nothing(tmp_path):
    result = write_note(tmp_path, "album", "# a", dry_run=True)
    assert result.status == "written" and result.dry_run and "Would" in result.message
    assert os.listdir(tmp_path) == []
    (tmp_path / "album.md").write_text("x")
    assert write_note(tmp_path, "album", "# a", dry_run=True).status == "exists"
    assert write_note(tmp_path, "album", "x" * 70000, dry_run=True).status == "too_large"


def test_too_large_refused_and_limit_is_inclusive(tmp_path):
    result = write_note(tmp_path, "about", "a" * (64 * 1024))
    assert result.status == "too_large" and os.listdir(tmp_path) == []
    ok = write_note(tmp_path, "about", "a" * (64 * 1024 - 1))
    assert ok.status == "written" and (tmp_path / "about.md").stat().st_size == 64 * 1024


def test_size_counts_utf8_bytes(tmp_path):
    assert write_note(tmp_path, "about", "å" * 40000).status == "too_large"


def test_missing_folder_creates_nothing(tmp_path):
    missing = tmp_path / "nope" / "deeper"
    result = write_note(missing, "artist", "# x")
    assert result.status == "error" and "does not exist" in result.message
    assert not (tmp_path / "nope").exists()


def test_unknown_kind_and_empty_text_are_errors(tmp_path):
    assert write_note(tmp_path, "readme", "x").status == "error"
    assert write_note(tmp_path, "artist", "  \n").status == "error"
    assert os.listdir(tmp_path) == []


def test_a_file_instead_of_a_folder_is_an_error(tmp_path):
    f = tmp_path / "f"
    f.write_text("x")
    assert write_note(f, "artist", "x").status == "error"


def test_failure_leaves_no_temp_file(tmp_path, monkeypatch):
    def boom(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(os, "link", boom)
    monkeypatch.setattr(os, "open", _failing_target_open(os.open))
    result = write_note(tmp_path, "artist", "# x")
    assert result.status == "error" and "disk full" in result.message
    assert os.listdir(tmp_path) == []


def _failing_target_open(real):
    def opener(path, flags, mode=0o777):
        if os.path.basename(path) == "artist.md":
            raise OSError("disk full")
        return real(path, flags, mode)

    return opener


def test_failure_while_writing_leaves_no_temp_file(tmp_path, monkeypatch):
    monkeypatch.setattr(os, "fsync", lambda fd: (_ for _ in ()).throw(OSError("io error")))
    result = write_note(tmp_path, "artist", "# x")
    assert result.status == "error" and "io error" in result.message
    assert os.listdir(tmp_path) == []


def test_fallback_when_hard_links_are_unavailable(tmp_path, monkeypatch):
    def nolink(*a, **k):
        raise OSError("links not supported")

    monkeypatch.setattr(os, "link", nolink)
    assert write_note(tmp_path, "artist", "# x").status == "written"
    assert (tmp_path / "artist.md").read_bytes() == b"# x\n"
    assert os.listdir(tmp_path) == ["artist.md"]


def test_fallback_still_refuses_an_existing_target(tmp_path, monkeypatch):
    monkeypatch.setattr(os, "link", lambda *a, **k: (_ for _ in ()).throw(OSError("links not supported")))
    # a note that appears after the first check but before the link: simulate by creating it inside existing_note
    real = notes.existing_note
    calls = {"n": 0}

    def late(folder):
        calls["n"] += 1
        if calls["n"] == 2:
            (tmp_path / "artist.md").write_bytes(b"mine")
            return None  # the second check misses it (as in a real race): the exclusive create must still refuse
        return real(folder)

    monkeypatch.setattr(notes, "existing_note", late)
    result = write_note(tmp_path, "artist", "# new")
    assert result.status == "exists"
    assert (tmp_path / "artist.md").read_bytes() == b"mine"
    assert os.listdir(tmp_path) == ["artist.md"]


def test_target_appearing_before_the_link_is_never_replaced(tmp_path, monkeypatch):
    real = notes.existing_note
    calls = {"n": 0}

    def late(folder):
        calls["n"] += 1
        if calls["n"] == 2:
            (tmp_path / "artist.md").write_bytes(b"mine")
            return None
        return real(folder)

    monkeypatch.setattr(notes, "existing_note", late)
    result = write_note(tmp_path, "artist", "# new")
    assert result.status == "exists"
    assert (tmp_path / "artist.md").read_bytes() == b"mine"
    assert os.listdir(tmp_path) == ["artist.md"]


def test_temp_name_is_recognised_by_the_family_filter(tmp_path):
    name = os.path.basename(notes._temp_path(str(tmp_path), "artist.md"))
    assert temp_names.is_app_temp_name(name)
    assert name.startswith(".") and name.endswith(".redact-part.md")


@pytest.mark.skipif(sys.platform == "win32", reason="read-only folders are not enforced by chmod on Windows")
def test_read_only_folder(tmp_path):
    if os.geteuid() == 0:
        pytest.skip("root ignores permissions")
    os.chmod(tmp_path, 0o555)
    try:
        result = write_note(tmp_path, "artist", "# x")
    finally:
        os.chmod(tmp_path, 0o755)
    assert result.status == "error" and os.listdir(tmp_path) == []


def test_written_note_parses_back_with_the_real_title(tmp_path):
    text = render_note(NoteFrontMatter(title="Kaizers Orchestra: live", years="2000-", genres=["rock"]), "# K\n")
    assert write_note(tmp_path, "artist", text).ok
    meta, body = pluteus_front_matter((tmp_path / "artist.md").read_text(encoding="utf-8"))
    assert meta["title"] == "Kaizers Orchestra: live" and meta["status"] == "draft" and body == "# K\n"
