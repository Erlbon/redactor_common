"""
redactor_common/core/comicinfo_write.py

The ONE way the apps write ComicInfo.xml (cbzredactor's Save and Remove
Pages, Dossier's tag editor). Before 2026-11-04 cbzredactor and Dossier each
had their own writer, and the same edit gave two different files (the code
review of that day, section 6, lists fifteen differences). What every
ComicInfo.xml written here looks like:

- an XML declaration with encoding="utf-8", two-space indentation of the
  top-level elements and a final newline; elements this module doesn't know
  (<Pages>, a newer schema draft's fields) keep their own inner whitespace;
- the root declares the xsi and xsd namespaces, as ComicRack wrote them,
  and keeps any other attribute it had (xsi:noNamespaceSchemaLocation);
- the known elements in the schema's order (Anansi Project v2.0 plus the
  v2.1 draft's Translator, Tags, StoryArcNumber and GTIN), unknown elements
  where <Pages> sits (before CommunityRating), in their own order, and
  cbzredactor's <RedactorScan> stamp last;
- values stripped of surrounding spaces; an empty known element is dropped
  (the way to clear a field); PageCount refreshed when the caller gives the
  archive's page count;
- a value that XML 1.0 cannot hold (a control character) is refused, never
  written into a file nobody can parse.

Parsing uses the standard library's ElementTree (expat: no external
entities, no network), so Dossier needs no lxml. Multiple values (Writer,
Genre, Tags...) are one text joined with LIST_SEPARATOR, the convention
ComicTagger and the apps' editors share.

write_comicinfo_into_cbz() is the whole job for a caller that has only a
path and a dict of edits; comicinfo_rewrite_plan() is the RewritePlan for a
caller that runs core/zip_rewrite.rewrite_archive() itself.
"""

from __future__ import annotations

import posixpath
import re
import zipfile
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from typing import Callable, Iterable, Optional

from redactor_common.cli import CliError
from redactor_common.cli.values import check_text, is_ascii_number
from redactor_common.core.zip_names import open_zip
from redactor_common.core.zip_rewrite import Action, NewEntry, RewritePlan, now_date_time, rewrite_archive

ROOT_TAG = "ComicInfo"
COMICINFO_NAME = "ComicInfo.xml"
XSI_NS = "http://www.w3.org/2001/XMLSchema-instance"
XSD_NS = "http://www.w3.org/2001/XMLSchema"
LIST_SEPARATOR = ", "
SCAN_STAMP_TAG = "RedactorScan"  # cbzredactor's validation-scan stamp: written last
MAX_COMICINFO_BYTES = 2 * 1024 * 1024
MAX_VALUE = 20_000

FIELDS_BEFORE_EXTRAS = (
    "Title", "Series", "Number", "Count", "Volume",
    "AlternateSeries", "AlternateNumber", "AlternateCount",
    "Summary", "Notes",
    "Year", "Month", "Day",
    "Writer", "Penciller", "Inker", "Colorist", "Letterer",
    "CoverArtist", "Editor", "Translator",
    "Publisher", "Imprint",
    "Genre", "Tags", "Web", "PageCount", "LanguageISO", "Format",
    "BlackAndWhite", "Manga",
    "Characters", "Teams", "Locations",
    "ScanInformation", "StoryArc", "StoryArcNumber", "SeriesGroup",
    "AgeRating",
)
FIELDS_AFTER_EXTRAS = ("CommunityRating", "MainCharacterOrTeam", "Review", "GTIN")
FIELDS = FIELDS_BEFORE_EXTRAS + FIELDS_AFTER_EXTRAS
# The schema's element order, <Pages> included (unknown elements go where it is).
COMICINFO_ORDER = FIELDS_BEFORE_EXTRAS + ("Pages",) + FIELDS_AFTER_EXTRAS

