"""Measure the time offset between two renders of the same DJ set.

Why this exists
---------------
fade-out publishes the *same* set as two different assets:

* SoundCloud gets the FLAC — a recording of the DJ mixer output that starts
  when Will hits record on the deck.
* YouTube gets the OBS/Twitch capture — a video that starts when the *stream*
  starts, which is minutes earlier ("starting soon" screen, the previous DJ on
  the raid train, chatting) and usually ends a little later.

The tracklist is detected once, against the FLAC. Its timestamps are therefore
correct for SoundCloud and wrong for YouTube by however long that lead-in was.
Measured on real sets: 613.0 s (2026-08-03) and 867.6 s (2026-07-20) — so the
lead-in is large, varies per stream, and can never be assumed or hardcoded.

Constant offset, not drift
--------------------------
Probing five points across each set showed the offset flat to within 0.14 s
over 2 h 16 m (613.06 → 612.92) and 0.08 s over 1 h 55 m (867.64 → 867.56) —
i.e. under 20 ppm, at the resolution limit of the 20 ms envelope used here.
Both files are sample-accurate digital captures of the same audio at the same
nominal rate, so there is nothing to make them run at different speeds; the
only difference is *when each capture began*. A single measured scalar per mix
is therefore the correct model, and this module still verifies that assumption
on every mix rather than trusting it: probes that disagree fail the alignment
instead of silently shipping a wrong offset.

Method
------
Cross-correlate the amplitude envelopes of short windows. Envelopes (not raw
samples) make this robust to the video's lossy encode, different sample rates
and any level difference, while still resolving to ~20 ms. It is deterministic,
offline and free — unlike the Shazam-based detection this replaces, which
sampled the video on a 30 s grid, demanded an exact track-title string match,
and only searched the first 600 s of video. With real offsets of 613 s and
868 s, that search window could not reach the answer on any real mix: it always
fell through to 0.0, which is exactly why every YouTube description shipped
with the SoundCloud timestamps.
"""

from __future__ import annotations

import asyncio
import logging
import statistics
from dataclasses import dataclass
from typing import List, Optional

logger = logging.getLogger(__name__)

# Envelope sampling. 4 kHz mono is far more than enough to track loudness, and
# a 20 ms hop sets the resolution of the answer.
_SAMPLE_RATE = 4000
_HOP_SAMPLES = _SAMPLE_RATE // 50
ENVELOPE_RATE = _SAMPLE_RATE / _HOP_SAMPLES  # 50 Hz

# A probe window long enough to be musically unique (mixes repeat 8-bar loops,
# a 4-bar window would match in a hundred places).
PROBE_WINDOW_SECONDS = 90.0

# How far either side of the current best guess to search.
COARSE_SEARCH_SECONDS = 1800.0
REFINE_SEARCH_SECONDS = 90.0

# A normalised correlation peak below this means "these are not the same audio"
# rather than "the offset is X".
MIN_PEAK_CORRELATION = 0.30

# Probes must agree this closely for the constant-offset model to hold. Real
# sets came in at 0.14 s and 0.08 s of spread, so 5 s is a generous ceiling
# that still catches a genuinely drifting or mismatched pair.
MAX_PROBE_SPREAD_SECONDS = 5.0

MIN_AGREEING_PROBES = 3


@dataclass
class Alignment:
    """The measured relationship between a reference and a target render."""

    offset_seconds: float
    """Seconds to ADD to a reference timestamp to get the target timestamp."""

    confidence: float
    """Mean normalised correlation peak across the agreeing probes (0-1)."""

    spread_seconds: float
    """Max-min of the per-probe offsets. Near zero for a constant offset."""

    probes: int
    """How many probe windows agreed."""


class AlignmentError(RuntimeError):
    """Raised when no trustworthy offset could be measured."""


async def _run(cmd: List[str]) -> bytes:
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    out, err = await proc.communicate()
    if proc.returncode != 0:
        raise AlignmentError(
            f"{cmd[0]} failed ({proc.returncode}): {err.decode(errors='replace')[:300]}"
        )
    return out


async def probe_duration(path: str) -> float:
    """Container duration in seconds via ffprobe."""
    out = await _run([
        "ffprobe", "-v", "error", "-show_entries", "format=duration",
        "-of", "default=nw=1:nk=1", path,
    ])
    try:
        return float(out.decode().strip())
    except ValueError as exc:  # pragma: no cover - malformed media
        raise AlignmentError(f"could not read duration of {path}") from exc


