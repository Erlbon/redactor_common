"""Tests for core/image_meta.py: embedded XMP in JPEG/PNG/WebP/TIFF, EXIF reading.

Fixtures are real images made with Qt (a hard dependency of the package), so
the files are decoded by an independent implementation; Pillow is used as a
second opinion when it happens to be installed.
"""

from __future__ import annotations

import os
import struct
import zlib

import pytest

from redactor_common.core import image_meta as im
from redactor_common.core.pipeline import CommitError

ALL_FIELDS = {
    "title": "Harbour at dawn",
    "description": "Two barques at anchor.\nSecond line, with <markup> & ampersand.",
    "tags": ["harbour", "ships", "1912"],
    "creator": ["Erlend Bonderud"],
    "rights": "Public domain",
    "rating": 4,
    "date": "1912-04-15",
    "people": ["Captain Smith", "Åse Nordby"],
    "place": "Kristiansand, Norway",
    "source_url": "https://example.org/a?b=1&c=2",
    "notes": "Scanned from the Vol. II plate.",
}


# --- fixtures --------------------------------------------------------------


def _qimage():
    from PyQt6.QtGui import QColor, QImage

    img = QImage(23, 17, QImage.Format.Format_RGB32)
    for y in range(17):
        for x in range(23):
            img.setPixelColor(x, y, QColor((x * 11) % 256, (y * 15) % 256, (x * y) % 256))
    return img


def _encode(fmt: str, **kw) -> bytes:
    from PyQt6.QtCore import QBuffer, QByteArray, QIODevice

    ba = QByteArray()
    buf = QBuffer(ba)
    buf.open(QIODevice.OpenModeFlag.WriteOnly)
    ok = _qimage().save(buf, fmt.upper(), **kw)
    buf.close()
    if not ok:
        pytest.skip(f"this Qt build cannot write {fmt}")
    return bytes(ba)


def make_tiff(endian: str = "<") -> bytes:
    """A 1x1 grey TIFF built by hand, in either byte order."""
    e = endian
    header = (b"II" if e == "<" else b"MM") + struct.pack(e + "HI", 42, 8)

    def short(tag, v):
        return struct.pack(e + "HHI", tag, 3, 1) + struct.pack(e + "H", v) + b"\x00\x00"

    def long_(tag, v):
        return struct.pack(e + "HHII", tag, 4, 1, v)

    entries = [long_(256, 1), long_(257, 1), short(258, 8), short(259, 1), short(262, 1),
               long_(273, 8 + 2 + 12 * 9 + 4), short(277, 1), long_(278, 1), long_(279, 1)]
    ifd = struct.pack(e + "H", len(entries)) + b"".join(entries) + struct.pack(e + "I", 0)
    return header + ifd + b"\x7f"


def _build_exif_blob() -> bytes:
    """Little-endian TIFF structure: Make, Model, DateTimeOriginal, GPS 59.5N 7.25E... as S/W test below."""
    e = "<"

    def ascii_(tag, text):
        raw = text.encode() + b"\x00"
        return (tag, 2, len(raw), raw)

    def rationals(tag, values):
        raw = b"".join(struct.pack(e + "II", n, d) for n, d in values)
        return (tag, 5, len(values), raw)

    def layout(entries, base):
        entries = sorted(entries, key=lambda t: t[0])
        ifd_len = 2 + 12 * len(entries) + 4
        out = bytearray(struct.pack(e + "H", len(entries)))
        data = bytearray()
        for tag, typ, cnt, val in entries:
            if isinstance(val, int):
                out += struct.pack(e + "HHII", tag, typ, cnt, val)
            elif len(val) <= 4:
                out += struct.pack(e + "HHI", tag, typ, cnt)[:8] + val.ljust(4, b"\x00")
            else:
                out += struct.pack(e + "HHII", tag, typ, cnt, base + ifd_len + len(data))
                data += val + (b"\x00" if len(val) & 1 else b"")
        out += struct.pack(e + "I", 0)
        return bytes(out + data)

    def size(entries):
        n = len(entries)
        extra = sum(len(v) + (len(v) & 1) for _t, _y, _c, v in entries if not isinstance(v, int) and len(v) > 4)
        return 2 + 12 * n + 4 + extra

    exif_entries = [ascii_(0x9003, "1912:04:15 09:30:00")]
    gps_entries = [ascii_(1, "S"), rationals(2, [(59, 1), (30, 1), (0, 1)]),
                   ascii_(3, "W"), rationals(4, [(7, 1), (15, 1), (0, 1)])]
    ifd0_probe = [ascii_(0x010F, "Canon"), ascii_(0x0110, "EOS Test"), (0x8769, 4, 1, 0), (0x8825, 4, 1, 0)]
    exif_at = 8 + size(ifd0_probe)
    gps_at = exif_at + size(exif_entries)
    ifd0 = [ascii_(0x010F, "Canon"), ascii_(0x0110, "EOS Test"), (0x8769, 4, 1, exif_at), (0x8825, 4, 1, gps_at)]
    return (b"II" + struct.pack(e + "HI", 42, 8) + layout(ifd0, 8) + layout(exif_entries, exif_at)
            + layout(gps_entries, gps_at))


