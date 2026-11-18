"""Check a stuck mix's SoundCloud leg against SoundCloud as it is right now.

Incident 2026-11-18: "Mirage Groove Reverie" was reported every two days as
"live on SoundCloud but not published to YouTube", and the owner was offered a
re-render and upload. Neither half was true any more:

* the pipeline's SoundCloud upload had been deleted (the track 404s). The
  "live on soundcloud" text was the ``pipeline_error`` written when the
  publish step finished, repeated word for word by the stuck-mix watchdog
  long after it stopped being true;
* the recording was already out as the owner's own upload, "SEE/K - Live at
  Beats & Branches 2026": same audio, same duration to the millisecond. No
  catalog row existed for it yet, so the duplicate guard could not see it.

So before the watchdog reports a mix as stuck (which is what invites a
re-render or re-upload), it asks SoundCloud two things:

1. Does the track the pipeline recorded still exist? A stored URL or id is a
   claim about the past, not evidence of the present.
2. Is there already an upload on the account that the pipeline did not make,
   with the same duration or the same title? If so, that is the canonical
   copy: the mix is parked as a duplicate of it and nobody is asked to publish
   it again.

A failed lookup is reported as UNVERIFIED. It is never read as "live" and
never as "gone".
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, Iterable, List, Optional, Set

logger = logging.getLogger(__name__)

# The owner's copy of a recording is a re-encode of the same audio, so the
# lengths agree to well under a second (the Mirage pair: 8319.754 s vs
# 8,319,754 ms). Two different mixes landing inside one second of each other
# is the risk with a duration-only match, so the window is kept that tight.
DURATION_MATCH_SECONDS = 1.0
# A title match still has to agree on length, just more loosely.
TITLE_MATCH_DURATION_SECONDS = 5.0
# Ignore very short tracks (edits, previews): a duration match only means
# something for something mix-length.
MIN_MATCH_SECONDS = 600.0

SC_LIVE = "live"
SC_GONE = "gone"
SC_UNVERIFIED = "unverified"
SC_NONE = "none"  # the pipeline never recorded a SoundCloud upload

TrackLister = Callable[[], Awaitable[List[Dict[str, Any]]]]


def _norm_title(value: Optional[str]) -> str:
    """Lowercase alphanumerics only: "SEE/K - Live" == "SEEK - Live"."""
    return re.sub(r"[^a-z0-9]+", "", (value or "").lower())


def _permalink(url: Optional[str]) -> str:
    return (url or "").split("?")[0].rstrip("/").lower()


def _seconds(track: Dict[str, Any]) -> Optional[float]:
    try:
        ms = float(track.get("duration"))
    except (TypeError, ValueError):
        return None
    return ms / 1000.0 if ms > 0 else None


def _usable(track: Dict[str, Any]) -> bool:
    """A track a listener can actually hear (not failed, not blocked)."""
    return track.get("state") in (None, "finished") and track.get("policy") != "BLOCK"


def find_recorded_track(
    tracks: Iterable[Dict[str, Any]], track_id: Optional[str], url: Optional[str]
) -> Optional[Dict[str, Any]]:
    """The track the pipeline recorded, if it is still on the account."""
    link = _permalink(url)
    for t in tracks:
        if track_id and str(t.get("id")) == str(track_id):
            return t
        if link and _permalink(t.get("permalink_url")) == link:
            return t
    return None


def _title_candidates(mix: Dict[str, Any]) -> Set[str]:
    names = [mix.get("title"), mix.get("title_youtube")]
    for path in (mix.get("audio_file_path"), mix.get("video_file_path")):
        if path:
            names.append(os.path.splitext(os.path.basename(path))[0])
    out = set()
    for name in names:
        full = _norm_title(name)
        if full:
            out.add(full)
        # "Mirage Groove Reverie | House Mix" -> "Mirage Groove Reverie"
        stem = _norm_title(re.split(r"[:|]", name or "", maxsplit=1)[0])
        if len(stem) >= 6:
            out.add(stem)
    return out


def find_owner_copy(
    mix: Dict[str, Any],
    tracks: Iterable[Dict[str, Any]],
    pipeline_keys: Set[str],
) -> Optional[Dict[str, Any]]:
    """An upload the pipeline did not make that is this same recording.

    ``pipeline_keys`` holds the ids and permalinks of every SoundCloud upload
    a pipeline mix has recorded; anything else on the account was uploaded by
    the owner. Matches on duration (within :data:`DURATION_MATCH_SECONDS`) or
    on title (with durations within :data:`TITLE_MATCH_DURATION_SECONDS` when
    both are known). The closest duration wins.
    """
    try:
        duration = float(mix.get("duration_seconds") or 0) or None
    except (TypeError, ValueError):
        duration = None
    titles = _title_candidates(mix)

    best: Optional[Dict[str, Any]] = None
    best_gap = float("inf")
    for t in tracks:
        if str(t.get("id")) in pipeline_keys or _permalink(t.get("permalink_url")) in pipeline_keys:
            continue
        if not _usable(t):
            continue
        secs = _seconds(t)
        gap = abs(secs - duration) if (secs is not None and duration is not None) else None

        by_duration = (
            gap is not None and gap <= DURATION_MATCH_SECONDS
            and duration >= MIN_MATCH_SECONDS
        )
        by_title = _norm_title(t.get("title")) in titles and (
            gap is None or gap <= TITLE_MATCH_DURATION_SECONDS
        )
        if not (by_duration or by_title):
            continue
        rank = gap if gap is not None else TITLE_MATCH_DURATION_SECONDS
        if rank < best_gap:
            best_gap = rank
            best = dict(t)
            best["_match"] = [
                r for r, ok in (("same duration", by_duration), ("same title", by_title)) if ok
            ]
    return best


@dataclass
class SoundCloudVerdict:
    recorded: str = SC_NONE  # SC_LIVE / SC_GONE / SC_UNVERIFIED / SC_NONE
    recorded_ref: Optional[str] = None
    canonical: Optional[Dict[str, Any]] = None
    error: Optional[str] = None
    checked: bool = False
    extra: Dict[str, Any] = field(default_factory=dict)

    def summary(self) -> str:
        if self.recorded == SC_LIVE:
            text = f"SoundCloud: verified live just now ({self.recorded_ref})."
        elif self.recorded == SC_GONE:
            text = (
                f"SoundCloud: the pipeline's upload ({self.recorded_ref}) no longer "
                "exists on the account; it is NOT live."
            )
        elif self.recorded == SC_UNVERIFIED:
            text = (
                "SoundCloud: UNVERIFIED, the live check failed"
                + (f" ({self.error})" if self.error else "")
                + ". Do not treat any SoundCloud leg as live or as missing."
            )
        else:
            text = "SoundCloud: the pipeline recorded no upload."
        return text

    def to_dict(self) -> Dict[str, Any]:
        canon = None
        if self.canonical:
            canon = {
                "track_id": str(self.canonical.get("id")),
                "title": self.canonical.get("title"),
                "url": _permalink(self.canonical.get("permalink_url")),
                "duration_ms": self.canonical.get("duration"),
                "match": self.canonical.get("_match", []),
            }
        return {
            "recorded": self.recorded,
            "recorded_ref": self.recorded_ref,
            "canonical": canon,
            "error": self.error,
        }


def verdict_for(
    mix: Dict[str, Any],
    tracks: Optional[List[Dict[str, Any]]],
    pipeline_keys: Set[str],
    error: Optional[str] = None,
) -> SoundCloudVerdict:
    """Classify one mix. ``tracks`` is None when the lookup failed."""
    track_id = mix.get("soundcloud_track_id")
    url = mix.get("soundcloud_url")
    ref = str(track_id) if track_id else (_permalink(url) or None)
    v = SoundCloudVerdict(recorded_ref=ref)
    if tracks is None:
        v.recorded = SC_UNVERIFIED if ref else SC_NONE
        v.error = error
        return v
    v.checked = True
    if ref:
        hit = find_recorded_track(tracks, track_id, url)
        v.recorded = SC_LIVE if (hit is not None and _usable(hit)) else SC_GONE
    own_keys = {k for k in (str(track_id) if track_id else None, _permalink(url)) if k}
    # The mix's own recorded upload is the pipeline's, never the owner's copy.
    v.canonical = find_owner_copy(mix, tracks, pipeline_keys | own_keys)
    return v


async def default_track_lister() -> List[Dict[str, Any]]:
    """Every track on the authed SoundCloud account, via the API, right now."""
    from app.database import async_session_factory
    from app.models import AppSettings
    from app.services.handlers import _sc_token_persister
    from app.services.soundcloud_uploader import SoundCloudUploader

    async with async_session_factory() as session:
        app_settings = await session.get(AppSettings, 1)
    sj = (app_settings.settings_json or {}) if app_settings else {}
    uploader = SoundCloudUploader(
        db_settings_json=sj,
        on_tokens_refreshed=_sc_token_persister(app_settings),
        emit_activity=False,
    )
    return await uploader.list_all_tracks()
