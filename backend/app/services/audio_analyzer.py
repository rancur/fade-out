"""Audio analysis service -- extracts features and identifies tracks via Shazam."""

import asyncio
import logging
import os
import tempfile
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

import httpx
import librosa
import numpy as np
import soundfile as sf
from shazamio import Shazam

from app.config import settings

logger = logging.getLogger(__name__)

SEGMENT_DURATION = 15  # seconds per Shazam sample clip
DEFAULT_SAMPLE_INTERVAL = settings.AUDIO_SAMPLE_INTERVAL_SECONDS  # denser = better recall
SECONDARY_CLIP_OFFSET = 30  # seconds after primary clip for transition catching
RETRY_CLIP_OFFSET = 45  # seconds offset for retry if primary Shazam fails
SHAZAM_TRANSIENT_RETRIES = 2  # in-place retries when recognize() errors (rate limit/network)
SHAZAM_RETRY_BACKOFF = 1.5  # seconds base backoff between transient retries
AUDD_ENDPOINT = "https://api.audd.io/"


@dataclass
class TrackHit:
    title: str
    artist: str
    timestamp_seconds: float
    # Recognition confidence in [0, 1]. Set from how many times the same title
    # was independently recognized (a confirmed multi-hit is far more reliable
    # than a lone hit on blended DJ audio). Consumed by the confidence merge.
    confidence: Optional[float] = None

    def to_dict(self) -> dict:
        d = {
            "title": self.title,
            "artist": self.artist,
            "timestamp_seconds": self.timestamp_seconds,
        }
        if self.confidence is not None:
            d["confidence"] = self.confidence
        return d


@dataclass
class AnalysisResult:
    genres: List[str] = field(default_factory=list)
    vibes: List[str] = field(default_factory=list)
    energy_profile: List[Dict[str, Any]] = field(default_factory=list)
    tracklist: List[Dict[str, Any]] = field(default_factory=list)
    bpm_range: Tuple[float, float] = (0.0, 0.0)
    duration_seconds: float = 0.0

    def to_dict(self) -> dict:
        return {
            "genres": self.genres,
            "vibes": self.vibes,
            "energy_profile": self.energy_profile,
            "tracklist": self.tracklist,
            "bpm_range": list(self.bpm_range),
            "duration_seconds": self.duration_seconds,
        }


# Genre inference from spectral features
_GENRE_HINTS: Dict[str, Dict[str, Tuple[float, float]]] = {
    # genre -> feature_name -> (low, high) expected range
    "drum and bass": {"bpm": (160, 180), "centroid_mean": (2000, 5000)},
    "house": {"bpm": (118, 132), "centroid_mean": (1500, 4000)},
    "techno": {"bpm": (125, 150), "centroid_mean": (1000, 3500)},
    "trance": {"bpm": (128, 145), "centroid_mean": (2000, 5000)},
    "dubstep": {"bpm": (135, 145), "centroid_mean": (500, 3000)},
    "ambient": {"bpm": (60, 100), "centroid_mean": (500, 2000)},
    "breakbeat": {"bpm": (120, 150), "centroid_mean": (1500, 4500)},
}

_VIBE_FROM_ENERGY: List[Tuple[str, float, float]] = [
    ("chill", 0.0, 0.25),
    ("mellow", 0.2, 0.4),
    ("groovy", 0.35, 0.6),
    ("energetic", 0.55, 0.8),
    ("intense", 0.75, 0.95),
    ("chaotic", 0.9, 1.0),
]


