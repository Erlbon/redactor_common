"""Tests for core/comicinfo_write.py (the one ComicInfo.xml writer) and the parts of core/zip_rewrite.py it and Dossier need:
streamed entries, the CRC check before the swap, new archives, a set-aside name of the caller's choice."""
import os
import xml.etree.ElementTree as ET
import zipfile

import pytest

from redactor_common.core import comicinfo_write as ci
from redactor_common.core.zip_rewrite import (
    NewEntry, RewriteError, RewritePlan, rewrite_archive, swap_in, write_new_archive,
)

XSI = "http://www.w3.org/2001/XMLSchema-instance"


def tags(xml):
    return [ci.local_name(c.tag) for c in ET.fromstring(xml)]


def make_cbz(path, info=None, info_name="ComicInfo.xml", pages=3, extra=()):
    with zipfile.ZipFile(path, "w") as zf:
        for i in range(pages):
            zf.writestr(f"{i:03}.jpg", bytes([0xFF, 0xD8]) + bytes(50))
        for name, data in extra:
            zf.writestr(name, data)
        if info is not None:
            zf.writestr(info_name, info)
    return str(path)


def info_of(path):
    with zipfile.ZipFile(path) as zf:
        name = ci.find_comicinfo_member(zf.namelist())
        return name, zf.read(name), zf.getinfo(name)


# --- serializing -------------------------------------------------------------


def test_a_new_file_declares_the_namespaces_and_keeps_schema_order():
    root = ci.new_comicinfo()
    ci.apply_comicinfo_edits(root, {"Writer": "Goscinny", "Series": "Astérix", "Title": "  Le Gaulois  "})
    xml = ci.serialize_comicinfo(root)
    assert xml.startswith(b'<?xml version="1.0" encoding="utf-8"?>\n<ComicInfo xmlns:xsi="' + XSI.encode())
    assert b'xmlns:xsd="http://www.w3.org/2001/XMLSchema"' in xml and xml.endswith(b"</ComicInfo>\n")
    assert tags(xml) == ["Title", "Series", "Writer"]
    assert ci.get_value(ET.fromstring(xml), "Title") == "Le Gaulois"  # stripped


def test_root_attributes_unknown_elements_pages_and_the_stamp_survive_in_their_slots():
    src = (b'<?xml version="1.0"?><ComicInfo xmlns:xsi="' + XSI.encode() + b'" xsi:noNamespaceSchemaLocation="ComicInfo.xsd">'
           b"<CommunityRating>4</CommunityRating><RedactorScan>OK;x</RedactorScan><Series>S</Series>"
           b'<Future>keep</Future><Pages>\n    <Page Image="0" Type="FrontCover"/>\n  </Pages><Summary></Summary></ComicInfo>')
    xml = ci.serialize_comicinfo(ci.parse_comicinfo(src))
    assert b'xsi:noNamespaceSchemaLocation="ComicInfo.xsd"' in xml and xml.count(b"xmlns:xsi") == 1
    assert tags(xml) == ["Series", "Future", "Pages", "CommunityRating", "RedactorScan"]  # empty Summary dropped
    assert b'\n    <Page Image="0" Type="FrontCover" />\n  ' in xml  # Pages keeps its own whitespace
    ET.fromstring(xml)


def test_page_count_is_refreshed_and_a_control_character_is_refused():
    root = ci.parse_comicinfo(b"<ComicInfo><PageCount>99</PageCount></ComicInfo>")
    assert ci.get_value(ET.fromstring(ci.serialize_comicinfo(root, page_count=3)), "PageCount") == "3"
    ci.apply_comicinfo_edits(root, {"Title": "bad" + chr(1)})
    with pytest.raises(ci.ComicInfoError, match="U\\+0001"):
        ci.serialize_comicinfo(root)


