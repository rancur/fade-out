"""Back-catalog sync: fetch platform catalogs, match, and upsert Mix rows.

``run_catalog_sync`` is the background job behind ``POST /api/catalog/sync``:

1. Fetch every YouTube upload and every SoundCloud track (via the additive
   listing APIs on the existing uploaders, so OAuth handling is shared).
2. Run the pure matcher (:mod:`app.services.catalog_match`).
3. Resolve the matcher's *ambiguous* candidates with a compact yes/no batch
   prompt through the DescriptionGenerator's LLM client (AIUsage-tracked).
4. Upsert ``Mix`` rows: existing mixes get their platform ids backfilled and
   platform metadata refreshed; new pairs/singles become ``source="imported"``
   rows. Idempotent — keyed on platform ids, re-syncs never duplicate.

Progress and the final summary are emitted as ``catalog_sync`` activity
events; the last-run summary is persisted under
``AppSettings.settings_json["catalog_last_sync"]``.
"""

import json
import logging
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import select

from app.database import async_session_factory
from app.models import AppSettings, Mix
from app.services import activity_log
from app.services.catalog_match import MatchPlan, build_match_plan
from app.services.soundcloud_uploader import SoundCloudUploader
from app.services.youtube_uploader import YouTubeUploader

logger = logging.getLogger(__name__)

LAST_SYNC_KEY = "catalog_last_sync"

_ISO_DURATION_RE = re.compile(
    r"^P(?:(?P<days>\d+)D)?(?:T(?:(?P<hours>\d+)H)?(?:(?P<minutes>\d+)M)?(?:(?P<seconds>\d+)S)?)?$"
)


# ---------------------------------------------------------------------------
# Normalization
# ---------------------------------------------------------------------------

def parse_iso8601_duration(value: str) -> Optional[float]:
    """ISO8601 duration (``PT1H2M3S``) -> seconds, or None if unparseable."""
    if not value:
        return None
    m = _ISO_DURATION_RE.match(value.strip())
    if not m:
        return None
    parts = {k: int(v) for k, v in m.groupdict().items() if v}
    return float(
        parts.get("days", 0) * 86400
        + parts.get("hours", 0) * 3600
        + parts.get("minutes", 0) * 60
        + parts.get("seconds", 0)
    )


def _parse_datetime(value: Any) -> Optional[datetime]:
    """Parse YouTube ISO (``...Z``) and SoundCloud (``YYYY/MM/DD hh:mm:ss +0000``)."""
    if not value:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    text = str(value).strip()
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        pass
    try:
        return datetime.strptime(text, "%Y/%m/%d %H:%M:%S %z")
    except ValueError:
        return None


def normalize_youtube_item(raw: Dict[str, Any]) -> Dict[str, Any]:
    """Raw videos.list resource -> matcher item dict."""
    snippet = raw.get("snippet", {}) or {}
    thumbs = snippet.get("thumbnails", {}) or {}
    thumb_url = None
    for key in ("maxres", "standard", "high", "medium", "default"):
        if key in thumbs and thumbs[key].get("url"):
            thumb_url = thumbs[key]["url"]
            break
    video_id = raw.get("id")
    return {
        "platform": "youtube",
        "id": str(video_id),
        "title": snippet.get("title") or "",
        "description": snippet.get("description") or "",
        "url": f"https://www.youtube.com/watch?v={video_id}",
        "duration_seconds": parse_iso8601_duration(
            (raw.get("contentDetails") or {}).get("duration") or ""
        ),
        "published_at": _parse_datetime(snippet.get("publishedAt")),
        "thumbnail_url": thumb_url,
        "privacy_status": (raw.get("status") or {}).get("privacyStatus"),
    }


def normalize_soundcloud_item(raw: Dict[str, Any]) -> Dict[str, Any]:
    """Raw /me/tracks resource -> matcher item dict."""
    duration_ms = raw.get("duration")
    return {
        "platform": "soundcloud",
        "id": str(raw.get("id")),
        "title": raw.get("title") or "",
        "description": raw.get("description") or "",
        "url": raw.get("permalink_url") or "",
        "duration_seconds": (
            float(duration_ms) / 1000.0 if duration_ms is not None else None
        ),
        "published_at": _parse_datetime(raw.get("created_at")),
        "artwork_url": raw.get("artwork_url"),
        "sharing": raw.get("sharing"),
    }


# ---------------------------------------------------------------------------
# Platform fetchers
# ---------------------------------------------------------------------------

