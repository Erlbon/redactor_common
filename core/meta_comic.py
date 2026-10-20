"""
redactor_common/core/meta_comic.py

Read-only ComicInfo.xml of a CBZ (a zip of page images). Nothing here ever
writes to the archive. Only what is filled in is returned; the page count
and the archive's own facts come from the zip listing.

ComicInfo.xml is looked for at the top of the archive first, then in any
folder (case-insensitive). The file is capped at 2 MB: a real one is a few
kilobytes, and parsing an oversized one only invites trouble.
"""
from __future__ import annotations

import os
import xml.etree.ElementTree as ET
import zipfile

from .meta_fields import GROUP_CREDITS, GROUP_PAGES, GROUP_TAGS, MetaResult, add

MAX_XML = 2 * 1024 * 1024
IMAGE_EXTENSIONS = (".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".tif", ".tiff", ".avif", ".jxl")

TAGS = (("Series", "Series"), ("Number", "Number"), ("Count", "Count"), ("Volume", "Volume"), ("Title", "Title"),
        ("AlternateSeries", "Alternate series"), ("AlternateNumber", "Alternate number"), ("StoryArc", "Story arc"),
        ("SeriesGroup", "Series group"), ("Summary", "Summary"), ("Notes", "Notes"), ("Genre", "Genre"),
        ("Tags", "Tags"), ("Characters", "Characters"), ("Teams", "Teams"), ("Locations", "Locations"),
        ("LanguageISO", "Language"), ("Format", "Format"), ("BlackAndWhite", "Black and white"), ("Manga", "Manga"),
        ("AgeRating", "Age rating"), ("CommunityRating", "Community rating"), ("Web", "Web"),
        ("ScanInformation", "Scan information"), ("Review", "Review"))
CREDITS = (("Writer", "Writer"), ("Penciller", "Penciller"), ("Inker", "Inker"), ("Colorist", "Colorist"),
           ("Letterer", "Letterer"), ("CoverArtist", "Cover artist"), ("Editor", "Editor"), ("Translator", "Translator"),
           ("Publisher", "Publisher"), ("Imprint", "Imprint"))
SNIPPET = 300


def _find_member(names: list) -> str:
    best, depth = "", 1 << 30
    for name in names:
        if name.replace("\\", "/").rsplit("/", 1)[-1].lower() == "comicinfo.xml":
            d = name.replace("\\", "/").count("/")
            if d < depth:
                best, depth = name, d
    return best


def _text(root, tag: str) -> str:
    node = root.find(tag)
    return (node.text or "").strip() if node is not None else ""


def read_comic(path: str) -> MetaResult:
    """ComicInfo and page facts of the CBZ at ``path``. Never raises."""
    result = MetaResult("comic")
    try:
        with zipfile.ZipFile(path) as z:
            infos = z.infolist()
            names = [i.filename for i in infos]
            pages = [i for i in infos if not i.is_dir() and i.filename.lower().endswith(IMAGE_EXTENSIONS)]
            member = _find_member(names)
            root = None
            if member:
                info = z.getinfo(member)
                if info.file_size > MAX_XML:
                    result.error = f"ComicInfo.xml is {info.file_size:,} bytes, too large to read"
                else:
                    try:
                        root = ET.fromstring(z.read(member))
                    except ET.ParseError as e:
                        result.error = f"ComicInfo.xml is not valid XML ({e})"
            if root is not None:
                for tag, label in TAGS:
                    value = _text(root, tag)
                    add(result.fields, tag.lower(), label, value if len(value) <= SNIPPET else value[:SNIPPET - 1] + "…", GROUP_TAGS)
                year, month, day = (_text(root, t) for t in ("Year", "Month", "Day"))
                date = "-".join(p.zfill(2) if i else p for i, p in enumerate([x for x in (year, month, day) if x]))
                add(result.fields, "date", "Date", date, GROUP_TAGS)
                for tag, label in CREDITS:
                    add(result.fields, tag.lower(), label, _text(root, tag), GROUP_CREDITS)
                declared = _text(root, "PageCount")
                add(result.fields, "pagecount", "Pages (ComicInfo)", declared, GROUP_PAGES)
            elif not member and not result.error:
                result.error = "no ComicInfo.xml in this archive"
            add(result.fields, "pages", "Image files", len(pages), GROUP_PAGES)
            add(result.fields, "unpacked", "Unpacked size", f"{sum(i.file_size for i in pages):,} bytes", GROUP_PAGES)
            add(result.fields, "size", "File size", f"{os.path.getsize(path):,} bytes", GROUP_PAGES)
    except zipfile.BadZipFile:
        result.fields = []
        result.error = "not a zip archive (only CBZ archives are read for now)"
    except OSError as e:
        result.fields = []
        result.error = str(e.strerror or e)
    except Exception as e:   # noqa: BLE001 -- a damaged archive is a per-file error, never a crash
        result.fields = []
        result.error = f"could not read the archive: {e}"
    return result
