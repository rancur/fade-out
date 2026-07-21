"""Series / raid-train title identity: derivation + mandatory title prefixes.

Live incident 2026-07-20: the pipeline processed "Twitch DJs House Nation Raid
Train (2026-07-20).flac" and shipped it as "Mirage Reverberation: Sonic Desert
Dances" (SoundCloud) / "Will See | House Breakbeat Mix | Energetic Beats"
(YouTube) — no series name, no date, unrecognizable to the owner. These tests
pin the fix: an identity derived from the source filename is a mandatory
prefix on both platform titles, the creative part is only the hook after it,
and uniqueness is enforced on the hook (never the shared prefix).
"""

from app.models import Mix
from app.services.description_generator import (
    DescriptionGenerator,
    derive_title_identity,
)
from app.services.uniqueness import KIND_TITLE, claim, is_taken


RAID_IDENTITY = "Twitch DJs House Nation Raid Train (2026-07-20)"


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
# derive_title_identity
# ---------------------------------------------------------------------------


class TestDeriveTitleIdentity:
    def test_raid_train_with_date_is_kept_verbatim(self):
        assert derive_title_identity(
            "Twitch DJs House Nation Raid Train (2026-07-20).flac"
        ) == RAID_IDENTITY

    def test_raid_train_lowercase_underscored(self):
        assert derive_title_identity(
            "twitch_djs_house_nation_raid_train_2026-07-20.flac"
        ) == RAID_IDENTITY

    def test_raid_train_without_date(self):
        assert derive_title_identity(
            "Twitch DJs Bass Coast Raid Train.flac"
        ) == "Twitch DJs Bass Coast Raid Train"

    def test_wednesdays_series(self):
        assert derive_title_identity(
            "Will See Wednesdays 2026-07-16.flac"
        ) == "Will See Wednesdays (2026-07-16)"

    def test_saturdays_series_2nd_spelling(self):
        assert derive_title_identity(
            "2nd Saturdays 2026-07-11.flac"
        ) == "Second Saturdays (2026-07-11)"

    def test_saturdays_series_spelled_out(self):
        assert derive_title_identity(
            "second_saturdays_2026-03-14.flac"
        ) == "Second Saturdays (2026-03-14)"

    def test_series_without_date(self):
        assert derive_title_identity("will_see_wednesdays.flac") == (
            "Will See Wednesdays"
        )

    def test_one_off_mix_has_no_identity(self):
        assert derive_title_identity("desert_session-live.flac") is None

    def test_date_only_filename_has_no_identity(self):
        assert derive_title_identity("2026-07-20.flac") is None

    def test_empty(self):
        assert derive_title_identity("") is None


# ---------------------------------------------------------------------------
# Creative (SoundCloud / mix) title keeps the identity prefix
# ---------------------------------------------------------------------------


class TestCreativeTitleKeepsIdentity:
    async def test_raid_train_prefix_retained(self, prepared_db, monkeypatch):
        mid = await _add_mix(title="Untitled")
        gen, seq = _generator(monkeypatch, ["Neon Mirage"])
        async with await _session() as session:
            title = await gen.generate_creative_title(
                genres=["house"], vibes=["energetic"],
                filename="Twitch DJs House Nation Raid Train (2026-07-20)",
                session=session, mix_id=mid, identity=RAID_IDENTITY,
            )
            await session.commit()
        assert title == f"{RAID_IDENTITY} — Neon Mirage"
        # The prompt tells the LLM the prefix is handled for it
        assert RAID_IDENTITY in seq.calls[0]
        assert "prepended" in seq.calls[0]

    async def test_uniqueness_enforced_on_hook_not_prefix(
        self, prepared_db, monkeypatch
    ):
        # Sibling episode already used the hook "Neon Mirage" — a new episode
        # drawing the same hook must retry, even though its full title (with
        # its own date) would differ.
        mid = await _add_mix(title="Untitled")
        gen, seq = _generator(monkeypatch, ["Neon Mirage", "Velvet Static"])
        async with await _session() as session:
            await claim(session, KIND_TITLE, "Neon Mirage")
            await session.commit()
            title = await gen.generate_creative_title(
                genres=["house"], vibes=["dark"],
                session=session, mix_id=mid,
                identity="Twitch DJs House Nation Raid Train (2026-07-27)",
            )
            await session.commit()
            assert title == (
                "Twitch DJs House Nation Raid Train (2026-07-27) — Velvet Static"
            )
            assert len(seq.calls) == 2
            # The HOOK is what got claimed — not the prefixed full title.
            assert await is_taken(session, KIND_TITLE, "Velvet Static")

    async def test_sibling_episode_prefix_does_not_collide(
        self, prepared_db, monkeypatch
    ):
        # The shared series prefix must not make a fresh hook look taken.
        mid = await _add_mix(title="Untitled")
        gen, seq = _generator(monkeypatch, ["Golden Static"])
        async with await _session() as session:
            await claim(session, KIND_TITLE, "Midnight Bloom")
            await session.commit()
            title = await gen.generate_creative_title(
                genres=["house"], vibes=["warm"],
                session=session, mix_id=mid, identity=RAID_IDENTITY,
            )
            await session.commit()
        assert title == f"{RAID_IDENTITY} — Golden Static"
        assert len(seq.calls) == 1  # accepted first draw, no false collision

    async def test_combined_title_capped_at_100_chars(
        self, prepared_db, monkeypatch
    ):
        mid = await _add_mix(title="Untitled")
        long_hook = "An Extremely Long And Winding Creative Hook That Never Ends"
        gen, _ = _generator(monkeypatch, [long_hook])
        async with await _session() as session:
            title = await gen.generate_creative_title(
                genres=["house"], vibes=["warm"],
                session=session, mix_id=mid, identity=RAID_IDENTITY,
            )
            await session.commit()
        assert title.startswith(f"{RAID_IDENTITY} — ")
        assert len(title) <= 100

    async def test_no_identity_keeps_current_behavior(
        self, prepared_db, monkeypatch
    ):
        mid = await _add_mix(title="Untitled")
        gen, seq = _generator(monkeypatch, ["Any Title At All"])
        async with await _session() as session:
            title = await gen.generate_creative_title(
                genres=["house"], vibes=["warm"], session=session, mix_id=mid,
            )
            await session.commit()
        assert title == "Any Title At All"
        assert "prepended" not in seq.calls[0]


