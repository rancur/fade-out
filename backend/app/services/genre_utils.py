"""Pure genre-keyword helpers (no heavy audio deps -- unit testable).

Extracted from ``audio_analyzer`` so the keyword-boost logic can be tested
without importing librosa/soundfile/shazamio.
"""

import re
from typing import Dict, List, Optional, Tuple

# Keyword -> genre boosts applied when a track's title/artist metadata mentions
# the term. Ordered so more-specific genres are represented.
GENRE_KEYWORDS: Dict[str, List[str]] = {
    "house": ["house"],
    "techno": ["techno"],
    "trance": ["trance"],
    "drum and bass": ["drum", "bass", "dnb", "d&b", "jungle", "liquid", "neurofunk"],
    "dubstep": ["dubstep", "riddim", "brostep"],
    "trap": ["trap"],
    "garage": ["garage", "ukg", "2-step", "2step", "speed garage"],
    "ambient": ["ambient", "chill"],
    "breakbeat": ["breakbeat", "breaks"],
}

# Genres whose keyword match should take precedence over purely-spectral guesses.
# A title/artist that literally says "dnb"/"dubstep"/"trap"/"garage"/"breaks" is a
# far stronger signal than a BPM/centroid bin, so these lead the ordering.
KEYWORD_PRECEDENCE_GENRES = frozenset(
    {"drum and bass", "dubstep", "trap", "garage", "breakbeat"}
)

# Genre inference from spectral features: genre -> feature_name -> (low, high).
# BPM ranges assume the tempo has been folded into a canonical dance window
# (see ``fold_tempo``) so half/double-tempo detection errors are corrected first.
GENRE_HINTS: Dict[str, Dict[str, Tuple[float, float]]] = {
    "drum and bass": {"bpm": (160, 180), "centroid_mean": (2000, 5000)},
    "house": {"bpm": (118, 132), "centroid_mean": (1500, 4000)},
    "techno": {"bpm": (125, 150), "centroid_mean": (1000, 3500)},
    "trance": {"bpm": (128, 145), "centroid_mean": (2000, 5000)},
    "dubstep": {"bpm": (135, 145), "centroid_mean": (500, 3000)},
    "ambient": {"bpm": (60, 100), "centroid_mean": (500, 2000)},
    "breakbeat": {"bpm": (120, 150), "centroid_mean": (1500, 4500)},
}


def fold_tempo(bpm: float, lo: float = 90.0, hi: float = 180.0) -> float:
    """Fold a detected BPM into a canonical dance-music window ``[lo, hi)``.

    librosa's beat tracker frequently reports half- or double-tempo (e.g. a
    174 BPM drum-and-bass track lands at ~87, dropping it squarely into house's
    118-132 band). Folding into a single octave before binning corrects both
    directions: 87 -> 174, 248 -> 124. This is the crux of the genre-mislabel
    fix -- without it a whole DnB set can be averaged into "house".
    """
    if bpm <= 0:
        return bpm
    while bpm < lo:
        bpm *= 2.0
    while bpm >= hi:
        bpm /= 2.0
    return bpm


def _best_genre_for_segment(bpm: float, centroid: float) -> Optional[str]:
    """Return the single best-matching genre for one segment's features.

    Scores each genre by folded-BPM band membership (strong) plus spectral
    centroid membership (weak tie-breaker) and returns the arg-max, or ``None``
    when nothing scores above zero.
    """
    folded = fold_tempo(bpm)
    best_genre: Optional[str] = None
    best_score = 0.0
    for genre, ranges in GENRE_HINTS.items():
        score = 0.0
        bpm_lo, bpm_hi = ranges["bpm"]
        if bpm_lo <= folded <= bpm_hi:
            score += 2.0
        elif abs(folded - (bpm_lo + bpm_hi) / 2) < 12:
            score += 0.5

        cent_lo, cent_hi = ranges["centroid_mean"]
        if cent_lo <= centroid <= cent_hi:
            score += 1.0

        if score > best_score:
            best_score = score
            best_genre = genre
    return best_genre


def classify_genres_from_segments(
    bpms: List[float],
    centroids: List[float],
    track_texts: List[str],
    max_genres: int = 4,
) -> List[str]:
    """Infer ordered genres from PER-SEGMENT features + track metadata.

    Unlike a single global ``mean(bpms)`` (which averages a multi-genre set into
    one wrong band), this tallies the arg-max genre of every sampled segment and
    orders by prevalence, so ``result[0]`` is the genre that actually dominates
    the mix. Keyword matches from identified tracks win precedence over spectral
    guesses. Returns ``["electronic"]`` when there is nothing to go on.
    """
    if not bpms:
        return ["electronic"]

    # Per-segment prevalence: argmax genre per sampled segment, tallied.
    prevalence: Dict[str, float] = {}
    for i, bpm in enumerate(bpms):
        centroid = centroids[i] if i < len(centroids) else 2000.0
        genre = _best_genre_for_segment(bpm, centroid)
        if genre:
            prevalence[genre] = prevalence.get(genre, 0.0) + 1.0

    # Keyword votes from identified-track metadata.
    keyword_votes: Dict[str, float] = {}
    for text in track_texts:
        for genre in keyword_boosts(text):
            keyword_votes[genre] = keyword_votes.get(genre, 0.0) + 1.0

    total_segments = float(len(bpms))
    scores: Dict[str, float] = dict(prevalence)
    for genre, votes in keyword_votes.items():
        # A keyword-confirmed genre must outrank any purely-spectral guess, so
        # lift it above the max possible spectral prevalence. High-precedence
        # bass-music genres get an extra nudge to lead when tied on keywords.
        boost = total_segments + votes
        if genre in KEYWORD_PRECEDENCE_GENRES:
            boost += total_segments
        scores[genre] = scores.get(genre, 0.0) + boost

    ordered = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
    result = [g for g, s in ordered if s > 0][:max_genres]
    return result or ["electronic"]


def keyword_matches(text: str, keyword: str) -> bool:
    """Return True if ``keyword`` occurs in ``text`` as a whole word.

    Word-boundary matching avoids the substring over-matching that previously
    credited "drum and bass" for any title containing "bass" as part of a longer
    word -- e.g. "bassline", "bassheavy", "embassy" -- which inflated the genre
    score on unrelated tracks. Matching is case-insensitive.
    """
    if not text or not keyword:
        return False
    pattern = r"(?<!\w)" + re.escape(keyword) + r"(?!\w)"
    return re.search(pattern, text, flags=re.IGNORECASE) is not None


def keyword_boosts(text: str, boost: float = 1.5) -> Dict[str, float]:
    """Return ``{genre: boost}`` for every genre whose keywords match ``text``."""
    out: Dict[str, float] = {}
    for genre, keywords in GENRE_KEYWORDS.items():
        if any(keyword_matches(text, kw) for kw in keywords):
            out[genre] = boost
    return out