def test_parse_refuses_another_root_broken_xml_and_entities(tmp_path):
    with pytest.raises(ci.ComicInfoError):
        ci.parse_comicinfo(b"<NotComicInfo/>")
    with pytest.raises(ci.ComicInfoError):
        ci.parse_comicinfo(b"<ComicInfo><Title>x</ComicInfo>")
    secret = tmp_path / "secret.txt"
    secret.write_text("SECRET")
    doc = f'<?xml version="1.0"?><!DOCTYPE ComicInfo [<!ENTITY x SYSTEM "{secret.as_uri()}">]><ComicInfo><Title>&x;</Title></ComicInfo>'
    with pytest.raises(ci.ComicInfoError):
        ci.parse_comicinfo(doc.encode())


@pytest.mark.parametrize("tag,value,why", [
    ("Year", "2019", ""), ("Year", "²019", "whole number"), ("Month", "13", "1 to 12"), ("Day", "0", "1 to 31"),
    ("Manga", "Maybe", "one of"), ("AgeRating", "Teen", ""), ("Title", "a" + chr(10) + "b", "control character"),
    ("Summary", "a" + chr(10) + "b", ""), ("Title", "x" * (ci.MAX_VALUE + 1), "too long"), ("Count", "", ""),
])
def test_values_are_checked(tag, value, why):
    got = ci.check_comicinfo_value(tag, value)
    assert (why in got and got) if why else got == ""


def test_the_lookup_rule_prefers_the_root_then_the_shallowest():
    assert ci.find_comicinfo_member(["a/b/ComicInfo.xml", "x/comicinfo.xml", "001.jpg"]) == "x/comicinfo.xml"
    assert ci.find_comicinfo_member(["x/ComicInfo.xml", "comicinfo.xml"]) == "comicinfo.xml"
    assert ci.find_comicinfo_member(["__MACOSX/ComicInfo.xml"]) is None
    assert ci.count_pages(["001.jpg", "__MACOSX/._001.jpg", "ComicInfo.xml", "d/"]) == 1


# --- into a cbz --------------------------------------------------------------


def test_write_into_a_cbz_moves_a_nested_one_to_the_root_deflated_and_bins_the_original(tmp_path):
    path = make_cbz(tmp_path / "a.cbz", b"<ComicInfo><Series>Old</Series><PageCount>9</PageCount></ComicInfo>", "sub/ComicInfo.xml")
    binned = []
    ci.write_comicinfo_into_cbz(path, {"Series": "New", "Number": "2"}, dispose_original=lambda p: (binned.append(p), os.remove(p)))
    with zipfile.ZipFile(path) as zf:
        assert [n for n in zf.namelist() if "comicinfo" in n.lower()] == ["ComicInfo.xml"]
    name, xml, info = info_of(path)
    assert info.compress_type == zipfile.ZIP_DEFLATED
    root = ET.fromstring(xml)
    assert (ci.get_value(root, "Series"), ci.get_value(root, "Number"), ci.get_value(root, "PageCount")) == ("New", "2", "3")
    assert len(binned) == 1 and sorted(os.listdir(tmp_path)) == ["a.cbz"]


def test_a_bad_value_leaves_the_cbz_untouched(tmp_path):
    path = make_cbz(tmp_path / "a.cbz", b"<ComicInfo/>")
    before = open(path, "rb").read()
    with pytest.raises(ci.ComicInfoError):
        ci.write_comicinfo_into_cbz(path, {"Year": "MMXX"})
    assert open(path, "rb").read() == before and os.listdir(tmp_path) == ["a.cbz"]


def test_other_entries_keep_extra_fields_internal_attr_and_legacy_names(tmp_path):
    raw = "Märchen.jpg".encode("cp1252")  # not valid UTF-8: kept byte for byte
    placeholder = b"M" + b"X" * (len(raw) - 1 - len(b"rchen.jpg")) + b"rchen.jpg"
    path = tmp_path / "old.cbz"
    with zipfile.ZipFile(path, "w") as zf:
        info = zipfile.ZipInfo(placeholder.decode("ascii"), (2001, 1, 1, 0, 0, 0))
        info.extra = b"\x0a\x00\x04\x00abcd"
        info.internal_attr = 1
        zf.writestr(info, b"picture")
        zf.writestr("Page.jpg", b"1")
        zf.writestr("page.jpg", b"2")  # names that differ only in case exist in real cbz files
    path.write_bytes(path.read_bytes().replace(placeholder, raw))
    ci.write_comicinfo_into_cbz(str(path), {"Title": "T"})
    data = path.read_bytes()
    assert data.count(raw) == 2
    with zipfile.ZipFile(path) as zf:
        old = zf.getinfo(raw.decode("cp437"))
        assert old.extra == b"\x0a\x00\x04\x00abcd" and old.internal_attr == 1 and old.date_time == (2001, 1, 1, 0, 0, 0)
        assert zf.read("Page.jpg") == b"1" and zf.read("page.jpg") == b"2"


