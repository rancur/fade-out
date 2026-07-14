"""Confidence-scored merge of tracklists from multiple detection sources.

Pure logic, no I/O or app-config imports (only the dependency-free
``tracklist_utils`` helpers), so it is trivially unit-testable and reusable.

The pipeline can learn a mix's tracklist from several places with very different
reliability:

* **CUE sheet** (DJCTL export) — authoritative timestamps *and* names. This is
  the ground truth when it exists.
* **DJCTL / Serato live metadata** (WebSocket "now playing") — authoritative,
  since it is the deck reporting exactly what is loaded.
* **AudD** — a paid acoustic-fingerprint API (only when a token is configured);
  markedly more accurate than the unofficial Shazam client on layered DJ audio.
* **Shazam** — the always-on fallback; roughly coin-flip accurate on blended /
  transitioning DJ audio, so its guesses must never override a better source.

``merge_detections`` combines candidate tracklists into one, following four
rules Will asked for:

1. **Authoritative sources win on overlap.** Where a CUE/DJCTL entry and a
   Shazam/AudD entry describe the same moment (within ``overlap_seconds``), the
   authoritative entry's name *and* timestamp are kept; the low-confidence guess
   is discarded.
2. **Low-confidence sources only fill gaps.** A Shazam/AudD entry is kept as a
   *new* track only when no authoritative entry already covers that moment.
3. **Name-gap fill.** When an authoritative entry is an unidentified placeholder
   (``ID - ID`` / CUE ``Track N``) and a trusted-enough lower source overlaps it,
   the name is borrowed to fill the placeholder while the authoritative
   *timestamp* is preserved.
4. **Honest unknowns.** A gap-filling candidate whose confidence is below
   ``name_confidence_threshold`` is kept as an ``ID - ID`` marker (we know
   *something* played there, but won't assert a probably-wrong name).

The result is normalized through :func:`tracklist_utils.clean_tracklist` so
every consumer (descriptions, YouTube chapters, cross-links) sees a sorted,
de-duplicated, consistently-formatted list.
"""

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from app.services.tracklist_utils import (
    ID_LABEL,
    clean_tracklist,
    format_timestamp,
    label_or_id,
)

# Base confidence per source when a candidate track does not carry its own.
# CUE/DJCTL sit well above Shazam so they always win an overlap.
SOURCE_CONFIDENCE: Dict[str, float] = {
    "cue": 0.95,
    "djctl": 0.90,
    "audd": 0.70,
    "shazam": 0.50,
}

# Per-hit Shazam confidences (the analyzer tags each hit by how many times the
# same title was independently recognized — confirmed hits are far more
# trustworthy than a lone one on blended audio).
SHAZAM_CONFIRMED_CONFIDENCE = 0.70
SHAZAM_SINGLE_CONFIDENCE = 0.50

# Sources whose timestamps AND names are authoritative.
AUTHORITATIVE_SOURCES = frozenset({"cue", "djctl"})

# Two candidates whose timestamps are within this many seconds are treated as
# describing the same moment in the mix.
DEFAULT_OVERLAP_SECONDS = 45.0

# A gap-filling candidate scoring strictly below this keeps its timestamp but is
# rendered as ``ID - ID`` instead of asserting a low-confidence name. Kept at the
# single-hit Shazam level so the default preserves current recall; raise it (via
# the merge caller / config) to be stricter and turn weak guesses into honest
# "ID" markers.
DEFAULT_NAME_CONFIDENCE_THRESHOLD = 0.50

_FALLBACK_CONFIDENCE = 0.30


@dataclass
class DetectionSource:
    """One source's candidate tracklist.

    ``tracks`` are plain dicts (``artist`` / ``title`` / ``timestamp_seconds``,
    optionally a per-track ``confidence``). ``confidence`` on the source, when
    given, overrides the :data:`SOURCE_CONFIDENCE` base for every track that does
    not carry its own.
    """

    name: str
    tracks: List[Dict[str, Any]]
    confidence: Optional[float] = None


@dataclass
class _Candidate:
    artist: str
    title: str
    timestamp_seconds: float
    source: str
    confidence: float
    # Whether the (raw) artist/title actually named a track, before any relabel.
    identified: bool


@dataclass
class MergeResult:
    tracklist: List[Dict[str, Any]] = field(default_factory=list)
    # "merged" when >1 source contributed, else the single contributing source
    # (normalized to the existing "djctl"/"shazam" labels), else "none".
    source: str = "none"

    def to_dict(self) -> dict:
        return {"tracklist": self.tracklist, "source": self.source}


