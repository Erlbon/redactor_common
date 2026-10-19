"""
redactor_common/core/image_meta.py

Embedded metadata for image files: read and write XMP inside JPEG, PNG,
WebP and TIFF, and read EXIF. Qt-free and dependency-free (pure Python, no
Pillow, no ExifTool), so it ships in every app's exe and stays portable.

Why XMP: it is the one standard that is extensible, UTF-8 and read by
Windows, Lightroom, digiKam and friends. EXIF is read-only here (camera
data must never be edited by accident); everything we add goes into XMP.

Managed fields (FIELDS below) map onto standard XMP properties wherever one
exists, so other programs see them too:

    title        dc:title                       (language alternative)
    description  dc:description                 (language alternative)
    tags         dc:subject                     (bag)
    creator      dc:creator                     (sequence, a list)
    rights       dc:rights                      (language alternative)
    rating       xmp:Rating                     (0-5)
    date         photoshop:DateCreated          (ISO 8601, partial dates
                                                 such as "1912" or "1912-04"
                                                 are allowed)
    people       Iptc4xmpExt:PersonInImage      (bag)
    place        Iptc4xmpCore:Location          (text)
    source_url   dossier:sourceUrl              (custom namespace)
    notes        dossier:notes                  (custom namespace)

Everything else already in the file's XMP (Lightroom settings, history, other
programs' fields) is left exactly as it was: a write edits only the fields
named in the call. A packet that cannot be parsed, or JPEG "extended XMP",
makes a write REFUSE rather than silently drop data.

Safety: writing is all-or-nothing. The new file is built beside the original,
checked (the image data must be byte-for-byte unchanged and the fields must
read back as written), and swapped in with pipeline.commit_in_place(), which
never loses the original.

Formats are recognised by their magic bytes, never by the file extension.
Not supported (raise ImageMetaError): BigTIFF, animated-image frame
metadata, AVIF/HEIC/JPEG XL (a later step).
"""

from __future__ import annotations

import os
import re
import struct
import uuid
import xml.etree.ElementTree as ET
import zlib
from dataclasses import dataclass
from typing import Any, Callable, Mapping

from redactor_common.core.pipeline import CommitResult, commit_in_place
from redactor_common.core.trash import move_to_trash

# --- namespaces and field table ---------------------------------------------

NS_RDF = "http://www.w3.org/1999/02/22-rdf-syntax-ns#"
NS_X = "adobe:ns:meta/"
NS_DC = "http://purl.org/dc/elements/1.1/"
NS_XMP = "http://ns.adobe.com/xap/1.0/"
NS_PHOTOSHOP = "http://ns.adobe.com/photoshop/1.0/"
NS_IPTC_CORE = "http://iptc.org/std/Iptc4xmpCore/1.0/xmlns/"
NS_IPTC_EXT = "http://iptc.org/std/Iptc4xmpExt/2008-02-29/"
NS_DOSSIER = "https://github.com/Erlbon/dossier/ns/1.0/"
NS_XML = "http://www.w3.org/XML/1998/namespace"

# Registered so that existing and new properties keep readable prefixes when
# the packet is written back (unregistered namespaces would become ns0, ns1...).
_PREFIXES = {
    NS_RDF: "rdf",
    NS_X: "x",
    NS_DC: "dc",
    NS_XMP: "xmp",
    NS_PHOTOSHOP: "photoshop",
    NS_IPTC_CORE: "Iptc4xmpCore",
    NS_IPTC_EXT: "Iptc4xmpExt",
    NS_DOSSIER: "dossier",
    "http://ns.adobe.com/xap/1.0/rights/": "xmpRights",
    "http://ns.adobe.com/xap/1.0/mm/": "xmpMM",
    "http://ns.adobe.com/xap/1.0/sType/ResourceEvent#": "stEvt",
    "http://ns.adobe.com/xap/1.0/sType/ResourceRef#": "stRef",
    "http://ns.adobe.com/exif/1.0/": "exif",
    "http://ns.adobe.com/exif/1.0/aux/": "aux",
    "http://ns.adobe.com/tiff/1.0/": "tiff",
    "http://ns.adobe.com/camera-raw-settings/1.0/": "crs",
    "http://ns.adobe.com/lightroom/1.0/": "lr",
    "http://ns.microsoft.com/photo/1.2/": "MP",
    "http://www.metadataworkinggroup.com/schemas/regions/": "mwg-rs",
    "http://ns.adobe.com/xmp/sType/Area#": "stArea",
    "http://ns.adobe.com/xap/1.0/g/img/": "xmpGImg",
    "http://www.w3.org/1999/xhtml": "xhtml",
}
for _uri, _prefix in _PREFIXES.items():
    ET.register_namespace(_prefix, _uri)

XMP_JPEG_HEADER = b"http://ns.adobe.com/xap/1.0/\x00"
XMP_JPEG_EXTENSION_HEADER = b"http://ns.adobe.com/xmp/extension/\x00"
XMP_PNG_KEYWORD = b"XML:com.adobe.xmp"

MAX_JPEG_XMP = 65502 - len(XMP_JPEG_HEADER)  # one APP1 segment; extended XMP is not written

