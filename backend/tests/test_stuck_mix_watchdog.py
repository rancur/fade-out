"""A mix that never published has to surface on its own."""

from datetime import datetime, timedelta, timezone

from sqlalchemy import text as sa_text

from app.database import async_session_factory
from app.models import Mix
from app.services.stuck_mix_watchdog import StuckMixWatchdog


async def _add_mix(mix_id, *, hours_old, status="failed", **fields):
    async with async_session_factory() as session:
        session.add(Mix(
            id=mix_id,
            title=f"Mix {mix_id}",
            source="pipeline",
            pipeline_status=status,
            created_at=datetime.now(timezone.utc) - timedelta(hours=hours_old),
            **fields,
        ))
        await session.commit()


class TestDetection:
    async def test_old_unpublished_mix_is_reported(self, prepared_db):
        await _add_mix("stuck-1", hours_old=30)
        result = await StuckMixWatchdog().check(alert=False)

        assert result["stuck_count"] == 1
        entry = result["stuck"][0]
        assert entry["id"] == "stuck-1"
        assert entry["kind"] == "unpublished"
        assert "never published" in entry["reason"]

    async def test_fresh_mix_is_left_alone(self, prepared_db):
        await _add_mix("fresh-1", hours_old=1, status="running")
        result = await StuckMixWatchdog().check(alert=False)
        assert result["stuck_count"] == 0

    async def test_partially_published_mix_is_reported(self, prepared_db):
        await _add_mix(
            "partial-1", hours_old=12, status="partial",
            youtube_url="https://youtube.com/watch?v=x",
        )
        result = await StuckMixWatchdog().check(alert=False)
        assert result["stuck_count"] == 1
        assert result["stuck"][0]["reason"] == "published to some targets but not all"

    async def test_completed_and_published_is_not_stuck(self, prepared_db):
        await _add_mix(
            "done-1", hours_old=200, status="completed",
            youtube_url="https://youtube.com/watch?v=x",
            soundcloud_url="https://soundcloud.com/x",
        )
        result = await StuckMixWatchdog().check(alert=False)
        assert result["stuck_count"] == 0

    async def test_completed_with_one_platform_unconfigured_is_not_stuck(
        self, prepared_db
    ):
        """SoundCloud-only setups must not be nagged about a missing YouTube URL."""
        await _add_mix(
            "sc-only", hours_old=200, status="completed",
            soundcloud_url="https://soundcloud.com/x",
        )
        result = await StuckMixWatchdog().check(alert=False)
        assert result["stuck_count"] == 0

    async def test_draft_gets_a_longer_but_finite_window(self, prepared_db):
        await _add_mix("draft-young", hours_old=10, status="draft_review")
        await _add_mix("draft-old", hours_old=100, status="draft_review")

        result = await StuckMixWatchdog().check(alert=False)
        ids = {e["id"] for e in result["stuck"]}
        assert ids == {"draft-old"}
        assert result["stuck"][0]["kind"] == "draft_unreviewed"

    async def test_missing_created_at_is_not_assumed_fresh(self, prepared_db):
        await _add_mix("no-ts", hours_old=1)
        # created_at carries a server default, so null it explicitly — the
        # case under test is a row whose age we genuinely cannot determine.
        async with async_session_factory() as session:
            await session.execute(
                sa_text("UPDATE mixes SET created_at = NULL WHERE id = 'no-ts'")
            )
            await session.commit()

        result = await StuckMixWatchdog().check(alert=False)
        assert {e["id"] for e in result["stuck"]} == {"no-ts"}
        assert result["stuck"][0]["age_hours"] is None


class TestReporting:
    async def test_never_run_reports_unknown_not_all_clear(self):
        watchdog = StuckMixWatchdog()
        assert watchdog.last_result["state"] == "unknown"
        assert watchdog.last_result["stuck"] == []

    async def test_alert_is_written_once_per_window(self, prepared_db, monkeypatch):
        from app.services import activity_log

        sent = []

        class _FakeNotifier:
            async def notify(self, *a, **kw):
                sent.append(kw.get("title"))

        monkeypatch.setattr(
            "app.services.notification_service.get_notification_service",
            lambda: _FakeNotifier(),
        )

        await _add_mix("stuck-2", hours_old=48)
        watchdog = StuckMixWatchdog()
        await watchdog.check(alert=True)
        await watchdog.check(alert=True)  # second sweep, same mix

        items, total = await activity_log.query(
            limit=10, mix_id="stuck-2", event="stuck_mix"
        )
        assert total == 1, "a stuck mix must not become an alert storm"
        assert len(sent) == 1


# ---------------------------------------------------------------------------
# SoundCloud leg is checked live before anything is reported (2026-11-18:
# "Mirage Groove Reverie" was reported as live on SoundCloud after its track
# had been deleted, and the owner's own upload of the same set was missed).
# ---------------------------------------------------------------------------

MIRAGE_DURATION = 8319.754270833333


def _sc(track_id, title, duration_ms, slug, **extra):
    return {
        "id": int(track_id), "title": title, "duration": duration_ms,
        "permalink_url": f"https://soundcloud.com/thewillsee/{slug}?utm_source=x",
        "state": "finished", **extra,
    }


OWNER_COPY = _sc(
    2437681227, "SEE/K - Live at Beats & Branches 2026", 8319754,
    "see-k-live-at-beats-branches",
)
CONTROL = _sc(2406452214, "Dune Mirage Groove | House Mix", 4268937, "dune-mirage-groove")