INT_FIELDS = ("Count", "Volume", "AlternateCount", "Year", "Month", "Day", "PageCount")
MULTILINE_FIELDS = ("Summary", "Notes", "Review", "ScanInformation")
BLACK_AND_WHITE_VALUES = ("Unknown", "No", "Yes")
MANGA_VALUES = ("Unknown", "No", "Yes", "YesAndRightToLeft")
AGE_RATING_VALUES = (
    "Unknown", "Adults Only 18+", "Early Childhood", "Everyone",
    "Everyone 10+", "G", "Kids to Adults", "M", "MA15+", "Mature 17+",
    "PG", "R18+", "Rating Pending", "Teen", "X18+",
)
_ENUMS = {"BlackAndWhite": BLACK_AND_WHITE_VALUES, "Manga": MANGA_VALUES, "AgeRating": AGE_RATING_VALUES}

IMAGE_EXTENSIONS = (".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp")

# Characters XML 1.0 cannot hold at all (tab, line feed and carriage return can).
_XML_ILLEGAL = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f￾￿\ud800-\udfff]")


class ComicInfoError(ValueError):
    """A ComicInfo.xml that can't be read, or a value that can't be written."""


# --- finding it --------------------------------------------------------------


@dataclass(frozen=True)
class ComicInfoLocation:
    name: str  # the entry that is read
    nested: bool  # it is not at the archive root
    others: tuple = field(default_factory=tuple)  # further nested copies that were passed over


def _is_candidate(name: str) -> bool:
    """A real ComicInfo.xml entry: right file name, not a folder entry, not
    inside macOS's __MACOSX resource-fork folder."""
    if name.endswith("/") or posixpath.basename(name).lower() != COMICINFO_NAME.lower():
        return False
    lowered = name.lower()
    return not (lowered.startswith("__macosx/") or "/__macosx/" in lowered)


def locate_comicinfo(names: Iterable[str]) -> Optional[ComicInfoLocation]:
    """Which entry is the archive's ComicInfo.xml. One at the root (any
    letter case) always wins; with none there, the shallowest nested one
    (then alphabetical order), the others reported as passed over."""
    nested = []
    for name in names:
        if not _is_candidate(name):
            continue
        if "/" not in name:
            return ComicInfoLocation(name, False)
        nested.append(name)
    if not nested:
        return None
    nested.sort(key=lambda n: (n.count("/"), n.casefold(), n))
    return ComicInfoLocation(nested[0], True, tuple(nested[1:]))


def find_comicinfo_member(names: Iterable[str]) -> Optional[str]:
    """The name of the ComicInfo.xml entry to read, or None."""
    found = locate_comicinfo(names)
    return found.name if found else None


def is_page(name: str) -> bool:
    """A page: an image file, but not a macOS "__MACOSX/" or "._name.jpg"
    resource-fork leftover."""
    if posixpath.splitext(name)[1].lower() not in IMAGE_EXTENSIONS:
        return False
    lowered = name.lower()
    return not (lowered.startswith("__macosx/") or "/__macosx/" in lowered
                or posixpath.basename(lowered).startswith("._"))


def count_pages(names: Iterable[str]) -> int:
    return sum(1 for n in names if is_page(n))


# --- reading -----------------------------------------------------------------


def local_name(tag) -> str:
    """An element's tag without its namespace ("" for a comment or PI)."""
    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else ""


def new_comicinfo() -> ET.Element:
    return ET.Element(ROOT_TAG)


def parse_comicinfo(data: bytes) -> ET.Element:
    """ComicInfo.xml bytes as an element tree root. Raises ComicInfoError on
    malformed XML, a DOCTYPE with entities, or another root element."""
    if len(data) > MAX_COMICINFO_BYTES:
        raise ComicInfoError(f"ComicInfo.xml is {len(data):,} bytes, too large to read safely")
    try:
        root = ET.fromstring(data)
    except ET.ParseError as exc:
        raise ComicInfoError(f"Malformed ComicInfo.xml: {exc}") from exc
    if local_name(root.tag) != ROOT_TAG:
        raise ComicInfoError(f"Expected root element <{ROOT_TAG}>, found <{local_name(root.tag)}>")
    return root


