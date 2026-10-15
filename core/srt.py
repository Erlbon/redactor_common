"""
redactor_common/core/srt.py

SubRip (.srt) text <-> cues, and a time-overlap alignment of two cue lists (Qt-free). Shared by the subtitle
translation (core/translate.py) and the "compare two subtitles" view in videoredactor.

  parse_srt(text)           tolerant: BOM, CRLF/CR, a missing or wrong index line, "," or "." before the
                            milliseconds, multi-line cues; a block without a readable time line is skipped
  format_srt(cues)          numbered cues, "HH:MM:SS,mmm --> HH:MM:SS,mmm", LF line ends, trailing newline
  align_cues(a, b)          rows pairing the cues of two lists by TIME OVERLAP (not by index), so lists with a
                            different number of cues still line up
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Sequence

_TIME = r"(\d{1,3}):(\d{1,2}):(\d{1,2})(?:[.,](\d{1,3}))?"
_TIME_LINE = re.compile(r"^\s*" + _TIME + r"\s*-->\s*" + _TIME + r".*$")


@dataclass
class Cue:
    start: float  # seconds
    end: float
    text: str     # lines joined with "\n"


def _seconds(h: str, m: str, s: str, frac: str | None) -> float:
    value = int(h) * 3600 + int(m) * 60 + int(s)
    if frac:
        value += int(frac) / (10 ** len(frac))
    return float(value)


def srt_time(seconds: float) -> str:
    ms = max(0, int(round(seconds * 1000)))
    h, rest = divmod(ms, 3_600_000)
    m, rest = divmod(rest, 60_000)
    s, ms = divmod(rest, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def clock_text(seconds: float) -> str:
    """A short time for a table: "1:02" or "1:02:03"."""
    total = max(0, int(seconds))
    h, rest = divmod(total, 3600)
    m, s = divmod(rest, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def parse_srt(text: str) -> list[Cue]:
    """Cues of an SRT text, in file order. Blocks without a readable time line are skipped."""
    text = (text or "").lstrip("﻿").replace("\r\n", "\n").replace("\r", "\n")
    cues: list[Cue] = []
    for block in re.split(r"\n\s*\n", text.strip("\n")):
        lines = block.split("\n")
        for i, line in enumerate(lines[:3]):
            m = _TIME_LINE.match(line)
            if m:
                g = m.groups()
                start = _seconds(*g[0:4])
                end = _seconds(*g[4:8])
                body = "\n".join(l.rstrip() for l in lines[i + 1:]).strip("\n")
                cues.append(Cue(start, max(end, start), body))
                break
    return cues


def format_srt(cues: Sequence[Cue]) -> str:
    blocks = [
        f"{n}\n{srt_time(c.start)} --> {srt_time(c.end)}\n{c.text.strip()}\n"
        for n, c in enumerate(cues, start=1)
    ]
    return "\n".join(blocks)


@dataclass
class AlignedRow:
    start: float
    end: float
    a: list[Cue] = field(default_factory=list)
    b: list[Cue] = field(default_factory=list)

    @property
    def a_text(self) -> str:
        return "\n".join(c.text for c in self.a)

    @property
    def b_text(self) -> str:
        return "\n".join(c.text for c in self.b)


def align_cues(a: Sequence[Cue], b: Sequence[Cue], tolerance: float = 0.2) -> list[AlignedRow]:
    """Rows of cues that overlap in time, one row per cluster: a cue joins the current row when it starts
    more than `tolerance` seconds before the row ends (cues of one list never overlap each other, so a
    cluster is always "some cues of A against some cues of B"). A cue that overlaps nothing of the other list
    gets a row of its own with the other side empty. Rows come in time order."""
    tagged = [(c.start, c.end, 0, c) for c in a] + [(c.start, c.end, 1, c) for c in b]
    tagged.sort(key=lambda t: (t[0], t[2]))
    rows: list[AlignedRow] = []
    current: AlignedRow | None = None
    for start, end, side, cue in tagged:
        if current is not None and start < current.end - tolerance:
            current.end = max(current.end, end)
        else:
            current = AlignedRow(start, end)
            rows.append(current)
        (current.a if side == 0 else current.b).append(cue)
    return rows
