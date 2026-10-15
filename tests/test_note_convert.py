import time

import pytest

from redactor_common.core import note_convert
from redactor_common.core.note_convert import from_comicinfo, from_nfo, from_opf
from redactor_common.core.notes import parse_front_matter

OPF = """<?xml version="1.0" encoding="utf-8"?>
<package xmlns="http://www.idpf.org/2007/opf" version="2.0">
  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:opf="http://www.idpf.org/2007/opf">
    <dc:title>Leviathan Wakes</dc:title>
    <dc:creator opf:role="aut">James S. A. Corey</dc:creator>
    <dc:creator opf:role="aut">Daniel Abraham</dc:creator>
    <dc:creator opf:role="edt">Some Editor</dc:creator>
    <dc:publisher>Orbit</dc:publisher>
    <dc:date>2011-06-15T00:00:00+00:00</dc:date>
    <dc:description>&lt;p&gt;Humanity has colonized the &lt;b&gt;solar system&lt;/b&gt;.&lt;/p&gt;&lt;p&gt;Then a ship appears.&lt;/p&gt;</dc:description>
    <dc:subject>Science Fiction</dc:subject>
    <dc:subject>Space Opera</dc:subject>
    <meta name="calibre:series" content="The Expanse"/>
    <meta name="calibre:series_index" content="1.0"/>
  </metadata>
</package>"""

OPF3 = """<package xmlns="http://www.idpf.org/2007/opf" version="3.0"><metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
<dc:title>Book Two</dc:title><dc:creator>Au Thor</dc:creator><dc:date>0101-01-01T00:00:00+00:00</dc:date>
<meta property="belongs-to-collection" id="c1">Great Saga</meta>
<meta refines="#c1" property="group-position">2</meta></metadata></package>"""

ARTIST_NFO = """<?xml version="1.0" encoding="UTF-8" standalone="yes" ?>
<artist>
  <name>Ultravox</name>
  <genre>New Wave</genre>
  <genre>Synthpop</genre>
  <style>Art Rock</style>
  <formed>1974</formed>
  <disbanded>1987-05-01</disbanded>
  <biography>British band &amp; pioneers.&#10;&#10;Reformed later.</biography>
  <member><name>Midge Ure</name></member>
  <member><name>Billy Currie</name></member>
  <label>Chrysalis</label>
</artist>"""

SOLO_NFO = "<artist><name>Cristina</name><born>1956-01-17</born><died>2020-03-31</died><overview>Singer.</overview></artist>"

ALBUM_NFO = """<album>
  <title>Vienna</title><artist>Ultravox</artist><genre>Pop</genre>
  <year>1980</year><releasedate>1980-07-11</releasedate><label>Chrysalis</label>
  <review>A classic.</review>
  <track><position>1</position><title>Astradyne</title><duration>7:07</duration></track>
  <track><position>2</position><title>New Europeans</title><duration>4:05</duration></track>
</album>"""

COMICINFO = """<?xml version="1.0"?>
<ComicInfo xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" xmlns:xsd="http://www.w3.org/2001/XMLSchema">
  <Title>Spider-Man!</Title><Series>The Amazing Spider-Man</Series><Number>1</Number><Year>1963</Year>
  <Summary>Peter Parker &amp; the Chameleon.</Summary>
  <Writer>Stan Lee</Writer><Penciller>Steve Ditko</Penciller><Publisher>Marvel</Publisher>
  <Genre>Superhero, Action</Genre><Web>https://example.org/a https://example.org/b ftp://x</Web>
</ComicInfo>"""


def test_opf_from_text():
    result = from_opf(OPF)
    assert result.ok
    draft = result.draft
    assert draft.kind == "about" and draft.source == "metadata.opf"
    assert draft.front.title == "Leviathan Wakes" and draft.front.years == "2011"
    assert draft.front.genres == ["Science Fiction", "Space Opera"]
    assert draft.body.startswith("# Leviathan Wakes\n\n**Author:** James S. A. Corey, Daniel Abraham  \n")
    assert "Some Editor" not in draft.body
    assert "**Series:** The Expanse (#1)" in draft.body
    assert "**Publisher:** Orbit" in draft.body
    assert "Humanity has colonized the **solar system**.\n\nThen a ship appears." in draft.body


def test_opf_from_file_bytes_and_epub3_series(tmp_path):
    path = tmp_path / "metadata.opf"
    path.write_text(OPF, encoding="utf-8")
    assert from_opf(str(path)).draft.front.title == "Leviathan Wakes"
    assert from_opf(path).ok
    assert from_opf(OPF.encode("utf-8")).ok
    three = from_opf(OPF3).draft
    assert "**Series:** Great Saga (#2)" in three.body
    assert three.front.years == ""  # Calibre's 0101 placeholder date is no year


def test_opf_rendered_note_parses_in_pluteus_grammar():
    text = from_opf(OPF).draft.render()
    meta, body = parse_front_matter(text)
    assert meta["title"] == "Leviathan Wakes" and meta["status"] == "draft" and meta["years"] == "2011"
    assert body.rstrip().endswith("*Draft. Taken from metadata.opf.*")


def test_nfo_artist_group():
    draft = from_nfo(ARTIST_NFO).draft
    assert draft.kind == "artist" and draft.source == "artist.nfo"
    assert draft.front.title == "Ultravox" and draft.front.years == "1974-1987"
    assert draft.front.genres == ["New Wave", "Synthpop", "Art Rock"]
    assert "British band & pioneers.\n\nReformed later." in draft.body
    assert "**Members:** Midge Ure, Billy Currie" in draft.body
    assert "**Formed:** 1974" in draft.body and "**Label:** Chrysalis" in draft.body