def _safe_ts(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _is_identified(artist: Any, title: Any) -> bool:
    """True when at least one of artist/title is a real (non-ID) name."""
    return label_or_id(artist) != ID_LABEL or label_or_id(title) != ID_LABEL


def _base_confidence(source: DetectionSource) -> float:
    if source.confidence is not None:
        return source.confidence
    return SOURCE_CONFIDENCE.get(source.name, _FALLBACK_CONFIDENCE)


def _normalize_source_label(name: str) -> str:
    """Map an internal source name to the label the pipeline already stores."""
    if name in AUTHORITATIVE_SOURCES:
        return "djctl"
    return name


def merge_detections(
    sources: List[DetectionSource],
    *,
    overlap_seconds: float = DEFAULT_OVERLAP_SECONDS,
    name_confidence_threshold: float = DEFAULT_NAME_CONFIDENCE_THRESHOLD,
) -> MergeResult:
    """Merge candidate tracklists into one confidence-weighted tracklist.

    See the module docstring for the four ordering rules. Input dicts are never
    mutated.
    """
    # 1. Flatten every source into scored candidates.
    candidates: List[_Candidate] = []
    for source in sources:
        if not source.tracks:
            continue
        base = _base_confidence(source)
        for track in source.tracks:
            artist = track.get("artist", "")
            title = track.get("title", "")
            conf = track.get("confidence")
            candidates.append(
                _Candidate(
                    artist=artist,
                    title=title,
                    timestamp_seconds=_safe_ts(track.get("timestamp_seconds", 0)),
                    source=source.name,
                    confidence=float(conf) if conf is not None else base,
                    identified=_is_identified(artist, title),
                )
            )

    if not candidates:
        return MergeResult(tracklist=[], source="none")

    authoritative = [c for c in candidates if c.source in AUTHORITATIVE_SOURCES]
    others = [c for c in candidates if c.source not in AUTHORITATIVE_SOURCES]

    accepted: List[_Candidate] = []
    contributing: set[str] = set()

    # 2. Lay down the authoritative skeleton first. Collapse authoritative
    #    entries that overlap each other, keeping the higher-confidence (and, on
    #    a tie, the identified / earlier) one.
    for cand in sorted(authoritative, key=lambda c: (c.timestamp_seconds, -c.confidence)):
        near = _nearest(accepted, cand.timestamp_seconds, overlap_seconds)
        if near is None:
            accepted.append(cand)
            contributing.add(cand.source)
            continue
        # Overlapping authoritative entries: prefer identified, then confidence.
        if (cand.identified, cand.confidence) > (near.identified, near.confidence):
            accepted.remove(near)
            accepted.append(cand)
            contributing.add(cand.source)

    # 3. Fold in the lower-confidence sources, strongest first so the best
    #    gap-filler claims a slot before weaker duplicates.
    for cand in sorted(others, key=lambda c: (-c.confidence, c.timestamp_seconds)):
        near = _nearest(accepted, cand.timestamp_seconds, overlap_seconds)

        if near is not None and near.source in AUTHORITATIVE_SOURCES:
            # Authoritative already owns this moment. Only borrow the name to
            # fill an unidentified placeholder, and only if we trust it enough.
            if (
                not near.identified
                and cand.identified
                and cand.confidence >= name_confidence_threshold
            ):
                near.artist = cand.artist
                near.title = cand.title
                near.identified = True
                contributing.add(cand.source)
            # Otherwise the authoritative entry wins the overlap outright.
            continue

        if near is not None:
            # Overlaps an already-accepted non-authoritative entry: a duplicate
            # detection. The stronger one was accepted first, so drop this one.
            continue

        # Genuine gap. Accept it, but downgrade a low-confidence name to an
        # honest ID - ID marker rather than asserting a probable mis-ID.
        if not cand.identified or cand.confidence < name_confidence_threshold:
            cand.artist = ID_LABEL
            cand.title = ID_LABEL
            cand.identified = False
        accepted.append(cand)
        contributing.add(cand.source)

    # 4. Emit as normalized dicts (sorted, de-duplicated, formatted).
    merged = [
        {
            "artist": c.artist,
            "title": c.title,
            "timestamp_seconds": round(c.timestamp_seconds, 2),
            "timestamp_formatted": format_timestamp(c.timestamp_seconds),
            "source": c.source,
            "confidence": round(c.confidence, 3),
        }
        for c in accepted
    ]
    merged = clean_tracklist(merged)

    if len(contributing) > 1:
        source_label = "merged"
    elif len(contributing) == 1:
        source_label = _normalize_source_label(next(iter(contributing)))
    else:
        source_label = "none"

    return MergeResult(tracklist=merged, source=source_label)


def _nearest(
    accepted: List[_Candidate], timestamp: float, overlap_seconds: float
) -> Optional[_Candidate]:
    """Return the accepted candidate closest in time within ``overlap_seconds``."""
    best: Optional[_Candidate] = None
    best_delta = overlap_seconds
    for cand in accepted:
        delta = abs(cand.timestamp_seconds - timestamp)
        if delta <= best_delta:
            best = cand
            best_delta = delta
    return best