def _with_exif(fmt: str, data: bytes, blob: bytes) -> bytes:
    if fmt == "jpeg":
        body = b"Exif\x00\x00" + blob
        return data[:2] + b"\xff\xe1" + struct.pack(">H", len(body) + 2) + body + data[2:]
    if fmt == "png":
        chunk = struct.pack(">I", len(blob)) + b"eXIf" + blob + struct.pack(">I", zlib.crc32(b"eXIf" + blob) & 0xFFFFFFFF)
        i = data.find(b"IDAT") - 4
        return data[:i] + chunk + data[i:]
    if fmt == "webp":
        chunks = im._webp_chunks(data)
        return im._webp_build(chunks + [(b"EXIF", blob)])
    raise AssertionError(fmt)


def _pixels(data: bytes):
    from PyQt6.QtGui import QImage

    img = QImage()
    assert img.loadFromData(data), "Qt could not decode the image"
    img = img.convertToFormat(QImage.Format.Format_RGBA8888)
    return img.width(), img.height(), bytes(img.constBits().asarray(img.sizeInBytes()))


@pytest.fixture(params=["jpeg", "png", "webp", "tiff", "tiff-be"])
def image(request):
    kind = request.param
    if kind == "tiff":
        return "tiff", make_tiff("<")
    if kind == "tiff-be":
        return "tiff", make_tiff(">")
    return kind, _encode(kind)


# --- round trips ---------------------------------------------------------------


def test_round_trip_all_fields(image):
    fmt, data = image
    assert im.detect_format(data) == fmt
    assert im.read_meta_bytes(data) == {}
    new = im.write_meta_bytes(data, ALL_FIELDS)
    assert im.detect_format(new) == fmt
    assert im.read_meta_bytes(new) == ALL_FIELDS


def test_rewrite_replaces_and_does_not_accumulate(image):
    _fmt, data = image
    once = im.write_meta_bytes(data, {"title": "A", "tags": ["x"]})
    twice = im.write_meta_bytes(once, {"title": "B"})
    assert im.read_meta_bytes(twice) == {"title": "B", "tags": ["x"]}
    assert twice.count(b"<x:xmpmeta") == 1  # the old packet is gone, not left behind (TIFF blanks it)


def test_untouched_fields_survive_and_removal_works(image):
    _fmt, data = image
    full = im.write_meta_bytes(data, ALL_FIELDS)
    cut = im.write_meta_bytes(full, {"people": [], "rating": None, "place": ""})
    expected = {k: v for k, v in ALL_FIELDS.items() if k not in ("people", "rating", "place")}
    assert im.read_meta_bytes(cut) == expected


def test_no_change_on_file_without_xmp_returns_identical_bytes(image):
    _fmt, data = image
    assert im.write_meta_bytes(data, {"title": "", "tags": []}) == data


def test_image_data_unchanged_by_decoding():
    for fmt in ("jpeg", "png", "webp"):
        data = _encode(fmt)
        new = im.write_meta_bytes(data, ALL_FIELDS)
        assert _pixels(new) == _pixels(data), fmt


