"""Catalog tracklist backfill: match local audio to imported mixes, analyze,
and refresh their live descriptions with the detected tracklist.

The back-catalog sync imports platform-only ``Mix`` rows (``source="imported"``)
that carry no ``audio_file_path`` and no tracklist. The watch folder, however,
still holds many of the original recordings. ``run_backfill`` (behind
``POST /api/catalog/backfill-tracklists``) closes that gap:

1. **Match** (:func:`match_local_audio`, pure/unit-testable): scan
   ``WATCH_AUDIO_PATH`` (+ any ``CATALOG_EXTRA_AUDIO_PATHS``) and pair each
   audio file with at most one imported-without-tracklist mix by filename date
   vs platform publish dates (±3 days), normalized filename-vs-title
   similarity, and — when both sides know a duration — a REQUIRED ±120s
   duration confirmation.
2. **Analyze** each matched pair sequentially (Shazam fingerprinting a 2h set
   takes ~10 minutes, so no parallelism) through the same
   ``analyze_audio_with_cue`` helper the pipeline uses (CUE sheets win over
   fingerprints), storing tracklist/genres/duration on the mix.
3. **Propose**: rebuild BOTH platform descriptions — strip any stale
   ``Tracklist:`` block from the live text and inject the fresh one before the
   brand-links section — as APPROVED ``MixProposal`` rows. The apply worker is
   NOT auto-run; the user pushes when ready.

Progress lands in ``catalog_backfill`` activity events; the run summary
(including the unmatched-mixes report) persists under
``AppSettings.settings_json["catalog_last_backfill"]``. A stored cancel flag
(``catalog_backfill_cancel``) is honored between mixes.
"""

import asyncio
import logging
import os
import re
from datetime import date, datetime, timezone
from difflib import SequenceMatcher
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import select

from app.config import settings
from app.database import async_session_factory
from app.models import Mix, MixProposal
from app.services import activity_log
from app.services.catalog_improve import extract_tracklist_block
from app.services.catalog_match import normalize_title
from app.services.catalog_sync import _get_settings_row
from app.services.tracklist_utils import format_timestamp

logger = logging.getLogger(__name__)

LAST_BACKFILL_KEY = "catalog_last_backfill"
BACKFILL_CANCEL_KEY = "catalog_backfill_cancel"

AUDIO_EXTENSIONS = {".flac", ".mp3", ".mp4", ".m4a", ".wav", ".aiff", ".ogg"}

# Matching thresholds
DATE_WINDOW_DAYS = 3
DURATION_TOLERANCE_SECONDS = 120
TITLE_SIMILARITY_THRESHOLD = 0.6

# The brand-links section of every generated description starts with the
# Twitch line (see settings.SOUNDCLOUD_LINKS / YOUTUBE_LINKS and the
# ``_ensure_tracklist_section`` pipeline helper this mirrors).
LINKS_MARKER = "\nTwitch: https://"

_DATE_YMD_RE = re.compile(r"(\d{4})-(\d{2})-(\d{2})")
_DATE_MDY_RE = re.compile(r"\b(\d{2})-(\d{2})-(\d{4})\b")


# ---------------------------------------------------------------------------
# Pure matching helpers
# ---------------------------------------------------------------------------

def extract_filename_date(filename: str) -> Optional[date]:
    """Recording date from a filename: ``YYYY-MM-DD`` first, then
    ``MM-DD-YYYY``. Returns None when no valid date is present."""
    m = _DATE_YMD_RE.search(filename)
    if m:
        try:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            pass
    m = _DATE_MDY_RE.search(filename)
    if m:
        try:
            return date(int(m.group(3)), int(m.group(1)), int(m.group(2)))
        except ValueError:
            pass
    return None


def _strip_dates(text: str) -> str:
    text = _DATE_YMD_RE.sub(" ", text)
    return _DATE_MDY_RE.sub(" ", text)