def get_value(root: ET.Element, tag: str) -> str:
    """The stripped text of the first `tag` child ("" when it has none)."""
    for child in root:
        if local_name(child.tag) == tag:
            return (child.text or "").strip()
    return ""


def comicinfo_values(root: Optional[ET.Element], tags: Iterable[str] = FIELDS) -> dict:
    """{tag: stripped text} for `tags` ("" where the file has none)."""
    return {tag: get_value(root, tag) if root is not None else "" for tag in tags}


# --- checking and editing ----------------------------------------------------


def normalize_value(value) -> str:
    """A value as it will read back: plain line breaks, no surrounding spaces."""
    return str(value).replace("\r\n", "\n").replace("\r", "\n").strip()


def check_comicinfo_value(tag: str, value: str) -> str:
    """Why `value` cannot be stored in `tag`, or "". Built on the CLI's
    check_text (control characters) and is_ascii_number ("²" is a digit to
    str.isdigit, not to int)."""
    if len(value) > MAX_VALUE:
        return "too long"
    try:
        check_text(tag, value, multiline=tag in MULTILINE_FIELDS)
    except CliError as exc:
        return str(exc)[len(tag):].strip()
    if not value:
        return ""
    if tag in INT_FIELDS:
        if not is_ascii_number(value):
            return "must be a whole number"
        if tag == "Month" and not 1 <= int(value) <= 12:
            return "must be 1 to 12"
        if tag == "Day" and not 1 <= int(value) <= 31:
            return "must be 1 to 31"
    if tag in _ENUMS and value not in _ENUMS[tag]:
        return "must be one of " + ", ".join(_ENUMS[tag])
    return ""


def apply_comicinfo_edits(root: ET.Element, edits: dict) -> None:
    """Sets each `tag: value` of `edits` on `root` (an empty value removes
    the element). Only the tags given are touched. Values are normalized
    (normalize_value); checking them is the caller's job (check_comicinfo_value)."""
    for tag, value in edits.items():
        value = normalize_value(value)
        matches = [c for c in root if local_name(c.tag) == tag]
        for extra in matches[1:]:
            root.remove(extra)
        if not value:
            if matches:
                root.remove(matches[0])
            continue
        node = matches[0] if matches else ET.SubElement(root, tag)
        for child in list(node):
            node.remove(child)
        node.text = value


# --- writing -----------------------------------------------------------------


def _root_attributes(root: ET.Element) -> dict:
    attrib = {"xmlns:xsi": XSI_NS, "xmlns:xsd": XSD_NS}
    for key, value in root.attrib.items():
        if key.startswith("{" + XSI_NS + "}"):
            key = "xsi:" + local_name(key)
        elif key.startswith("{" + XSD_NS + "}"):
            key = "xsd:" + local_name(key)
        elif key.startswith("xmlns"):
            continue
        attrib[key] = value
    return attrib


def serialize_comicinfo(root: ET.Element, page_count: Optional[int] = None) -> bytes:
    """ComicInfo.xml bytes for `root`, in the form the module docstring
    describes. `page_count` (when given) replaces PageCount. Raises
    ComicInfoError for a value XML cannot hold."""
    known: dict = {}
    extras: list = []
    stamp = None
    for child in root:
        tag = local_name(child.tag)
        if not tag:
            continue  # a comment or processing instruction
        if tag == SCAN_STAMP_TAG:
            if stamp is None and (child.text or "").strip():
                stamp = child.text.strip()
        elif tag in FIELDS and len(child) == 0:
            if tag not in known:
                known[tag] = (child.text or "").strip()
        else:
            extras.append(child)
    if page_count is not None:
        known["PageCount"] = str(page_count)

    out = ET.Element(ROOT_TAG, _root_attributes(root))

    def add(tag: str, text: str) -> None:
        bad = _XML_ILLEGAL.search(text)
        if bad:
            raise ComicInfoError(f"{tag} cannot contain the control character U+{ord(bad.group()):04X}")
        ET.SubElement(out, tag).text = text

    for tag in FIELDS_BEFORE_EXTRAS:
        if known.get(tag):
            add(tag, known[tag])
    for element in extras:
        out.append(element)
    for tag in FIELDS_AFTER_EXTRAS:
        if known.get(tag):
            add(tag, known[tag])
    if stamp:
        add(SCAN_STAMP_TAG, stamp)

    children = list(out)
    if children:
        out.text = "\n  "
        for child in children:
            child.tail = "\n  "
        children[-1].tail = "\n"
    body = ET.tostring(out, encoding="unicode")
    return ('<?xml version="1.0" encoding="utf-8"?>\n' + body + "\n").encode("utf-8")


