"""Smart tag generation for SoundCloud and YouTube."""

import logging
from datetime import datetime
from typing import Any, Dict, List, Optional, Set

from app.config import settings

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Tag database by genre, ordered roughly by popularity / search volume
# ---------------------------------------------------------------------------

GENRE_TAGS: Dict[str, List[str]] = {
    "house": [
        "house music", "deep house", "tech house", "house mix",
        "dance music", "club music", "disco house", "funky house",
        "soulful house", "progressive house", "afro house", "melodic house",
        "jackin house", "bass house", "acid house", "chicago house",
        "ibiza", "underground house",
    ],
    "techno": [
        "techno", "techno mix", "dark techno", "hard techno",
        "minimal techno", "acid techno", "industrial techno",
        "melodic techno", "peak time techno", "warehouse techno",
        "detroit techno", "berlin techno", "hypnotic techno",
        "driving techno", "rave",
    ],
    "drum and bass": [
        "drum and bass", "dnb", "dnb mix", "liquid dnb",
        "neurofunk", "jump up", "jungle", "liquid drum and bass",
        "rollers", "dancefloor dnb", "heavy dnb", "atmospheric dnb",
        "hospital records", "ram records", "breakcore",
    ],
    "trance": [
        "trance", "trance mix", "uplifting trance", "progressive trance",
        "psytrance", "vocal trance", "classic trance", "tech trance",
        "goa trance", "acid trance", "euphoric trance", "anjunabeats",
        "dreamstate", "asot",
    ],
    "dubstep": [
        "dubstep", "dubstep mix", "riddim", "heavy dubstep",
        "melodic dubstep", "bass music", "brostep", "deep dubstep",
        "tearout", "filthy dubstep", "wub", "excision",
    ],
    "ambient": [
        "ambient", "ambient mix", "chillout", "downtempo",
        "atmospheric", "meditation music", "ambient electronic",
        "drone", "dark ambient", "space ambient", "nature sounds",
        "relaxing music", "sleep music",
    ],
    "breakbeat": [
        "breakbeat", "breaks", "breakbeat mix", "nu breaks",
        "progressive breaks", "funky breaks", "big beat",
        "electro breaks", "acid breaks",
    ],
    "electronic": [
        "electronic music", "electronic mix", "edm",
        "dance music", "underground electronic", "experimental electronic",
        "synth", "electronica",
    ],
}

VIBE_TAGS: Dict[str, List[str]] = {
    "chill": ["chill mix", "chill vibes", "relaxing", "late night", "sunset"],
    "mellow": ["mellow", "smooth", "laid back", "easy listening"],
    "groovy": ["groovy", "groove", "funky", "rhythmic", "bouncy"],
    "energetic": ["energetic", "high energy", "peak time", "festival", "rave"],
    "intense": ["intense", "heavy", "dark", "underground", "warehouse"],
    "chaotic": ["chaotic", "experimental", "wild", "unpredictable"],
    "journey": ["musical journey", "long mix", "extended set", "marathon"],
    "dynamic": ["dynamic", "varied", "eclectic", "genre-fluid"],
    "mixed": ["eclectic", "multi-genre", "genre-fluid"],
}

# Brand tags always included
BRAND_TAGS: List[str] = [
    "Will See",
    "willsee",
    "dj mix",
    "dj set",
    "mix",
]

# Always-include generic tags
GENERIC_TAGS: List[str] = [
    "electronic music",
    "live mix",
    "four decks",
]


class TagGenerator:
    """Generate optimized tags for SoundCloud and YouTube."""

    def generate(
        self,
        genres: List[str],
        vibes: List[str],
        tracklist: Optional[List[Dict[str, Any]]] = None,
        max_tags: int = 30,
    ) -> List[str]:
        """Generate tags with priority: brand > genre > vibe > tracklist > generic.

        Returns up to max_tags tags.
        """
        tags: List[str] = []
        seen: Set[str] = set()

        def _add(tag: str) -> bool:
            normalized = tag.lower().strip()
            if normalized in seen or not normalized:
                return False
            if len(tags) >= max_tags:
                return False
            seen.add(normalized)
            tags.append(tag.strip())
            return True

        # 1. Brand tags (highest priority)
        for t in BRAND_TAGS:
            _add(t)

        # Current year
        _add(str(datetime.now().year))

        # 2. Genre-specific tags
        for genre in genres:
            genre_lower = genre.lower()
            genre_tag_list = GENRE_TAGS.get(genre_lower, [])

            # Add the genre name itself
            _add(genre)

            # Add genre-specific tags
            for t in genre_tag_list:
                if len(tags) >= max_tags:
                    break
                _add(t)

        # 3. Vibe-specific tags
        for vibe in vibes:
            vibe_lower = vibe.lower()
            vibe_tag_list = VIBE_TAGS.get(vibe_lower, [])
            _add(vibe)
            for t in vibe_tag_list:
                if len(tags) >= max_tags:
                    break
                _add(t)

        # 4. Artist tags from tracklist (good for discovery)
        if tracklist:
            artists_added = 0
            for track in tracklist:
                if artists_added >= 5:
                    break
                artist = track.get("artist", "")
                if artist and artist.lower() != "unknown":
                    if _add(artist):
                        artists_added += 1

        # 5. Generic filler tags
        for t in GENERIC_TAGS:
            _add(t)

        logger.info("Generated %d tags for genres=%s vibes=%s", len(tags), genres, vibes)
        return tags[:max_tags]

    def format_for_soundcloud(self, tags: List[str]) -> str:
        """Format tags for SoundCloud: space-separated, multi-word in quotes."""
        formatted: List[str] = []
        for tag in tags:
            if " " in tag:
                formatted.append(f'"{tag}"')
            else:
                formatted.append(tag)
        return " ".join(formatted)

    def format_for_youtube(self, tags: List[str]) -> List[str]:
        """Format tags for YouTube: plain list (YouTube API accepts them directly)."""
        return list(tags)

    def get_primary_genre_tag(self, genres: List[str]) -> str:
        """Get the best single genre label for dropdown selection (e.g., SoundCloud genre field)."""
        # Map to SoundCloud's genre dropdown values
        genre_map = {
            "house": "House",
            "deep house": "Deep House",
            "tech house": "Tech House",
            "techno": "Techno",
            "drum and bass": "Drum & Bass",
            "trance": "Trance",
            "dubstep": "Dubstep",
            "ambient": "Ambient",
            "breakbeat": "Breakbeat",
            "electronic": "Electronic",
        }
        for genre in genres:
            mapped = genre_map.get(genre.lower())
            if mapped:
                return mapped
        return "Electronic"
