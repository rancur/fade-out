"""Apply worker: platform pushes, status transitions, error capture, quota."""

import json
from datetime import date

import pytest
from sqlalchemy import select

import app.services.catalog_apply as apply_mod
from app.config import settings
from app.services.catalog_apply import run_apply


class FakeYT:
    def __init__(self):
        self.calls = []
        self.fail_on = None  # e.g. ("update_video_fields",)

    def _record(self, name, *args, **kwargs):
        self.calls.append((name, args, kwargs))
        if self.fail_on and name in self.fail_on:
            raise RuntimeError(f"yt {name} exploded")

    async def update_video_fields(self, video_id, title=None, description=None, tags=None):
        self._record("update_video_fields", video_id, title=title, description=description, tags=tags)

    async def set_thumbnail(self, video_id, path):
        self._record("set_thumbnail", video_id, path)

    async def add_video_to_playlist(self, playlist_id, video_id):
        self._record("add_video_to_playlist", playlist_id, video_id)

    async def remove_video_from_playlist(self, playlist_id, video_id):
        self._record("remove_video_from_playlist", playlist_id, video_id)


class FakeSC:
    def __init__(self):
        self.calls = []
        self.fail_on = None

    async def update_track_fields(self, track_id, title=None, description=None,
                                  tags=None, artwork_path=None):
        self.calls.append(
            ("update_track_fields", track_id,
             {"title": title, "description": description, "tags": tags,
              "artwork_path": artwork_path})
        )
        if self.fail_on and "update_track_fields" in self.fail_on:
            raise RuntimeError("sc update exploded")


@pytest.fixture
def fake_uploaders(monkeypatch):
    yt, sc = FakeYT(), FakeSC()
    monkeypatch.setattr(apply_mod, "get_youtube_uploader", lambda sj: yt)
    monkeypatch.setattr(apply_mod, "get_soundcloud_uploader", lambda sj, cb=None: sc)
    return yt, sc


async def _make_mix(**kwargs):
    from app.database import async_session_factory
    from app.models import Mix

    defaults = dict(
        id="mx-1",
        title="Old Title",
        title_youtube="Old YT Title",
        youtube_video_id="vid00000001",
        soundcloud_track_id="900",
        source="imported",
        pipeline_status="imported",
    )
    defaults.update(kwargs)
    async with async_session_factory() as session:
        mix = Mix(**defaults)
        session.add(mix)
        await session.commit()
    return defaults["id"]


async def _make_proposal(mix_id, platform="both", field="title",
                         proposed="New Title", status="approved", **kwargs):
    from app.database import async_session_factory
    from app.models import MixProposal

    async with async_session_factory() as session:
        p = MixProposal(
            mix_id=mix_id, platform=platform, field=field,
            proposed_value=proposed, status=status,
            created_by=kwargs.pop("created_by", "user"), **kwargs,
        )
        session.add(p)
        await session.commit()
        return p.id


async def _get_proposal(pid):
    from app.database import async_session_factory
    from app.models import MixProposal

    async with async_session_factory() as session:
        return (
            await session.execute(select(MixProposal).where(MixProposal.id == pid))
        ).scalar_one()


async def _get_mix(mid):
    from app.database import async_session_factory
    from app.models import Mix

    async with async_session_factory() as session:
        return (
            await session.execute(select(Mix).where(Mix.id == mid))
        ).scalar_one()


async def _get_quota():
    from app.database import async_session_factory
    from app.models import AppSettings

    async with async_session_factory() as session:
        row = (
            await session.execute(select(AppSettings).where(AppSettings.id == 1))
        ).scalar_one_or_none()
        return ((row.settings_json or {}).get("catalog_yt_quota")) if row else None


class TestApplyBasics:
    async def test_title_both_platforms(self, prepared_db, fake_uploaders):
        yt, sc = fake_uploaders
        mix_id = await _make_mix()
        pid = await _make_proposal(mix_id, "both", "title", "New Title")

        summary = await run_apply()
        assert summary == {"applied": 1, "failed": 0, "queued": 0, "paused": False}

        p = await _get_proposal(pid)
        assert p.status == "applied"
        assert p.applied_at is not None and p.error is None
        assert yt.calls[0][0] == "update_video_fields"
        assert yt.calls[0][2]["title"] == "New Title"
        assert sc.calls[0][1] == "900" and sc.calls[0][2]["title"] == "New Title"

        mix = await _get_mix(mix_id)
        assert mix.title == "New Title"  # SC title is the canonical mix title
        assert mix.title_youtube == "New Title"

    async def test_description_yt_only(self, prepared_db, fake_uploaders):
        yt, sc = fake_uploaders
        mix_id = await _make_mix()
        await _make_proposal(mix_id, "youtube", "description", "fresh desc")
        await run_apply()
        assert yt.calls[0][2]["description"] == "fresh desc"
        assert sc.calls == []
        assert (await _get_mix(mix_id)).description_youtube == "fresh desc"

    async def test_tags_json_decoded(self, prepared_db, fake_uploaders):
        yt, sc = fake_uploaders
        mix_id = await _make_mix()
        await _make_proposal(mix_id, "both", "tags", json.dumps(["house", "dj mix"]))
        await run_apply()
        assert yt.calls[0][2]["tags"] == ["house", "dj mix"]
        assert sc.calls[0][2]["tags"] == ["house", "dj mix"]
        assert (await _get_mix(mix_id)).tags == ["house", "dj mix"]

    async def test_thumbnail_paths(self, prepared_db, fake_uploaders):
        yt, sc = fake_uploaders
        mix_id = await _make_mix()
        await _make_proposal(mix_id, "youtube", "thumbnail", "/output/thumbs/x.jpg")
        await _make_proposal(mix_id, "soundcloud", "thumbnail", "/output/art/x.jpg")
        await run_apply()
        assert ("set_thumbnail", ("vid00000001", "/output/thumbs/x.jpg"), {}) in yt.calls
        assert sc.calls[0][2]["artwork_path"] == "/output/art/x.jpg"

    async def test_playlist_add_remove(self, prepared_db, fake_uploaders):
        yt, _sc = fake_uploaders
        mix_id = await _make_mix()
        await _make_proposal(
            mix_id, "youtube", "playlist",
            json.dumps({"add": ["PLnew"], "remove": ["PLold"]}),
        )
        await run_apply()
        assert ("add_video_to_playlist", ("PLnew", "vid00000001"), {}) in yt.calls
        assert ("remove_video_from_playlist", ("PLold", "vid00000001"), {}) in yt.calls
        assert (await _get_mix(mix_id)).youtube_playlist_id == "PLnew"

    async def test_missing_mix_marks_failed(self, prepared_db, fake_uploaders):
        pid = await _make_proposal("ghost-mix", "youtube", "title", "x")
        summary = await run_apply()
        assert summary["failed"] == 1
        p = await _get_proposal(pid)
        assert p.status == "failed" and "no longer exists" in p.error