def test_pillow_still_opens_the_result(image):
    pil = pytest.importorskip("PIL.Image")
    import io

    fmt, data = image
    new = im.write_meta_bytes(data, ALL_FIELDS)
    a = pil.open(io.BytesIO(data))
    b = pil.open(io.BytesIO(new))
    a.load()
    b.load()
    assert a.size == b.size
    assert a.tobytes() == b.tobytes()


def test_unicode_survives(image):
    _fmt, data = image
    new = im.write_meta_bytes(data, {"title": "Blåbærsyltetøy – 北京 ☃", "people": ["Ærlig Øye"]})
    got = im.read_meta_bytes(new)
    assert got["title"] == "Blåbærsyltetøy – 北京 ☃"
    assert got["people"] == ["Ærlig Øye"]


def test_lists_are_deduplicated_and_blank_items_dropped(image):
    _fmt, data = image
    new = im.write_meta_bytes(data, {"tags": ["a", " a ", "", "b"], "creator": "Solo"})
    assert im.read_meta_bytes(new) == {"tags": ["a", "b"], "creator": ["Solo"]}


# --- validation ----------------------------------------------------------------


@pytest.mark.parametrize("fields", [{"nope": "x"}, {"date": "15/04/1912"}, {"rating": 9}, {"rating": "x"}, {"date": "1912-4"}])
def test_bad_fields_are_rejected(fields):
    data = _encode("png")
    with pytest.raises(ValueError):
        im.write_meta_bytes(data, fields)


@pytest.mark.parametrize("date", ["1912", "1912-04", "1912-04-15", "1912-04-15T09:30", "1912-04-15T09:30:00Z",
                                  "1912-04-15T09:30:00+02:00"])
def test_partial_and_full_dates_are_accepted(date):
    data = _encode("png")
    assert im.read_meta_bytes(im.write_meta_bytes(data, {"date": date}))["date"] == date


def test_garbage_and_bigtiff_are_refused():
    with pytest.raises(im.ImageMetaError):
        im.detect_format(b"not an image at all")
    with pytest.raises(im.ImageMetaError):
        im.detect_format(b"II+\x00" + b"\x00" * 20)


# --- existing XMP is preserved ---------------------------------------------------

FOREIGN = (
    b'<?xpacket begin="\xef\xbb\xbf" id="W5M0MpCehiHzreSzNTczkc9d"?>'
    b'<x:xmpmeta xmlns:x="adobe:ns:meta/"><rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">'
    b'<rdf:Description rdf:about="" xmlns:crs="http://ns.adobe.com/camera-raw-settings/1.0/"'
    b' xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:xmp="http://ns.adobe.com/xap/1.0/"'
    b' crs:Exposure2012="+0.50" dc:format="image/png" xmp:CreatorTool="Lightroom">'
    b"<dc:title><rdf:Alt><rdf:li xml:lang=\"x-default\">Old title</rdf:li></rdf:Alt></dc:title>"
    b"</rdf:Description></rdf:RDF></x:xmpmeta><?xpacket end=\"w\"?>"
)


@pytest.mark.parametrize("fmt", ["jpeg", "png", "webp"])
def test_foreign_xmp_properties_survive_a_write(fmt):
    data = im._SET_XMP[fmt](_encode(fmt), FOREIGN)
    assert im.read_meta_bytes(data)["title"] == "Old title"
    new = im.write_meta_bytes(data, {"title": "New title", "tags": ["t"]})
    packet = im._GET_XMP[fmt](new).decode("utf-8")
    assert "Exposure2012" in packet and "+0.50" in packet
    assert "Lightroom" in packet and "image/png" in packet
    assert im.read_meta_bytes(new) == {"title": "New title", "tags": ["t"]}
    assert "Old title" not in packet


def test_unparseable_existing_xmp_blocks_the_write_instead_of_destroying_it():
    data = im._SET_XMP["png"](_encode("png"), b"<x:xmpmeta xmlns:x='adobe:ns:meta/'><broken></x:xmpmeta>")
    with pytest.raises(im.ImageMetaError):
        im.write_meta_bytes(data, {"title": "x"})
    assert im.read_meta_bytes(data) == {}  # reading just reports nothing


