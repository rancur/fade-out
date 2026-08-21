"""One unified, genre-forward, click-optimized title per mix.

Supersedes ``test_title_identity.py``. That suite pinned PR #30's behavior —
a mandatory series / raid-train prefix plus the recording date in front of the
creative hook, and a second, separate SEO title for YouTube. The owner
reversed that decision: he wants the abstract creative titles back, without a
show name or date, and he wants exactly three properties instead:

  1. the SoundCloud title and the YouTube title are the SAME string
  2. the title is optimized for click-through
  3. the genre appears in the title, because listeners click on genre

These tests pin those three, plus the guarantees that were already load-
bearing: uniqueness enforcement and a length cap.
"""

from app.models import Mix
from app.services.description_generator import (
    TITLE_MAX_CHARS,
    DescriptionGenerator,
    enforce_title_shape,
    genre_in_title,
    resolve_title_genre,
)
from app.services.uniqueness import KIND_TITLE, claim, is_taken


class FakeUsage:
    prompt_tokens = 10
    completion_tokens = 5


class FakeResponse:
    usage = FakeUsage()


class SeqGenerator:
    """LLM stand-in that returns a scripted sequence of completions."""

    def __init__(self, texts):
        self.texts = list(texts)
        self.calls = []

    async def _create_completion(self, prompt, max_tokens, temperature):
        self.calls.append(prompt)
        return FakeResponse(), self.texts.pop(0)


async def _session():
    from app.database import async_session_factory

    return async_session_factory()


async def _add_mix(**kwargs):
    from app.database import async_session_factory

    defaults = dict(title="Mix", source="imported", pipeline_status="imported")
    defaults.update(kwargs)
    async with async_session_factory() as session:
        mix = Mix(**defaults)
        session.add(mix)
        await session.commit()
        return mix.id


def _generator(monkeypatch, texts):
    gen = DescriptionGenerator({"openai_api_key": "test-key"})
    seq = SeqGenerator(texts)
    monkeypatch.setattr(gen, "_create_completion", seq._create_completion)

    async def _noop_usage(session, mix_id, op, in_tok, out_tok):
        pass

    monkeypatch.setattr(gen, "_track_usage", _noop_usage)
    return gen, seq


# ---------------------------------------------------------------------------
# Genre resolution reuses the existing classifier
# ---------------------------------------------------------------------------


class TestResolveTitleGenre:
    def test_house_bucket(self):
        assert resolve_title_genre(["house"]) == "House"

    def test_sub_genre_beats_parent(self):
        assert resolve_title_genre(["tech house"]) == "Tech House"
        assert resolve_title_genre(["deep house"]) == "Deep House"

    def test_dnb_gets_searchable_spelling(self):
        assert resolve_title_genre(["drum and bass"]) == "Drum & Bass"

    def test_primary_genre_wins_over_secondary(self):
        assert resolve_title_genre(["dubstep", "house"]) == "Dubstep"

    def test_alias_routes_through_thumbnail_classifier(self):
        # "neurofunk"/"riddim"/"ukg" are aliases the thumbnail genre-bucket
        # classifier already knows; the title path must not need its own copy.
        assert resolve_title_genre(["neurofunk"]) == "Drum & Bass"
        assert resolve_title_genre(["riddim"]) == "Dubstep"
        assert resolve_title_genre(["ukg"]) == "UK Garage"
        assert resolve_title_genre(["progressive"]) == "Melodic House"

    def test_unknown_and_empty_fall_back(self):
        assert resolve_title_genre(["electronic"]) == "Open Format"
        assert resolve_title_genre([]) == "Open Format"
        assert resolve_title_genre(None) == "Open Format"


class TestGenreInTitle:
    def test_exact_term(self):
        assert genre_in_title("Neon Cactus | Tech House Mix", "Tech House")

    def test_alternate_spelling_counts(self):
        assert genre_in_title("DnB Overdrive", "Drum & Bass")
        assert genre_in_title("Drum and Bass Fever", "Drum & Bass")

    def test_missing_genre(self):
        assert not genre_in_title("Mirage Reverberation", "Techno")


# ---------------------------------------------------------------------------
# enforce_title_shape: genre guaranteed, cap respected, keyword never trimmed
# ---------------------------------------------------------------------------


class TestEnforceTitleShape:
    def test_genre_appended_when_model_omits_it(self):
        assert enforce_title_shape("Mirage Reverberation", "Tech House") == (
            "Mirage Reverberation | Tech House Mix"
        )

    def test_title_with_genre_left_alone(self):
        title = "Mirage Reverberation | Tech House Mix"
        assert enforce_title_shape(title, "Tech House") == title

    def test_quotes_and_whitespace_stripped(self):
        assert enforce_title_shape('  "Silk and Static | House Mix" ', "House") == (
            "Silk and Static | House Mix"
        )

    def test_over_cap_is_truncated(self):
        long = "A Very Long Creative Hook That Simply Refuses To End | House Mix"
        out = enforce_title_shape(long + " And More Words", "House")
        assert len(out) <= TITLE_MAX_CHARS

    def test_hook_gives_up_chars_not_the_genre(self):
        hook = "An Extremely Long And Winding Creative Hook That Never Ever Ends"
        out = enforce_title_shape(hook, "Drum & Bass")
        assert len(out) <= TITLE_MAX_CHARS
        assert out.endswith("Drum & Bass")
        assert genre_in_title(out, "Drum & Bass")