def _lister(tracks):
    calls = []

    async def lister():
        calls.append(1)
        return list(tracks)

    lister.calls = calls
    return lister


async def _add_mirage(mix_id="mirage", **overrides):
    fields = dict(
        status="partial",
        soundcloud_url=(
            "https://soundcloud.com/thewillsee/mirage-groove-reverie-house"
            "?utm_medium=api"
        ),
        duration_seconds=MIRAGE_DURATION,
        audio_file_path="/watch/audio/SEEK - Live at Beats & Branches 2026.flac",
        pipeline_error=(
            "Partial publish — live on soundcloud; not published to youtube. "
            "youtube: Video file never appeared"
        ),
    )
    fields.update(overrides)
    await _add_mix(mix_id, hours_old=100, **fields)


def _capture_notifications(monkeypatch):
    sent = []

    class _FakeNotifier:
        async def notify(self, *a, **kw):
            sent.append(kw)

    monkeypatch.setattr(
        "app.services.notification_service.get_notification_service",
        lambda: _FakeNotifier(),
    )
    return sent


class TestSoundCloudVerification:
    async def test_owner_upload_with_same_audio_supersedes_the_mix(
        self, prepared_db, monkeypatch
    ):
        sent = _capture_notifications(monkeypatch)
        await _add_mirage()
        watchdog = StuckMixWatchdog(sc_track_lister=_lister([OWNER_COPY, CONTROL]))

        result = await watchdog.check(alert=True)

        assert result["stuck_count"] == 0
        assert [e["id"] for e in result["superseded"]] == ["mirage"]
        assert sent == [], "nobody may be asked to re-publish an owner's upload"
        async with async_session_factory() as session:
            mix = await session.get(Mix, "mirage")
        assert mix.pipeline_status == "duplicate"
        assert mix.metadata_json["duplicate_of"] == "soundcloud:2437681227"
        assert mix.metadata_json["canonical_upload"]["track_id"] == "2437681227"

        # And it stays resolved on the next sweep.
        again = await StuckMixWatchdog(sc_track_lister=_lister([OWNER_COPY])).check(
            alert=True
        )
        assert again["stuck_count"] == 0 and again["superseded"] == []

    async def test_deleted_track_is_not_reported_as_live(
        self, prepared_db, monkeypatch
    ):
        sent = _capture_notifications(monkeypatch)
        await _add_mirage()
        # Only an unrelated track is on the account: the pipeline's is gone and
        # there is no owner copy.
        result = await StuckMixWatchdog(sc_track_lister=_lister([CONTROL])).check(
            alert=True
        )

        assert result["stuck_count"] == 1
        entry = result["stuck"][0]
        assert entry["soundcloud_check"]["recorded"] == "gone"
        assert "deleted" in entry["reason"]
        message = sent[0]["message"]
        assert "live on soundcloud" not in message.lower()
        assert "NOT live" in message

    async def test_live_track_is_confirmed(self, prepared_db, monkeypatch):
        sent = _capture_notifications(monkeypatch)
        pipeline_track = _sc(
            2437814676, "Mirage Groove Reverie | House Mix", 8319754,
            "mirage-groove-reverie-house",
        )
        await _add_mirage()
        result = await StuckMixWatchdog(
            sc_track_lister=_lister([pipeline_track])
        ).check(alert=True)

        # The pipeline's own upload is never mistaken for an owner copy.
        assert result["stuck_count"] == 1
        assert result["stuck"][0]["soundcloud_check"]["recorded"] == "live"
        assert "verified live" in sent[0]["message"]

    async def test_failed_lookup_is_unverified_not_live(
        self, prepared_db, monkeypatch
    ):
        sent = _capture_notifications(monkeypatch)
        await _add_mirage()

        async def broken():
            raise RuntimeError("SoundCloud track listing failed (503)")

        result = await StuckMixWatchdog(sc_track_lister=broken).check(alert=True)
        assert result["stuck_count"] == 1
        assert result["stuck"][0]["soundcloud_check"]["recorded"] == "unverified"
        message = sent[0]["message"]
        assert "UNVERIFIED" in message
        assert "live on soundcloud" not in message.lower()

    async def test_title_match_counts_but_different_length_does_not(
        self, prepared_db, monkeypatch
    ):
        _capture_notifications(monkeypatch)
        await _add_mirage("by-title", soundcloud_url=None, status="failed")
        await _add_mirage(
            "other-set", soundcloud_url=None, status="failed",
            audio_file_path="/watch/audio/something else.flac",
        )
        same_title = _sc(
            1, "SEEK - Live at Beats & Branches 2026", 8321000, "seek-live",
        )
        # Same title, but a 20-minute edit: not the same recording.
        short_edit = _sc(2, "Something Else", 1200000, "something-else")
        result = await StuckMixWatchdog(
            sc_track_lister=_lister([same_title, short_edit])
        ).check(alert=False, verify=True)

        assert {e["id"] for e in result["superseded"]} == {"by-title"}
        assert {e["id"] for e in result["stuck"]} == {"other-set"}

    async def test_status_view_does_not_call_soundcloud(self, prepared_db):
        await _add_mirage()
        lister = _lister([OWNER_COPY])
        await StuckMixWatchdog(sc_track_lister=lister).check(alert=False)
        assert lister.calls == []
