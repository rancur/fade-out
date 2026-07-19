"""Pure matching logic for back-catalog import.

Given the full list of YouTube uploads, SoundCloud tracks, and the existing
``Mix`` rows, produce a *match plan*: which platform items belong to existing
mixes, which YT/SC items are the same mix published on both platforms, which
pairs are too ambiguous to decide mechanically (deferred to an LLM judge by the
sync service — this module NEVER calls the LLM), and which items stand alone.

No I/O, no DB, no network: everything here is deterministic and unit-testable
with plain dicts.

Item shape (produced by the fetchers in ``catalog_sync``)::

    {
        "platform": "youtube" | "soundcloud",
        "id": str,                 # video id / track id (str)
        "title": str,
        "description": str,
        "url": str,                # watch URL / permalink URL
        "duration_seconds": float,
        "published_at": datetime | None,
        ...platform extras (thumbnail_url / artwork_url / privacy...)
    }

Existing-mix shape (lightweight projection of Mix rows)::

    {
        "id": str,
        "youtube_video_id": str | None,
        "soundcloud_track_id": str | None,
        "youtube_url": str | None,
        "soundcloud_url": str | None,
    }
"""

import re
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from typing import Any, Dict, List, Optional, Tuple

# Thresholds (see design spec section 6)
TITLE_SIMILARITY_THRESHOLD = 0.8
DURATION_TOLERANCE_SECONDS = 90
PUBLISH_WINDOW_DAYS = 14

# Boilerplate stripped from titles before similarity comparison. Order matters:
# multi-word phrases first so "dj set" goes before a lone "mix" pass.
_BOILERPLATE_PHRASES = ["official", "dj set", "mix"]

_YT_ID_RE = re.compile(
    r"(?:youtube\.com/watch\?(?:[^#\s]*&)?v=|youtu\.be/)([A-Za-z0-9_-]{4,})"
)
_SC_PERMALINK_RE = re.compile(r"(?:https?://)?(?:www\.)?soundcloud\.com/([\w\-/]+)")


@dataclass
class MatchPlan:
    """Output of :func:`build_match_plan`.

    * ``existing_links`` — platform items claimed by existing Mix rows:
      ``{"mix_id", "youtube": item|None, "soundcloud": item|None}``.
    * ``pairs`` — new cross-platform pairs: ``(yt_item, sc_item, confidence,
      reason)`` with reason ``"cross-link"`` or ``"title+duration"``.
    * ``ambiguous`` — candidate pairs the matcher cannot decide (duration and
      publish date align, but titles do not): resolved by the sync service via
      an LLM judge.
    * ``singles`` — leftover single-platform items.
    """

    existing_links: List[Dict[str, Any]] = field(default_factory=list)
    pairs: List[Tuple[Dict, Dict, float, str]] = field(default_factory=list)
    ambiguous: List[Tuple[Dict, Dict]] = field(default_factory=list)
    singles: List[Dict] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Normalization helpers
# ---------------------------------------------------------------------------