def check_written(xml: bytes, edits: dict) -> Optional[str]:
    """None when `xml` parses and says what `edits` asked for, else the
    first field that does not (by tag)."""
    try:
        back = parse_comicinfo(xml)
    except ComicInfoError as exc:
        return str(exc)
    for tag, value in edits.items():
        if get_value(back, tag) != normalize_value(value):
            return f"{tag} would not read back as written"
    return None


def comicinfo_rewrite_plan(source_name: Optional[str], xml: bytes, decide=None) -> tuple:
    """(RewritePlan, written name) that puts `xml` in the archive as its
    ComicInfo.xml: at the root, under the source entry's own name when that
    was at the root, else as "ComicInfo.xml" (the nested source is dropped,
    so there are never two). Deflated, stamped now, written last, and the
    written entry checked before the swap. `decide` handles every other
    entry (default: keep)."""
    write_name = source_name if source_name and "/" not in source_name else COMICINFO_NAME

    def _decide(entry, read):
        if entry.name == source_name or entry.name == write_name:
            return Action(drop=True)
        return decide(entry, read) if decide is not None else None

    def _verify(zf: zipfile.ZipFile) -> Optional[str]:
        return None if zf.read(write_name) == xml else "ComicInfo.xml did not read back as written"

    plan = RewritePlan(
        decide=_decide,
        after=[NewEntry(write_name, xml, zipfile.ZIP_DEFLATED, now_date_time())],
        verify=_verify,
    )
    return plan, write_name


def read_comicinfo_from_cbz(path: str) -> tuple:
    """(member name or None, parsed root or None, entry names) of the cbz at
    `path`. Raises ComicInfoError for a ComicInfo.xml that can't be read, and
    what zipfile raises for a file that isn't a zip."""
    with open_zip(path) as zf:
        names = zf.namelist()
        member = find_comicinfo_member(names)
        if member is None:
            return None, None, names
        info = zf.getinfo(member)
        if info.file_size > MAX_COMICINFO_BYTES:
            raise ComicInfoError(f"ComicInfo.xml is {info.file_size:,} bytes, too large to change safely")
        return member, parse_comicinfo(zf.read(member)), names


def write_comicinfo_into_cbz(
    path: str,
    edits: dict,
    *,
    refresh_page_count: bool = True,
    dispose_original: Optional[Callable[[str], None]] = None,
    **rewrite_kwargs,
) -> bytes:
    """Applies `edits` ({tag: value}, "" removes) to the ComicInfo.xml of the
    cbz at `path` (a new one when it has none), with PageCount refreshed
    from the archive's pages, and rewrites the archive in place through
    rewrite_archive() (`dispose_original` and the other keyword arguments
    go to it). The new XML is checked before anything is written; a bad
    value raises ComicInfoError and leaves the file as it was. Returns the
    XML written."""
    for tag, value in edits.items():
        why = check_comicinfo_value(tag, normalize_value(value))
        if why:
            raise ComicInfoError(f"{tag} {why}")
    member, root, names = read_comicinfo_from_cbz(path)
    if root is None:
        root = new_comicinfo()
    apply_comicinfo_edits(root, edits)
    xml = serialize_comicinfo(root, count_pages(names) if refresh_page_count else None)
    problem = check_written(xml, edits)
    if problem:
        raise ComicInfoError(problem)
    plan, _name = comicinfo_rewrite_plan(member, xml)
    rewrite_archive(path, path, plan, dispose_original=dispose_original, **rewrite_kwargs)
    return xml