def test_dtd_in_xmp_is_refused():
    evil = (b'<!DOCTYPE x [<!ENTITY a "aaaa">]><x:xmpmeta xmlns:x="adobe:ns:meta/"><rdf:RDF '
            b'xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"><rdf:Description rdf:about=""/></rdf:RDF></x:xmpmeta>')
    data = im._SET_XMP["png"](_encode("png"), evil)
    with pytest.raises(im.ImageMetaError):
        im.write_meta_bytes(data, {"title": "x"})


# --- format specifics ---------------------------------------------------------------


def test_jpeg_exif_stays_first_and_xmp_goes_after_it():
    blob = _build_exif_blob()
    data = _with_exif("jpeg", _encode("jpeg"), blob)
    new = im.write_meta_bytes(data, {"title": "t"})
    segs = im._jpeg_segments(new)
    assert im._jpeg_payload(new, segs[0]).startswith(b"Exif\x00\x00") or segs[0][0] == 0xE0
    first_non_app0 = next(s for s in segs if s[0] != 0xE0)
    assert im._jpeg_payload(new, first_non_app0).startswith(b"Exif\x00\x00")
    assert im.read_exif_bytes(new)["make"] == "Canon"


def test_jpeg_extended_xmp_is_refused():
    data = _encode("jpeg")
    body = im.XMP_JPEG_EXTENSION_HEADER + b"x" * 40
    ext = b"\xff\xe1" + struct.pack(">H", len(body) + 2) + body
    data = data[:2] + ext + data[2:]
    with pytest.raises(im.ImageMetaError):
        im.write_meta_bytes(data, {"title": "x"})


def test_jpeg_oversize_xmp_is_refused_not_truncated():
    with pytest.raises(im.ImageMetaError):
        im.write_meta_bytes(_encode("jpeg"), {"notes": "x" * 70000})
    # other formats have no such limit
    assert im.read_meta_bytes(im.write_meta_bytes(_encode("png"), {"notes": "x" * 70000}))["notes"] == "x" * 70000


def test_webp_simple_file_gets_vp8x_with_the_right_canvas():
    data = _encode("webp")
    assert b"VP8X" not in data[:20]  # the fixture starts as a simple file
    new = im.write_meta_bytes(data, {"title": "t"})
    chunks = im._webp_chunks(new)
    assert chunks[0][0] == b"VP8X"
    flags = chunks[0][1][0]
    assert flags & 0x04
    assert int.from_bytes(chunks[0][1][4:7], "little") + 1 == 23
    assert int.from_bytes(chunks[0][1][7:10], "little") + 1 == 17
    assert _pixels(new)[:2] == (23, 17)
    # riff size field is right
    assert struct.unpack("<I", new[4:8])[0] == len(new) - 8
    # removing the XMP clears the flag again
    cleared = im.write_meta_bytes(new, {"title": None})
    assert not (im._webp_chunks(cleared)[0][1][0] & 0x04)


def test_png_chunk_crcs_are_valid():
    new = im.write_meta_bytes(_encode("png"), ALL_FIELDS)
    for ctype, start, end in im._png_chunks(new):
        payload = new[start + 8 : end - 4]
        (crc,) = struct.unpack(">I", new[end - 4 : end])
        assert crc == zlib.crc32(ctype + payload) & 0xFFFFFFFF, ctype


def test_png_compressed_xmp_is_read():
    packet = im._serialize(im._new_root())
    root = im._new_root()
    im._write_property(root, im.FIELDS["title"], "Zipped")
    packet = im._serialize(root)
    payload = im.XMP_PNG_KEYWORD + b"\x00\x01\x00\x00\x00" + zlib.compress(packet)
    data = _encode("png")
    i = data.find(b"IDAT") - 4
    data = data[:i] + im._png_make_chunk(b"iTXt", payload) + data[i:]
    assert im.read_meta_bytes(data)["title"] == "Zipped"