def filename_title_similarity(filename: str, title: str) -> float:
    """Similarity (0.0-1.0) between an audio filename and a mix title.

    Both sides go through the catalog matcher's normalization (lowercase,
    punctuation and boilerplate stripped) with date tokens removed first —
    dates are matched separately and would otherwise dominate. The score is
    the max of the SequenceMatcher ratio and the shared-token containment
    ratio (the containment path requires >= 2 shared tokens so a lone common
    word can never carry a match).
    """
    stem = os.path.splitext(os.path.basename(filename))[0]
    na = normalize_title(_strip_dates(stem))
    nb = normalize_title(_strip_dates(title))
    if not na or not nb:
        return 0.0
    ratio = SequenceMatcher(None, na, nb).ratio()
    ta, tb = set(na.split()), set(nb.split())
    shared = ta & tb
    containment = 0.0
    if len(shared) >= 2:
        containment = len(shared) / min(len(ta), len(tb))
    return max(ratio, containment)


def _min_date_gap_days(
    file_date: Optional[date], published_dates: List[date]
) -> Optional[int]:
    if file_date is None or not published_dates:
        return None
    return min(abs((file_date - d).days) for d in published_dates)


def match_local_audio(
    files: List[Dict[str, Any]], mixes: List[Dict[str, Any]]
) -> Dict[str, Any]:
    """Pair local audio files with imported mixes missing tracklists.

    Pure function — no I/O. Inputs:

    * ``files``: ``{"path", "filename", "date": date|None,
      "duration_seconds": float|None}`` (see :func:`scan_local_audio`).
    * ``mixes``: ``{"id", "title", "published_dates": [date, ...],
      "duration_seconds": float|None}``.

    A (file, mix) pair qualifies when EITHER the filename date falls within
    ±``DATE_WINDOW_DAYS`` of any platform publish date OR the normalized
    filename/title similarity reaches ``TITLE_SIMILARITY_THRESHOLD`` — and,
    whenever BOTH sides carry a duration, the durations agree within
    ±``DURATION_TOLERANCE_SECONDS`` (a duration mismatch disqualifies
    outright). Pairs are consumed best-score-first; a file matches at most one
    mix and vice versa.

    Returns ``{"matches": [(mix_id, file, reason)], "unmatched_mixes": [...],
    "unmatched_files": [...]}``.
    """
    scored: List[Tuple[float, str, Dict, Dict]] = []
    for f in files:
        for m in mixes:
            fd = f.get("duration_seconds")
            md = m.get("duration_seconds")
            durations_known = fd is not None and md is not None
            if durations_known and abs(float(fd) - float(md)) > DURATION_TOLERANCE_SECONDS:
                continue  # REQUIRED confirmation failed

            gap = _min_date_gap_days(f.get("date"), m.get("published_dates") or [])
            date_ok = gap is not None and gap <= DATE_WINDOW_DAYS
            sim = filename_title_similarity(f.get("filename") or "", m.get("title") or "")
            title_ok = sim >= TITLE_SIMILARITY_THRESHOLD
            if not date_ok and not title_ok:
                continue

            reasons = []
            if date_ok:
                reasons.append("date" if gap else "date-exact")
            if title_ok:
                reasons.append("title")
            if durations_known:
                reasons.append("duration")

            score = sim
            if date_ok:
                score += 2.0 if gap == 0 else 1.5
            if durations_known:
                score += 0.5
            scored.append((score, "+".join(reasons), f, m))

    scored.sort(key=lambda t: t[0], reverse=True)

    matches: List[Tuple[str, Dict, str]] = []
    taken_files: set = set()
    taken_mixes: set = set()
    for _score, reason, f, m in scored:
        if f["path"] in taken_files or m["id"] in taken_mixes:
            continue
        taken_files.add(f["path"])
        taken_mixes.add(m["id"])
        matches.append((m["id"], f, reason))

    return {
        "matches": matches,
        "unmatched_mixes": [m for m in mixes if m["id"] not in taken_mixes],
        "unmatched_files": [f for f in files if f["path"] not in taken_files],
    }


# ---------------------------------------------------------------------------
# Scanning (cheap I/O: directory listing + mutagen headers, no decoding)
# ---------------------------------------------------------------------------