async def _envelope(path: str, start: float, length: float):
    """Normalised amplitude envelope of ``length`` seconds from ``start``."""
    import numpy as np

    raw = await _run([
        "ffmpeg", "-v", "error",
        "-ss", f"{max(0.0, start):.3f}", "-t", f"{length:.3f}",
        "-i", path, "-vn", "-ac", "1", "-ar", str(_SAMPLE_RATE),
        "-f", "f32le", "-",
    ])
    samples = np.frombuffer(raw, dtype=np.float32)
    frames = samples.size // _HOP_SAMPLES
    if frames < 2:
        return None
    env = np.abs(samples[: frames * _HOP_SAMPLES].reshape(frames, _HOP_SAMPLES)).mean(axis=1)
    env = env - env.mean()
    sigma = float(env.std())
    if sigma <= 0:  # digital silence — carries no alignment information
        return None
    return env / sigma


async def _probe_offset(
    reference: str,
    target: str,
    reference_time: float,
    centre: float,
    search: float,
    window: float,
):
    """Locate ``reference_time`` inside ``target``. Returns (offset, peak)."""
    import numpy as np

    ref = await _envelope(reference, reference_time, window)
    if ref is None:
        return None

    haystack_start = max(0.0, reference_time + centre - search)
    haystack = await _envelope(target, haystack_start, window + 2 * search)
    if haystack is None or haystack.size <= ref.size:
        return None

    correlation = np.correlate(haystack, ref, mode="valid")
    best = int(np.argmax(correlation))
    peak = float(correlation[best] / ref.size)
    target_time = haystack_start + best / ENVELOPE_RATE
    return target_time - reference_time, peak


async def measure_offset(
    reference_path: str,
    target_path: str,
    reference_duration: Optional[float] = None,
    window: float = PROBE_WINDOW_SECONDS,
) -> Alignment:
    """Measure how far ``target`` runs behind ``reference``.

    ``reference`` is the asset the tracklist timestamps were detected against
    (the FLAC); ``target`` is the other render (the video, or audio pulled back
    down from the publishing platform). The returned offset is what you ADD to
    a reference timestamp to get the corresponding target timestamp.

    Raises :class:`AlignmentError` rather than returning a guess when the two
    files do not confidently line up — a failed alignment usually means the
    wrong source file is attached to the mix, and shipping a fabricated offset
    for that is worse than shipping none.
    """
    if reference_duration is None:
        reference_duration = await probe_duration(reference_path)
    if reference_duration <= window:
        raise AlignmentError("reference too short to align")

    # A coarse probe near the start finds the rough lead-in, then every probe
    # is refined around it. Searching +/-1800 s at every probe point would be
    # needlessly slow once the answer is roughly known.
    coarse = await _probe_offset(
        reference_path, target_path, min(60.0, reference_duration * 0.1),
        centre=0.0, search=COARSE_SEARCH_SECONDS, window=window,
    )
    if coarse is None or coarse[1] < MIN_PEAK_CORRELATION:
        raise AlignmentError(
            "no confident coarse alignment — the target is probably not the "
            "same set as the reference"
        )
    centre = coarse[0]

    # Spread probes across the whole set. If the two files drifted apart rather
    # than simply starting at different moments, these disagree and we bail.
    fractions = (0.05, 0.25, 0.5, 0.75, 0.95)
    offsets: List[float] = []
    peaks: List[float] = []
    for fraction in fractions:
        at = reference_duration * fraction
        if at + window > reference_duration:
            at = max(0.0, reference_duration - window - 1.0)
        result = await _probe_offset(
            reference_path, target_path, at,
            centre=centre, search=REFINE_SEARCH_SECONDS, window=window,
        )
        if result is None:
            continue
        offset, peak = result
        if peak >= MIN_PEAK_CORRELATION:
            offsets.append(offset)
            peaks.append(peak)

    if len(offsets) < MIN_AGREEING_PROBES:
        raise AlignmentError(
            f"only {len(offsets)} of {len(fractions)} probes matched — "
            "refusing to guess an offset"
        )

    median = statistics.median(offsets)
    # Discard a lone bad probe (a long ambient breakdown can mis-lock) before
    # judging the spread.
    kept = [(o, p) for o, p in zip(offsets, peaks) if abs(o - median) <= MAX_PROBE_SPREAD_SECONDS]
    if len(kept) < MIN_AGREEING_PROBES:
        raise AlignmentError(
            "probe offsets disagree — the two renders are not a constant "
            "offset apart (variable-speed or edited target?)"
        )

    kept_offsets = [o for o, _ in kept]
    spread = max(kept_offsets) - min(kept_offsets)
    if spread > MAX_PROBE_SPREAD_SECONDS:
        raise AlignmentError(f"offset drifts by {spread:.1f}s across the set")

    alignment = Alignment(
        offset_seconds=round(statistics.median(kept_offsets), 2),
        confidence=round(sum(p for _, p in kept) / len(kept), 3),
        spread_seconds=round(spread, 3),
        probes=len(kept),
    )
    logger.info(
        "Aligned %s -> %s: offset %.2fs (confidence %.2f, spread %.2fs, %d probes)",
        reference_path, target_path, alignment.offset_seconds,
        alignment.confidence, alignment.spread_seconds, alignment.probes,
    )
    return alignment