KIND_TEXT = "text"
KIND_LANGALT = "langalt"
KIND_BAG = "bag"
KIND_SEQ = "seq"
KIND_INT = "int"


@dataclass(frozen=True)
class FieldSpec:
    ns: str
    local: str
    kind: str


FIELDS: dict[str, FieldSpec] = {
    "title": FieldSpec(NS_DC, "title", KIND_LANGALT),
    "description": FieldSpec(NS_DC, "description", KIND_LANGALT),
    "tags": FieldSpec(NS_DC, "subject", KIND_BAG),
    "creator": FieldSpec(NS_DC, "creator", KIND_SEQ),
    "rights": FieldSpec(NS_DC, "rights", KIND_LANGALT),
    "rating": FieldSpec(NS_XMP, "Rating", KIND_INT),
    "date": FieldSpec(NS_PHOTOSHOP, "DateCreated", KIND_TEXT),
    "people": FieldSpec(NS_IPTC_EXT, "PersonInImage", KIND_BAG),
    "place": FieldSpec(NS_IPTC_CORE, "Location", KIND_TEXT),
    "source_url": FieldSpec(NS_DOSSIER, "sourceUrl", KIND_TEXT),
    "notes": FieldSpec(NS_DOSSIER, "notes", KIND_TEXT),
}

LIST_FIELDS = frozenset(k for k, s in FIELDS.items() if s.kind in (KIND_BAG, KIND_SEQ))

_DATE_RE = re.compile(
    r"^\d{4}(-\d{2}(-\d{2}(T\d{2}:\d{2}(:\d{2}(\.\d+)?)?(Z|[+-]\d{2}:\d{2})?)?)?)?$"
)

FORMAT_JPEG = "jpeg"
FORMAT_PNG = "png"
FORMAT_WEBP = "webp"
FORMAT_TIFF = "tiff"


class ImageMetaError(Exception):
    """The file is not a supported image, is damaged, or its metadata cannot be
    edited safely. The message is fit to show to the user."""


# --- format detection ---------------------------------------------------------


def detect_format(data: bytes) -> str:
    """Format of the image in `data` by its magic bytes."""
    if data[:3] == b"\xff\xd8\xff":
        return FORMAT_JPEG
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return FORMAT_PNG
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return FORMAT_WEBP
    if data[:4] in (b"II*\x00", b"MM\x00*"):
        return FORMAT_TIFF
    if data[:4] in (b"II+\x00", b"MM\x00+"):
        raise ImageMetaError("BigTIFF files are not supported.")
    raise ImageMetaError("Not a supported image (JPEG, PNG, WebP or TIFF).")


# --- JPEG ---------------------------------------------------------------------


def _jpeg_segments(data: bytes) -> list[tuple[int, int, int]]:
    """(marker, start, end) of every segment before the scan data; `start` is
    the 0xFF byte, `end` is one past the segment. The entropy-coded data
    from SOS onward is not a segment (it runs to the end of the file)."""
    if data[:2] != b"\xff\xd8":
        raise ImageMetaError("Damaged JPEG: missing start marker.")
    out: list[tuple[int, int, int]] = []
    pos = 2
    n = len(data)
    while pos < n:
        if data[pos] != 0xFF:
            raise ImageMetaError("Damaged JPEG: bad segment marker.")
        while pos < n and data[pos] == 0xFF:
            pos += 1
        if pos >= n:
            raise ImageMetaError("Damaged JPEG: truncated.")
        marker = data[pos]
        marker_start = pos - 1
        pos += 1
        if marker == 0xD9:  # EOI
            break
        if marker == 0x01 or 0xD0 <= marker <= 0xD7:  # standalone
            out.append((marker, marker_start, pos))
            continue
        if pos + 2 > n:
            raise ImageMetaError("Damaged JPEG: truncated segment.")
        length = struct.unpack(">H", data[pos : pos + 2])[0]
        end = pos + length
        if length < 2 or end > n:
            raise ImageMetaError("Damaged JPEG: bad segment length.")
        out.append((marker, marker_start, end))
        if marker == 0xDA:  # SOS: scan data follows
            break
        pos = end
    return out


def _jpeg_payload(data: bytes, seg: tuple[int, int, int]) -> bytes:
    marker, start, end = seg
    return data[start + 4 : end]


def _jpeg_is_xmp(data: bytes, seg: tuple[int, int, int]) -> bool:
    return seg[0] == 0xE1 and _jpeg_payload(data, seg).startswith(XMP_JPEG_HEADER)


def _jpeg_is_extended_xmp(data: bytes, seg: tuple[int, int, int]) -> bool:
    return seg[0] == 0xE1 and _jpeg_payload(data, seg).startswith(XMP_JPEG_EXTENSION_HEADER)


def _jpeg_get_xmp(data: bytes) -> bytes | None:
    for seg in _jpeg_segments(data):
        if _jpeg_is_xmp(data, seg):
            return _jpeg_payload(data, seg)[len(XMP_JPEG_HEADER) :]
    return None