def scan_local_audio(paths: Optional[List[str]] = None) -> List[Dict[str, Any]]:
    """List candidate audio files in the watch dir (+ configured extras).

    Durations come from mutagen header reads (cheap — no decode); a file whose
    header cannot be read still participates, just without the duration
    confirmation.
    """
    from mutagen import File as MutagenFile

    dirs = (
        paths
        if paths is not None
        else [settings.WATCH_AUDIO_PATH, *settings.get_catalog_extra_audio_paths()]
    )
    out: List[Dict[str, Any]] = []
    seen: set = set()
    for d in dirs:
        if not os.path.isdir(d):
            logger.debug("Backfill scan: not a directory, skipping: %s", d)
            continue
        for fname in sorted(os.listdir(d)):
            if os.path.splitext(fname)[1].lower() not in AUDIO_EXTENSIONS:
                continue
            path = os.path.join(d, fname)
            if not os.path.isfile(path):
                continue
            real = os.path.realpath(path)
            if real in seen:
                continue
            seen.add(real)
            duration = None
            try:
                mf = MutagenFile(path)
                if mf is not None and mf.info is not None:
                    duration = float(mf.info.length)
            except Exception:
                logger.debug("mutagen could not read %s", path, exc_info=True)
            out.append(
                {
                    "path": path,
                    "filename": fname,
                    "date": extract_filename_date(fname),
                    "duration_seconds": duration,
                }
            )
    return out


# ---------------------------------------------------------------------------
# Description rebuild
# ---------------------------------------------------------------------------

def build_tracklist_block(tracklist: List[Dict[str, Any]]) -> str:
    """Render the canonical ``Tracklist:`` block for a description."""
    lines = []
    for t in tracklist:
        ts = t.get("timestamp_formatted") or format_timestamp(
            float(t.get("timestamp_seconds", 0) or 0)
        )
        lines.append(f"{ts} {t.get('artist', 'ID')} - {t.get('title', 'ID')}")
    return "Tracklist:\n" + "\n".join(lines)


def rebuild_description_with_tracklist(
    description: Optional[str], tracklist: List[Dict[str, Any]]
) -> str:
    """Return the description with a fresh tracklist block in place.

    Any stale ``Tracklist:`` block is stripped first
    (:func:`catalog_improve.extract_tracklist_block`), then the new block is
    injected before the brand-links section (the Twitch line) when present —
    mirroring the pipeline's ``_ensure_tracklist_section`` — otherwise
    appended. An empty tracklist leaves the description untouched.
    """
    desc = (description or "").strip()
    if not tracklist:
        return desc
    stale = extract_tracklist_block(desc)
    if stale:
        desc = desc.replace(stale, "", 1)
        desc = re.sub(r"\n{3,}", "\n\n", desc).strip()
    block = build_tracklist_block(tracklist)
    if LINKS_MARKER in desc:
        desc = desc.replace(LINKS_MARKER, f"\n{block}\n{LINKS_MARKER}", 1)
    elif desc:
        desc = f"{desc}\n\n{block}"
    else:
        desc = block
    return desc


# ---------------------------------------------------------------------------
# Cancel flag (stored in settings_json, checked between mixes)
# ---------------------------------------------------------------------------

async def request_cancel() -> None:
    """Ask a running backfill to stop after the mix it is currently on."""
    async with async_session_factory() as session:
        row = await _get_settings_row(session)
        merged = dict(row.settings_json or {})
        merged[BACKFILL_CANCEL_KEY] = True
        row.settings_json = merged
        await session.commit()


async def _cancel_requested() -> bool:
    async with async_session_factory() as session:
        row = await _get_settings_row(session)
        return bool((row.settings_json or {}).get(BACKFILL_CANCEL_KEY))


async def _clear_cancel() -> None:
    async with async_session_factory() as session:
        row = await _get_settings_row(session)
        merged = dict(row.settings_json or {})
        if merged.pop(BACKFILL_CANCEL_KEY, None) is not None:
            row.settings_json = merged
            await session.commit()


# ---------------------------------------------------------------------------
# Backfill run
# ---------------------------------------------------------------------------

def _published_dates(mix: Mix) -> List[date]:
    """Platform publish dates from the catalog metadata the sync stored."""
    catalog_meta = (mix.metadata_json or {}).get("catalog") or {}
    out: List[date] = []
    for platform in ("youtube", "soundcloud"):
        published = (catalog_meta.get(platform) or {}).get("published_at")
        if not published:
            continue
        try:
            out.append(
                datetime.fromisoformat(str(published).replace("Z", "+00:00")).date()
            )
        except ValueError:
            continue
    return out


