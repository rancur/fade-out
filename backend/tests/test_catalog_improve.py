"""AI improve: heuristic title triage, LLM fallback, proposal drafting."""

import json

import pytest
from sqlalchemy import select

from app.config import settings
from app.services.catalog_improve import (
    build_platform_description,
    classify_title_heuristic,
    extract_tracklist_block,
    run_improve,
)


# ---------------------------------------------------------------------------
# Heuristic classifier
# ---------------------------------------------------------------------------

class TestHeuristicClassifier:
    @pytest.mark.parametrize(
        "title",
        [
            "Raid Train 2024-05-01",
            "raidtrain hour 3",
            "DJ Will See Live 5/1",
            "DJ See Set part 2",
            "Twitch VOD",
            "untitled",
            "2024-05-01",
            "5/1/24 live stream",
            "Friday May 3rd",
            "",
        ],
    )
    def test_generic_titles(self, title):
        assert classify_title_heuristic(title) == "generic"

    @pytest.mark.parametrize(
        "title",
        [
            "Desert Frequencies After Dark",
            "Four Decks and a Prayer",
            "Neon Cactus After Dark",
            "The Eye Opens at Midnight",
        ],
    )
    def test_keeper_titles(self, title):
        assert classify_title_heuristic(title) == "keeper"

    @pytest.mark.parametrize(
        "title",
        [
            "Silk and Static Vol 3",  # creative but has a digit
            "Warehouse",              # too short to call
        ],
    )
    def test_middle_ground_goes_to_llm(self, title):
        assert classify_title_heuristic(title) == "unknown"


# ---------------------------------------------------------------------------
# Tracklist retention helpers
# ---------------------------------------------------------------------------

TRACKLIST = "Tracklist:\n0:00 Intro\n5:30 Artist - Song\n1:02:11 Other - Tune"


class TestTracklistRetention:
    def test_extracts_tracklist_block(self):
        desc = f"Some prose about the mix.\n\n{TRACKLIST}\n\nTwitch: https://twitch.tv/x"
        assert extract_tracklist_block(desc) == TRACKLIST

    def test_no_tracklist_returns_empty(self):
        assert extract_tracklist_block("just prose here") == ""
        assert extract_tracklist_block("") == ""

    def test_build_appends_tracklist_then_links(self):
        out = build_platform_description("New body.", TRACKLIST, "Links: x")
        assert out == f"New body.\n\n{TRACKLIST}\n\nLinks: x"

    def test_build_without_tracklist(self):
        assert build_platform_description("Body.", "", "L") == "Body.\n\nL"


# ---------------------------------------------------------------------------
# run_improve end-to-end (LLM faked)
# ---------------------------------------------------------------------------

class FakeUsage:
    prompt_tokens = 10
    completion_tokens = 5


class FakeResponse:
    usage = FakeUsage()


class FakeGenerator:
    """Answers classify prompts with 'generic' and draft prompts with a canned
    title/description JSON. Instantiated via the patched DescriptionGenerator."""

    instances = []

    def __init__(self, sj=None):
        self.calls = []
        FakeGenerator.instances.append(self)

    async def _create_completion(self, prompt, max_tokens, temperature):
        self.calls.append(prompt)
        if "triaging DJ mix titles" in prompt:
            n = prompt.count("\n1. ") + prompt.count("\n2. ") + prompt.count("\n3. ")
            text = json.dumps([{"n": i + 1, "class": "generic"} for i in range(n)])
        else:
            text = json.dumps(
                {"title": "Peak-Time Techno Rampage", "description": "Fresh body copy."}
            )
        return FakeResponse(), text

    async def _track_usage(self, session, mix_id, op, in_tok, out_tok):
        pass


@pytest.fixture
def fake_llm(monkeypatch):
    import app.services.description_generator as dg

    FakeGenerator.instances = []
    monkeypatch.setattr(dg, "DescriptionGenerator", FakeGenerator)
    return FakeGenerator


async def _make_mix(**kwargs):
    from app.database import async_session_factory
    from app.models import Mix

    defaults = dict(
        title="Raid Train 2024-05-01",
        source="imported",
        pipeline_status="imported",
        youtube_video_id="vidImp00001",
        soundcloud_track_id="1500",
        description_youtube=f"Old YT text.\n\n{TRACKLIST}",
        description_soundcloud="Old SC text, no tracklist.",
    )
    defaults.update(kwargs)
    async with async_session_factory() as session:
        mix = Mix(**defaults)
        session.add(mix)
        await session.commit()
        return mix.id