async def fetch_youtube_catalog(
    db_settings_json: Optional[Dict[str, Any]] = None,
) -> List[Dict[str, Any]]:
    """All YouTube uploads on the channel, normalized for the matcher."""
    uploader = YouTubeUploader(db_settings_json)
    raw = await uploader.list_all_uploads()
    return [normalize_youtube_item(r) for r in raw]


async def fetch_soundcloud_catalog(
    db_settings_json: Optional[Dict[str, Any]] = None,
    on_tokens_refreshed: Optional[Any] = None,
) -> List[Dict[str, Any]]:
    """All SoundCloud tracks owned by the user, normalized for the matcher."""
    uploader = SoundCloudUploader(db_settings_json, on_tokens_refreshed)
    raw = await uploader.list_all_tracks()
    return [normalize_soundcloud_item(r) for r in raw]


# ---------------------------------------------------------------------------
# LLM judge for ambiguous candidates
# ---------------------------------------------------------------------------

JUDGE_PROMPT = """\
You are matching a DJ's uploads across YouTube and SoundCloud. For each
numbered candidate pair below, answer whether the YouTube video and the
SoundCloud track are the SAME mix (same recording, possibly retitled).
Durations are in seconds; dates are upload dates.

{pairs_block}

Respond with ONLY a JSON array, one object per pair, e.g.
[{{"pair": 1, "same": true}}, {{"pair": 2, "same": false}}]\
"""


def _judge_pairs_block(candidates: List[Tuple[Dict, Dict]]) -> str:
    lines = []
    for i, (yt, sc) in enumerate(candidates, 1):
        lines.append(
            f"Pair {i}:\n"
            f"  YouTube: title={yt.get('title')!r} duration={yt.get('duration_seconds')} "
            f"published={yt.get('published_at')}\n"
            f"  SoundCloud: title={sc.get('title')!r} duration={sc.get('duration_seconds')} "
            f"published={sc.get('published_at')}"
        )
    return "\n".join(lines)


def _parse_judge_response(text: str, n: int) -> List[bool]:
    """Parse the judge's JSON array into a same/not-same list of length n."""
    cleaned = text.strip()
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", cleaned, flags=re.S)
    data = json.loads(cleaned)
    verdicts = [False] * n
    for entry in data:
        idx = int(entry.get("pair", 0)) - 1
        if 0 <= idx < n:
            verdicts[idx] = bool(entry.get("same"))
    return verdicts


async def resolve_ambiguous_with_llm(
    candidates: List[Tuple[Dict, Dict]],
    db_settings_json: Optional[Dict[str, Any]] = None,
    session=None,
) -> Tuple[List[Tuple[Dict, Dict, float, str]], List[Dict]]:
    """Judge ambiguous candidates in one compact batch prompt.

    Returns ``(pairs, singles)``: judged-same candidates as
    ``(yt, sc, 0.6, "llm-judge")`` pairs, everything else split into singles.
    On any LLM failure all candidates fall back to singles (never guess).
    """
    if not candidates:
        return [], []

    from app.services.description_generator import DescriptionGenerator

    pairs: List[Tuple[Dict, Dict, float, str]] = []
    singles: List[Dict] = []
    try:
        generator = DescriptionGenerator(db_settings_json)
        prompt = JUDGE_PROMPT.format(pairs_block=_judge_pairs_block(candidates))
        response, text = await generator._create_completion(
            prompt, max_tokens=30 * len(candidates) + 50, temperature=0.0
        )
        verdicts = _parse_judge_response(text, len(candidates))
        if session is not None:
            await generator._track_usage(
                session,
                None,
                "catalog_match_judge",
                response.usage.prompt_tokens,
                response.usage.completion_tokens,
            )
    except Exception as exc:
        logger.warning("Catalog LLM judge failed; treating all as singles: %s", exc)
        for yt, sc in candidates:
            singles.extend([yt, sc])
        return [], singles

    for (yt, sc), same in zip(candidates, verdicts):
        if same:
            pairs.append((yt, sc, 0.6, "llm-judge"))
        else:
            singles.extend([yt, sc])
    return pairs, singles


# ---------------------------------------------------------------------------
# Upsert
# ---------------------------------------------------------------------------

