"""
redactor_common/core/notes_phases.py -- where the time of a Create Notes run goes, phase by phase (Qt-free, thread-safe).

A run is walked folders (scan), tags read from the files (tags: files and seconds), requests to MusicBrainz, Discogs and
Wikipedia (seconds INCLUDING the throttle waits and the waits for a busy service; answers from the cache or from memory cost
nothing and are counted apart), and the notes written. Each phase adds its seconds and its count to a PhaseTimer; the review
banner, the command line and the log show the result, so a slow run says what it was slow at.

With the lookups of two services running side by side the phases add up to more than the clock on the wall: the sum is
work, the sentence 'Looked up N folders in H:MM:SS' is the wall clock.
"""

from __future__ import annotations

import threading
import time
from collections import defaultdict
from contextlib import contextmanager

BUSY = "busy"  # time spent waiting for a service that said it was busy (503): part of its phase, counted apart
SCAN, WRITE = "scan", "write"


class PhaseSet:
    """The phases an app times, in display order: `labels` maps a phase id to the word shown, `units` to what is
    counted ('requests', 'files'); `busy_phase` is the phase whose line says how much of it was waiting."""

    def __init__(self, labels: dict, units: dict, busy_phase: str = ""):
        self.order = tuple(labels)
        self.labels, self.units, self.busy_phase = dict(labels), dict(units), busy_phase


def mmss(seconds: float) -> str:
    """m:ss, or h:mm:ss from an hour on."""
    total = int(round(max(0.0, float(seconds))))
    if total >= 3600:
        return f"{total // 3600}:{total % 3600 // 60:02d}:{total % 60:02d}"
    return f"{total // 60}:{total % 60:02d}"


class PhaseTimer:
    def __init__(self, phases: "PhaseSet") -> None:
        self.set = phases
        self._lock = threading.Lock()
        self._seconds: dict[str, float] = defaultdict(float)
        self._counts: dict[str, int] = defaultdict(int)

    def add(self, phase: str, seconds: float, count: int = 1) -> None:
        with self._lock:
            self._seconds[phase] += max(0.0, seconds)
            self._counts[phase] += count

    @contextmanager
    def phase(self, phase: str, count: int = 1):
        start = time.perf_counter()
        try:
            yield
        finally:
            self.add(phase, time.perf_counter() - start, count)

    def seconds(self, phase: str) -> float:
        with self._lock:
            return self._seconds.get(phase, 0.0)

    def count(self, phase: str) -> int:
        with self._lock:
            return self._counts.get(phase, 0)

    def _used(self) -> list[str]:
        with self._lock:
            return [p for p in self.set.order if self._seconds.get(p, 0.0) > 0 or self._counts.get(p, 0) > 0]

    def time_line(self) -> str:
        """'Time: scan 0:12, tags 8:40, MusicBrainz 21:10, Discogs 25:30, Wikipedia 1:20' (the phases that did something)."""
        parts = [f"{self.set.labels[p]} {mmss(self.seconds(p))}" + self._busy_note(p) for p in self._used()]
        return "Time: " + ", ".join(parts) if parts else ""

    def _busy_note(self, phase: str) -> str:
        if not self.set.busy_phase or phase != self.set.busy_phase or self.count(BUSY) == 0:
            return ""
        return f" (of it {mmss(self.seconds(BUSY))} waiting for a busy service, {self.count(BUSY)}x)"

    def detail(self) -> str:
        """The same with the counts: 'tags 8:40 (1080 files), MusicBrainz 21:10 (402 requests)'."""
        return "Time: " + ", ".join(
            f"{self.set.labels[p]} {mmss(self.seconds(p))} ({self.count(p)} {self.set.units[p]})" + self._busy_note(p) for p in self._used())

    def to_dict(self) -> dict:
        out = {p: {"seconds": round(self.seconds(p), 2), "count": self.count(p)} for p in self._used()}
        if self.count(BUSY):
            out[BUSY] = {"seconds": round(self.seconds(BUSY), 2), "count": self.count(BUSY)}
        return out
