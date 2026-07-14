"""Pure helpers for cleaning and formatting mix tracklists.

Kept dependency-free (no app.config / DB / network imports) so the logic is
trivially unit-testable and reusable across the analyzer, the DJCTL/CUE parser,
and the description generator, which previously each carried their own copy of
the timestamp-formatting code.
"""

import re
from typing import Any, Dict, List

# DJ convention: a track that is present but could not be identified is labelled
# "ID" for both artist and title, rendering as "ID - ID". This is the single
# canonical placeholder for unidentified tracks across the whole backend
# (tracklist storage, descriptions, YouTube chapters, cross-links).
ID_LABEL = "ID"

_UNKNOWN_MARKERS = {"", "unknown", "unknown artist", "unknown title", "id", "n/a"}

# CUE sheets emit "Track 3" style placeholders for un-named cue points; these
# are unidentified tracks and should also collapse to the ID label.
_TRACK_PLACEHOLDER_RE = re.compile(r"^track\s+\d+$")

# YouTube only renders chapters when: the first stamp is exactly 0:00, there are
# at least 3 chapters, and each is >= 10 seconds after the previous one.
YOUTUBE_MIN_CHAPTERS = 3
YOUTUBE_MIN_CHAPTER_GAP_SECONDS = 10


def format_timestamp(seconds: float) -> str:
    """Format seconds as ``m:ss`` (or ``h:mm:ss`` past an hour).

    Negative values are clamped to zero. This is the single canonical formatter
    for the whole backend.
    """
    total = int(max(0.0, seconds))
    h, remainder = divmod(total, 3600)
    m, s = divmod(remainder, 60)
    if h > 0:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"


def _norm(value: Any) -> str:
    return str(value or "").strip().lower()


def _is_unknown(value: Any) -> bool:
    norm = _norm(value)
    return norm in _UNKNOWN_MARKERS or bool(_TRACK_PLACEHOLDER_RE.match(norm))


def label_or_id(value: Any) -> str:
    """Return a display label for an artist/title, or ``"ID"`` if unidentified.

    Unknown/blank values and CUE "Track N" placeholders map to the canonical
    ``ID`` label so unidentified tracks render as ``ID - ID`` everywhere.
    """
    if _is_unknown(value):
        return ID_LABEL
    return str(value).strip()


def clean_tracklist(tracks: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Return a cleaned copy of a tracklist.

    - Sorts entries by ``timestamp_seconds`` (ascending).
    - Normalizes unidentified artist/title fields (unknown/blank/"Track N") to
      the canonical ``ID`` label, so unidentified tracks render as ``ID - ID``
      (DJ convention) rather than ``Unknown - Unknown`` or blank.
    - Collapses consecutive duplicate detections (same artist+title, including
      consecutive ``ID - ID`` recognition gaps), keeping the earliest timestamp.
    - Recomputes ``timestamp_formatted`` from ``timestamp_seconds`` so every
      consumer sees a consistently formatted stamp.

    Input dicts are not mutated; new dicts are returned.
    """
    if not tracks:
        return []

    # Stable sort by timestamp; entries without a timestamp sort to the front.
    ordered = sorted(tracks, key=lambda t: _safe_ts(t))

    cleaned: List[Dict[str, Any]] = []
    last_key = None
    for track in ordered:
        artist = label_or_id(track.get("artist", ""))
        title = label_or_id(track.get("title", ""))

        key = (artist.lower(), title.lower())
        if key == last_key:
            # Consecutive duplicate (or a run of unidentified ID - ID segments) —
            # keep the first (earlier) occurrence only.
            continue
        last_key = key

        ts = _safe_ts(track)
        new_track = dict(track)
        new_track["artist"] = artist
        new_track["title"] = title
        new_track["timestamp_seconds"] = ts
        new_track["timestamp_formatted"] = format_timestamp(ts)
        cleaned.append(new_track)

    return cleaned


def _safe_ts(track: Dict[str, Any]) -> float:
    try:
        return float(track.get("timestamp_seconds", 0) or 0)
    except (TypeError, ValueError):
        return 0.0


def build_youtube_chapters(tracks: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Shape a tracklist into a valid YouTube chapter list.

    Guarantees the first chapter starts at 0:00 (prepending an "Intro" marker if
    the first real track begins later) and drops chapters spaced closer than
    YouTube's 10-second minimum. Returns ``[]`` if fewer than 3 chapters remain,
    since YouTube would not render chapters at all in that case.

    Wired into ``description_generator.generate_youtube_description`` which
    appends a deterministic, validated chapter block to the YouTube description.
    """
    cleaned = clean_tracklist(tracks)
    if not cleaned:
        return []

    chapters: List[Dict[str, Any]] = []

    if _safe_ts(cleaned[0]) > 0:
        chapters.append({
            "title": "Intro",
            "artist": "",
            "timestamp_seconds": 0.0,
            "timestamp_formatted": format_timestamp(0.0),
        })

    for track in cleaned:
        ts = _safe_ts(track)
        if chapters and (ts - _safe_ts(chapters[-1])) < YOUTUBE_MIN_CHAPTER_GAP_SECONDS:
            # Too close to the previous chapter — YouTube would reject the set.
            continue
        entry = dict(track)
        entry["timestamp_seconds"] = ts
        entry["timestamp_formatted"] = format_timestamp(ts)
        chapters.append(entry)

    if len(chapters) < YOUTUBE_MIN_CHAPTERS:
        return []
    return chapters
