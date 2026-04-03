"""Audio analysis service -- extracts features and identifies tracks via Shazam."""

import asyncio
import logging
import os
import tempfile
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

import librosa
import numpy as np
import soundfile as sf
from shazamio import Shazam

from app.config import settings

logger = logging.getLogger(__name__)

SEGMENT_DURATION = 15  # seconds per Shazam sample clip
DEFAULT_SAMPLE_INTERVAL = settings.AUDIO_SAMPLE_INTERVAL_SECONDS  # 300s = 5 min


@dataclass
class TrackHit:
    title: str
    artist: str
    timestamp_seconds: float

    def to_dict(self) -> dict:
        return {
            "title": self.title,
            "artist": self.artist,
            "timestamp_seconds": self.timestamp_seconds,
        }


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
        """Use Shazam to identify tracks at sample points."""
        identified: List[TrackHit] = []
        seen_titles: set[str] = set()

        for t in sample_times:
            hit = await self._shazam_segment(path, t, sr_native)
            if hit and hit.title.lower() not in seen_titles:
                seen_titles.add(hit.title.lower())
                identified.append(hit)

        # Sort by timestamp
        identified.sort(key=lambda h: h.timestamp_seconds)
        return identified

    async def _shazam_segment(
        self, path: str, offset: float, sr_native: int
    ) -> Optional[TrackHit]:
        """Extract a segment and run Shazam on it."""
        try:
            y, sr = await asyncio.to_thread(
                librosa.load, path, sr=sr_native, offset=offset, duration=SEGMENT_DURATION
            )
        except Exception:
            return None

        # Write segment to a temp WAV file for Shazam
        tmp = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
        try:
            sf.write(tmp.name, y, sr)
            tmp.close()
            result = await self._shazam.recognize(tmp.name)
        except Exception as exc:
            logger.debug("Shazam failed at %.1fs: %s", offset, exc)
            return None
        finally:
            try:
                os.unlink(tmp.name)
            except OSError:
                pass

        matches = result.get("matches", [])
        track_info = result.get("track")
        if not matches or not track_info:
            return None

        title = track_info.get("title", "Unknown")
        artist = track_info.get("subtitle", "Unknown")
        return TrackHit(title=title, artist=artist, timestamp_seconds=offset)

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

        # Boost genres mentioned in Shazam track metadata
        genre_keywords = {
            "house": ["house"],
            "techno": ["techno"],
            "trance": ["trance"],
            "drum and bass": ["drum", "bass", "dnb", "d&b", "jungle"],
            "dubstep": ["dubstep", "riddim"],
            "ambient": ["ambient", "chill"],
            "breakbeat": ["breakbeat", "breaks"],
        }
        for track in tracklist:
            combined = f"{track.title} {track.artist}".lower()
            for genre, keywords in genre_keywords.items():
                if any(kw in combined for kw in keywords):
                    scores[genre] = scores.get(genre, 0) + 1.5

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
