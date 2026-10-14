"""cli/fields.py (FieldResolver) and cli/main_factory.py, checked against
the spellings each app's own *cli/fields.py and *cli/main.py accepted."""

import json
import os

import pytest

from redactor_common.cli import EXIT_OK, CliError
from redactor_common.cli.fields import FieldResolver, spelling_variants
from redactor_common.cli.main_factory import build_app_parser, make_main

# -- the four apps' tables, in miniature ------------------------------------

_MP3_FIELDS = [("title", "Title"), ("albumartist", "Album Artist"), ("discnumber", "Disc Number"), ("comment", "Comment")]


def _mp3():
    spellings = {}
    for key, label in _MP3_FIELDS:
        for spelling in spelling_variants(key, label):
            spellings[spelling] = key

    def check(key, value):
        value = value.strip()
        if key == "discnumber" and value and not value.isdigit():
            raise CliError(f"{key} must be a number, not {value!r}")
        return value

    return FieldResolver(spellings, [k for k, _ in _MP3_FIELDS], check=check)


def _epub():
    names = ["title", "authors", "series_index", "year"]
    spellings = {n: n for n in names}
    spellings.update({"author": "authors", "author(s)": "authors", "series #": "series_index",
                      "series#": "series_index", "pub_year": "year"})
    return FieldResolver(spellings, names)


def _video():
    names = ["title", "show_title", "season_number", "sort_title"]
    spellings = {n: n for n in names}
    spellings.update({n.replace("_", ""): n for n in names})
    spellings.update({"show": "show_title", "season": "season_number"})
    return FieldResolver(spellings, names)


def _cbz():
    tag_to_attr = {"Series": "series", "Number": "number", "PageCount": "page_count"}
    spellings = {}
    for tag, attr in tag_to_attr.items():
        for spelling in {tag.lower(), attr, attr.replace("_", ""), attr.replace("_", "-")}:
            spellings[spelling] = tag
    return FieldResolver(
        spellings, sorted(tag_to_attr.values()), settable={"Series", "Number"}, settable_names=["number", "series"],
    )


# -- resolve ------------------------------------------------------------------

@pytest.mark.parametrize("typed", ["albumartist", "Album Artist", "album_artist", "ALBUM-ARTIST", "  albumartist  "])
def test_mp3_spellings(typed):
    assert _mp3().resolve(typed) == "albumartist"


@pytest.mark.parametrize("typed,key", [
    ("author", "authors"), ("Author(s)", "authors"), ("Series #", "series_index"), ("series#", "series_index"),
    ("series-index", "series_index"), ("series index", "series_index"), ("pub_year", "year"), ("PUB-YEAR", "year"),
])
def test_epub_aliases_and_normalised_forms(typed, key):
    assert _epub().resolve(typed) == key


@pytest.mark.parametrize("typed,key", [
    ("Season Number", "season_number"), ("season-number", "season_number"), ("seasonnumber", "season_number"),
    ("sorttitle", "sort_title"), ("Sort Title", "sort_title"), ("show", "show_title"), ("sort__title", "sort_title"),
])
def test_video_underscore_squeezing(typed, key):
    assert _video().resolve(typed) == key


def test_exact_spelling_wins_over_a_normalised_one():
    resolver = FieldResolver({"a b": "first", "a_b": "second"}, ["first", "second"])
    assert resolver.resolve("A B") == "first"
    assert resolver.resolve("a-b") == "second"


def test_unknown_field_lists_the_names_in_order():
    with pytest.raises(CliError, match=r"unknown field 'nope'\. Fields: title, albumartist, discnumber, comment"):
        _mp3().resolve("nope")


def test_empty_name_is_unknown():
    with pytest.raises(CliError):
        _mp3().resolve("   ")


def test_cli_error_is_a_usage_error():
    with pytest.raises(CliError) as info:
        _mp3().resolve("nope")
    assert info.value.code == 2


def test_cbz_settable_restriction():
    resolver = _cbz()
    assert resolver.resolve("pagecount") == "PageCount"  # info may read it
    assert resolver.resolve("page-count") == "PageCount"
    with pytest.raises(CliError, match="Fields: number, series$"):
        resolver.resolve("page_count", settable=True)
    assert resolver.resolve("series", settable=True) == "Series"


def test_settable_flag_is_a_no_op_without_a_settable_set():
    assert _epub().resolve("author", settable=True) == "authors"


def test_find_contains_keys_and_add():
    resolver = _video()
    assert resolver.find("nope") is None
    assert "show" in resolver and "nope" not in resolver and 5 not in resolver
    assert resolver.keys == {"title", "show_title", "season_number", "sort_title"}
    resolver.add(["series", "", "Programme"], "show_title")
    assert resolver.resolve("programme") == "show_title"
    assert resolver.resolve("Series") == "show_title"


def test_spelling_variants():
    assert spelling_variants("album_artist", "Album Artist") >= {
        "album_artist", "albumartist", "album-artist", "album artist",
    }
    assert spelling_variants("", None) == set()


# -- parse_assignment ------------------------------------------------------------

def test_parse_assignment_resolves_and_checks():
    assert _mp3().parse_assignment("Album Artist= Queen ") == ("albumartist", "Queen")


