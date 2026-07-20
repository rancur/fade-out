"""Cross-platform playlist grouping for discoverability (YouTube + SoundCloud).

``run_organize_playlists`` (behind ``POST /api/catalog/organize-playlists``)
sorts every cataloged mix into ONE playlist bucket and makes sure it is a
member of that playlist on each platform it lives on:

* **Series first** — titles matching the recurring shows win outright:
  "Will See Wednesdays" and "2nd/Second Saturdays".
* **Raid trains** get a genre bucket derived from title + genres
  (DnB & Jungle, Dubstep & Bass, House, Trance, Techno, EDM & Big Room,
  Garage & Breaks, Melodic & Progressive, Open Format).
* Everything else falls back to a genre bucket from ``mix.genres``, then the
  catch-all "DJ Sets".

Existing platform playlists are respected: bucket names are fuzzy-matched
(normalized token containment) against what is already on the channel/profile
before any "Will See | {bucket}" playlist is created.

YouTube inserts (playlistItems.insert AND playlists.insert) cost 50 quota
units each and share the apply worker's daily budget record
(``catalog_yt_quota``); hitting the budget pauses the YouTube phase (remaining
placements stay queued for the next run — the whole flow is idempotent).
SoundCloud writes are unbudgeted.

Placements are recorded on the mix (``youtube_playlist_id`` kept, plus
``metadata_json.catalog.playlists.{platform}``) and the run summary persists
under ``AppSettings.settings_json["catalog_last_playlists"]``.
"""

import logging
import re
from datetime import date, datetime, timezone
from typing import Any, Dict, List, Optional, Tuple, Union

from sqlalchemy import select

from app.config import settings
from app.database import async_session_factory
from app.models import Mix
from app.services import activity_log, app_config
from app.services.catalog_apply import QUOTA_KEY, YT_WRITE_COST
from app.services.genre_utils import keyword_matches
from app.services.soundcloud_uploader import SoundCloudUploader
from app.services.youtube_uploader import YouTubeUploader

logger = logging.getLogger(__name__)

LAST_PLAYLISTS_KEY = "catalog_last_playlists"

# Real YouTube playlist ids are ~34 chars ("PL" + 32). A live run surfaced a
# 13-char id ("PLJPcS6-qELD0" — "PL" + what looks like an 11-char VIDEO id)
# that 404s on playlistItems.list; anything under this floor is treated as
# malformed and its membership listing skipped rather than trusted.
MIN_YT_PLAYLIST_ID_LEN = 20

# --- Buckets ---------------------------------------------------------------

SERIES_WEDNESDAYS = "Will See Wednesdays"
SERIES_SATURDAYS = "Second Saturdays"
DEFAULT_BUCKET = "DJ Sets"
RAID_FALLBACK_BUCKET = "Open Format"

GENRE_BUCKETS = [
    "DnB & Jungle",
    "Dubstep & Bass",
    "House",
    "Trance",
    "Techno",
    "EDM & Big Room",
    "Garage & Breaks",
    "Melodic & Progressive",
    "Open Format",
]

ALL_BUCKETS = [SERIES_WEDNESDAYS, SERIES_SATURDAYS, *GENRE_BUCKETS, DEFAULT_BUCKET]

_WEDNESDAYS_RE = re.compile(r"will\s*see\s*wednesday", re.IGNORECASE)
_SATURDAYS_RE = re.compile(r"\b(2nd|second)\s*saturday", re.IGNORECASE)
RAID_TRAIN_RE = re.compile(r"raid.?train", re.IGNORECASE)