async def _proposals():
    from app.database import async_session_factory
    from app.models import MixProposal

    async with async_session_factory() as session:
        return (await session.execute(select(MixProposal))).scalars().all()


async def _mix(mid):
    from app.database import async_session_factory
    from app.models import Mix

    async with async_session_factory() as session:
        return (
            await session.execute(select(Mix).where(Mix.id == mid))
        ).scalar_one()


class TestRunImprove:
    async def test_generic_mix_gets_title_and_description_drafts(
        self, prepared_db, fake_llm
    ):
        mix_id = await _make_mix()
        summary = await run_improve("all_generic")

        assert summary["generic"] == 1
        assert summary["keepers_locked"] == 0
        assert summary["proposals_drafted"] == 3  # title(both) + desc(yt) + desc(sc)

        proposals = await _proposals()
        by_key = {(p.platform, p.field): p for p in proposals}
        title = by_key[("both", "title")]
        assert title.proposed_value == "Peak-Time Techno Rampage"
        assert title.status == "draft" and title.created_by == "ai"
        assert title.current_value == "Raid Train 2024-05-01"

        # YT description retains the YT tracklist + YT links; SC gets SC links.
        yt_desc = by_key[("youtube", "description")].proposed_value
        assert "Fresh body copy." in yt_desc
        assert TRACKLIST in yt_desc
        assert settings.YOUTUBE_LINKS in yt_desc
        sc_desc = by_key[("soundcloud", "description")].proposed_value
        assert TRACKLIST not in sc_desc  # SC description had no tracklist
        assert settings.SOUNDCLOUD_LINKS in sc_desc

    async def test_keeper_title_locked_and_never_proposed(self, prepared_db, fake_llm):
        mix_id = await _make_mix(title="Four Decks and a Prayer")
        summary = await run_improve("all_generic")

        assert summary["keepers_locked"] == 1
        assert summary["proposals_drafted"] == 0
        assert (await _mix(mix_id)).title_locked is True
        assert await _proposals() == []

    async def test_unknown_titles_classified_by_llm(self, prepared_db, fake_llm):
        # digit-bearing title -> heuristic "unknown" -> fake LLM says generic
        await _make_mix(title="Silk and Static Vol 3")
        summary = await run_improve("all_generic")
        assert summary["generic"] == 1
        assert summary["proposals_drafted"] == 3
        classify_calls = [
            c for g in fake_llm.instances for c in g.calls if "triaging" in c
        ]
        assert len(classify_calls) == 1

    async def test_locked_mixes_are_skipped(self, prepared_db, fake_llm):
        await _make_mix(title="Raid Train 2024-05-01", title_locked=True)
        summary = await run_improve("all_generic")
        assert summary["classified"] == 0  # excluded by the all_generic query
        assert await _proposals() == []

    async def test_rerun_skips_mixes_with_open_proposals(self, prepared_db, fake_llm):
        await _make_mix()
        first = await run_improve("all_generic")
        second = await run_improve("all_generic")
        assert first["proposals_drafted"] == 3
        assert second["proposals_drafted"] == 0
        assert second["skipped"] == 1
        assert len(await _proposals()) == 3  # no duplicates

    async def test_explicit_mix_ids_scope(self, prepared_db, fake_llm):
        target = await _make_mix(title="Raid Train 2024-05-01")
        await _make_mix(title="Twitch VOD 22")
        summary = await run_improve([target])
        assert summary["classified"] == 1
        proposals = await _proposals()
        assert {p.mix_id for p in proposals} == {target}

    async def test_llm_classify_failure_defaults_to_keeper(
        self, prepared_db, monkeypatch
    ):
        import app.services.description_generator as dg

        class BrokenGenerator(FakeGenerator):
            async def _create_completion(self, prompt, max_tokens, temperature):
                raise RuntimeError("api down")

        monkeypatch.setattr(dg, "DescriptionGenerator", BrokenGenerator)
        mix_id = await _make_mix(title="Warehouse")  # heuristic unknown
        summary = await run_improve("all_generic")
        assert summary["keepers_locked"] == 1
        assert summary["proposals_drafted"] == 0
        assert (await _mix(mix_id)).title_locked is True

    async def test_activity_summary_emitted(self, prepared_db, fake_llm):
        await _make_mix()
        await run_improve("all_generic")
        from app.services import activity_log

        items, _ = await activity_log.query(event="catalog_improve")
        assert len(items) == 1
        assert "1 keeper" not in items[0]["message"]  # 0 keepers here
        assert "3 proposals drafted" in items[0]["message"]