def _jpeg_get_exif(data: bytes) -> bytes | None:
    for seg in _jpeg_segments(data):
        if seg[0] == 0xE1:
            payload = _jpeg_payload(data, seg)
            if payload.startswith(b"Exif\x00\x00"):
                return payload[6:]
    return None


def _jpeg_set_xmp(data: bytes, packet: bytes | None) -> bytes:
    segs = _jpeg_segments(data)
    if any(_jpeg_is_extended_xmp(data, s) for s in segs):
        raise ImageMetaError(
            "This JPEG has 'extended XMP' (more than 64 KB of XMP); editing it is not supported, "
            "so it was left untouched."
        )
    if packet is not None and len(packet) > MAX_JPEG_XMP:
        raise ImageMetaError("The metadata is too large to fit in a JPEG (64 KB limit).")
    # Cut out the existing XMP segment(s).
    pieces: list[bytes] = [data[:2]]
    cursor = 2
    for seg in segs:
        if _jpeg_is_xmp(data, seg):
            pieces.append(data[cursor : seg[1]])
            cursor = seg[2]
    pieces.append(data[cursor:])
    stripped = b"".join(pieces)
    if packet is None:
        return stripped
    # Exif must stay first (after JFIF): insert after the leading APP0 / Exif APP1 segments.
    pos = 2
    for seg in _jpeg_segments(stripped):
        marker, start, end = seg
        if start == pos and (
            marker == 0xE0 or (marker == 0xE1 and _jpeg_payload(stripped, seg).startswith(b"Exif\x00\x00"))
        ):
            pos = end
        else:
            break
    body = XMP_JPEG_HEADER + packet
    segment = b"\xff\xe1" + struct.pack(">H", len(body) + 2) + body
    return stripped[:pos] + segment + stripped[pos:]


# --- PNG ----------------------------------------------------------------------


def _png_chunks(data: bytes) -> list[tuple[bytes, int, int]]:
    """(type, start, end) of every chunk; `start` is the length field."""
    out: list[tuple[bytes, int, int]] = []
    pos = 8
    n = len(data)
    while pos < n:
        if pos + 12 > n:
            raise ImageMetaError("Damaged PNG: truncated chunk.")
        length = struct.unpack(">I", data[pos : pos + 4])[0]
        ctype = data[pos + 4 : pos + 8]
        end = pos + 12 + length
        if end > n:
            raise ImageMetaError("Damaged PNG: bad chunk length.")
        out.append((ctype, pos, end))
        pos = end
        if ctype == b"IEND":
            break
    return out


def _png_make_chunk(ctype: bytes, payload: bytes) -> bytes:
    crc = zlib.crc32(ctype + payload) & 0xFFFFFFFF
    return struct.pack(">I", len(payload)) + ctype + payload + struct.pack(">I", crc)


def _png_itxt(data: bytes, chunk: tuple[bytes, int, int]) -> tuple[bytes, bytes] | None:
    """(keyword, text bytes) of an iTXt chunk, decompressed if need be."""
    payload = data[chunk[1] + 8 : chunk[2] - 4]
    nul = payload.find(b"\x00")
    if nul < 0 or len(payload) < nul + 3:
        return None
    keyword = payload[:nul]
    flag = payload[nul + 1]
    rest = payload[nul + 3 :]  # skip compression flag + method
    for _ in range(2):  # language tag, translated keyword
        i = rest.find(b"\x00")
        if i < 0:
            return None
        rest = rest[i + 1 :]
    if flag:
        try:
            rest = zlib.decompress(rest)
        except zlib.error:
            return None
    return keyword, rest


def _png_is_xmp(data: bytes, chunk: tuple[bytes, int, int]) -> bool:
    if chunk[0] != b"iTXt":
        return False
    payload = data[chunk[1] + 8 : chunk[2] - 4]
    return payload.startswith(XMP_PNG_KEYWORD + b"\x00")


def _png_get_xmp(data: bytes) -> bytes | None:
    for chunk in _png_chunks(data):
        if _png_is_xmp(data, chunk):
            parsed = _png_itxt(data, chunk)
            if parsed:
                return parsed[1]
    return None


def _png_get_exif(data: bytes) -> bytes | None:
    for chunk in _png_chunks(data):
        if chunk[0] == b"eXIf":
            return data[chunk[1] + 8 : chunk[2] - 4]
    return None


def _png_set_xmp(data: bytes, packet: bytes | None) -> bytes:
    chunks = _png_chunks(data)
    out = [data[:8]]
    inserted = packet is None
    new_chunk = (
        _png_make_chunk(b"iTXt", XMP_PNG_KEYWORD + b"\x00\x00\x00\x00\x00" + packet) if packet is not None else b""
    )
    for chunk in chunks:
        if _png_is_xmp(data, chunk):
            continue
        if not inserted and chunk[0] in (b"IDAT", b"IEND"):
            out.append(new_chunk)
            inserted = True
        out.append(data[chunk[1] : chunk[2]])
    tail_start = chunks[-1][2] if chunks else 8
    out.append(data[tail_start:])
    return b"".join(out)


# --- WebP ---------------------------------------------------------------------


