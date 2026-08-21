"""DJCTL CUE sheet parser and WebSocket listener for real-time track data."""

import asyncio
import json
import logging
import os
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional

import websockets

from app.config import settings
from app.services.tracklist_utils import ID_LABEL, label_or_id

logger = logging.getLogger(__name__)


@dataclass
class CueTrack:
    number: int
    title: str
    artist: str
    index_mm: int
    index_ss: int
    index_ff: int  # frames at 75 fps

    @property
    def timestamp_seconds(self) -> float:
        return self.index_mm * 60 + self.index_ss + self.index_ff / 75.0

    @property
    def timestamp_formatted(self) -> str:
        total = int(self.timestamp_seconds)
        h, remainder = divmod(total, 3600)
        m, s = divmod(remainder, 60)
        if h > 0:
            return f"{h}:{m:02d}:{s:02d}"
        return f"{m}:{s:02d}"

    def to_dict(self) -> dict:
        return {
            "title": self.title,
            "artist": self.artist,
            "timestamp_seconds": round(self.timestamp_seconds, 2),
            "timestamp_formatted": self.timestamp_formatted,
        }


@dataclass
class DJCTLResult:
    tracklist: List[Dict[str, Any]] = field(default_factory=list)
    source: str = "djctl"

    def to_dict(self) -> dict:
        return {
            "tracklist": self.tracklist,
            "source": self.source,
        }


# --------------------------------------------------------------------------
# CUE sheet parsing
# --------------------------------------------------------------------------

_RE_PERFORMER = re.compile(r'^\s*PERFORMER\s+"(.+)"', re.MULTILINE)
_RE_TITLE = re.compile(r'^\s*TITLE\s+"(.+)"', re.MULTILINE)
_RE_TRACK = re.compile(r'^\s*TRACK\s+(\d+)\s+AUDIO', re.MULTILINE)
_RE_INDEX = re.compile(r'^\s*INDEX\s+01\s+(\d+):(\d+):(\d+)', re.MULTILINE)


def parse_cue_sessions(cue_path: str) -> List[List[CueTrack]]:
    """Parse a CUE sheet into recording sessions.

    DJCTL appends a fresh PERFORMER/TITLE/FILE header block each time the
    recorder restarts (e.g. after a crash), so one .cue file can hold several
    sessions whose track timestamps each restart at 00:00. Treating them as
    one flat list produces timestamps that rewind mid-list — sessions must be
    kept separate and the caller picks the one that fits the audio.
    """
    with open(cue_path, "r", encoding="utf-8", errors="replace") as f:
        content = f.read()

    sessions: List[List[CueTrack]] = []
    tracks: List[CueTrack] = []
    current_track_num: Optional[int] = None
    current_title: Optional[str] = None
    current_performer: Optional[str] = None
    global_performer: Optional[str] = None

    def _close_session() -> None:
        nonlocal tracks
        if tracks:
            sessions.append(tracks)
            tracks = []

    for line in content.splitlines():
        line_stripped = line.strip()

        # A FILE directive after collected tracks = recorder restarted
        if line_stripped.upper().startswith("FILE") and tracks:
            _close_session()
            current_track_num = None
            continue

        # Global performer (before any TRACK)
        pm = _RE_PERFORMER.match(line_stripped)
        if pm and current_track_num is None:
            global_performer = pm.group(1)
            continue

        # Track start
        tm = _RE_TRACK.match(line_stripped)
        if tm:
            current_track_num = int(tm.group(1))
            current_title = None
            current_performer = None
            continue

        if current_track_num is not None:
            # Title inside track block
            ttm = _RE_TITLE.match(line_stripped)
            if ttm:
                current_title = ttm.group(1)
                continue

            # Performer inside track block
            pm2 = _RE_PERFORMER.match(line_stripped)
            if pm2:
                current_performer = pm2.group(1)
                continue

            # Index inside track block
            im = _RE_INDEX.match(line_stripped)
            if im:
                mm = int(im.group(1))
                ss = int(im.group(2))
                ff = int(im.group(3))
                track = CueTrack(
                    number=current_track_num,
                    title=current_title or f"Track {current_track_num}",
                    artist=current_performer or global_performer or ID_LABEL,
                    index_mm=mm,
                    index_ss=ss,
                    index_ff=ff,
                )
                # Timestamp rewind without a FILE header = session restart too
                if tracks and track.timestamp_seconds < tracks[-1].timestamp_seconds:
                    _close_session()
                tracks.append(track)
                current_track_num = None
                continue

    _close_session()
    logger.info(
        "Parsed %d session(s) (%s tracks) from CUE file %s",
        len(sessions), "/".join(str(len(s)) for s in sessions) or "0", cue_path,
    )
    return sessions