def test_nfo_artist_person_and_open_ended_years():
    solo = from_nfo(SOLO_NFO).draft
    assert solo.front.years == "1956-2020" and "Singer." in solo.body and "**Born:** 1956-01-17" in solo.body
    assert from_nfo("<artist><name>X</name><formed>1990</formed></artist>").draft.front.years == "1990-"


def test_nfo_album():
    draft = from_nfo(ALBUM_NFO).draft
    assert draft.kind == "album" and draft.front.title == "Vienna" and draft.front.years == "1980"
    assert "**Artist:** Ultravox  \n**Released:** 1980-07-11  \n**Label:** Chrysalis" in draft.body
    assert "A classic." in draft.body
    assert "## Tracklist\n\n1. Astradyne 7:07\n2. New Europeans 4:05" in draft.body


def test_nfo_namespaces_and_odd_roots():
    assert from_nfo('<ns:artist xmlns:ns="urn:x"><ns:name>NS</ns:name></ns:artist>').draft.front.title == "NS"
    assert not from_nfo("<movie><title>Film</title></movie>").ok
    assert not from_nfo("<artist></artist>").ok


def test_comicinfo_series_level():
    draft = from_comicinfo(COMICINFO).draft
    assert draft.kind == "series" and draft.source == "ComicInfo.xml"
    assert draft.front.title == "The Amazing Spider-Man" and draft.front.years == "1963"
    assert draft.front.genres == ["Superhero", "Action"]
    assert draft.front.links == ["https://example.org/a", "https://example.org/b"]
    assert "Peter Parker & the Chameleon." in draft.body
    assert "**Writer:** Stan Lee  \n**Penciller:** Steve Ditko  \n**Publisher:** Marvel" in draft.body
    assert draft.body.rstrip().endswith("*Taken from issue #1 “Spider-Man!”.*")
    assert not from_comicinfo("<ComicInfo><Title>t</Title></ComicInfo>").ok
    assert not from_comicinfo(OPF).ok


@pytest.mark.parametrize("fn", [from_opf, from_nfo, from_comicinfo])
@pytest.mark.parametrize("bad", ["", "   ", "<unclosed>", "not xml at all", "<a><b></a>", "<?xml version='1.0'?>",
                                 b"\xff\xfe<\x00a\x00/\x00>\x00", b"\x00\x01\x02", "<" + "a" * 100])
def test_broken_input_never_crashes(fn, bad):
    result = fn(bad)
    assert not result.ok and result.message and result.draft is None


@pytest.mark.parametrize("fn", [from_opf, from_nfo, from_comicinfo])
def test_missing_file_and_wrong_types(fn, tmp_path):
    assert not fn(str(tmp_path / "missing.xml")).ok
    assert not fn(tmp_path).ok
    assert not fn(None).ok
    assert not fn(12).ok


BOMB = """<?xml version="1.0"?>
<!DOCTYPE lolz [
 <!ENTITY lol "lol">
 <!ENTITY lol2 "&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;">
 <!ENTITY lol3 "&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;">
 <!ENTITY lol4 "&lol3;&lol3;&lol3;&lol3;&lol3;&lol3;&lol3;&lol3;&lol3;&lol3;">
]>
<artist><name>&lol4;</name></artist>"""

XXE = """<?xml version="1.0"?><!DOCTYPE a [<!ENTITY x SYSTEM "file:///nonexistent-secret">]><artist><name>&x;</name></artist>"""


@pytest.mark.parametrize("fn", [from_opf, from_nfo, from_comicinfo])
def test_entity_bomb_and_external_entity_are_refused_fast(fn):
    for hostile in (BOMB, XXE, BOMB.lower(), BOMB.encode("utf-16")):
        started = time.monotonic()
        result = fn(hostile)
        assert not result.ok and "refused" in result.message
        assert time.monotonic() - started < 2


def test_huge_input_is_capped(tmp_path):
    big = "<artist><name>x</name><biography>" + "a" * (note_convert.MAX_SIDECAR_BYTES + 10) + "</biography></artist>"
    assert not from_nfo(big).ok
    path = tmp_path / "artist.nfo"
    path.write_text(big, encoding="utf-8")
    result = from_nfo(path)
    assert not result.ok and "too large" in result.message


def test_deeply_nested_xml_does_not_crash():
    deep = "<artist><name>x</name>" + "<a>" * 100000 + "</a>" * 100000 + "</artist>"
    result = from_nfo(deep)
    assert isinstance(result.ok, bool)


def test_cp1252_and_bom_inputs():
    text = '<?xml version="1.0" encoding="ISO-8859-1"?><artist><name>Sigur Rós</name></artist>'
    assert from_nfo(text.encode("cp1252")).draft.front.title == "Sigur Rós"
    assert from_nfo(b"\xef\xbb\xbf" + text.encode("utf-8")).draft.front.title == "Sigur Rós"


def test_draft_render_is_a_valid_note_for_hostile_values():
    nfo = "<artist><name>Re: [Zero] # 1</name><genre>a, b</genre><biography>- not a list</biography></artist>"
    text = from_nfo(nfo).draft.render()
    meta, _ = parse_front_matter(text)
    assert meta["title"] == "Re: [Zero] # 1" and meta["genres"] == ["a, b"] and meta["status"] == "draft"