# ---------------------------------------------------------------------------
# YouTube title keeps the identity prefix
# ---------------------------------------------------------------------------


class TestYoutubeTitleKeepsIdentity:
    async def test_raid_train_prefix_retained(self, monkeypatch):
        gen, seq = _generator(monkeypatch, ["House Mix | Peak Energy"])
        title = await gen.generate_youtube_title(
            genres=["house"], vibes=["energetic"], identity=RAID_IDENTITY,
        )
        assert title == f"{RAID_IDENTITY} | House Mix | Peak Energy"
        assert len(title) <= 100
        # Prompt swaps the brand-led format for the identity-led one
        assert RAID_IDENTITY in seq.calls[0]
        assert "prepended automatically" in seq.calls[0]

    async def test_no_identity_keeps_current_behavior(self, monkeypatch):
        gen, seq = _generator(
            monkeypatch, ["Will See | House Mix | Late Night Grooves"]
        )
        title = await gen.generate_youtube_title(
            genres=["house"], vibes=["warm"],
        )
        assert title == "Will See | House Mix | Late Night Grooves"
        assert "prepended automatically" not in seq.calls[0]


# ---------------------------------------------------------------------------
# Pipeline wiring: handle_generate_description derives + applies the identity
# ---------------------------------------------------------------------------


class TestHandlerAppliesIdentity:
    async def _run_handler(self, monkeypatch, audio_file_path):
        import app.services.handlers as handlers_mod
        from app.database import async_session_factory
        from app.models import AppSettings

        seq = SeqGenerator([
            "Neon Mirage",                       # creative hook
            "SoundCloud description body",       # sc description
            "YouTube description body",          # yt description
            "House Mix | Peak Energy",           # yt title
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

    async def test_raid_train_file_keeps_identity_on_both_titles(
        self, prepared_db, monkeypatch
    ):
        title, yt_title = await self._run_handler(
            monkeypatch,
            "/watch/audio/Twitch DJs House Nation Raid Train (2026-07-20).flac",
        )
        assert title == f"{RAID_IDENTITY} — Neon Mirage"
        assert yt_title == f"{RAID_IDENTITY} | House Mix | Peak Energy"

    async def test_series_file_keeps_series_prefix(
        self, prepared_db, monkeypatch
    ):
        title, yt_title = await self._run_handler(
            monkeypatch, "/watch/audio/will_see_wednesdays_2026-07-16.flac"
        )
        assert title == "Will See Wednesdays (2026-07-16) — Neon Mirage"
        assert yt_title == (
            "Will See Wednesdays (2026-07-16) | House Mix | Peak Energy"
        )

    async def test_one_off_file_stays_fully_creative(
        self, prepared_db, monkeypatch
    ):
        title, yt_title = await self._run_handler(
            monkeypatch, "/watch/audio/desert_session-live.flac"
        )
        assert title == "Neon Mirage"
        assert yt_title == "House Mix | Peak Energy"