def normalize_title(title: str) -> str:
    """Lowercase, strip punctuation and boilerplate, collapse whitespace."""
    t = (title or "").lower()
    t = re.sub(r"[^\w\s]", " ", t)
    for phrase in _BOILERPLATE_PHRASES:
        t = re.sub(rf"\b{re.escape(phrase)}\b", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def title_similarity(a: str, b: str) -> float:
    """Similarity ratio of two normalized titles (0.0 - 1.0)."""
    na, nb = normalize_title(a), normalize_title(b)
    if not na or not nb:
        return 0.0
    return SequenceMatcher(None, na, nb).ratio()


def _durations_close(yt: Dict, sc: Dict) -> bool:
    dy = yt.get("duration_seconds")
    ds = sc.get("duration_seconds")
    if dy is None or ds is None:
        return False
    return abs(float(dy) - float(ds)) <= DURATION_TOLERANCE_SECONDS


def _published_close(yt: Dict, sc: Dict) -> bool:
    py = yt.get("published_at")
    ps = sc.get("published_at")
    if not py or not ps:
        return False
    return abs((py - ps).total_seconds()) <= PUBLISH_WINDOW_DAYS * 86400


# ---------------------------------------------------------------------------
# Cross-link detection
# ---------------------------------------------------------------------------

def _sc_permalink_path(url: str) -> Optional[str]:
    """The user/slug path portion of a SoundCloud permalink URL."""
    if not url:
        return None
    m = _SC_PERMALINK_RE.search(url)
    if not m:
        return None
    path = m.group(1).strip("/")
    return path or None


def _yt_description_links_sc(yt: Dict, sc: Dict) -> bool:
    """True when the YT description contains the SC track's permalink."""
    desc = yt.get("description") or ""
    path = _sc_permalink_path(sc.get("url") or "")
    if not path:
        return False
    return path in desc


def _sc_description_links_yt(sc: Dict, yt: Dict) -> bool:
    """True when the SC description contains the YT watch URL / video id."""
    desc = sc.get("description") or ""
    vid = str(yt.get("id") or "")
    if not vid:
        return False
    for m in _YT_ID_RE.finditer(desc):
        if m.group(1) == vid:
            return True
    return False


def _cross_linked(yt: Dict, sc: Dict) -> bool:
    """Either-direction cross-link counts (bidirectional obviously does too)."""
    return _yt_description_links_sc(yt, sc) or _sc_description_links_yt(sc, yt)


# ---------------------------------------------------------------------------
# Existing-mix claims
# ---------------------------------------------------------------------------

def _mix_claims_yt(mix: Dict, yt: Dict) -> bool:
    vid = str(yt.get("id") or "")
    if not vid:
        return False
    if mix.get("youtube_video_id") and str(mix["youtube_video_id"]) == vid:
        return True
    url = mix.get("youtube_url") or ""
    m = _YT_ID_RE.search(url)
    return bool(m and m.group(1) == vid)


def _mix_claims_sc(mix: Dict, sc: Dict) -> bool:
    tid = str(sc.get("id") or "")
    if tid and mix.get("soundcloud_track_id") and str(mix["soundcloud_track_id"]) == tid:
        return True
    mix_path = _sc_permalink_path(mix.get("soundcloud_url") or "")
    item_path = _sc_permalink_path(sc.get("url") or "")
    return bool(mix_path and item_path and mix_path == item_path)


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def build_match_plan(
    yt_items: List[Dict],
    sc_items: List[Dict],
    existing_mixes: List[Dict],
) -> MatchPlan:
    """Build the full match plan. Never double-assigns an item."""
    plan = MatchPlan()
    yt_pool: List[Dict] = list(yt_items)
    sc_pool: List[Dict] = list(sc_items)

    # (1) Existing Mix rows claim their platform items first.
    for mix in existing_mixes:
        claimed_yt = next((y for y in yt_pool if _mix_claims_yt(mix, y)), None)
        claimed_sc = next((s for s in sc_pool if _mix_claims_sc(mix, s)), None)
        if claimed_yt is None and claimed_sc is None:
            continue
        if claimed_yt is not None:
            yt_pool.remove(claimed_yt)
        if claimed_sc is not None:
            sc_pool.remove(claimed_sc)
        plan.existing_links.append(
            {"mix_id": mix["id"], "youtube": claimed_yt, "soundcloud": claimed_sc}
        )

    # (2) Cross-link URL detection (either direction).
    for yt in list(yt_pool):
        linked = next((sc for sc in sc_pool if _cross_linked(yt, sc)), None)
        if linked is not None:
            yt_pool.remove(yt)
            sc_pool.remove(linked)
            plan.pairs.append((yt, linked, 1.0, "cross-link"))

    # (3) Title similarity >= threshold AND duration within tolerance.
    # Score every remaining combination and take the best matches first so a
    # near-duplicate title can never steal another item's partner.
    scored: List[Tuple[float, Dict, Dict]] = []
    for yt in yt_pool:
        for sc in sc_pool:
            if not _durations_close(yt, sc):
                continue
            sim = title_similarity(yt.get("title") or "", sc.get("title") or "")
            if sim >= TITLE_SIMILARITY_THRESHOLD:
                scored.append((sim, yt, sc))
    scored.sort(key=lambda t: t[0], reverse=True)
    for sim, yt, sc in scored:
        if yt not in yt_pool or sc not in sc_pool:
            continue  # partner already taken by a better-scoring pair
        yt_pool.remove(yt)
        sc_pool.remove(sc)
        plan.pairs.append((yt, sc, sim, "title+duration"))

    # (4) Duration + publish-date alignment -> ambiguous (LLM judge decides).
    for yt in list(yt_pool):
        candidate = next(
            (
                sc
                for sc in sc_pool
                if _durations_close(yt, sc) and _published_close(yt, sc)
            ),
            None,
        )
        if candidate is not None:
            yt_pool.remove(yt)
            sc_pool.remove(candidate)
            plan.ambiguous.append((yt, candidate))

    # (5) Leftovers are single-platform mixes.
    plan.singles.extend(yt_pool)
    plan.singles.extend(sc_pool)
    return plan