def _mix_projection(mix: Mix) -> Dict[str, Any]:
    return {
        "id": mix.id,
        "title": mix.title,
        "published_dates": _published_dates(mix),
        "duration_seconds": mix.duration_seconds,
    }


async def _backfill_one(mix_id: str, file_info: Dict[str, Any], reason: str) -> Dict[str, int]:
    """Analyze one matched (mix, file) pair and draft the description refresh.

    Returns ``{"tracks_found": n, "proposals_created": n}``.

    Session discipline (same pattern as catalog_playlists._save_placement):
    sqlite holds its single write lock from the first flushed write until
    COMMIT, and Shazam fingerprinting a 2-hour set takes ~10 minutes — a
    session held across the analysis would block every other writer (activity
    log, used_creative claims, concurrent jobs) for the whole run. So:

    (a) ``audio_file_path`` is assigned + committed in its OWN short session
        BEFORE the analysis;
    (b) ``analyze_audio_with_cue`` runs with NO session held (it takes no
        session — its activity events open their own short-lived ones);
    (c) results + proposals are written in a FRESH short session afterward.

    Regression-guarded by ``test_no_session_held_during_analysis``.
    """
    from app.services import handlers

    fname = file_info["filename"]
    await activity_log.info(
        "catalog_backfill",
        f"Analyzing {fname} (matched by {reason}) — Shazam fingerprinting a "
        f"long set can take ~10 minutes.",
        mix_id=mix_id, filename=fname,
        context={"reason": reason},
    )

    # (a) short session: link the audio file, commit, close.
    async with async_session_factory() as session:
        mix = await session.get(Mix, mix_id)
        if mix is None:
            raise RuntimeError(f"Mix {mix_id} not found")
        mix.audio_file_path = file_info["path"]
        await session.commit()

    # (b) the long analysis runs with no session (and no txn) open.
    result, tracklist, source = await handlers.analyze_audio_with_cue(
        file_info["path"], mix_id=mix_id
    )

    # (c) fresh short session: persist results + draft proposals.
    proposals_created = 0
    async with async_session_factory() as session:
        mix = await session.get(Mix, mix_id)
        if mix is None:
            raise RuntimeError(f"Mix {mix_id} disappeared mid-backfill")
        mix.tracklist = tracklist
        mix.genres = result.genres
        mix.vibes = result.vibes
        mix.energy_profile = result.energy_profile
        if result.duration_seconds:
            mix.duration_seconds = result.duration_seconds

        if tracklist:
            targets = [
                ("youtube", mix.description_youtube,
                 bool(mix.youtube_video_id or mix.youtube_url)),
                ("soundcloud", mix.description_soundcloud,
                 bool(mix.soundcloud_track_id or mix.soundcloud_url)),
            ]
            for platform, current, on_platform in targets:
                if not on_platform:
                    continue
                proposed = rebuild_description_with_tracklist(current, tracklist)
                if proposed == (current or "").strip():
                    continue
                session.add(
                    MixProposal(
                        mix_id=mix.id,
                        platform=platform,
                        field="description",
                        current_value=current,
                        proposed_value=proposed,
                        status="approved",
                        created_by="ai",
                    )
                )
                proposals_created += 1
        await session.commit()

    await activity_log.info(
        "catalog_backfill",
        f"Backfilled tracklist from {fname}: {len(tracklist)} tracks "
        f"(source: {source}); {proposals_created} description proposals approved.",
        mix_id=mix_id, filename=fname,
        context={
            "tracks_found": len(tracklist),
            "source": source,
            "matched_by": reason,
            "proposals_created": proposals_created,
        },
    )
    return {"tracks_found": len(tracklist), "proposals_created": proposals_created}


