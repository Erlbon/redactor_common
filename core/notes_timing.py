"""
redactor_common/core/notes_timing.py -- how long the Create Notes lookups take, said plainly (Qt-free, pure).

  format_duration(seconds)                           '35 s', '4 min', '1 h 41 min'
  clock(seconds)                                     H:MM:SS
  progress_suffix(done, total, elapsed, durations)   ", 4 min elapsed, about 1 h 41 min left" for the progress label
                                                     ('' while the guess would be silly: fewer than 3 folders done)
  lookup_sentence(folders, elapsed, requests, cached)  'Looked up 656 folders in 1:50:12 (5120 requests, 800 from the cache).'

What a scope costs (the per-request numbers of one app's services) is that app's own business: it builds its scope
hint from format_duration().
"""


from __future__ import annotations

from typing import Iterable

MIN_DONE_FOR_ESTIMATE = 3
RECENT = 12  # how many of the latest folders the remaining time is averaged over


def format_duration(seconds: float) -> str:
    """'35 s', '4 min', '1 h 41 min' (rounded: nobody needs the seconds of an hour)."""
    seconds = max(0.0, float(seconds))
    if seconds < 60:
        return f"{int(round(seconds))} s"
    minutes = int(round(seconds / 60))
    if minutes < 60:
        return f"{minutes} min"
    hours, minutes = divmod(minutes, 60)
    return f"{hours} h {minutes:02d} min" if minutes else f"{hours} h"


def clock(seconds: float) -> str:
    """H:MM:SS."""
    total = int(round(max(0.0, float(seconds))))
    return f"{total // 3600}:{total % 3600 // 60:02d}:{total % 60:02d}"


def progress_suffix(done: int, total: int, elapsed: float, durations: Iterable[float]) -> str:
    """The timing words for the progress label, or '' while it would be a silly guess (fewer than 3 folders done, or
    nothing left). The time left is the average of the RECENT folders times the folders left: a folder answered from the
    cache is fast and counts as fast, so a resumed run's estimate falls as it goes."""
    recent = [d for d in list(durations)[-RECENT:] if d >= 0]
    left = total - done
    if done < MIN_DONE_FOR_ESTIMATE or left <= 0 or not recent:
        return ""
    remaining = left * (sum(recent) / len(recent))
    if remaining < 15:
        return f", {format_duration(elapsed)} elapsed, almost done"
    return f", {format_duration(elapsed)} elapsed, about {format_duration(remaining)} left"


def _plural(n: int, word: str) -> str:
    return f"{n} {word}" if n == 1 else f"{n} {word}s"


def lookup_sentence(folders: int, elapsed: float, requests: int, from_cache: int) -> str:
    return (f"Looked up {_plural(folders, 'folder')} in {clock(elapsed)} "
            f"({_plural(requests, 'request')}, {from_cache} from the cache).")