class AudioAnalyzer:
    """Analyze a mix file for BPM, genre, tracklist, and energy profile."""

    def __init__(self, sample_interval: int = DEFAULT_SAMPLE_INTERVAL) -> None:
        self._sample_interval = sample_interval
        self._shazam = Shazam()
        self._audd_token = (settings.AUDD_API_TOKEN or "").strip()

    async def analyze(self, audio_path: str) -> AnalysisResult:
        """Full analysis pipeline. Handles files up to 6 hours."""
        logger.info("Starting analysis of %s", audio_path)
        result = AnalysisResult()

        # Load just enough to get duration without reading entire file
        info = sf.info(audio_path)
        result.duration_seconds = info.duration
        sr_native = info.samplerate
        logger.info("File duration: %.1f seconds (%.1f hours)", info.duration, info.duration / 3600)

        # Determine sample points
        sample_times = self._get_sample_times(info.duration)
        logger.info("Will sample %d segments", len(sample_times))

        # Extract features from segments
        bpms: List[float] = []
        centroids: List[float] = []
        bandwidths: List[float] = []
        rolloffs: List[float] = []
        energy_points: List[Dict[str, Any]] = []

        for t in sample_times:
            features = await asyncio.to_thread(
                self._extract_segment_features, audio_path, t, sr_native
            )
            if features is None:
                continue

            bpms.append(features["bpm"])
            centroids.append(features["centroid"])
            bandwidths.append(features["bandwidth"])
            rolloffs.append(features["rolloff"])
            energy_points.append({
                "timestamp_seconds": t,
                "rms": round(features["rms"], 4),
                "bpm": round(features["bpm"], 1),
            })

        result.energy_profile = energy_points

        if bpms:
            result.bpm_range = (round(min(bpms), 1), round(max(bpms), 1))

        # Shazam identification
        tracklist = await self._identify_tracks(audio_path, sample_times, sr_native)
        result.tracklist = [t.to_dict() for t in tracklist]

        # Genre classification from features + identified tracks
        result.genres = self._classify_genres(
            bpms, centroids, tracklist
        )

        # Vibe classification from energy profile
        result.vibes = self._classify_vibes(energy_points)

        logger.info(
            "Analysis complete: %d tracks, genres=%s, vibes=%s, bpm=%.0f-%.0f",
            len(tracklist), result.genres, result.vibes,
            result.bpm_range[0], result.bpm_range[1],
        )
        return result

    def _get_sample_times(self, duration: float) -> List[float]:
        """Generate timestamps to sample, starting at 30s in to skip intros."""
        times: List[float] = []
        t = 30.0  # skip first 30s
        while t < duration - SEGMENT_DURATION:
            times.append(t)
            t += self._sample_interval
        # Always include a point near the end
        if duration > 120:
            end_sample = duration - 60
            if not times or (end_sample - times[-1]) > 60:
                times.append(end_sample)
        return times

    def _extract_segment_features(
        self, path: str, offset: float, sr_target: int = 22050
    ) -> Optional[Dict[str, float]]:
        """Extract audio features from a short segment."""
        try:
            y, sr = librosa.load(
                path, sr=sr_target, offset=offset, duration=SEGMENT_DURATION
            )
        except Exception as exc:
            logger.warning("Failed to load segment at %.1fs: %s", offset, exc)
            return None

        if len(y) < sr:  # less than 1 second of audio
            return None

        # BPM
        tempo, _ = librosa.beat.beat_track(y=y, sr=sr)
        bpm = float(np.atleast_1d(tempo)[0])

        # Spectral features
        centroid = float(np.mean(librosa.feature.spectral_centroid(y=y, sr=sr)))
        bandwidth = float(np.mean(librosa.feature.spectral_bandwidth(y=y, sr=sr)))
        rolloff = float(np.mean(librosa.feature.spectral_rolloff(y=y, sr=sr)))

        # RMS energy
        rms = float(np.mean(librosa.feature.rms(y=y)))

        return {
            "bpm": bpm,
            "centroid": centroid,
            "bandwidth": bandwidth,
            "rolloff": rolloff,
            "rms": rms,
        }

    async def _identify_tracks(
        self, path: str, sample_times: List[float], sr_native: int
    ) -> List[TrackHit]:
        """Use Shazam to identify tracks at sample points.

        For each sample point, tries two clips (primary and +30s offset) to
        catch transitions — BOTH tracks of a transition are kept. If the
        primary clip fails recognition, it retries at +45s. Each clip is run
        through Shazam first and, if an AudD token is configured, falls back
        to AudD when Shazam returns nothing.

        Dedup is consecutive-only: a track may legitimately reappear later in
        the set (A -> B -> A), so a global seen-set would wrongly drop the
        comeback.
        """
        identified: List[TrackHit] = []
        last_title: Optional[str] = None

        # Track how many times each song is identified (for confidence scoring)
        title_hit_count: dict[str, int] = {}

        def _emit(hit: TrackHit) -> None:
            nonlocal last_title
            title_key = hit.title.lower()
            title_hit_count[title_key] = title_hit_count.get(title_key, 0) + 1
            if title_key == last_title:
                return
            identified.append(hit)
            last_title = title_key

        for t in sample_times:
            # Primary clip at sample point
            hit = await self._recognize_segment(path, t, sr_native)
            if not hit:
                # Retry with offset if primary fails
                hit = await self._recognize_segment(path, t + RETRY_CLIP_OFFSET, sr_native)
            if hit:
                _emit(hit)

            # Secondary clip at +30s: catches the incoming track during a
            # transition window. Emit it even when the primary hit — if it
            # differs, both tracks of the blend belong in the tracklist.
            hit2 = await self._recognize_segment(path, t + SECONDARY_CLIP_OFFSET, sr_native)
            if hit2:
                _emit(hit2)

        # Sort by timestamp, then re-apply consecutive dedup in time order
        # (out-of-order emission across sample points can duplicate neighbors)
        identified.sort(key=lambda h: h.timestamp_seconds)
        deduped: List[TrackHit] = []
        for track in identified:
            if deduped and deduped[-1].title.lower() == track.title.lower():
                continue
            deduped.append(track)
        identified = deduped

        # Tag each surviving hit with a recognition confidence derived from how
        # many times its title was independently recognized. The confidence
        # merge uses this to decide whether a Shazam name is trustworthy enough
        # to keep or should collapse to an "ID - ID" marker.
        from app.services.confidence_merge import (
            SHAZAM_CONFIRMED_CONFIDENCE,
            SHAZAM_SINGLE_CONFIDENCE,
        )

        for hit in identified:
            hits = title_hit_count.get(hit.title.lower(), 0)
            hit.confidence = (
                SHAZAM_CONFIRMED_CONFIDENCE if hits >= 2 else SHAZAM_SINGLE_CONFIDENCE
            )

        return identified

    async def _recognize_segment(
        self, path: str, offset: float, sr_native: int
    ) -> Optional[TrackHit]:
        """Recognize a single clip: Shazam first, AudD fallback if configured.

        Extracts the segment to a temp WAV once and reuses it for both
        providers so we don't decode the audio twice.
        """
        wav_path = await self._extract_segment_wav(path, offset, sr_native)
        if wav_path is None:
            return None
        try:
            hit = await self._shazam_wav(wav_path, offset)
            if hit is None and self._audd_token:
                hit = await self._audd_wav(wav_path, offset)
            return hit
        finally:
            try:
                os.unlink(wav_path)
            except OSError:
                pass

    async def _extract_segment_wav(
        self, path: str, offset: float, sr_native: int
    ) -> Optional[str]:
        """Decode a clip and write it to a temp WAV. Returns the path or None."""
        try:
            y, sr = await asyncio.to_thread(
                librosa.load, path, sr=sr_native, offset=offset, duration=SEGMENT_DURATION
            )
        except Exception:
            return None
        tmp = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
        try:
            sf.write(tmp.name, y, sr)
            tmp.close()
        except Exception as exc:
            logger.debug("Segment write failed at %.1fs: %s", offset, exc)
            try:
                os.unlink(tmp.name)
            except OSError:
                pass
            return None
        return tmp.name

    async def _shazam_wav(self, wav_path: str, offset: float) -> Optional[TrackHit]:
        """Run Shazam on an already-extracted WAV, retrying transient errors.

        A clean "no match" returns immediately. Exceptions (rate limiting,
        transient network failures) are retried in place with backoff so a
        recoverable blip doesn't silently drop a real track.
        """
        for attempt in range(SHAZAM_TRANSIENT_RETRIES + 1):
            try:
                result = await self._shazam.recognize(wav_path)
                break
            except Exception as exc:
                if attempt < SHAZAM_TRANSIENT_RETRIES:
                    await asyncio.sleep(SHAZAM_RETRY_BACKOFF * (attempt + 1))
                    continue
                logger.debug("Shazam failed at %.1fs after retries: %s", offset, exc)
                return None

        matches = result.get("matches", [])
        track_info = result.get("track")
        if not matches or not track_info:
            return None

        # DJ convention: an unidentified field is labelled "ID" (renders "ID - ID").
        title = track_info.get("title") or "ID"
        artist = track_info.get("subtitle") or "ID"
        return TrackHit(title=title, artist=artist, timestamp_seconds=offset)

    async def _audd_wav(self, wav_path: str, offset: float) -> Optional[TrackHit]:
        """Fallback recognition via AudD (https://audd.io). Requires a token.

        Any error is swallowed and returns None so the pipeline never breaks on
        a provider hiccup.
        """
        try:
            with open(wav_path, "rb") as fh:
                files = {"file": ("segment.wav", fh, "audio/wav")}
                data = {"api_token": self._audd_token}
                async with httpx.AsyncClient(timeout=30.0) as client:
                    resp = await client.post(AUDD_ENDPOINT, data=data, files=files)
            payload = resp.json()
        except Exception as exc:
            logger.debug("AudD failed at %.1fs: %s", offset, exc)
            return None

        if payload.get("status") != "success":
            logger.debug("AudD non-success at %.1fs: %s", offset, payload.get("error"))
            return None
        track_info = payload.get("result")
        if not track_info:
            return None

        title = track_info.get("title") or "ID"
        artist = track_info.get("artist") or "ID"
        return TrackHit(title=title, artist=artist, timestamp_seconds=offset)

    async def _shazam_segment(
        self, path: str, offset: float, sr_native: int
    ) -> Optional[TrackHit]:
        """Extract a segment and run Shazam on it (Shazam-only entry point).

        Kept for callers that specifically want Shazam (e.g. the YouTube
        timestamp-offset probe in handlers). Track identification goes through
        _recognize_segment, which also uses the AudD fallback.
        """
        wav_path = await self._extract_segment_wav(path, offset, sr_native)
        if wav_path is None:
            return None
        try:
            return await self._shazam_wav(wav_path, offset)
        finally:
            try:
                os.unlink(wav_path)
            except OSError:
                pass

    def _classify_genres(
        self,
        bpms: List[float],
        centroids: List[float],
        tracklist: List[TrackHit],
    ) -> List[str]:
        """Infer genres from BPM + spectral features + track metadata."""
        if not bpms:
            return ["electronic"]

        avg_bpm = float(np.mean(bpms))
        avg_centroid = float(np.mean(centroids)) if centroids else 2000.0

        scores: Dict[str, float] = {}
        for genre, ranges in _GENRE_HINTS.items():
            score = 0.0
            bpm_lo, bpm_hi = ranges["bpm"]
            if bpm_lo <= avg_bpm <= bpm_hi:
                score += 2.0
            elif abs(avg_bpm - (bpm_lo + bpm_hi) / 2) < 20:
                score += 0.5

            cent_lo, cent_hi = ranges["centroid_mean"]
            if cent_lo <= avg_centroid <= cent_hi:
                score += 1.0

            scores[genre] = score

        # Boost genres mentioned in Shazam track metadata. Word-boundary matching
        # (see genre_utils.keyword_matches) avoids over-crediting e.g. "drum and
        # bass" for titles that merely contain "bass" inside a longer word.
        from app.services.genre_utils import keyword_boosts

        for track in tracklist:
            combined = f"{track.title} {track.artist}"
            for genre, boost in keyword_boosts(combined).items():
                scores[genre] = scores.get(genre, 0) + boost

        # Return top genres with score > 0
        sorted_genres = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
        result = [g for g, s in sorted_genres if s > 0][:4]
        return result if result else ["electronic"]

    def _classify_vibes(self, energy_points: List[Dict[str, Any]]) -> List[str]:
        """Classify overall vibes from energy profile."""
        if not energy_points:
            return ["mixed"]

        rms_values = [p["rms"] for p in energy_points]
        max_rms = max(rms_values) if rms_values else 1.0
        if max_rms == 0:
            max_rms = 1.0

        # Normalize to 0-1
        normalized = [v / max_rms for v in rms_values]
        avg_energy = float(np.mean(normalized))
        std_energy = float(np.std(normalized))

        vibes: List[str] = []
        for label, lo, hi in _VIBE_FROM_ENERGY:
            if lo <= avg_energy <= hi:
                vibes.append(label)

        # High variance = "journey" / "dynamic"
        if std_energy > 0.25:
            vibes.append("journey")
        if std_energy > 0.35:
            vibes.append("dynamic")

        return vibes[:4] if vibes else ["mixed"]