@pytest.mark.parametrize("endian", ["<", ">"])
def test_tiff_strip_data_and_other_tags_untouched(endian):
    data = make_tiff(endian)
    new = im.write_meta_bytes(data, {"title": "t"})
    assert new[8 : len(data)] == data[8:]  # nothing before the original end moved
    entries, _ = im._tiff_ifd(new, struct.unpack(endian + "I", new[4:8])[0], endian)
    tags = [t[0] for t in entries]
    assert tags == sorted(tags) and 700 in tags and len(tags) == 10


# --- EXIF ------------------------------------------------------------------------


@pytest.mark.parametrize("fmt", ["jpeg", "png", "webp"])
def test_exif_is_read(fmt):
    data = _with_exif(fmt, _encode(fmt), _build_exif_blob())
    exif = im.read_exif_bytes(data)
    assert exif["make"] == "Canon" and exif["model"] == "EOS Test"
    assert exif["date"] == "1912-04-15T09:30:00"
    lat, lon = exif["gps"]
    assert lat == pytest.approx(-59.5) and lon == pytest.approx(-7.25)


def test_exif_is_not_touched_by_a_write():
    data = _with_exif("jpeg", _encode("jpeg"), _build_exif_blob())
    new = im.write_meta_bytes(data, ALL_FIELDS)
    assert im.read_exif_bytes(new) == im.read_exif_bytes(data)
    assert _build_exif_blob() in new


def test_exif_absent_is_empty_dict():
    assert im.read_exif_bytes(_encode("png")) == {}
    assert im.read_exif_bytes(make_tiff()) == {}


# --- copy_meta ---------------------------------------------------------------------


def test_copy_meta_carries_xmp_across_formats():
    src = im.write_meta_bytes(_encode("png"), ALL_FIELDS)
    out = im.copy_meta_bytes(src, _encode("jpeg"))
    assert im.read_meta_bytes(out) == ALL_FIELDS


# --- files ---------------------------------------------------------------------------


def _no_trash(path):
    os.remove(path)


def test_write_meta_file_round_trip_leaves_no_temp_files(tmp_path):
    p = tmp_path / "cover.jpg"
    p.write_bytes(_encode("jpeg"))
    assert im.write_meta(str(p), ALL_FIELDS, trash=_no_trash) is not None
    assert im.read_meta(str(p)) == ALL_FIELDS
    assert os.listdir(tmp_path) == ["cover.jpg"]


def test_write_meta_nothing_to_do_returns_none(tmp_path):
    p = tmp_path / "cover.png"
    raw = _encode("png")
    p.write_bytes(raw)
    assert im.write_meta(str(p), {"title": ""}, trash=_no_trash) is None
    assert p.read_bytes() == raw


def test_failed_verification_keeps_the_original(tmp_path, monkeypatch):
    p = tmp_path / "cover.png"
    raw = _encode("png")
    p.write_bytes(raw)
    real = im.write_meta_bytes

    def corrupting(data, fields):
        out = bytearray(real(data, fields))
        out[-20] ^= 0xFF  # damage the image data (inside IEND/IDAT area)
        return bytes(out)

    monkeypatch.setattr(im, "write_meta_bytes", corrupting)
    with pytest.raises((CommitError, im.ImageMetaError)):
        im.write_meta(str(p), {"title": "x"}, trash=_no_trash)
    assert p.read_bytes() == raw
    assert os.listdir(tmp_path) == ["cover.png"]


def test_copy_meta_files(tmp_path):
    a = tmp_path / "a.png"
    b = tmp_path / "b.webp"
    a.write_bytes(im.write_meta_bytes(_encode("png"), {"title": "T", "people": ["P"]}))
    b.write_bytes(_encode("webp"))
    im.copy_meta(str(a), str(b), trash=_no_trash)
    assert im.read_meta(str(b)) == {"title": "T", "people": ["P"]}


def test_unreadable_path_gives_a_friendly_error(tmp_path):
    with pytest.raises(im.ImageMetaError):
        im.read_meta(str(tmp_path / "missing.jpg"))