def _webp_chunks(data: bytes) -> list[tuple[bytes, bytes]]:
    """(fourcc, payload) of every top-level RIFF chunk."""
    if len(data) < 12:
        raise ImageMetaError("Damaged WebP: truncated.")
    out: list[tuple[bytes, bytes]] = []
    pos = 12
    n = len(data)
    while pos + 8 <= n:
        fourcc = data[pos : pos + 4]
        size = struct.unpack("<I", data[pos + 4 : pos + 8])[0]
        end = pos + 8 + size
        if end > n:
            raise ImageMetaError("Damaged WebP: bad chunk length.")
        out.append((fourcc, data[pos + 8 : end]))
        pos = end + (size & 1)
    return out


def _webp_build(chunks: list[tuple[bytes, bytes]]) -> bytes:
    body = b"WEBP"
    for fourcc, payload in chunks:
        body += fourcc + struct.pack("<I", len(payload)) + payload + (b"\x00" if len(payload) & 1 else b"")
    return b"RIFF" + struct.pack("<I", len(body)) + body


def _webp_canvas(chunks: list[tuple[bytes, bytes]]) -> tuple[int, int, bool]:
    """(width, height, has_alpha) of a simple (no VP8X) WebP."""
    for fourcc, payload in chunks:
        if fourcc == b"VP8L" and len(payload) >= 5 and payload[0] == 0x2F:
            bits = int.from_bytes(payload[1:5], "little")
            return (bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1, bool((bits >> 28) & 1)
        if fourcc == b"VP8 " and len(payload) >= 10 and payload[3:6] == b"\x9d\x01\x2a":
            return (
                int.from_bytes(payload[6:8], "little") & 0x3FFF,
                int.from_bytes(payload[8:10], "little") & 0x3FFF,
                False,
            )
    raise ImageMetaError("Damaged WebP: no image data.")


def _webp_get_xmp(data: bytes) -> bytes | None:
    for fourcc, payload in _webp_chunks(data):
        if fourcc == b"XMP ":
            return payload
    return None


def _webp_get_exif(data: bytes) -> bytes | None:
    for fourcc, payload in _webp_chunks(data):
        if fourcc == b"EXIF":
            return payload[6:] if payload.startswith(b"Exif\x00\x00") else payload
    return None


def _webp_set_xmp(data: bytes, packet: bytes | None) -> bytes:
    chunks = [c for c in _webp_chunks(data) if c[0] != b"XMP "]
    has_vp8x = bool(chunks) and chunks[0][0] == b"VP8X"
    if packet is not None:
        if not has_vp8x:
            width, height, alpha = _webp_canvas(chunks)
            flags = 0x04 | (0x10 if alpha else 0)
            vp8x = bytes([flags, 0, 0, 0]) + (width - 1).to_bytes(3, "little") + (height - 1).to_bytes(3, "little")
            chunks.insert(0, (b"VP8X", vp8x))
        else:
            payload = bytearray(chunks[0][1])
            payload[0] |= 0x04
            chunks[0] = (b"VP8X", bytes(payload))
        chunks.append((b"XMP ", packet))
    elif has_vp8x:
        payload = bytearray(chunks[0][1])
        payload[0] &= ~0x04 & 0xFF
        chunks[0] = (b"VP8X", bytes(payload))
    return _webp_build(chunks)


def _webp_signature(data: bytes) -> bytes:
    """The image-bearing chunks only (not VP8X, not XMP)."""
    return b"".join(
        f + struct.pack("<I", len(p)) + p for f, p in _webp_chunks(data) if f not in (b"VP8X", b"XMP ")
    )


# --- TIFF (also the container of EXIF) -----------------------------------------

_TIFF_TYPE_SIZE = {1: 1, 2: 1, 3: 2, 4: 4, 5: 8, 6: 1, 7: 1, 8: 2, 9: 4, 10: 8, 11: 4, 12: 8}
_TAG_XMP = 700


def _tiff_endian(data: bytes) -> str:
    if data[:2] == b"II":
        return "<"
    if data[:2] == b"MM":
        return ">"
    raise ImageMetaError("Damaged TIFF: bad byte order.")


def _tiff_ifd(data: bytes, offset: int, endian: str) -> tuple[list[tuple[int, int, int, bytes]], int]:
    """Entries (tag, type, count, 4-byte value field) and the next-IFD offset."""
    if offset < 8 or offset + 2 > len(data):
        raise ImageMetaError("Damaged TIFF: bad directory offset.")
    (count,) = struct.unpack(endian + "H", data[offset : offset + 2])
    end = offset + 2 + 12 * count
    if end + 4 > len(data):
        raise ImageMetaError("Damaged TIFF: truncated directory.")
    entries = []
    for i in range(count):
        e = offset + 2 + 12 * i
        tag, typ, cnt = struct.unpack(endian + "HHI", data[e : e + 8])
        entries.append((tag, typ, cnt, data[e + 8 : e + 12]))
    (nxt,) = struct.unpack(endian + "I", data[end : end + 4])
    return entries, nxt


def _tiff_value_bytes(data: bytes, entry: tuple[int, int, int, bytes], endian: str) -> bytes | None:
    _tag, typ, cnt, field = entry
    size = _TIFF_TYPE_SIZE.get(typ)
    if size is None:
        return None
    total = size * cnt
    if total <= 4:
        return field[:total]
    (off,) = struct.unpack(endian + "I", field)
    if off + total > len(data):
        return None
    return data[off : off + total]


def _tiff_get_xmp(data: bytes) -> bytes | None:
    endian = _tiff_endian(data)
    (ifd0,) = struct.unpack(endian + "I", data[4:8])
    entries, _ = _tiff_ifd(data, ifd0, endian)
    for entry in entries:
        if entry[0] == _TAG_XMP:
            return _tiff_value_bytes(data, entry, endian)
    return None


def _tiff_xmp_region(data: bytes) -> tuple[int, int] | None:
    """(offset, length) of the XMP packet's bytes, or None when absent or inline."""
    endian = _tiff_endian(data)
    (ifd0,) = struct.unpack(endian + "I", data[4:8])
    entries, _ = _tiff_ifd(data, ifd0, endian)
    for tag, typ, cnt, field in entries:
        if tag == _TAG_XMP and typ in (1, 7) and cnt > 4:
            (off,) = struct.unpack(endian + "I", field)
            if off + cnt <= len(data):
                return off, cnt
    return None


def _tiff_blank_xmp(data: bytes) -> bytes:
    """`data` with the XMP packet's bytes zeroed (so a stale copy never lingers)."""
    region = _tiff_xmp_region(data)
    if region is None:
        return data
    off, cnt = region
    return data[:off] + b"\x00" * cnt + data[off + cnt :]


def _tiff_set_xmp(data: bytes, packet: bytes | None) -> bytes:
    endian = _tiff_endian(data)
    (ifd0,) = struct.unpack(endian + "I", data[4:8])
    entries, nxt = _tiff_ifd(data, ifd0, endian)
    kept = [e for e in entries if e[0] != _TAG_XMP]
    if packet is None and len(kept) == len(entries):
        return data
    out = bytearray(_tiff_blank_xmp(data))
    if len(out) & 1:
        out.append(0)
    if packet is not None:
        packet_offset = len(out)
        out += packet
        if len(out) & 1:
            out.append(0)
        kept.append((_TAG_XMP, 1, len(packet), struct.pack(endian + "I", packet_offset)))
    kept.sort(key=lambda e: e[0])
    new_ifd = len(out)
    out += struct.pack(endian + "H", len(kept))
    for tag, typ, cnt, field in kept:
        out += struct.pack(endian + "HHI", tag, typ, cnt) + field
    out += struct.pack(endian + "I", nxt)
    out[4:8] = struct.pack(endian + "I", new_ifd)
    return bytes(out)


# --- EXIF reading --------------------------------------------------------------


def _exif_ascii(data: bytes, entry: tuple[int, int, int, bytes], endian: str) -> str | None:
    raw = _tiff_value_bytes(data, entry, endian)
    if raw is None:
        return None
    text = raw.split(b"\x00", 1)[0].decode("utf-8", "replace").strip()
    return text or None


def _exif_rationals(data: bytes, entry: tuple[int, int, int, bytes], endian: str) -> list[float] | None:
    raw = _tiff_value_bytes(data, entry, endian)
    if raw is None or entry[1] != 5 or len(raw) % 8:
        return None
    out = []
    for i in range(0, len(raw), 8):
        num, den = struct.unpack(endian + "II", raw[i : i + 8])
        out.append(num / den if den else 0.0)
    return out


def _exif_sub_ifd(data: bytes, entries: list[tuple[int, int, int, bytes]], tag: int, endian: str):
    for entry in entries:
        if entry[0] == tag and entry[1] in (4, 13) and entry[2] == 1:
            (off,) = struct.unpack(endian + "I", entry[3])
            try:
                return _tiff_ifd(data, off, endian)[0]
            except ImageMetaError:
                return None
    return None


def parse_exif(blob: bytes) -> dict[str, Any]:
    """The few camera/EXIF facts the apps show. `blob` is a TIFF structure (the
    payload of a JPEG's Exif segment, a PNG eXIf chunk, ...). Keys present only
    when found: make, model, description, artist, copyright, date (ISO 8601,
    no zone), gps ((latitude, longitude) in decimal degrees)."""
    out: dict[str, Any] = {}
    try:
        endian = _tiff_endian(blob)
        (ifd0,) = struct.unpack(endian + "I", blob[4:8])
        entries, _ = _tiff_ifd(blob, ifd0, endian)
    except (ImageMetaError, struct.error):
        return out
    text_tags = {0x010F: "make", 0x0110: "model", 0x010E: "description", 0x013B: "artist", 0x8298: "copyright"}
    for entry in entries:
        key = text_tags.get(entry[0])
        if key and entry[1] == 2:
            value = _exif_ascii(blob, entry, endian)
            if value:
                out[key] = value
    exif = _exif_sub_ifd(blob, entries, 0x8769, endian)
    if exif:
        for entry in exif:
            if entry[0] in (0x9003, 0x9004) and entry[1] == 2:  # DateTimeOriginal / Digitized
                value = _exif_ascii(blob, entry, endian)
                m = re.match(r"^(\d{4}):(\d{2}):(\d{2}) (\d{2}):(\d{2}):(\d{2})$", value or "")
                if m and m.group(1) != "0000":
                    out["date"] = "{}-{}-{}T{}:{}:{}".format(*m.groups())
                    if entry[0] == 0x9003:
                        break
    gps = _exif_sub_ifd(blob, entries, 0x8825, endian)
    if gps:
        by_tag = {e[0]: e for e in gps}
        try:
            lat = _exif_rationals(blob, by_tag[2], endian)
            lon = _exif_rationals(blob, by_tag[4], endian)
            lat_ref = _exif_ascii(blob, by_tag[1], endian)
            lon_ref = _exif_ascii(blob, by_tag[3], endian)
            if lat and lon and len(lat) == 3 and len(lon) == 3 and lat_ref and lon_ref:
                lat_d = lat[0] + lat[1] / 60 + lat[2] / 3600
                lon_d = lon[0] + lon[1] / 60 + lon[2] / 3600
                out["gps"] = (-lat_d if lat_ref.upper() == "S" else lat_d, -lon_d if lon_ref.upper() == "W" else lon_d)
        except KeyError:
            pass
    return out


# --- format dispatch -------------------------------------------------------------

_GET_XMP: dict[str, Callable[[bytes], bytes | None]] = {
    FORMAT_JPEG: _jpeg_get_xmp,
    FORMAT_PNG: _png_get_xmp,
    FORMAT_WEBP: _webp_get_xmp,
    FORMAT_TIFF: _tiff_get_xmp,
}
_SET_XMP: dict[str, Callable[[bytes, bytes | None], bytes]] = {
    FORMAT_JPEG: _jpeg_set_xmp,
    FORMAT_PNG: _png_set_xmp,
    FORMAT_WEBP: _webp_set_xmp,
    FORMAT_TIFF: _tiff_set_xmp,
}


def _get_exif_blob(data: bytes, fmt: str) -> bytes | None:
    if fmt == FORMAT_JPEG:
        return _jpeg_get_exif(data)
    if fmt == FORMAT_PNG:
        return _png_get_exif(data)
    if fmt == FORMAT_WEBP:
        return _webp_get_exif(data)
    return data  # TIFF is itself the EXIF container


def _payload_signature(data: bytes, fmt: str, original_len: int | None = None) -> bytes:
    """What must be identical before and after a metadata write: everything
    except the XMP packet and the bookkeeping that points at it."""
    if fmt == FORMAT_WEBP:
        return _webp_signature(data)
    if fmt == FORMAT_TIFF:
        # Only appended to (the old XMP bytes are blanked); IFD0's offset (bytes 4-8) is the one allowed change.
        blanked = _tiff_blank_xmp(data)
        return blanked[:4] + blanked[8:original_len] if original_len is not None else blanked[:4] + blanked[8:]
    return _SET_XMP[fmt](data, None)


# --- XMP packet model ----------------------------------------------------------


def _q(ns: str, local: str) -> str:
    return "{%s}%s" % (ns, local)


def _parse_packet(packet: bytes) -> ET.Element:
    """The x:xmpmeta element of an XMP packet. Refuses DTDs/entities (a
    hostile file must not be able to expand them)."""
    if b"<!DOCTYPE" in packet or b"<!ENTITY" in packet:
        raise ImageMetaError("The file's XMP contains a DTD, which is not allowed.")
    start = packet.find(b"<x:xmpmeta")
    if start < 0:
        start = packet.find(b"<xmpmeta")
    end = packet.rfind(b"</x:xmpmeta>")
    if start < 0 or end < 0:
        # Some writers omit the wrapper: accept a bare rdf:RDF.
        start = packet.find(b"<rdf:RDF")
        end = packet.rfind(b"</rdf:RDF>")
        if start < 0 or end < 0:
            raise ImageMetaError("The file's XMP packet is not recognisable.")
        end += len(b"</rdf:RDF>")
        try:
            rdf = ET.fromstring(packet[start:end])
        except ET.ParseError as exc:
            raise ImageMetaError(f"The file's XMP cannot be parsed: {exc}") from exc
        root = ET.Element(_q(NS_X, "xmpmeta"))
        root.append(rdf)
        return root
    end += len(b"</x:xmpmeta>")
    try:
        return ET.fromstring(packet[start:end])
    except ET.ParseError as exc:
        raise ImageMetaError(f"The file's XMP cannot be parsed: {exc}") from exc


def _new_root() -> ET.Element:
    root = ET.Element(_q(NS_X, "xmpmeta"))
    rdf = ET.SubElement(root, _q(NS_RDF, "RDF"))
    desc = ET.SubElement(rdf, _q(NS_RDF, "Description"))
    desc.set(_q(NS_RDF, "about"), "")
    return root


def _descriptions(root: ET.Element) -> list[ET.Element]:
    return list(root.iter(_q(NS_RDF, "Description")))


def _li_texts(container: ET.Element | None) -> list[str]:
    if container is None:
        return []
    return [(li.text or "").strip() for li in container.findall(_q(NS_RDF, "li")) if (li.text or "").strip()]


def _read_property(root: ET.Element, spec: FieldSpec) -> Any:
    key = _q(spec.ns, spec.local)
    for desc in _descriptions(root):
        value: Any = None
        if key in desc.attrib:  # compact form: a plain attribute
            text = desc.attrib[key].strip()
            if text:
                value = [text] if spec.kind in (KIND_BAG, KIND_SEQ) else text
        else:
            elem = desc.find(key)
            if elem is not None:
                if spec.kind == KIND_LANGALT:
                    alt = elem.find(_q(NS_RDF, "Alt"))
                    items = alt.findall(_q(NS_RDF, "li")) if alt is not None else []
                    chosen = next((li for li in items if li.get(_q(NS_XML, "lang")) == "x-default"), None)
                    if chosen is None and items:
                        chosen = items[0]
                    text = (chosen.text or "").strip() if chosen is not None else (elem.text or "").strip()
                    value = text or None
                elif spec.kind in (KIND_BAG, KIND_SEQ):
                    container = elem.find(_q(NS_RDF, "Bag" if spec.kind == KIND_BAG else "Seq"))
                    if container is None:  # tolerate either container kind
                        container = elem.find(_q(NS_RDF, "Seq"))
                    if container is None:
                        container = elem.find(_q(NS_RDF, "Bag"))
                    items = _li_texts(container)
                    value = items or None
                else:
                    text = (elem.text or "").strip()
                    if not text:  # some writers wrap a plain value in a one-item Alt
                        li = elem.find(_q(NS_RDF, "Alt") + "/" + _q(NS_RDF, "li"))
                        text = (li.text or "").strip() if li is not None else ""
                    value = text or None
        if value is not None:
            if spec.kind == KIND_INT:
                try:
                    return int(float(value))
                except ValueError:
                    continue
            return value
    return None


def _remove_property(root: ET.Element, spec: FieldSpec) -> None:
    key = _q(spec.ns, spec.local)
    for desc in _descriptions(root):
        desc.attrib.pop(key, None)
        for elem in desc.findall(key):
            desc.remove(elem)


def _write_property(root: ET.Element, spec: FieldSpec, value: Any) -> None:
    _remove_property(root, spec)
    descs = _descriptions(root)
    if not descs:
        rdf = root.find(_q(NS_RDF, "RDF"))
        if rdf is None:
            rdf = ET.SubElement(root, _q(NS_RDF, "RDF"))
        desc = ET.SubElement(rdf, _q(NS_RDF, "Description"))
        desc.set(_q(NS_RDF, "about"), "")
    else:
        desc = descs[0]
    elem = ET.SubElement(desc, _q(spec.ns, spec.local))
    if spec.kind == KIND_LANGALT:
        alt = ET.SubElement(elem, _q(NS_RDF, "Alt"))
        li = ET.SubElement(alt, _q(NS_RDF, "li"))
        li.set(_q(NS_XML, "lang"), "x-default")
        li.text = value
    elif spec.kind in (KIND_BAG, KIND_SEQ):
        container = ET.SubElement(elem, _q(NS_RDF, "Bag" if spec.kind == KIND_BAG else "Seq"))
        for item in value:
            ET.SubElement(container, _q(NS_RDF, "li")).text = item
    else:
        elem.text = str(value)


def _serialize(root: ET.Element) -> bytes:
    ET.indent(root, space=" ")
    body = ET.tostring(root, encoding="utf-8")
    return b'<?xpacket begin="\xef\xbb\xbf" id="W5M0MpCehiHzreSzNTczkc9d"?>\n' + body + b'\n<?xpacket end="w"?>'


def _normalise(key: str, value: Any) -> Any:
    """Cleaned value for `key`, or None meaning "remove the field"."""
    spec = FIELDS[key]
    if value is None:
        return None
    if spec.kind in (KIND_BAG, KIND_SEQ):
        items = [value] if isinstance(value, str) else list(value)
        seen: list[str] = []
        for item in items:
            text = str(item).strip()
            if text and text not in seen:
                seen.append(text)
        return seen or None
    if spec.kind == KIND_INT:
        if isinstance(value, str) and not value.strip():
            return None
        try:
            number = int(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{key} must be a whole number 0-5") from exc
        if not 0 <= number <= 5:
            raise ValueError(f"{key} must be between 0 and 5")
        return number
    text = str(value).strip()
    if not text:
        return None
    if key == "date" and not _DATE_RE.match(text):
        raise ValueError("date must be ISO 8601 such as 1912, 1912-04, 1912-04-15 or 1912-04-15T09:30:00")
    return text


# --- public API on bytes ------------------------------------------------------


def read_meta_bytes(data: bytes) -> dict[str, Any]:
    """The managed fields present in the image `data` (absent fields are not in
    the dict). `date` falls back to xmp:CreateDate when DateCreated is absent."""
    fmt = detect_format(data)
    packet = _GET_XMP[fmt](data)
    if not packet:
        return {}
    try:
        root = _parse_packet(packet)
    except ImageMetaError:
        return {}
    out: dict[str, Any] = {}
    for key, spec in FIELDS.items():
        value = _read_property(root, spec)
        if value is not None:
            out[key] = value
    if "date" not in out:
        fallback = _read_property(root, FieldSpec(NS_XMP, "CreateDate", KIND_TEXT))
        if fallback:
            out["date"] = fallback
    return out


def read_exif_bytes(data: bytes) -> dict[str, Any]:
    """Camera facts from the EXIF of `data` (see parse_exif); {} when none."""
    fmt = detect_format(data)
    blob = _get_exif_blob(data, fmt)
    return parse_exif(blob) if blob else {}


def write_meta_bytes(data: bytes, fields: Mapping[str, Any]) -> bytes:
    """`data` with the given fields written into its XMP. Only the fields named
    in `fields` change: a value of None, "" or [] removes that field. Raises
    ValueError for an unknown or invalid field, ImageMetaError when the file
    cannot be edited safely."""
    unknown = [k for k in fields if k not in FIELDS]
    if unknown:
        raise ValueError("unknown metadata field(s): " + ", ".join(sorted(unknown)))
    cleaned = {k: _normalise(k, v) for k, v in fields.items()}
    fmt = detect_format(data)
    existing = _GET_XMP[fmt](data)
    if existing:
        root = _parse_packet(existing)  # raises rather than overwrite what we cannot read
    else:
        if all(v is None for v in cleaned.values()):
            return data
        root = _new_root()
    for key, value in cleaned.items():
        if value is None:
            _remove_property(root, FIELDS[key])
        else:
            _write_property(root, FIELDS[key], value)
    if _is_empty(root):  # nothing left worth keeping: drop the packet entirely
        return _SET_XMP[fmt](data, None)
    return _SET_XMP[fmt](data, _serialize(root))


def _is_empty(root: ET.Element) -> bool:
    for desc in _descriptions(root):
        if len(desc) or any(k != _q(NS_RDF, "about") or v for k, v in desc.attrib.items()):
            return False
    return True


def copy_meta_bytes(src: bytes, dst: bytes) -> bytes:
    """`dst` carrying `src`'s XMP packet unchanged (for a resized or converted
    copy of an image). The formats may differ. EXIF is not carried over."""
    packet = _GET_XMP[detect_format(src)](src)
    return _SET_XMP[detect_format(dst)](dst, packet)


def _verify(old: bytes, new: bytes, expected: Mapping[str, Any]) -> tuple[bool, str]:
    fmt = detect_format(new)
    if fmt != detect_format(old):
        return False, "the image format changed"
    if _payload_signature(old, fmt) != _payload_signature(new, fmt, len(old)):
        return False, "the image data would have changed"
    got = read_meta_bytes(new)
    for key, value in expected.items():
        want = _normalise(key, value)
        if want is None:
            if key in got and key != "date":
                return False, f"{key} was not removed"
        elif got.get(key) != want:
            return False, f"{key} did not read back as written"
    return True, ""


# --- public API on files --------------------------------------------------------


def _read(path: str) -> bytes:
    try:
        with open(path, "rb") as handle:
            return handle.read()
    except OSError as exc:
        raise ImageMetaError(f"Cannot read {os.path.basename(path)}: {exc.strerror or exc}") from exc


def read_meta(path: str) -> dict[str, Any]:
    """The managed metadata fields embedded in the image file at `path`."""
    return read_meta_bytes(_read(path))


def read_exif(path: str) -> dict[str, Any]:
    """Camera facts embedded in the image file at `path` (read-only)."""
    return read_exif_bytes(_read(path))


def _swap_in(path: str, old: bytes, new: bytes, expected: Mapping[str, Any], trash) -> CommitResult | None:
    if new == old:
        return None
    folder, name = os.path.split(os.path.abspath(path))
    stem, ext = os.path.splitext(name)
    temp = os.path.join(folder, f".{stem}.redact-{uuid.uuid4().hex[:8]}{ext}")
    try:
        with open(temp, "wb") as handle:
            handle.write(new)
        return commit_in_place(path, temp, trash=trash, verify=lambda p: _verify(old, _read(p), expected))
    finally:
        if os.path.exists(temp):
            try:
                os.remove(temp)
            except OSError:
                pass


def write_meta(
    path: str,
    fields: Mapping[str, Any],
    *,
    trash: Callable[[str], None] = move_to_trash,
) -> CommitResult | None:
    """Write `fields` into the image at `path`, all-or-nothing (see the module
    docstring). Returns the CommitResult, or None when nothing needed to
    change. Raises ValueError for a bad field, ImageMetaError for a file that
    cannot be edited safely, CommitError when the swap-in failed (the
    original is intact)."""
    old = _read(path)
    new = write_meta_bytes(old, fields)
    return _swap_in(path, old, new, fields, trash)


def copy_meta(
    src: str,
    dst: str,
    *,
    trash: Callable[[str], None] = move_to_trash,
) -> CommitResult | None:
    """Copy the XMP of image `src` into image `dst`, e.g. after a resize or a
    format conversion has produced `dst` without it."""
    src_data = _read(src)
    old = _read(dst)
    new = copy_meta_bytes(src_data, old)
    return _swap_in(dst, old, new, read_meta_bytes(src_data), trash)
