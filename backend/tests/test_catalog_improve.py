"""AI improve: heuristic title triage, LLM fallback, proposal drafting."""

import json

import pytest
from sqlalchemy import select

from app.config import settings
from app.services.catalog_improve import (
    OVERUSED_TITLE_WORDS,
    build_platform_description,
    classify_title_heuristic,
    draft_improvement_llm,
    draft_with_diversity_guard,
    extract_tracklist_block,
    run_improve,
    title_diversity_problem,
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


def _transient_mix():
    """A Mix instance that never touches the DB (draft prompt unit tests)."""
    from app.models import Mix

    return Mix(
        id="unit-mix", title="Raid Train 2024-05-01", genres=["techno"],
        duration_seconds=3600.0, description_youtube="Old text.",
    )


class TestTitleDiversityProblem:
    @pytest.mark.parametrize("word", OVERUSED_TITLE_WORDS)
    def test_banned_words_flagged_any_casing(self, word):
        assert title_diversity_problem(f"Neon {word.title()} Nights", []) is not None

    def test_banned_word_requires_word_boundary(self):
        # "sonically" contains "sonic" but is not the banned word itself
        assert title_diversity_problem("Sonically Yours", []) is None

    def test_similar_title_flagged(self):
        used = ["Desert Frequencies After Dark"]
        assert title_diversity_problem("Desert Frequencies After Dark!", used)
        assert title_diversity_problem("Completely Unrelated Banger", used) is None


class TestDraftPromptDiversity:
    async def test_prompt_includes_banned_words_and_used_titles(self):
        gen = FakeGenerator()
        draft = await draft_improvement_llm(
            _transient_mix(), gen, None,
            used_titles=["Neon Cactus After Dark", "Four Decks and a Prayer"],
        )
        assert draft is not None
        prompt = gen.calls[0]
        assert "BANNED WORDS" in prompt
        for word in OVERUSED_TITLE_WORDS:
            assert word in prompt
        assert "- Neon Cactus After Dark" in prompt
        assert "- Four Decks and a Prayer" in prompt
        assert "vary the title structure" in prompt.lower()

    async def test_prompt_caps_used_titles_at_40_most_recent(self):
        gen = FakeGenerator()
        await draft_improvement_llm(
            _transient_mix(), gen, None,
            used_titles=[f"Filler Title Number {i}" for i in range(100)],
        )
        prompt = gen.calls[0]
        assert "Filler Title Number 99" in prompt   # recent tail kept
        assert "Filler Title Number 59" not in prompt  # older ones dropped


class TestDiversityGuard:
    async def test_retry_on_duplicate_then_unique(self):
        titles = iter(["Desert Frequencies After Dark", "Freight Train Techno"])

        class Seq(FakeGenerator):
            async def _create_completion(self, prompt, max_tokens, temperature):
                self.calls.append(prompt)
                return FakeResponse(), json.dumps(
                    {"title": next(titles), "description": "Body."}
                )

        gen = Seq()
        draft = await draft_with_diversity_guard(
            _transient_mix(), gen, None,
            used_titles=["Desert Frequencies After Dark"],
        )
        assert draft["title"] == "Freight Train Techno"
        assert len(gen.calls) == 2
        assert "WAS REJECTED" in gen.calls[1]
        assert "too similar" in gen.calls[1].lower()

    async def test_no_retry_when_first_draft_is_fine(self):
        gen = FakeGenerator()
        draft = await draft_with_diversity_guard(
            _transient_mix(), gen, None, used_titles=["Something Else Entirely"]
        )
        assert draft["title"] == "Peak-Time Techno Rampage"
        assert len(gen.calls) == 1


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

    async def test_used_titles_and_accumulator_thread_across_mixes(
        self, prepared_db, monkeypatch
    ):
        """The 2nd mix's draft prompt lists the 1st mix's fresh title (and DB
        proposal titles); a duplicate draft triggers the guard's retry."""
        import app.services.description_generator as dg

        titles = iter(
            ["Midnight Freight Elevator",
             "Midnight Freight Elevator",  # duplicate -> guard retries
             "Bassline Border Crossing"]
        )

        class SeqGenerator(FakeGenerator):
            async def _create_completion(self, prompt, max_tokens, temperature):
                self.calls.append(prompt)
                if "triaging DJ mix titles" in prompt:
                    return FakeResponse(), json.dumps(
                        [{"n": 1, "class": "generic"}]
                    )
                return FakeResponse(), json.dumps(
                    {"title": next(titles), "description": "Body."}
                )

        FakeGenerator.instances = []
        monkeypatch.setattr(dg, "DescriptionGenerator", SeqGenerator)

        first = await _make_mix(title="Raid Train 2024-05-01")
        await _make_mix(title="Twitch VOD 22")
        # An applied AI title proposal already in the DB must count as used.
        from app.database import async_session_factory
        from app.models import MixProposal

        async with async_session_factory() as session:
            session.add(
                MixProposal(
                    mix_id=first, platform="both", field="title",
                    proposed_value="Cactus Bloom Sundown", status="applied",
                    created_by="ai",
                )
            )
            await session.commit()

        summary = await run_improve("all_generic")
        assert summary["proposals_drafted"] == 6  # 3 per mix

        proposals = await _proposals()
        drafted = {
            p.proposed_value for p in proposals
            if p.field == "title" and p.status == "draft"
        }
        assert drafted == {"Midnight Freight Elevator", "Bassline Border Crossing"}

        draft_prompts = [
            c for g in FakeGenerator.instances for c in g.calls if "triaging" not in c
        ]
        assert len(draft_prompts) == 3  # draft + duplicate retry + draft
        # Every draft prompt carries the applied proposal title from the DB.
        assert all("Cactus Bloom Sundown" in p for p in draft_prompts)
        # The run's first accepted title reached the later prompts.
        assert "Midnight Freight Elevator" in draft_prompts[-1]
        # The retry prompt carries the "be different" addendum.
        assert any("WAS REJECTED" in p for p in draft_prompts)

    async def test_guard_warns_when_retry_is_still_bad(
        self, prepared_db, monkeypatch
    ):
        import app.services.description_generator as dg

        class StubbornGenerator(FakeGenerator):
            async def _create_completion(self, prompt, max_tokens, temperature):
                self.calls.append(prompt)
                if "triaging DJ mix titles" in prompt:
                    return FakeResponse(), json.dumps([{"n": 1, "class": "generic"}])
                return FakeResponse(), json.dumps(
                    {"title": "Sonic Odyssey", "description": "Body."}
                )

        FakeGenerator.instances = []
        monkeypatch.setattr(dg, "DescriptionGenerator", StubbornGenerator)
        await _make_mix()
        summary = await run_improve("all_generic")

        # Kept despite the banned words, but with an activity warning.
        assert summary["proposals_drafted"] == 3
        titles = {p.proposed_value for p in await _proposals() if p.field == "title"}
        assert titles == {"Sonic Odyssey"}

        from app.services import activity_log

        items, _ = await activity_log.query(event="catalog_improve", level="warn")
        assert len(items) == 1
        assert "Diversity guard" in items[0]["message"]

    async def test_activity_summary_emitted(self, prepared_db, fake_llm):
        await _make_mix()
        await run_improve("all_generic")
        from app.services import activity_log

        items, _ = await activity_log.query(event="catalog_improve")
        assert len(items) == 1
        assert "1 keeper" not in items[0]["message"]  # 0 keepers here
        assert "3 proposals drafted" in items[0]["message"]