# keyword -> bucket, ordered most specific first (the DnB phrases must win
# before the lone "bass" keyword can claim the Dubstep bucket, etc.).
_BUCKET_KEYWORDS: List[Tuple[str, str]] = [
    ("drum and bass", "DnB & Jungle"),
    ("drum & bass", "DnB & Jungle"),
    ("dnb", "DnB & Jungle"),
    ("d&b", "DnB & Jungle"),
    ("jungle", "DnB & Jungle"),
    ("liquid", "DnB & Jungle"),
    ("neurofunk", "DnB & Jungle"),
    ("dubstep", "Dubstep & Bass"),
    ("riddim", "Dubstep & Bass"),
    ("brostep", "Dubstep & Bass"),
    ("trap", "Dubstep & Bass"),
    ("bass", "Dubstep & Bass"),
    ("big room", "EDM & Big Room"),
    ("edm", "EDM & Big Room"),
    ("electro house", "EDM & Big Room"),
    ("garage", "Garage & Breaks"),
    ("ukg", "Garage & Breaks"),
    ("2-step", "Garage & Breaks"),
    ("2step", "Garage & Breaks"),
    ("breakbeat", "Garage & Breaks"),
    ("breaks", "Garage & Breaks"),
    ("melodic", "Melodic & Progressive"),
    ("progressive", "Melodic & Progressive"),
    ("organic", "Melodic & Progressive"),
    ("ambient", "Melodic & Progressive"),
    ("downtempo", "Melodic & Progressive"),
    ("trance", "Trance"),
    ("techno", "Techno"),
    ("house", "House"),  # after the more specific buckets; covers deep/tech house
    ("open format", "Open Format"),
    # Weakest signal — checked dead last so "Trance Festival" stays Trance.
    ("festival", "EDM & Big Room"),
]


def _genre_bucket(texts: List[str]) -> Optional[str]:
    """First keyword bucket (most specific wins) matching any of ``texts``."""
    for keyword, bucket in _BUCKET_KEYWORDS:
        for text in texts:
            if text and keyword_matches(text, keyword):
                return bucket
    return None


def classify_playlist(mix: Mix) -> str:
    """The single playlist bucket a mix belongs to (pure — no I/O).

    Series titles win first, then raid trains get a genre bucket from
    title + genres (falling back to "Open Format"). Plain mixes get a genre
    bucket from ``mix.genres``, then from TITLE keywords — imported mixes
    routinely carry empty genres (140/222 fell to the catch-all in the first
    live run), and titles like "DnB Tuesday" or "Pure Bass Therapy" classify
    fine — and only then the "DJ Sets" catch-all. Genres outrank the title so
    a keyword in the name never overrides real analysis data.
    """
    title = mix.title or ""
    if _WEDNESDAYS_RE.search(title):
        return SERIES_WEDNESDAYS
    if _SATURDAYS_RE.search(title):
        return SERIES_SATURDAYS
    genres = [g for g in (mix.genres or []) if g]
    if RAID_TRAIN_RE.search(title):
        return _genre_bucket([title, *genres]) or RAID_FALLBACK_BUCKET
    return _genre_bucket(genres) or _genre_bucket([title]) or DEFAULT_BUCKET


# --- Fuzzy playlist-name matching ------------------------------------------

# Filler words ignored when comparing playlist names to bucket names, so
# "Will See | House Mixes" and the "House" bucket land on the same tokens.
_GENERIC_NAME_TOKENS = {
    "will", "see", "mix", "mixes", "set", "sets", "playlist", "playlists",
    "the", "and", "raid", "train", "trains", "live", "music",
}

_NON_ALNUM_RE = re.compile(r"[^a-z0-9]+")


def _name_tokens(name: str) -> List[str]:
    """Lowercased alphanumeric tokens of a playlist/bucket name."""
    return [t for t in _NON_ALNUM_RE.split((name or "").lower()) if t]


def _stripped_tokens(name: str) -> List[str]:
    """Name tokens minus filler; falls back to the full token list when the
    strip would leave nothing (e.g. the "DJ Sets" bucket)."""
    tokens = _name_tokens(name)
    stripped = [t for t in tokens if t not in _GENERIC_NAME_TOKENS and t != "dj"]
    return stripped or tokens


