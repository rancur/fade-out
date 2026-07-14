"""Pure helpers for cleaning and formatting mix tracklists.

Kept dependency-free (no app.config / DB / network imports) so the logic is
trivially unit-testable and reusable across the analyzer, the DJCTL/CUE parser,
and the description generator, which previously each carried their own copy of
the timestamp-formatting code.
"""

from typing import Any, Dict, List

_UNKNOWN_MARKERS = {"", "unknown", "unknown artist", "unknown title", "id", "n/a"}

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
    return _norm(value) in _UNKNOWN_MARKERS


def clean_tracklist(tracks: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Return a cleaned copy of a tracklist.

    - Sorts entries by ``timestamp_seconds`` (ascending).
    - Drops "phantom" entries where BOTH artist and title are unknown/empty
      (recognition noise that would otherwise show up as ``Unknown - Unknown``).
    - Collapses consecutive duplicate detections (same artist+title), keeping the
      earliest timestamp.
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
        artist = track.get("artist", "")
        title = track.get("title", "")

        # Drop entries with no usable identity at all.
        if _is_unknown(artist) and _is_unknown(title):
            continue

        key = (_norm(artist), _norm(title))
        if key == last_key:
            # Consecutive duplicate — keep the first (earlier) occurrence only.
            continue
        last_key = key

        ts = _safe_ts(track)
        new_track = dict(track)
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

    Note: not yet wired into the branded description body (that lives in
    ``description_generator`` and is also being changed in PR #8); exposed and
    tested so it can be dropped in cleanly.
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