class TestApplyFailures:
    async def test_platform_error_captured_and_run_continues(
        self, prepared_db, fake_uploaders
    ):
        yt, sc = fake_uploaders
        yt.fail_on = ("update_video_fields",)
        mix_id = await _make_mix()
        pid_fail = await _make_proposal(mix_id, "youtube", "title", "boom")
        pid_ok = await _make_proposal(mix_id, "soundcloud", "title", "fine")

        summary = await run_apply()
        assert summary["applied"] == 1 and summary["failed"] == 1

        p_fail = await _get_proposal(pid_fail)
        assert p_fail.status == "failed"
        assert "yt update_video_fields exploded" in p_fail.error
        assert (await _get_proposal(pid_ok)).status == "applied"

    async def test_draft_proposals_are_not_applied(self, prepared_db, fake_uploaders):
        yt, sc = fake_uploaders
        mix_id = await _make_mix()
        pid = await _make_proposal(mix_id, "youtube", "title", "x", status="draft")
        summary = await run_apply()
        assert summary["applied"] == 0
        assert (await _get_proposal(pid)).status == "draft"
        assert yt.calls == []


class TestQuotaBudget:
    async def test_pauses_at_budget_and_persists_usage(
        self, prepared_db, fake_uploaders, monkeypatch
    ):
        monkeypatch.setattr(settings, "YOUTUBE_DAILY_QUOTA_BUDGET", 100)
        mix_id = await _make_mix()
        pids = [
            await _make_proposal(mix_id, "youtube", "title", f"t{i}")
            for i in range(3)
        ]

        summary = await run_apply()
        assert summary["applied"] == 2
        assert summary["paused"] is True
        assert summary["queued"] == 1
        # Exactly one proposal stays queued (same-second created_at makes the
        # exact victim nondeterministic under uuid tiebreak).
        statuses = sorted([(await _get_proposal(pid)).status for pid in pids])
        assert statuses == ["applied", "applied", "approved"]

        quota = await _get_quota()
        assert quota == {"date": date.today().isoformat(), "used": 100}

        # Budget-pause warning lands in the activity log.
        from app.services import activity_log

        items, _ = await activity_log.query(event="catalog_apply", level="warn")
        assert any("quota budget" in i["message"] for i in items)

    async def test_resumes_next_run_when_quota_resets(
        self, prepared_db, fake_uploaders, monkeypatch
    ):
        from app.database import async_session_factory
        from app.models import AppSettings

        monkeypatch.setattr(settings, "YOUTUBE_DAILY_QUOTA_BUDGET", 100)
        # Yesterday's exhausted quota must not block today.
        async with async_session_factory() as session:
            session.add(
                AppSettings(
                    id=1,
                    settings_json={"catalog_yt_quota": {"date": "2020-01-01", "used": 99999}},
                )
            )
            await session.commit()

        mix_id = await _make_mix()
        pid = await _make_proposal(mix_id, "youtube", "title", "resumed")
        summary = await run_apply()
        assert summary["applied"] == 1 and summary["paused"] is False
        assert (await _get_proposal(pid)).status == "applied"

    async def test_soundcloud_writes_do_not_consume_quota(
        self, prepared_db, fake_uploaders, monkeypatch
    ):
        monkeypatch.setattr(settings, "YOUTUBE_DAILY_QUOTA_BUDGET", 50)
        mix_id = await _make_mix()
        for i in range(3):
            await _make_proposal(mix_id, "soundcloud", "title", f"sc{i}")
        summary = await run_apply()
        assert summary["applied"] == 3 and summary["paused"] is False
        assert (await _get_quota())["used"] == 0

    async def test_playlist_cost_counts_each_operation(
        self, prepared_db, fake_uploaders, monkeypatch
    ):
        monkeypatch.setattr(settings, "YOUTUBE_DAILY_QUOTA_BUDGET", 100)
        mix_id = await _make_mix()
        # 3 playlist ops = 150 units > 100 budget -> paused before applying.
        pid = await _make_proposal(
            mix_id, "youtube", "playlist",
            json.dumps({"add": ["a", "b"], "remove": ["c"]}),
        )
        summary = await run_apply()
        assert summary["paused"] is True and summary["applied"] == 0
        assert (await _get_proposal(pid)).status == "approved"