def parse_cue_file(cue_path: str) -> List[CueTrack]:
    """Parse a standard CUE sheet and return a flat list of tracks.

    Backward-compatible wrapper around :func:`parse_cue_sessions` — callers
    that know the mix duration should prefer :func:`select_cue_tracks`.
    """
    tracks = [t for session in parse_cue_sessions(cue_path) for t in session]
    logger.info("Parsed %d tracks from CUE file %s", len(tracks), cue_path)
    return tracks


def select_cue_tracks(
    cue_path: str,
    duration_seconds: Optional[float],
    min_coverage: float = 0.5,
    slack_seconds: float = 90.0,
) -> Optional[List[CueTrack]]:
    """Pick the CUE session that actually matches the recorded audio.

    A session is valid when its timestamps fit inside [0, duration+slack] and
    its span covers at least ``min_coverage`` of the mix — otherwise a CUE
    from an unrelated (e.g. much shorter) session would inject a bogus
    tracklist. Returns None when no session qualifies; the caller should then
    fall back to fingerprint-only detection.
    """
    sessions = parse_cue_sessions(cue_path)
    if not sessions:
        return None
    if not duration_seconds or duration_seconds <= 0:
        # No duration to validate against — use the longest session
        return max(sessions, key=lambda s: s[-1].timestamp_seconds if s else 0.0)

    best: Optional[List[CueTrack]] = None
    for session in sessions:
        if not session:
            continue
        span = session[-1].timestamp_seconds
        if span > duration_seconds + slack_seconds:
            logger.info(
                "CUE session rejected (%d tracks): span %.0fs exceeds mix duration %.0fs",
                len(session), span, duration_seconds,
            )
            continue
        if span < duration_seconds * min_coverage:
            logger.info(
                "CUE session rejected (%d tracks): span %.0fs covers <%d%% of %.0fs mix",
                len(session), span, int(min_coverage * 100), duration_seconds,
            )
            continue
        if best is None or span > best[-1].timestamp_seconds:
            best = session

    if best:
        logger.info(
            "Selected CUE session with %d tracks (span %.0fs) from %s",
            len(best), best[-1].timestamp_seconds, cue_path,
        )
    return best


def find_cue_for_audio(audio_path: str, cue_directory: Optional[str] = None) -> Optional[str]:
    """Find a CUE sheet whose filename date matches the audio's recording date.

    The recording date comes from the audio FILENAME (e.g.
    will-see-...-2026-07-15.flac). File mtime is NOT a recording date — it
    shifts whenever a mix is copied or re-encoded, which used to match a CUE
    from whatever set was played most recently and inject a bogus tracklist.
    mtime is only consulted when the filename carries no date at all, and even
    then only an exact same-day CUE is accepted.
    """
    cue_dir = cue_directory or settings.DJCTL_CUE_PATH
    if not os.path.isdir(cue_dir):
        logger.debug("CUE directory does not exist: %s", cue_dir)
        return None

    date_pattern = re.compile(r'(\d{4}-\d{2}-\d{2})')

    audio_date = None
    fname_match = date_pattern.search(os.path.basename(audio_path))
    if fname_match:
        try:
            audio_date = datetime.strptime(fname_match.group(1), "%Y-%m-%d").date()
        except ValueError:
            audio_date = None
    if audio_date is None:
        audio_mtime = os.path.getmtime(audio_path)
        audio_date = datetime.fromtimestamp(audio_mtime).date()
        logger.warning(
            "Audio filename has no date, falling back to mtime date %s for %s "
            "(unreliable — consider dating the filename)",
            audio_date, audio_path,
        )

    for fname in sorted(os.listdir(cue_dir)):
        if not fname.lower().endswith(".cue"):
            continue
        m = date_pattern.search(fname)
        if not m:
            continue
        try:
            cue_date = datetime.strptime(m.group(1), "%Y-%m-%d").date()
        except ValueError:
            continue
        if cue_date == audio_date:
            match = os.path.join(cue_dir, fname)
            logger.info("Matched CUE %s to audio %s (exact date)", match, audio_path)
            return match

    logger.info("No same-date CUE found for %s (date %s)", audio_path, audio_date)
    return None


# --------------------------------------------------------------------------
# WebSocket listener
# --------------------------------------------------------------------------

