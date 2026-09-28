"""
redactor_common/core/release_notes.py

Builds a GitHub Release's notes from an app's CHANGELOG.md: every
version section AFTER the previous published release, up to and
including the version being released -- not just the newest section.
Every Redactor app's .github/workflows/release.yml runs it (after
installing requirements.txt, which brings this package):

    python -m redactor_common.core.release_notes <tag> <published tags file> <output .md>

from the app's repo root (it reads ./CHANGELOG.md).

Why: before 2026-09-28 the workflows took only the tagged version's own
section, so a release after several unreleased check-ins described only
the last one -- whole features went unannounced (mp3redactor's
MusicBrainz lookup, epubredactor's Suggest Genres). It began as
cbzredactor's release_notes.py, was copied to the other three apps the
same day, then promoted here so there's one copy.

Versions look like "2026-09-28#05" in CHANGELOG.md headings
("## 2026-09-28#05 -- title", any title separator) and
"v2026-09-28-05" as tags.
"""

from __future__ import annotations

import re
import sys
from typing import Optional

_HEADING_RE = re.compile(r"^## (\d{4}-\d{2}-\d{2})#(\d+)\b[^\n]*$", re.M)
_TAG_RE = re.compile(r"^v(\d{4}-\d{2}-\d{2})-(\d+)$")


def version_key_from_tag(tag: str) -> Optional[tuple[str, int]]:
    match = _TAG_RE.match(tag.strip())
    return (match.group(1), int(match.group(2))) if match else None


def previous_release(tag: str, published_tags: list[str]) -> Optional[str]:
    """The highest published release tag below `tag` (so re-running an
    old tag, or a release made out of date order, still gets the right
    range). None if there is none."""
    current = version_key_from_tag(tag)
    older = [
        (key, t) for t in published_tags
        if (key := version_key_from_tag(t)) is not None and current is not None and key < current
    ]
    return max(older)[1] if older else None


def build_notes(changelog: str, tag: str, previous_tag: Optional[str]) -> str:
    """Every CHANGELOG section with previous < version <= tag, newest
    first as in the file. With one section, just its body (as before);
    with several, each keeps its "## version -- title" heading so the
    notes read as a list of what each check-in brought."""
    current = version_key_from_tag(tag)
    if current is None:
        raise ValueError(f"Not a release tag: {tag}")
    floor = version_key_from_tag(previous_tag) if previous_tag else None

    headings = list(_HEADING_RE.finditer(changelog))
    sections = []
    for i, heading in enumerate(headings):
        key = (heading.group(1), int(heading.group(2)))
        if key > current or (floor is not None and key <= floor):
            continue
        end = headings[i + 1].start() if i + 1 < len(headings) else len(changelog)
        body = changelog[heading.end():end].strip()
        sections.append((key, heading.group(0), body))

    if not any(key == current for key, _h, _b in sections):
        raise ValueError(f"CHANGELOG.md has no section for {current[0]}#{current[1]:02d}")
    if len(sections) == 1:
        return sections[0][2] + "\n"
    since = f"since {previous_tag}" if previous_tag else "in this release"
    parts = [f"Everything {since} ({len(sections)} versions):"]
    parts += [f"{heading}\n\n{body}" for _key, heading, body in sections]
    return "\n\n".join(parts) + "\n"


def main(argv: list[str]) -> int:
    tag, tags_file, output = argv
    with open(tags_file, encoding="utf-8") as f:
        published = [line.strip() for line in f if line.strip() and line.strip() != tag]
    with open("CHANGELOG.md", encoding="utf-8") as f:
        changelog = f.read()
    previous = previous_release(tag, published)
    notes = build_notes(changelog, tag, previous)
    with open(output, "w", encoding="utf-8") as f:
        f.write(notes)
    print(f"Release notes for {tag}: changes since {previous or '(no earlier release)'}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
