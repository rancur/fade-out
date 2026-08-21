# fade-out Roadmap

Priority-ordered feature backlog. The Claude Code roadmap driver
(`.github/workflows/claude-roadmap.yml`) picks the top unchecked item under
**Ready** and implements it, so keep this list accurate — a stale entry gets
re-implemented on top of working code.

## In Progress

_(nothing currently)_

## Ready

- [ ] Rate limiting on the public API surface
- [ ] Prometheus metrics endpoint for pipeline monitoring
- [ ] Loudness normalization before upload (LUFS targeting per platform)
- [ ] Batch processing mode for multiple mixes in one run
- [ ] AcoustID as a free fingerprinting fallback alongside Shazam/AudD
      (see the closed PR #43 — needs the `fpcalc` binary in the image, tests,
      and a deliberate decision on sampling density and API cost)
- [ ] Optional authentication in front of the dashboard, so the deployment
      model is not the only security boundary (see SECURITY.md)
- [ ] RSS podcast feed generation
- [ ] Waveform video generation for YouTube (audio-reactive visuals)
- [ ] Multi-brand support (different visual presets per genre/series)
- [ ] Analytics dashboard (play counts, listener stats across platforms)
- [ ] Code-split the frontend bundle (currently ~910 kB before gzip)
- [ ] Finish de-hardcoding the back-catalog title heuristics: the
      `GENERIC_PATTERNS` entry matching the original author's channel
      (`^dj (will )?see (live|set|stream)`) is still baked in. Harmless for
      other users — it simply never matches — but it belongs in
      `CATALOG_CHANNEL_NAME` alongside the rest.

## Done

- [x] Comprehensive backend test suite (993 tests) and frontend tests (30)
- [x] CI running tests, typecheck, Docker build, and dependency audit
- [x] Functional health endpoint with per-platform credential assertions
      (`/api/health`) plus a liveness endpoint (`/api/health/live`)
- [x] Alembic database migrations
- [x] Structured logging throughout the backend, with optional JSON output
- [x] WebSocket live pipeline status
- [x] OpenAPI/Swagger documentation (FastAPI built-in, at `/docs`)
- [x] Retry logic and backoff across ingest, upload, and notification paths
- [x] Input validation and crash-safe state for file-watcher events
- [x] Thumbnail template/design system with configurable layouts
- [x] Per-platform decoupled publishing with independent retry
- [x] Mixcloud upload support (opt-in)
- [x] YouTube chapter markers from tracklist timestamps
- [x] Shazam-based track identification, with optional AudD fallback
- [x] Scheduled uploads (immediate/scheduled publish modes)
- [x] Back-catalog import, matching, and metadata improvement
- [x] Shorts/vertical clip auto-uploader
- [x] Source-file renaming to match generated titles (opt-in)