class DJCTLWebSocketListener:
    """Connects to DJCTL WebSocket and collects real-time track data."""

    def __init__(self, ws_url: Optional[str] = None) -> None:
        self._ws_url = ws_url or settings.DJCTL_WS_URL
        self._tracks: List[Dict[str, Any]] = []
        self._running = False
        self._task: Optional[asyncio.Task] = None

    async def start(self) -> None:
        """Start listening in background.

        No-op when no WebSocket URL is configured: the live DJCTL feed is
        opt-in, and without a URL there is nothing to connect to.
        """
        if not self._ws_url:
            logger.info("DJCTL WebSocket listener not started: DJCTL_WS_URL is unset")
            return
        self._running = True
        self._task = asyncio.create_task(self._listen())
        logger.info("DJCTL WebSocket listener started: %s", self._ws_url)

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info("DJCTL WebSocket listener stopped")

    async def _listen(self) -> None:
        while self._running:
            try:
                async with websockets.connect(self._ws_url) as ws:
                    logger.info("Connected to DJCTL WebSocket")
                    async for raw_msg in ws:
                        try:
                            msg = json.loads(raw_msg)
                            self._handle_message(msg)
                        except json.JSONDecodeError:
                            logger.debug("Non-JSON message from DJCTL: %s", raw_msg[:100])
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("DJCTL WebSocket error: %s, reconnecting in 10s", exc)
                await asyncio.sleep(10)

    def _handle_message(self, msg: Dict[str, Any]) -> None:
        """Process a message from DJCTL WebSocket."""
        # Expected format: {"event": "track_change", "title": "...", "artist": "...", "timestamp": ...}
        event_type = msg.get("event") or msg.get("type", "")
        if event_type in ("track_change", "now_playing", "track"):
            track = {
                "title": label_or_id(msg.get("title", "")),
                "artist": label_or_id(msg.get("artist", "")),
                "timestamp_seconds": msg.get("timestamp", msg.get("elapsed", 0)),
            }
            self._tracks.append(track)
            logger.info("DJCTL track: %s - %s", track["artist"], track["title"])

    def get_tracks(self) -> List[Dict[str, Any]]:
        return list(self._tracks)

    def clear(self) -> None:
        self._tracks.clear()


# --------------------------------------------------------------------------
# Merge logic
# --------------------------------------------------------------------------

def merge_tracklists(
    cue_tracks: Optional[List[CueTrack]],
    shazam_tracks: Optional[List[Dict[str, Any]]],
    ws_tracks: Optional[List[Dict[str, Any]]] = None,
) -> DJCTLResult:
    """Merge track data from CUE, Shazam, and WebSocket sources.

    Priority: CUE data preferred for tracklist, audio analysis for genre/vibe.
    """
    if cue_tracks:
        tracklist = [t.to_dict() for t in cue_tracks]
        source = "djctl"

        # Supplement with Shazam data if CUE has missing info
        if shazam_tracks:
            shazam_by_time: Dict[int, Dict] = {}
            for st in shazam_tracks:
                key = int(st.get("timestamp_seconds", 0) // 60)
                shazam_by_time[key] = st

            for entry in tracklist:
                key = int(entry["timestamp_seconds"] // 60)
                shazam_match = shazam_by_time.get(key)
                if shazam_match and entry["title"].startswith("Track "):
                    entry["title"] = shazam_match.get("title", entry["title"])
                    entry["artist"] = shazam_match.get("artist", entry["artist"])

            source = "merged"

        return DJCTLResult(tracklist=tracklist, source=source)

    if ws_tracks:
        # Format WebSocket tracks
        formatted = []
        for wt in ws_tracks:
            ts = wt.get("timestamp_seconds", 0)
            total = int(ts)
            h, rem = divmod(total, 3600)
            m, s = divmod(rem, 60)
            tf = f"{h}:{m:02d}:{s:02d}" if h > 0 else f"{m}:{s:02d}"
            formatted.append({
                "title": label_or_id(wt.get("title", "")),
                "artist": label_or_id(wt.get("artist", "")),
                "timestamp_seconds": ts,
                "timestamp_formatted": tf,
            })
        return DJCTLResult(tracklist=formatted, source="djctl")

    if shazam_tracks:
        formatted = []
        for st in shazam_tracks:
            ts = st.get("timestamp_seconds", 0)
            total = int(ts)
            h, rem = divmod(total, 3600)
            m, s = divmod(rem, 60)
            tf = f"{h}:{m:02d}:{s:02d}" if h > 0 else f"{m}:{s:02d}"
            formatted.append({
                "title": label_or_id(st.get("title", "")),
                "artist": label_or_id(st.get("artist", "")),
                "timestamp_seconds": ts,
                "timestamp_formatted": tf,
            })
        return DJCTLResult(tracklist=formatted, source="shazam")

    return DJCTLResult(tracklist=[], source="none")


async def get_tracklist_for_audio(audio_path: str) -> DJCTLResult:
    """High-level helper: try CUE file match, return parsed data."""
    cue_path = find_cue_for_audio(audio_path)
    if cue_path:
        tracks = parse_cue_file(cue_path)
        if tracks:
            return DJCTLResult(
                tracklist=[t.to_dict() for t in tracks],
                source="djctl",
            )
    return DJCTLResult(tracklist=[], source="none")
