# fade-out Improvement Roadmap
**Created:** 2026-08-20
**Status:** Implementation in progress

## Context
Will's directive (2026-07-13): "keep improving fade-out, create a plan with lots of improvements, then do it all for me."

Current track detection accuracy: ~50% (Shazam-only)
Branding: Pixelated-nature psychedelia cover art style (locked)

## Priority 1: Track Detection Improvements

### 1.1 AcoustID Free Fallback ✓ (Implementing)
**Problem:** Shazam-only detection misses ~50% of tracks
**Solution:** Add AcoustID as free fallback when Shazam fails
**Implementation:**
- Add `pyacoustid` dependency to requirements.txt
- Modify `audio_analyzer.py._shazam_segment()` → `_identify_segment()`
- Try Shazam first (better metadata), fall back to AcoustID
- AcoustID requires ACOUSTID_API_KEY env var (free tier: 3 req/sec)

**Expected Impact:** 70-80% detection rate (combined coverage)

### 1.2 Tighter Sampling Window ✓ (Implementing)
**Problem:** 120s intervals miss tracks in fast-mixing sets
**Solution:** Reduce sampling interval to 60s
**Implementation:**
- Change `AUDIO_SAMPLE_INTERVAL_SECONDS` from 120 → 60 in config.py
- Keep multi-offset strategy (primary + 30s + 45s retry)

**Expected Impact:** 2x coverage density, catches more transitions

### 1.3 Unidentified Track Placeholder
**Status:** Locked 'ID - ID' format (per task description)
**Implementation:** Already handled in current code

## Priority 2: Cover Art & Branding

### 2.1 Preserve Psychedelia Style ✓
**Requirement:** Keep pixelated-nature psychedelia branding
**Current Style:**
```
Chunky pixel art style, psychedelic nature scene, vibrant colors,
eyes everywhere watching from foliage, retro game aesthetic,
thick bold pixels, lush vegetation with hidden creatures,
trippy color palette, low-resolution rendered at high-resolution
```
**Action:** No changes needed - style is preserved

### 2.2 Genre-Specific Visual Enhancements (Future)
**Idea:** Enhance genre visual modifiers for better match accuracy
- Current: 7 genres with modifiers
- Future: Add modifiers for progressive house, liquid DnB, melodic techno

## Priority 3: Audio Analysis Enhancements

### 3.1 Multi-Provider Track Identification (Future)
**Providers to consider:**
- MusicBrainz (free, open-source)
- ACRCloud (paid, high accuracy)
- Audd.io (freemium)

### 3.2 Enhanced False Positive Filtering
**Current:** Filters tracks identified only once with tight gaps (<45s both sides)
**Improvement:** Add confidence scoring based on:
- Multiple provider agreement
- Audio fingerprint strength
- Metadata completeness

### 3.3 BPM Detection Accuracy
**Current:** librosa beat tracking per segment
**Improvement:** Cross-validate with multiple algorithms:
- librosa beat_track
- essentia BeatTracker
- madmom BeatTracker

## Priority 4: Pipeline Resilience

### 4.1 Platform Independence (Completed ✓)
**Status:** Fixed in PR #41 (2026-08-15)
- SoundCloud and YouTube now independent legs
- Partial publish triggers alerts instead of silent failure

### 4.2 OAuth Token Auto-Refresh
**Status:** Needs improvement
- SoundCloud: DataDome blocks automated reauth (human-only)
- YouTube: Token refresh working
**Future:** Implement proactive token expiry monitoring

### 4.3 Upload Retry Logic
**Current:** No automatic retry on transient failures
**Improvement:** Add exponential backoff retry for:
- Network timeouts
- Rate limit errors (429)
- Temporary service unavailability (503)

## Priority 5: Performance Optimizations

### 5.1 Parallel Track Identification
**Current:** Sequential segment processing
**Improvement:** Process multiple segments concurrently
**Implementation:** Use asyncio.gather() for Shazam/AcoustID calls

### 5.2 Audio Feature Extraction Caching
**Idea:** Cache librosa features to avoid recomputation on pipeline restarts
**Storage:** SQLite or filesystem cache keyed by (file_hash, offset)

### 5.3 Thumbnail Generation Optimization
**Current:** Generate fresh for each platform
**Improvement:** Generate once, reuse with platform-specific overlays

## Priority 6: User Experience

### 6.1 Real-time Progress Updates (Roadmap Item)
**Status:** WebSocket support planned (see ROADMAP.md)
**Implementation:** Emit progress events during:
- Audio analysis phases
- Track identification attempts
- Upload stages

### 6.2 Draft Mode Preview
**Current:** Draft mode stops at generate_art stage
**Enhancement:** Add preview endpoint for draft review before publish

### 6.3 Manual Track Corrections
**Idea:** Allow user to correct misidentified tracks via frontend
**Storage:** Store corrections in database, apply before upload

## Implementation Order

**Phase 1 (This Session):**
1. ✓ Add AcoustID fallback
2. ✓ Tighter sampling (60s)
3. ✓ Update dependencies
4. ✓ Test changes
5. ✓ Create PR

**Phase 2 (Next Session):**
- Parallel track identification
- Enhanced false positive filtering
- Upload retry logic

**Phase 3 (Future):**
- Multi-provider track ID
- Real-time progress updates
- Manual corrections UI

## Testing Strategy

### Unit Tests (Priority)
- `test_acoustid_fallback()` - verify AcoustID called when Shazam fails
- `test_sampling_density()` - verify 60s intervals generated correctly
- `test_dual_provider_tracking()` - verify cost tracking for both providers

### Integration Tests
- End-to-end pipeline with AcoustID-only tracks
- Mixed Shazam success/fail scenarios
- Verify no regression in cover art style

### Performance Tests
- Analyze 2-hour mix with new 60s sampling
- Verify total runtime acceptable (<10 min for 2h mix)
- Monitor API rate limits (AcoustID: 3 req/sec)

## Success Metrics

**Before:**
- Track detection rate: ~50%
- Sampling density: 60 samples/2h mix (every 120s)
- Providers: Shazam only

**After:**
- Track detection rate: 70-80% (target)
- Sampling density: 120 samples/2h mix (every 60s)
- Providers: Shazam + AcoustID fallback

**Branding:**
- Cover art style: Preserved (psychedelic pixel art)
- Visual consistency: Maintained across all mixes