# --- zip_rewrite additions ---------------------------------------------------


def test_a_bad_crc_in_the_result_stops_the_swap(tmp_path, monkeypatch):
    path = make_cbz(tmp_path / "a.cbz", b"<ComicInfo/>")
    before = open(path, "rb").read()
    monkeypatch.setattr(zipfile.ZipFile, "testzip", lambda self: "000.jpg")
    with pytest.raises(RewriteError, match="checksum"):
        rewrite_archive(path, path, RewritePlan())
    assert open(path, "rb").read() == before and os.listdir(tmp_path) == ["a.cbz"]


def test_untouched_entries_are_streamed_with_byte_progress_and_a_cancel_inside_an_entry(tmp_path, monkeypatch):
    from redactor_common.core import zip_rewrite
    monkeypatch.setattr(zip_rewrite, "CHUNK", 1000)
    path = make_cbz(tmp_path / "a.cbz", None, extra=[("big.bin", os.urandom(10_000))])
    seen = []
    result = rewrite_archive(path, path, RewritePlan(), byte_progress=lambda d, t: seen.append((d, t)))
    assert result.bytes_in == result.bytes_out and seen[-1][0] == seen[-1][1] and len(seen) > 10
    calls = []
    with pytest.raises(zip_rewrite.RewriteCancelled):
        rewrite_archive(path, path, RewritePlan(), should_cancel=lambda: calls.append(1) or len(calls) > 6)
    assert os.listdir(tmp_path) == ["a.cbz"]


def test_new_entries_from_files_folders_levels_and_unreadable_files(tmp_path):
    src = tmp_path / "f.txt"
    src.write_text("text " * 100)
    out = str(tmp_path / "n.zip")
    gone = []
    result = write_new_archive(out, [
        NewEntry("d/"), NewEntry("d/f.txt", source_path=str(src), compress_type=zipfile.ZIP_DEFLATED, compresslevel=9),
        NewEntry("d/missing.txt", source_path=str(tmp_path / "missing.txt")), NewEntry("b.bin", b"xyz"),
    ], comment=b"c", on_unreadable=lambda e, exc: gone.append(e.name))
    with zipfile.ZipFile(out) as zf:
        assert zf.namelist() == ["d/", "d/f.txt", "b.bin"] and zf.read("d/f.txt") == src.read_bytes() and zf.comment == b"c"
        assert zf.getinfo("d/f.txt").compress_type == zipfile.ZIP_DEFLATED and zf.getinfo("d/").is_dir()
    assert gone == ["d/missing.txt"] and result.files == 2 and result.folders == 1 and result.skipped[0][0].endswith("missing.txt")
    with pytest.raises(OSError):
        write_new_archive(str(tmp_path / "m.zip"), [NewEntry("x", source_path=str(tmp_path / "missing.txt"))])
    assert not os.path.exists(tmp_path / "m.zip")


def test_swap_in_with_a_namer_can_keep_the_new_file_when_the_bin_refuses(tmp_path):
    target, new = tmp_path / "a.txt", tmp_path / "a.part"
    target.write_text("OLD")
    new.write_text("NEW")

    def refuse(_path):
        raise OSError("no bin")

    swap_in(str(new), str(target), refuse, namer=lambda p: p + ".old", keep_new_if_dispose_fails=True)
    assert target.read_text() == "NEW" and (tmp_path / "a.txt.old").read_text() == "OLD"
    new.write_text("NEWER")
    with pytest.raises(OSError):
        swap_in(str(new), str(target), refuse, namer=lambda p: p + ".old2")
    assert target.read_text() == "NEW"  # the default: a refused bin means nothing changed