def _match_score(playlist_title: str, bucket: str) -> int:
    """How well an existing playlist title matches a bucket name (0 = no).

    Exact normalized equality wins outright; then a playlist whose meaningful
    tokens are a subset of the bucket's ("EDM Mixes" -> "EDM & Big Room");
    then a playlist that contains every bucket token ("Will See DnB & Jungle
    Archive" -> "DnB & Jungle"). Closer coverage scores higher.
    """
    pt = _stripped_tokens(playlist_title)
    bt = _stripped_tokens(bucket)
    if not pt or not bt:
        return 0
    if pt == bt or _name_tokens(playlist_title) == _name_tokens(bucket):
        return 100
    ps, bs = set(pt), set(bt)
    if ps <= bs:
        return 90 - min(len(bs - ps), 19)
    if bs <= ps:
        return 70 - min(len(ps - bs), 19)
    return 0


def find_matching_playlist(
    bucket: str, playlists: List[Dict[str, Any]]
) -> Optional[Dict[str, Any]]:
    """Best existing playlist for a bucket (``{"id", "title", ...}``) or None.

    A playlist is only claimed for ``bucket`` when no other bucket matches it
    strictly better — "Will See | Deep House" must never be swallowed by the
    "House" lookup if a more specific bucket owns it.
    """
    best: Optional[Dict[str, Any]] = None
    best_score = 0
    for pl in playlists:
        title = pl.get("title") or ""
        score = _match_score(title, bucket)
        if score <= 0 or score < best_score:
            continue
        other_best = max(
            (_match_score(title, b) for b in ALL_BUCKETS if b != bucket),
            default=0,
        )
        if other_best > score:
            continue  # another bucket owns this playlist
        if score > best_score:
            best, best_score = pl, score
    return best


def playlist_title_for_bucket(bucket: str, prefix: Optional[str] = None) -> str:
    """Display title used when a bucket's playlist must be created."""
    if prefix is None:
        prefix = settings.YOUTUBE_DEFAULT_PLAYLIST_PREFIX
    if bucket.lower().startswith(prefix.lower()):
        return bucket
    return f"{prefix} | {bucket}"


# --- Placement recording ----------------------------------------------------

def record_placement(mix: Mix, platform: str, playlist_id: Any, title: str) -> None:
    """Append a playlist membership to ``metadata_json.catalog.playlists``."""
    meta = dict(mix.metadata_json or {})
    catalog = dict(meta.get("catalog") or {})
    placements = dict(catalog.get("playlists") or {})
    plat_list = list(placements.get(platform) or [])
    if not any(str(p.get("id")) == str(playlist_id) for p in plat_list):
        plat_list.append({"id": playlist_id, "title": title})
    placements[platform] = plat_list
    catalog["playlists"] = placements
    meta["catalog"] = catalog
    mix.metadata_json = meta


# --- Uploader factories (monkeypatchable in tests) --------------------------

def get_youtube_uploader(db_settings_json: Optional[Dict[str, Any]]) -> YouTubeUploader:
    return YouTubeUploader(db_settings_json)


def get_soundcloud_uploader(
    db_settings_json: Optional[Dict[str, Any]],
    on_tokens_refreshed: Optional[Any] = None,
) -> SoundCloudUploader:
    return SoundCloudUploader(db_settings_json, on_tokens_refreshed)


# --- Run --------------------------------------------------------------------

def _mix_projection(mix: Mix) -> Dict[str, Any]:
    return {
        "id": mix.id,
        "title": mix.title,
        "youtube_video_id": mix.youtube_video_id,
        "soundcloud_track_id": mix.soundcloud_track_id,
    }


async def _save_placement(
    mix_id: str, platform: str, playlist_id: Any, bucket: str
) -> None:
    """Persist one placement on the mix in a short-lived session.

    Sessions here (and everywhere in this run) are deliberately short: sqlite
    holds its single write lock from the first flushed write until COMMIT, so
    keeping an open write transaction across uploader/activity awaits would
    stall every other connection (observed as a hard wedge in testing).
    """
    async with async_session_factory() as session:
        mix = await session.get(Mix, mix_id)
        if mix is None:
            return
        if platform == "youtube":
            mix.youtube_playlist_id = playlist_id
        record_placement(mix, platform, playlist_id, bucket)
        await session.commit()


async def _persist_settings(**updates: Any) -> None:
    """Merge keys into AppSettings.settings_json in a short-lived session."""
    from app.services.catalog_sync import _get_settings_row

    async with async_session_factory() as session:
        settings_row = await _get_settings_row(session)
        merged = dict(settings_row.settings_json or {})
        merged.update(updates)
        settings_row.settings_json = merged
        await session.commit()


