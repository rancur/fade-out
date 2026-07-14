"""DJCTL CUE sheet parser and WebSocket listener for real-time track data."""

import asyncio
import json
import logging
import os
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
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


def parse_cue_file(cue_path: str) -> List[CueTrack]:
    """Parse a standard CUE sheet and return a list of tracks."""
    with open(cue_path, "r", encoding="utf-8", errors="replace") as f:
        content = f.read()

    tracks: List[CueTrack] = []

    # Split by TRACK directives to parse each block
    # We'll use a different approach: iterate line-by-line tracking state
    current_track_num: Optional[int] = None
    current_title: Optional[str] = None
    current_performer: Optional[str] = None
    global_performer: Optional[str] = None

    for line in content.splitlines():
        line_stripped = line.strip()

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
                tracks.append(CueTrack(
                    number=current_track_num,
                    title=current_title or f"Track {current_track_num}",
                    artist=current_performer or global_performer or ID_LABEL,
                    index_mm=mm,
                    index_ss=ss,
                    index_ff=ff,
                ))
                current_track_num = None
                continue

    logger.info("Parsed %d tracks from CUE file %s", len(tracks), cue_path)
    return tracks


def find_cue_for_audio(audio_path: str, cue_directory: Optional[str] = None) -> Optional[str]:
    """Find a CUE sheet matching an audio file by date proximity.

    CUE filenames contain date like djctl-2025-12-05.cue.
    Audio files are matched by their modification date.
    """
    cue_dir = cue_directory or settings.DJCTL_CUE_PATH
    if not os.path.isdir(cue_dir):
        logger.debug("CUE directory does not exist: %s", cue_dir)
        return None

    date_pattern = re.compile(r'(\d{4}-\d{2}-\d{2})')

    # Prefer a date embedded in the audio FILENAME over the file mtime. mtime
    # shifts when a mix is copied, re-encoded, or moved between hosts, which
    # silently breaks the (much more accurate) CUE-based tracklist and forces
    # the Shazam fallback. The recording date in the name is stable.
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

    best_match: Optional[str] = None
    best_delta: int = 999

    for fname in os.listdir(cue_dir):
        if not fname.lower().endswith(".cue"):
            continue
        m = date_pattern.search(fname)
        if not m:
            continue
        try:
            cue_date = datetime.strptime(m.group(1), "%Y-%m-%d").date()
        except ValueError:
            continue

        delta = abs((audio_date - cue_date).days)
        if delta < best_delta:
            best_delta = delta
            best_match = os.path.join(cue_dir, fname)

    # Allow up to 2 days of slack: a set recorded past midnight, or a CUE
    # exported the morning after, should still match its recording.
    if best_match and best_delta <= 2:
        logger.info("Matched CUE %s to audio %s (delta=%d days)", best_match, audio_path, best_delta)
        return best_match

    logger.debug("No CUE match found for %s (best delta=%d)", audio_path, best_delta)
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
        """Start listening in background."""
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