class TestUniquenessIgnoresTheGenreTail:
    """The genre keyword is shared branding, like a series prefix.

    Comparing it would cut both ways: it hides a repeated hook behind a
    different genre, and it inflates the similarity of two genuinely different
    hooks in the same genre. ``uniqueness.normalize`` strips it, so the guard
    keeps measuring the invented part.
    """

    def test_every_genre_term_is_stripped(self):
        from app.services.description_generator import GENRE_TITLE_TERMS
        from app.services.uniqueness import KIND_TITLE, normalize

        for term, _spellings in GENRE_TITLE_TERMS.values():
            for tail in (f" | {term} Mix", f" | {term}"):
                assert normalize(KIND_TITLE, f"Neon Cactus{tail}") == "neon cactus", (
                    f"genre tail {tail!r} not stripped"
                )

    def test_same_hook_different_genre_still_collides(self):
        from app.services.uniqueness import KIND_TITLE, normalize

        assert normalize(KIND_TITLE, "Neon Cactus | House Mix") == normalize(
            KIND_TITLE, "Neon Cactus | Techno Mix"
        )

    def test_hook_is_not_over_stripped(self):
        from app.services.uniqueness import KIND_TITLE, normalize

        # Only the trailing genre segment goes; the hook keeps its own words.
        assert normalize(KIND_TITLE, "House of Mirrors | Techno Mix") == (
            "house of mirrors"
        )


# ---------------------------------------------------------------------------
# generate_creative_title: the single source of truth
# ---------------------------------------------------------------------------


class TestUnifiedTitleGeneration:
    async def test_genre_term_present_in_generated_title(
        self, prepared_db, monkeypatch
    ):
        mid = await _add_mix(title="Untitled")
        gen, seq = _generator(monkeypatch, ["Mirage Reverberation | Tech House Mix"])
        async with await _session() as session:
            title = await gen.generate_creative_title(
                genres=["tech house"], vibes=["hypnotic"],
                session=session, mix_id=mid,
            )
            await session.commit()
        assert title == "Mirage Reverberation | Tech House Mix"
        assert genre_in_title(title, "Tech House")
        # The prompt hands the model the resolved keyword and the CTR rules.
        assert "Tech House" in seq.calls[0]
        assert "Front-load the hook" in seq.calls[0]

    async def test_genre_forced_in_when_model_ignores_the_rule(
        self, prepared_db, monkeypatch
    ):
        mid = await _add_mix(title="Untitled")
        gen, _ = _generator(monkeypatch, ["Mirage Reverberation"])
        async with await _session() as session:
            title = await gen.generate_creative_title(
                genres=["drum and bass"], vibes=["rolling"],
                session=session, mix_id=mid,
            )
            await session.commit()
        assert title == "Mirage Reverberation | Drum & Bass Mix"

    async def test_abstract_brand_voice_survives(self, prepared_db, monkeypatch):
        # The operator's evocative style is the point — the genre rides along with it,
        # it does not replace it.
        mid = await _add_mix(title="Untitled")
        gen, _ = _generator(monkeypatch, ["Neon Cactus After Dark"])
        async with await _session() as session:
            title = await gen.generate_creative_title(
                genres=["house"], vibes=["late night"],
                session=session, mix_id=mid,
            )
            await session.commit()
        assert title.startswith("Neon Cactus After Dark")
        assert title == "Neon Cactus After Dark | House Mix"

    async def test_length_cap_respected(self, prepared_db, monkeypatch):
        mid = await _add_mix(title="Untitled")
        gen, _ = _generator(monkeypatch, [
            "An Extremely Long And Winding Creative Hook That Never Ever Ends"
        ])
        async with await _session() as session:
            title = await gen.generate_creative_title(
                genres=["house"], vibes=["warm"], session=session, mix_id=mid,
            )
            await session.commit()
        assert len(title) <= TITLE_MAX_CHARS
        assert genre_in_title(title, "House")

    async def test_uniqueness_still_enforced(self, prepared_db, monkeypatch):
        mid = await _add_mix(title="Untitled")
        gen, seq = _generator(monkeypatch, [
            "Neon Mirage | House Mix", "Velvet Static | House Mix",
        ])
        async with await _session() as session:
            await claim(session, KIND_TITLE, "Neon Mirage | House Mix")
            await session.commit()
            title = await gen.generate_creative_title(
                genres=["house"], vibes=["dark"], session=session, mix_id=mid,
            )
            await session.commit()
            assert title == "Velvet Static | House Mix"
            assert len(seq.calls) == 2
            assert await is_taken(session, KIND_TITLE, "Velvet Static | House Mix")

    async def test_stubborn_collision_gets_numeral_before_the_genre(
        self, prepared_db, monkeypatch
    ):
        # Three straight collisions: the numeral lands on the hook so the
        # search keyword stays at the end where it still reads naturally.
        mid = await _add_mix(title="Untitled")
        gen, _ = _generator(monkeypatch, ["Neon Mirage | House Mix"] * 3)
        async with await _session() as session:
            await claim(session, KIND_TITLE, "Neon Mirage | House Mix")
            await session.commit()
            title = await gen.generate_creative_title(
                genres=["house"], vibes=["warm"], session=session, mix_id=mid,
            )
            await session.commit()
        assert title == "Neon Mirage II | House Mix"
        assert genre_in_title(title, "House")