def _catalog_meta(item: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """JSON-safe platform metadata stored under metadata_json['catalog']."""
    if not item:
        return None
    published = item.get("published_at")
    meta = {
        "title": item.get("title"),
        "url": item.get("url"),
        "duration_seconds": item.get("duration_seconds"),
        "published_at": published.isoformat() if published else None,
    }
    if item["platform"] == "youtube":
        meta["thumbnail_url"] = item.get("thumbnail_url")
        meta["privacy_status"] = item.get("privacy_status")
    else:
        meta["artwork_url"] = item.get("artwork_url")
        meta["sharing"] = item.get("sharing")
    return meta


def _attach_platform_data(
    mix: Mix, yt: Optional[Dict], sc: Optional[Dict]
) -> None:
    """Backfill ids/urls/descriptions/metadata from platform items onto a Mix."""
    meta = dict(mix.metadata_json or {})
    catalog_meta = dict(meta.get("catalog") or {})

    if yt:
        mix.youtube_video_id = yt["id"]
        mix.youtube_url = yt["url"]
        if not mix.description_youtube and yt.get("description"):
            mix.description_youtube = yt["description"]
        # Titles are unified: one string per mix, identical on both platforms
        # (see catalog_improve / description_generator). Backfilling
        # ``title_youtube`` from whatever the video is currently called would
        # re-open the per-platform split every time a sync runs — a mix whose
        # SoundCloud title is the canonical one would silently regain a
        # different YouTube title, and the improve pass would have to unify it
        # again. So only adopt the live value when it AGREES with mix.title
        # (or the mix has no title yet); a disagreeing live title is left for
        # a title proposal to overwrite. Nothing is lost either way: the live
        # YouTube title is always recorded under
        # metadata_json['catalog']['youtube']['title'] below.
        if not mix.title_youtube and yt.get("title"):
            live_title = yt["title"].strip()
            if not mix.title or live_title == (mix.title or "").strip():
                mix.title_youtube = yt["title"]
        if mix.duration_seconds is None and yt.get("duration_seconds") is not None:
            mix.duration_seconds = yt["duration_seconds"]
        catalog_meta["youtube"] = _catalog_meta(yt)
    if sc:
        mix.soundcloud_track_id = sc["id"]
        mix.soundcloud_url = sc["url"]
        if not mix.description_soundcloud and sc.get("description"):
            mix.description_soundcloud = sc["description"]
        if mix.duration_seconds is None and sc.get("duration_seconds") is not None:
            mix.duration_seconds = sc["duration_seconds"]
        catalog_meta["soundcloud"] = _catalog_meta(sc)

    meta["catalog"] = catalog_meta
    mix.metadata_json = meta


def _new_imported_mix(yt: Optional[Dict], sc: Optional[Dict]) -> Mix:
    """A fresh Mix row for an imported pair/single (no pipeline steps)."""
    # Prefer the SoundCloud title (the creative one) when both exist.
    title = (sc or {}).get("title") or (yt or {}).get("title") or "Untitled import"
    mix = Mix(
        title=title,
        source="imported",
        pipeline_status="imported",
    )
    _attach_platform_data(mix, yt, sc)
    return mix


async def _upsert_plan(
    session, plan_pairs, plan_singles, plan_existing
) -> Dict[str, int]:
    """Apply the match plan to the DB (idempotent by platform ids)."""
    counts = {"existing_updated": 0, "pairs_created": 0, "singles_created": 0}

    mixes_by_id = {}
    result = await session.execute(select(Mix))
    for m in result.scalars().all():
        mixes_by_id[m.id] = m

    for link in plan_existing:
        mix = mixes_by_id.get(link["mix_id"])
        if not mix:
            continue
        _attach_platform_data(mix, link.get("youtube"), link.get("soundcloud"))
        counts["existing_updated"] += 1

    # Safety net for idempotency: never insert when a row already owns one of
    # the platform ids (covers races the matcher's claim step can't see).
    async def _already_present(yt: Optional[Dict], sc: Optional[Dict]) -> bool:
        conditions = []
        if yt:
            conditions.append(Mix.youtube_video_id == yt["id"])
        if sc:
            conditions.append(Mix.soundcloud_track_id == sc["id"])
        if not conditions:
            return True
        from sqlalchemy import or_

        found = await session.execute(select(Mix.id).where(or_(*conditions)))
        return found.first() is not None

    for yt, sc, _confidence, _reason in plan_pairs:
        if await _already_present(yt, sc):
            continue
        session.add(_new_imported_mix(yt, sc))
        counts["pairs_created"] += 1

    for item in plan_singles:
        yt = item if item["platform"] == "youtube" else None
        sc = item if item["platform"] == "soundcloud" else None
        if await _already_present(yt, sc):
            continue
        session.add(_new_imported_mix(yt, sc))
        counts["singles_created"] += 1

    return counts


# ---------------------------------------------------------------------------
# Sync entry point
# ---------------------------------------------------------------------------

async def _get_settings_row(session) -> AppSettings:
    result = await session.execute(select(AppSettings).where(AppSettings.id == 1))
    row = result.scalar_one_or_none()
    if not row:
        row = AppSettings(id=1)
        session.add(row)
        await session.flush()
    return row


async def run_catalog_sync() -> Dict[str, Any]:
    """Full back-catalog sync. Returns (and persists) the run summary."""
    started_at = datetime.now(timezone.utc)
    summary: Dict[str, Any] = {
        "started_at": started_at.isoformat(),
        "status": "running",
        "errors": [],
    }
    await activity_log.info("catalog_sync", "Catalog sync started.")

    try:
        async with async_session_factory() as session:
            settings_row = await _get_settings_row(session)
            sj = dict(settings_row.settings_json or {})
            await session.commit()

        async def _persist_sc_tokens(access_token: str, refresh_token: str) -> None:
            async with async_session_factory() as s:
                row = await _get_settings_row(s)
                merged = dict(row.settings_json or {})
                merged["soundcloud_access_token"] = access_token
                merged["soundcloud_refresh_token"] = refresh_token
                row.settings_json = merged
                await s.commit()

        yt_items: List[Dict] = []
        sc_items: List[Dict] = []
        try:
            yt_items = await fetch_youtube_catalog(sj)
        except Exception as exc:
            summary["errors"].append(f"youtube: {exc}")
            await activity_log.warn(
                "catalog_sync", f"YouTube catalog fetch failed: {exc}",
                platform="youtube",
            )
        try:
            sc_items = await fetch_soundcloud_catalog(sj, _persist_sc_tokens)
        except Exception as exc:
            summary["errors"].append(f"soundcloud: {exc}")
            await activity_log.warn(
                "catalog_sync", f"SoundCloud catalog fetch failed: {exc}",
                platform="soundcloud",
            )
        summary["youtube_items"] = len(yt_items)
        summary["soundcloud_items"] = len(sc_items)
        await activity_log.info(
            "catalog_sync",
            f"Fetched {len(yt_items)} YouTube uploads and {len(sc_items)} SoundCloud tracks.",
        )

        async with async_session_factory() as session:
            result = await session.execute(select(Mix))
            existing = [
                {
                    "id": m.id,
                    "youtube_video_id": m.youtube_video_id,
                    "soundcloud_track_id": m.soundcloud_track_id,
                    "youtube_url": m.youtube_url,
                    "soundcloud_url": m.soundcloud_url,
                }
                for m in result.scalars().all()
            ]

        plan: MatchPlan = build_match_plan(yt_items, sc_items, existing)

        async with async_session_factory() as session:
            judged_pairs, judged_singles = await resolve_ambiguous_with_llm(
                plan.ambiguous, sj, session
            )
            await session.commit()

        all_pairs = list(plan.pairs) + judged_pairs
        all_singles = list(plan.singles) + judged_singles
        summary["matched_pairs"] = len(all_pairs)
        summary["ambiguous_judged"] = len(plan.ambiguous)
        summary["singles"] = len(all_singles)

        async with async_session_factory() as session:
            counts = await _upsert_plan(
                session, all_pairs, all_singles, plan.existing_links
            )
            await session.commit()
        summary.update(counts)
        summary["status"] = "ok" if not summary["errors"] else "partial"

    except Exception as exc:  # pragma: no cover - defensive catch-all
        logger.exception("Catalog sync failed")
        summary["status"] = "failed"
        summary["errors"].append(str(exc))

    summary["finished_at"] = datetime.now(timezone.utc).isoformat()

    try:
        async with async_session_factory() as session:
            settings_row = await _get_settings_row(session)
            merged = dict(settings_row.settings_json or {})
            merged[LAST_SYNC_KEY] = summary
            settings_row.settings_json = merged
            await session.commit()
    except Exception:  # pragma: no cover - defensive
        logger.exception("Failed to persist catalog sync summary")

    level = activity_log.info if summary["status"] == "ok" else activity_log.warn
    await level(
        "catalog_sync",
        (
            f"Catalog sync {summary['status']}: "
            f"{summary.get('pairs_created', 0)} pairs + "
            f"{summary.get('singles_created', 0)} singles imported, "
            f"{summary.get('existing_updated', 0)} existing updated."
        ),
        context=summary,
    )
    return summary