async def run_organize_playlists(
    mix_ids: Union[List[str], str, None] = None,
) -> Dict[str, Any]:
    """Classify + place every targeted mix into its platform playlists."""
    started_at = datetime.now(timezone.utc)
    summary: Dict[str, Any] = {
        "started_at": started_at.isoformat(),
        "status": "running",
        "errors": [],
        "targeted": 0,
        "buckets": {},
        "youtube": {
            "placed": 0, "already_member": 0, "created_playlists": 0,
            "queued": 0, "paused": False,
        },
        "soundcloud": {
            "placed": 0, "already_member": 0, "created_playlists": 0,
        },
    }
    budget = int(await app_config.resolve("youtube_daily_quota_budget"))
    today = date.today().isoformat()
    used = 0
    await activity_log.info("catalog_playlists", "Playlist organization started.")

    try:
        # Load settings + targets in a short session, then work from plain
        # dicts — no DB transaction stays open across uploader/activity calls.
        async with async_session_factory() as session:
            from app.services.catalog_sync import _get_settings_row

            settings_row = await _get_settings_row(session)
            sj = dict(settings_row.settings_json or {})
            quota = sj.get(QUOTA_KEY) or {}
            used = int(quota.get("used", 0)) if quota.get("date") == today else 0

            query = select(Mix)
            if isinstance(mix_ids, list):
                query = query.where(Mix.id.in_(mix_ids))
            rows = list((await session.execute(query)).scalars().all())
            by_bucket: Dict[str, List[Dict[str, Any]]] = {}
            targeted = 0
            for mix in rows:
                if not (mix.youtube_video_id or mix.soundcloud_track_id):
                    continue
                targeted += 1
                by_bucket.setdefault(classify_playlist(mix), []).append(
                    _mix_projection(mix)
                )
            await session.commit()
        summary["targeted"] = targeted
        summary["buckets"] = {b: len(ms) for b, ms in by_bucket.items()}

        async def _persist_sc_tokens(access_token: str, refresh_token: str) -> None:
            await _persist_settings(
                soundcloud_access_token=access_token,
                soundcloud_refresh_token=refresh_token,
            )

        # ---------------- YouTube phase (quota-budgeted) ----------------
        yt = summary["youtube"]
        if any(m["youtube_video_id"] for ms in by_bucket.values() for m in ms):
            try:
                uploader = get_youtube_uploader(sj)
                raw = await uploader.list_playlists()
                existing = [
                    {"id": p.get("id"),
                     "title": (p.get("snippet") or {}).get("title") or ""}
                    for p in raw
                ]

                for bucket in list(by_bucket):
                    bucket_mixes = [
                        m for m in by_bucket[bucket] if m["youtube_video_id"]
                    ]
                    if not bucket_mixes:
                        continue
                    if yt["paused"]:
                        yt["queued"] += len(bucket_mixes)
                        continue

                    # Per-bucket isolation: one playlist 404/failure (seen live
                    # with a malformed matched playlist id) must not abort the
                    # rest of the YouTube phase.
                    try:
                        match = find_matching_playlist(bucket, existing)
                        if match:
                            playlist_id = match["id"]
                            playlist_title = match["title"]
                        else:
                            if used + YT_WRITE_COST > budget:
                                yt["paused"] = True
                                yt["queued"] += len(bucket_mixes)
                                continue
                            playlist_title = playlist_title_for_bucket(
                                bucket,
                                await app_config.resolve("youtube_default_playlist_prefix"),
                            )
                            playlist_id = await uploader.create_playlist(
                                playlist_title,
                                description=f"{bucket} DJ sets by {settings.BRAND_NAME}",
                            )
                            used += YT_WRITE_COST
                            yt["created_playlists"] += 1
                            existing.append(
                                {"id": playlist_id, "title": playlist_title}
                            )
                            await activity_log.info(
                                "catalog_playlists",
                                f'Created YouTube playlist "{playlist_title}".',
                                platform="youtube",
                                context={"playlist_id": playlist_id},
                            )

                        # Guard: a real playlist id is ~34 chars. A short id
                        # (observed live: 13-char "PLJPcS6-qELD0") 404s on
                        # playlistItems.list — log it and treat the playlist
                        # as empty instead of aborting the platform.
                        if len(str(playlist_id or "")) < MIN_YT_PLAYLIST_ID_LEN:
                            logger.warning(
                                "YouTube playlist id %r for bucket %r looks "
                                "malformed (<%d chars); skipping membership "
                                "listing and treating it as empty",
                                playlist_id, bucket, MIN_YT_PLAYLIST_ID_LEN,
                            )
                            await activity_log.warn(
                                "catalog_playlists",
                                (
                                    f'YouTube playlist id "{playlist_id}" for '
                                    f'bucket "{bucket}" looks malformed; '
                                    f"treating the playlist as empty."
                                ),
                                platform="youtube",
                                context={
                                    "playlist_id": playlist_id, "bucket": bucket,
                                },
                            )
                            members = set()
                        else:
                            members = set(
                                await uploader.list_playlist_video_ids(playlist_id)
                            )
                        for m in bucket_mixes:
                            vid = m["youtube_video_id"]
                            if vid in members:
                                yt["already_member"] += 1
                                await _save_placement(
                                    m["id"], "youtube", playlist_id, bucket
                                )
                                continue
                            if used + YT_WRITE_COST > budget:
                                yt["paused"] = True
                                yt["queued"] += 1
                                continue
                            await uploader.add_video_to_playlist(playlist_id, vid)
                            used += YT_WRITE_COST
                            yt["placed"] += 1
                            members.add(vid)
                            await _save_placement(
                                m["id"], "youtube", playlist_id, bucket
                            )
                            await activity_log.info(
                                "catalog_playlists",
                                f'Added "{m["title"]}" to YouTube playlist '
                                f'"{playlist_title}".',
                                mix_id=m["id"],
                                platform="youtube",
                                context={
                                    "playlist_id": playlist_id, "bucket": bucket,
                                },
                            )
                    except Exception as exc:
                        logger.exception(
                            "YouTube playlist bucket %r failed", bucket
                        )
                        summary["errors"].append(f"youtube/{bucket}: {exc}")
                        await activity_log.error(
                            "catalog_playlists",
                            f'YouTube playlist bucket "{bucket}" failed: {exc}',
                            platform="youtube",
                            context={"bucket": bucket},
                        )
                if yt["paused"]:
                    await activity_log.warn(
                        "catalog_playlists",
                        (
                            f"YouTube quota budget reached ({used}/{budget} units "
                            f"used); {yt['queued']} playlist placements left "
                            f"queued for the next run."
                        ),
                        context={"used": used, "budget": budget},
                    )
            except Exception as exc:
                logger.exception("YouTube playlist phase failed")
                summary["errors"].append(f"youtube: {exc}")
                await activity_log.error(
                    "catalog_playlists",
                    f"YouTube playlist organization failed: {exc}",
                    platform="youtube",
                )

        # ---------------- SoundCloud phase (unbudgeted) -----------------
        sc = summary["soundcloud"]
        if any(m["soundcloud_track_id"] for ms in by_bucket.values() for m in ms):
            try:
                uploader = get_soundcloud_uploader(sj, _persist_sc_tokens)
                raw = await uploader.list_playlists()
                existing_sc = [
                    {
                        "id": p.get("id"),
                        "title": p.get("title") or "",
                        "track_ids": {
                            str(t.get("id"))
                            for t in (p.get("tracks") or [])
                            if t.get("id") is not None
                        },
                    }
                    for p in raw
                ]

                for bucket in list(by_bucket):
                    bucket_mixes = [
                        m for m in by_bucket[bucket] if m["soundcloud_track_id"]
                    ]
                    if not bucket_mixes:
                        continue

                    # Per-bucket isolation, mirroring the YouTube phase: one
                    # bucket's create/update failure must not zero out the rest.
                    try:
                        await _sc_place_bucket(
                            uploader, bucket, bucket_mixes, existing_sc, sc
                        )
                    except Exception as exc:
                        logger.exception(
                            "SoundCloud playlist bucket %r failed", bucket
                        )
                        summary["errors"].append(f"soundcloud/{bucket}: {exc}")
                        await activity_log.error(
                            "catalog_playlists",
                            f'SoundCloud playlist bucket "{bucket}" failed: {exc}',
                            platform="soundcloud",
                            context={"bucket": bucket},
                        )
            except Exception as exc:
                logger.exception("SoundCloud playlist phase failed")
                summary["errors"].append(f"soundcloud: {exc}")
                await activity_log.error(
                    "catalog_playlists",
                    f"SoundCloud playlist organization failed: {exc}",
                    platform="soundcloud",
                )

        summary["status"] = "ok" if not summary["errors"] else "partial"
    except Exception as exc:  # pragma: no cover - defensive catch-all
        logger.exception("Playlist organization failed")
        summary["status"] = "failed"
        summary["errors"].append(str(exc))

    summary["finished_at"] = datetime.now(timezone.utc).isoformat()

    try:
        await _persist_settings(
            **{
                QUOTA_KEY: {"date": today, "used": used},
                LAST_PLAYLISTS_KEY: summary,
            }
        )
    except Exception:  # pragma: no cover - defensive
        logger.exception("Failed to persist playlist organization summary")

    level = activity_log.info if summary["status"] == "ok" else activity_log.warn
    await level(
        "catalog_playlists",
        (
            f"Playlist organization {summary['status']}: "
            f"{summary['youtube']['placed']} YouTube + "
            f"{summary['soundcloud']['placed']} SoundCloud placements, "
            f"{summary['youtube']['created_playlists'] + summary['soundcloud']['created_playlists']} playlists created, "
            f"{summary['youtube']['queued']} queued on quota."
        ),
        context=summary,
    )
    return summary


