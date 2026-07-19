"""Brand thumbnail regen: targeting, proposal creation, and the summary."""

import pytest
from sqlalchemy import select

import app.services.catalog_thumbnails as ctn
from app.models import Mix, MixProposal
from app.services.catalog_thumbnails import run_regen_thumbnails


class FakeArtGenerator:
    """Writes a marker file instead of calling fal/DALL-E."""

    def __init__(self, fail_for=None):
        self.calls = []  # ("thumbnail"|"cover", mix_id, output_path)
        self.fail_for = fail_for or set()

    async def generate_youtube_thumbnail(self, **kwargs):
        mix_id = kwargs.get("mix_id")
        if mix_id in self.fail_for:
            raise RuntimeError("fal exploded")
        path = kwargs["output_path"]
        self.calls.append(("thumbnail", mix_id, path))
        return path

    async def generate_cover_art(self, **kwargs):
        mix_id = kwargs.get("mix_id")
        if mix_id in self.fail_for:
            raise RuntimeError("fal exploded")
        path = kwargs["output_path"]
        self.calls.append(("cover", mix_id, path))
        return path


@pytest.fixture
def fake_art(monkeypatch, tmp_path):
    gen = FakeArtGenerator()
    monkeypatch.setattr(ctn, "get_art_generator", lambda sj: gen)
    monkeypatch.setattr(ctn.settings, "OUTPUT_THUMBNAILS_PATH", str(tmp_path / "thumbs"))
    monkeypatch.setattr(ctn.settings, "OUTPUT_COVER_ART_PATH", str(tmp_path / "covers"))
    return gen


async def _add_mix(**kwargs):
    from app.database import async_session_factory

    defaults = dict(title="Mix", source="imported", pipeline_status="imported")
    defaults.update(kwargs)
    async with async_session_factory() as session:
        mix = Mix(**defaults)
        session.add(mix)
        await session.commit()
        return mix.id


async def _proposals():
    from app.database import async_session_factory

    async with async_session_factory() as session:
        return (await session.execute(select(MixProposal))).scalars().all()


async def _settings_json():
    from app.database import async_session_factory
    from app.models import AppSettings

    async with async_session_factory() as session:
        row = (
            await session.execute(select(AppSettings).where(AppSettings.id == 1))
        ).scalar_one_or_none()
        return dict(row.settings_json or {}) if row else {}


class TestRunRegenThumbnails:
    async def test_generates_per_platform_and_creates_approved_proposals(
        self, prepared_db, fake_art
    ):
        mid = await _add_mix(
            title="Raid Train dnb",
            genres=["drum and bass"],
            youtube_video_id="vidX",
            soundcloud_track_id="900",
            thumbnail_path="/old/thumb.jpg",
        )
        summary = await run_regen_thumbnails("all")

        assert summary["status"] == "ok"
        assert summary["targeted"] == 1
        assert summary["generated_youtube"] == 1
        assert summary["generated_soundcloud"] == 1
        assert summary["proposals_created"] == 2
        kinds = {(kind, mix_id) for kind, mix_id, _ in fake_art.calls}
        assert kinds == {("thumbnail", mid), ("cover", mid)}

        proposals = await _proposals()
        by_platform = {p.platform: p for p in proposals}
        yt = by_platform["youtube"]
        assert yt.field == "thumbnail" and yt.status == "approved"
        assert yt.created_by == "ai"
        assert yt.proposed_value.endswith(f"{mid}.jpg")
        assert yt.current_value == "/old/thumb.jpg"
        sc = by_platform["soundcloud"]
        assert sc.field == "thumbnail" and sc.status == "approved"
        assert sc.proposed_value.endswith(f"{mid}.jpg")

        sj = await _settings_json()
        assert sj["catalog_last_regen_thumbs"]["status"] == "ok"

    async def test_platform_without_id_is_skipped(self, prepared_db, fake_art):
        await _add_mix(title="YT only", youtube_video_id="v1")
        summary = await run_regen_thumbnails("all")
        assert summary["generated_youtube"] == 1
        assert summary["generated_soundcloud"] == 0
        assert [p.platform for p in await _proposals()] == ["youtube"]

    async def test_raid_trains_selector_filters_by_title(self, prepared_db, fake_art):
        raid = await _add_mix(title="Raid Train hour 2", youtube_video_id="vR")
        await _add_mix(title="A Proper Album Mix", youtube_video_id="vA")
        summary = await run_regen_thumbnails("raid-trains")
        assert summary["targeted"] == 1
        assert [mix_id for _, mix_id, _ in fake_art.calls] == [raid]

    async def test_explicit_mix_id_list(self, prepared_db, fake_art):
        target = await _add_mix(title="One", youtube_video_id="v1")
        await _add_mix(title="Two", youtube_video_id="v2")
        summary = await run_regen_thumbnails([target])
        assert summary["targeted"] == 1
        assert [mix_id for _, mix_id, _ in fake_art.calls] == [target]

    async def test_open_thumbnail_proposal_skips_platform(self, prepared_db, fake_art):
        from app.database import async_session_factory

        mid = await _add_mix(title="Busy", youtube_video_id="v1")
        async with async_session_factory() as session:
            session.add(
                MixProposal(
                    mix_id=mid, platform="youtube", field="thumbnail",
                    proposed_value="/x.jpg", status="approved", created_by="user",
                )
            )
            await session.commit()

        summary = await run_regen_thumbnails("all")
        assert summary["generated_youtube"] == 0
        assert summary["skipped"] == 1
        assert fake_art.calls == []

    async def test_failure_recorded_and_run_continues(self, prepared_db, fake_art):
        bad = await _add_mix(title="Bad", youtube_video_id="v1")
        good = await _add_mix(title="Good", youtube_video_id="v2")
        fake_art.fail_for = {bad}

        summary = await run_regen_thumbnails("all")
        assert summary["status"] == "partial"
        assert summary["failed"] == 1
        assert any("fal exploded" in e for e in summary["errors"])
        assert summary["generated_youtube"] == 1
        assert [p.mix_id for p in await _proposals()] == [good]
