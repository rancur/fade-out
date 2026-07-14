"""Pure genre-keyword helpers (no heavy audio deps -- unit testable).

Extracted from ``audio_analyzer`` so the keyword-boost logic can be tested
without importing librosa/soundfile/shazamio.
"""

import re
from typing import Dict, List

# Keyword -> genre boosts applied when a track's title/artist metadata mentions
# the term. Ordered so more-specific genres are represented.
GENRE_KEYWORDS: Dict[str, List[str]] = {
    "house": ["house"],
    "techno": ["techno"],
    "trance": ["trance"],
    "drum and bass": ["drum", "bass", "dnb", "d&b", "jungle"],
    "dubstep": ["dubstep", "riddim"],
    "ambient": ["ambient", "chill"],
    "breakbeat": ["breakbeat", "breaks"],
}


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