async def run_backfill(mix_ids: Optional[List[str]] = None) -> Dict[str, Any]:
    """Full tracklist backfill. Returns (and persists) the run summary."""
    started_at = datetime.now(timezone.utc)
    summary: Dict[str, Any] = {
        "started_at": started_at.isoformat(),
        "status": "running",
        "errors": [],
        "eligible_mixes": 0,
        "audio_files": 0,
        "matched": 0,
        "processed": 0,
        "tracks_found": 0,
        "proposals_created": 0,
        "failed": 0,
        "cancelled": False,
        "unmatched_files": 0,
        "unmatched_mixes": [],
    }
    await _clear_cancel()
    await activity_log.info("catalog_backfill", "Tracklist backfill started.")

    try:
        async with async_session_factory() as session:
            query = select(Mix).where(Mix.source == "imported")
            if mix_ids:
                query = query.where(Mix.id.in_(mix_ids))
            rows = (await session.execute(query)).scalars().all()
            mix_dicts = [_mix_projection(m) for m in rows if not m.tracklist]
        summary["eligible_mixes"] = len(mix_dicts)

        files = await asyncio.to_thread(scan_local_audio)
        summary["audio_files"] = len(files)

        plan = match_local_audio(files, mix_dicts)
        summary["matched"] = len(plan["matches"])
        summary["unmatched_files"] = len(plan["unmatched_files"])
        # The unmatched report: imported-without-tracklist mixes that found no
        # local audio (candidates for a future yt-dlp download path).
        summary["unmatched_mixes"] = [
            {"mix_id": m["id"], "title": m["title"]}
            for m in plan["unmatched_mixes"]
        ]
        await activity_log.info(
            "catalog_backfill",
            (
                f"Matched {summary['matched']} of {summary['eligible_mixes']} "
                f"imported mixes missing tracklists against "
                f"{summary['audio_files']} local audio files "
                f"({len(plan['unmatched_mixes'])} mixes have no local audio)."
            ),
            context={
                "matched": summary["matched"],
                "eligible_mixes": summary["eligible_mixes"],
                "audio_files": summary["audio_files"],
                "unmatched_mixes": len(plan["unmatched_mixes"]),
            },
        )

        titles = {m["id"]: m["title"] for m in mix_dicts}
        total = len(plan["matches"])
        for n, (mix_id, file_info, reason) in enumerate(plan["matches"], start=1):
            if await _cancel_requested():
                summary["cancelled"] = True
                await activity_log.warn(
                    "catalog_backfill",
                    f"Backfill cancelled after {summary['processed']}/{total} mixes.",
                )
                break
            await activity_log.info(
                "catalog_backfill",
                f"Backfilling mix {n}/{total}: {titles.get(mix_id, mix_id)}",
                mix_id=mix_id,
            )
            try:
                one = await _backfill_one(mix_id, file_info, reason)
                summary["processed"] += 1
                summary["tracks_found"] += one["tracks_found"]
                summary["proposals_created"] += one["proposals_created"]
            except Exception as exc:
                logger.exception("Backfill failed for mix %s", mix_id)
                summary["failed"] += 1
                summary["errors"].append(f"{titles.get(mix_id, mix_id)}: {exc}")
                await activity_log.error(
                    "catalog_backfill",
                    f"Backfill failed for {titles.get(mix_id, mix_id)}: {exc}",
                    mix_id=mix_id,
                )

        if summary["cancelled"]:
            summary["status"] = "cancelled"
        else:
            summary["status"] = "ok" if not summary["errors"] else "partial"

    except Exception as exc:  # pragma: no cover - defensive catch-all
        logger.exception("Tracklist backfill failed")
        summary["status"] = "failed"
        summary["errors"].append(str(exc))

    summary["finished_at"] = datetime.now(timezone.utc).isoformat()
    await _clear_cancel()

    try:
        async with async_session_factory() as session:
            settings_row = await _get_settings_row(session)
            merged = dict(settings_row.settings_json or {})
            merged[LAST_BACKFILL_KEY] = summary
            settings_row.settings_json = merged
            await session.commit()
    except Exception:  # pragma: no cover - defensive
        logger.exception("Failed to persist backfill summary")

    level = activity_log.info if summary["status"] == "ok" else activity_log.warn
    await level(
        "catalog_backfill",
        (
            f"Tracklist backfill {summary['status']}: {summary['processed']} of "
            f"{summary['matched']} matched mixes processed, "
            f"{summary['tracks_found']} tracks found, "
            f"{summary['proposals_created']} description proposals approved, "
            f"{len(summary['unmatched_mixes'])} mixes without local audio."
        ),
        context=summary,
    )
    return summary