def test_parse_assignment_splits_on_the_first_equals():
    assert _epub().parse_assignment("title=a=b") == ("title", "a=b")


def test_parse_assignment_needs_an_equals():
    with pytest.raises(CliError, match="expected FIELD=VALUE, got 'title'"):
        _mp3().parse_assignment("title")


def test_parse_assignment_runs_the_value_check():
    with pytest.raises(CliError, match="discnumber must be a number"):
        _mp3().parse_assignment("disc number=two")


def test_parse_assignment_empty_value_clears():
    assert _mp3().parse_assignment("comment=") == ("comment", "")


def test_parse_assignment_without_check_strips():
    assert _video().parse_assignment("show= Dune ") == ("show_title", "Dune")


def test_parse_assignment_refuses_a_read_only_field():
    with pytest.raises(CliError, match="unknown field 'PageCount'"):
        _cbz().parse_assignment("PageCount=3")
    assert _cbz().parse_assignment("Series=Saga") == ("Series", "Saga")


# -- main factory -----------------------------------------------------------------

def _add_info(sub):
    parser = sub.add_parser("info")
    parser.add_argument("paths", nargs="*")
    parser.add_argument("--json", action="store_true")
    parser.set_defaults(handler=lambda args, out: (out.record({"paths": args.paths}), out.finish({"files": 1}), 0)[2])


def _add_fail(sub):
    parser = sub.add_parser("fail")
    parser.set_defaults(handler=lambda args, out: 1)


def test_parser_has_version_and_commands(capsys):
    parser = build_app_parser("demo", "2026-10-14#01", "Demo CLI", [_add_info, _add_fail], commands=("info", "fail"))
    assert parser.prog == "demo"
    assert parser.description == "Demo CLI"
    with pytest.raises(SystemExit):
        parser.parse_args(["--version"])
    assert "demo 2026-10-14#01" in capsys.readouterr().out


def test_command_is_required(capsys):
    build_parser, main = make_main("demo", "1", "Demo", [_add_info])
    with pytest.raises(SystemExit) as info:
        main([])
    assert info.value.code == 2


def test_commands_must_match_the_subparsers():
    with pytest.raises(ValueError, match="must list the subcommands"):
        build_app_parser("demo", "1", "Demo", [_add_info, _add_fail], commands=("info",))
    with pytest.raises(ValueError):
        build_app_parser("demo", "1", "Demo", [_add_info, _add_fail], commands=("fail", "info"))


def test_main_dispatches_and_returns_the_handler_code(capsys):
    build_parser, main = make_main("demo", "1", "Demo", [_add_info, _add_fail], commands=("info", "fail"))
    assert main(["fail"]) == 1
    assert main(["info", "a", "b", "--json"]) == EXIT_OK
    document = json.loads(capsys.readouterr().out)
    assert document["results"] == [{"paths": ["a", "b"]}] and document["files"] == 1


def test_main_closes_the_output_file_even_when_the_handler_fails(tmp_path):
    def add_boom(sub):
        parser = sub.add_parser("boom")
        parser.add_argument("-o", "--output")
        parser.add_argument("--json", action="store_true")

        def handler(args, out):
            raise RuntimeError("bug")

        parser.set_defaults(handler=handler)

    _, main = make_main("demo", "1", "Demo", [add_boom])
    target = tmp_path / "result.json"
    with pytest.raises(RuntimeError):
        main(["boom", "--json", "-o", str(target)])
    document = json.loads(target.read_text(encoding="utf-8"))
    assert document["error"] == "bug"


def test_build_parser_makes_a_fresh_parser_each_time():
    build_parser, _ = make_main("demo", "1", "Demo", [_add_info])
    assert build_parser() is not build_parser()


def test_expand_paths_skip_drops_leftovers_found_by_searching_but_keeps_named_files(tmp_path):
    from redactor_common.cli import expand_paths
    from redactor_common.core.temp_names import is_app_temp_name

    (tmp_path / "sub").mkdir()
    for name in ("a.mkv", "a.redact-orig.mkv", "sub/b.mkv", "sub/b.redact-part.mkv"):
        (tmp_path / name).write_text("x")
    files, _ = expand_paths([str(tmp_path)], [".mkv"], skip=is_app_temp_name)
    assert [os.path.basename(f) for f in files] == ["a.mkv", "b.mkv"]
    flat, _ = expand_paths([str(tmp_path)], [".mkv"], recursive=False, skip=is_app_temp_name)
    assert [os.path.basename(f) for f in flat] == ["a.mkv"]
    wild, _ = expand_paths([str(tmp_path / "*.mkv")], [".mkv"], skip=is_app_temp_name)
    assert [os.path.basename(f) for f in wild] == ["a.mkv"]
    named, _ = expand_paths([str(tmp_path / "a.redact-orig.mkv")], [".mkv"], skip=is_app_temp_name)
    assert [os.path.basename(f) for f in named] == ["a.redact-orig.mkv"]  # the user named it: kept
    everything, _ = expand_paths([str(tmp_path)], [".mkv"])
    assert len(everything) == 4  # no skip: unchanged behaviour
