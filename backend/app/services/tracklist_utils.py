"""Pure helpers for cleaning and formatting mix tracklists.

Kept dependency-free (no app.config / DB / network imports) so the logic is
trivially unit-testable and reusable across the analyzer, the DJCTL/CUE parser,
and the description generator, which previously each carried their own copy of
the timestamp-formatting code.
"""

import re
from typing import Any, Dict, List, Optional

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

# A YouTube upload is the whole stream: a "starting soon" card before the set
# and whatever trailed after it. Giving those their own chapters lets a viewer
# skip straight to the music, and makes the mandatory 0:00 chapter an honest
# label for what is actually on screen rather than a placeholder.
PRE_ROLL_LABEL = "Starting Soon"
OUTRO_LABEL = "Stream Ended"


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


def build_youtube_chapters(
    tracks: List[Dict[str, Any]],
    lead_in_seconds: float = 0.0,
    mix_end_seconds: Optional[float] = None,
    video_duration_seconds: Optional[float] = None,
    pre_roll_label: str = PRE_ROLL_LABEL,
    outro_label: str = OUTRO_LABEL,
) -> List[Dict[str, Any]]:
    """Shape a tracklist into a valid YouTube chapter list.

    ``tracks`` timestamps must already be on the target video's timeline (the
    caller shifts them by the measured offset). The optional arguments describe
    the video around the mix, so the stream's non-music bookends get chapters
    of their own and the middle is purely tracks:

    * ``lead_in_seconds`` — how long the "starting soon" card and whatever else
      preceded the set runs. This is the same measured offset that shifts the
      tracklist, so it costs no extra analysis.
    * ``mix_end_seconds`` — where the mix audio stops in video time, i.e.
      ``lead_in + flac_duration``. Also free: no new detection needed.
    * ``video_duration_seconds`` — the published video's length, used only to
      confirm there really is trailing content worth a chapter.

    YouTube renders chapters only when the first stamp is exactly 0:00, there
    are at least 3 of them, and each is at least 10 s after the previous. A
    bookend that would violate any of that is dropped rather than emitted:
    one invalid chapter kills chapters for the entire video. Returns ``[]``
    when a valid set cannot be built.
    """
    cleaned = clean_tracklist(tracks)
    if not cleaned:
        return []

    def marker(title: str, ts: float) -> Dict[str, Any]:
        return {
            "title": title,
            "artist": "",
            "timestamp_seconds": ts,
            "timestamp_formatted": format_timestamp(ts),
        }

    chapters: List[Dict[str, Any]] = []

    # 0:00 is mandatory. When there is a real pre-roll, name it for what it is;
    # otherwise fall back to a neutral marker so the rule is still satisfied.
    first_ts = _safe_ts(cleaned[0])
    if first_ts > 0:
        label = pre_roll_label if lead_in_seconds >= YOUTUBE_MIN_CHAPTER_GAP_SECONDS else "Intro"
        chapters.append(marker(label, 0.0))

    for track in cleaned:
        ts = _safe_ts(track)
        if chapters and (ts - _safe_ts(chapters[-1])) < YOUTUBE_MIN_CHAPTER_GAP_SECONDS:
            # Too close to the previous chapter — YouTube would reject the set.
            continue
        entry = dict(track)
        entry["timestamp_seconds"] = ts
        entry["timestamp_formatted"] = format_timestamp(ts)
        chapters.append(entry)

    # Closing chapter for the trailing stream content. Needs to sit far enough
    # past the last track, and to have at least 10 s of video after it —
    # otherwise it is not a chapter YouTube will accept.
    if mix_end_seconds is not None and chapters:
        trailing_ok = (
            video_duration_seconds is None
            or video_duration_seconds - mix_end_seconds >= YOUTUBE_MIN_CHAPTER_GAP_SECONDS
        )
        gap_ok = mix_end_seconds - _safe_ts(chapters[-1]) >= YOUTUBE_MIN_CHAPTER_GAP_SECONDS
        if trailing_ok and gap_ok:
            chapters.append(marker(outro_label, float(mix_end_seconds)))

    if len(chapters) < YOUTUBE_MIN_CHAPTERS:
        return []
    return chapters