# ---------------------------------------------------------------------------
# No series / raid-train / date prefix survives anywhere
# ---------------------------------------------------------------------------


class TestNoIdentityPrefix:
    def test_identity_derivation_is_gone(self):
        import app.services.description_generator as dg

        assert not hasattr(dg, "derive_title_identity")
        assert not hasattr(dg, "TITLE_WITH_IDENTITY_MAX")

    def test_separate_youtube_generator_is_gone(self):
        assert not hasattr(DescriptionGenerator, "generate_youtube_title")

    def test_prompt_forbids_series_names_and_dates(self):
        from app.services.description_generator import CREATIVE_TITLE_PROMPT

        assert "no series or show name" in CREATIVE_TITLE_PROMPT
        assert "raid train" in CREATIVE_TITLE_PROMPT
        assert "No dates" in CREATIVE_TITLE_PROMPT

    async def test_raid_train_filename_gets_no_prefix(
        self, prepared_db, monkeypatch
    ):
        mid = await _add_mix(title="Untitled")
        gen, _ = _generator(monkeypatch, ["Mirage Reverberation | House Mix"])
        async with await _session() as session:
            title = await gen.generate_creative_title(
                genres=["house"], vibes=["energetic"],
                filename="Twitch DJs House Nation Raid Train (2026-07-20)",
                session=session, mix_id=mid,
            )
            await session.commit()
        assert title == "Mirage Reverberation | House Mix"
        assert "Raid Train" not in title
        assert "2026-07-20" not in title


# ---------------------------------------------------------------------------
# Pipeline wiring: one title, written to both platform fields
# ---------------------------------------------------------------------------


class TestHandlerWritesOneTitleToBothFields:
    async def _run_handler(self, monkeypatch, audio_file_path, hook):
        import app.services.handlers as handlers_mod
        from app.database import async_session_factory
        from app.models import AppSettings

        seq = SeqGenerator([
            hook,                            # the one title
            "SoundCloud description body",   # sc description
            "YouTube description body",      # yt description
        ])
        monkeypatch.setattr(
            DescriptionGenerator,
            "_create_completion",
            lambda self, prompt, max_tokens, temperature: seq._create_completion(
                prompt, max_tokens, temperature
            ),
        )

        async with async_session_factory() as session:
            session.add(AppSettings(
                id=1, settings_json={"openai_api_key": "test-key"}
            ))
            await session.commit()

        # A real ingest title (not "Untitled"): the filename-fallback branch
        # would dirty the mix on the caller session and its autoflush would
        # hold sqlite's write lock against the title session.
        mid = await _add_mix(
            title="Recording",
            audio_file_path=audio_file_path,
            genres=["house"],
            vibes=["energetic"],
        )
        async with async_session_factory() as session:
            await handlers_mod.handle_generate_description(mid, session)
            await session.commit()
        async with async_session_factory() as session:
            mix = await session.get(Mix, mid)
            return mix.title, mix.title_youtube

    async def test_both_platform_fields_get_the_same_string(
        self, prepared_db, monkeypatch
    ):
        title, yt_title = await self._run_handler(
            monkeypatch,
            "/watch/audio/desert_session-live.flac",
            "Mirage Reverberation | House Mix",
        )
        assert title == yt_title == "Mirage Reverberation | House Mix"

    async def test_raid_train_file_ships_without_series_or_date(
        self, prepared_db, monkeypatch
    ):
        title, yt_title = await self._run_handler(
            monkeypatch,
            "/watch/audio/Twitch DJs House Nation Raid Train (2026-07-20).flac",
            "Mirage Reverberation | House Mix",
        )
        assert title == yt_title == "Mirage Reverberation | House Mix"
        for field in (title, yt_title):
            assert "Raid Train" not in field
            assert "Twitch" not in field
            assert "2026-07-20" not in field

    async def test_series_file_ships_without_series_or_date(
        self, prepared_db, monkeypatch
    ):
        title, yt_title = await self._run_handler(
            monkeypatch,
            "/watch/audio/will_see_wednesdays_2026-07-16.flac",
            "Velvet Static | House Mix",
        )
        assert title == yt_title == "Velvet Static | House Mix"
        assert "Wednesdays" not in title
        assert "2026-07-16" not in title

    async def test_genre_forced_onto_the_published_pair(
        self, prepared_db, monkeypatch
    ):
        title, yt_title = await self._run_handler(
            monkeypatch,
            "/watch/audio/desert_session-live.flac",
            "Mirage Reverberation",  # model forgot the genre
        )
        assert title == yt_title == "Mirage Reverberation | House Mix"
        assert genre_in_title(title, "House")