async def _sc_place_bucket(
    uploader: SoundCloudUploader,
    bucket: str,
    bucket_mixes: List[Dict[str, Any]],
    existing_sc: List[Dict[str, Any]],
    sc: Dict[str, Any],
) -> None:
    """Place one bucket's mixes on SoundCloud (create or append)."""
    match = find_matching_playlist(bucket, existing_sc)
    if match is None:
        playlist_title = playlist_title_for_bucket(
            bucket, await app_config.resolve("youtube_default_playlist_prefix")
        )
        track_ids = [m["soundcloud_track_id"] for m in bucket_mixes]
        created = await uploader.create_playlist(
            playlist_title, track_ids, sharing="public"
        )
        playlist_id = created.get("id")
        sc["created_playlists"] += 1
        sc["placed"] += len(bucket_mixes)
        existing_sc.append(
            {
                "id": playlist_id,
                "title": playlist_title,
                "track_ids": {str(t) for t in track_ids},
            }
        )
        for m in bucket_mixes:
            await _save_placement(m["id"], "soundcloud", playlist_id, bucket)
        await activity_log.info(
            "catalog_playlists",
            (
                f'Created SoundCloud playlist "{playlist_title}" '
                f"with {len(track_ids)} tracks."
            ),
            platform="soundcloud",
            context={"playlist_id": playlist_id, "bucket": bucket},
        )
        return

    playlist_id = match["id"]
    playlist_title = match["title"]
    member_ids = match.get("track_ids") or set()
    for m in bucket_mixes:
        tid = str(m["soundcloud_track_id"])
        if tid in member_ids:
            sc["already_member"] += 1
            await _save_placement(m["id"], "soundcloud", playlist_id, bucket)
            continue
        await uploader.add_track_to_playlist(playlist_id, m["soundcloud_track_id"])
        sc["placed"] += 1
        member_ids.add(tid)
        await _save_placement(m["id"], "soundcloud", playlist_id, bucket)
        await activity_log.info(
            "catalog_playlists",
            f'Added "{m["title"]}" to SoundCloud playlist "{playlist_title}".',
            mix_id=m["id"],
            platform="soundcloud",
            context={"playlist_id": playlist_id, "bucket": bucket},
        )
