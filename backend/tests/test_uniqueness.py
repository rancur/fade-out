"""Uniqueness engine: registry semantics, unique hooks/scenes, title retry."""

import json
from types import SimpleNamespace

from sqlalchemy import select

from app.models import Mix, UsedCreative
from app.services import thumbnail_design
from app.services.uniqueness import (
    KIND_HOOK,
    KIND_SCENE,
    KIND_TITLE,
    claim,
    is_taken,
    normalize,
    recent_values,
    release_for_mix,
)


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

    async def _track_usage(self, session, mix_id, op, in_tok, out_tok):
        pass


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


# ---------------------------------------------------------------------------
# normalize
# ---------------------------------------------------------------------------


class TestNormalize:
    def test_strips_punctuation_case_and_whitespace(self):
        assert normalize(KIND_TITLE, "  Desert-Frequencies:  Vol. III! ") == (
            "desert frequencies vol iii"
        )

    def test_title_drops_series_prefix(self):
        assert normalize(KIND_TITLE, "Will See Wednesdays: Golden Hour") == "golden hour"
        assert normalize(KIND_TITLE, "Second Saturdays - Neon Drift") == "neon drift"
        assert normalize(KIND_TITLE, "2nd Saturdays | Neon Drift") == "neon drift"

    def test_series_prefix_only_applies_to_titles(self):
        assert normalize(KIND_HOOK, "Will See Wednesdays: Golden Hour") == (
            "will see wednesdays golden hour"
        )

    def test_mid_title_series_words_kept(self):
        # Only a LEADING prefix is branding; mid-title mentions are content.
        assert "wednesdays" in normalize(KIND_TITLE, "Lost on Will See Wednesdays")


# ---------------------------------------------------------------------------
# is_taken / claim / release
# ---------------------------------------------------------------------------


class TestRegistry:
    async def test_exact_match_normalized(self, prepared_db):
        async with await _session() as session:
            await claim(session, KIND_TITLE, "Desert Frequencies Vol. III")
            await session.commit()
            assert await is_taken(session, KIND_TITLE, "desert frequencies vol iii!")
            assert not await is_taken(session, KIND_TITLE, "Completely Different Set")

    async def test_series_prefix_rule_compares_post_prefix_part(self, prepared_db):
        async with await _session() as session:
            await claim(session, KIND_TITLE, "Will See Wednesdays: Golden Hour")
            await session.commit()
            # same post-prefix part under another series prefix -> taken
            assert await is_taken(session, KIND_TITLE, "Second Saturdays: Golden Hour")
            assert await is_taken(session, KIND_TITLE, "Golden Hour")
            assert not await is_taken(session, KIND_TITLE, "Will See Wednesdays: Blue Hour")

    async def test_title_similarity_threshold(self, prepared_db):
        async with await _session() as session:
            await claim(session, KIND_TITLE, "Desert Frequencies After Dark")
            await session.commit()
            assert await is_taken(session, KIND_TITLE, "Desert Frequencies After Darkk")
            assert not await is_taken(session, KIND_TITLE, "Freight Train Techno")

    async def test_hooks_are_exact_match_only(self, prepared_db):
        async with await _session() as session:
            await claim(session, KIND_HOOK, "ALL VIBES")
            await session.commit()
            assert await is_taken(session, KIND_HOOK, "all vibes")
            # one-char-off would fuzzy-match a title; hooks must not
            assert not await is_taken(session, KIND_HOOK, "TALL VIBES")

    async def test_scene_similarity_threshold(self, prepared_db):
        desc = "house | bathed in golden-hour light | beneath a vast clear sky"
        async with await _session() as session:
            await claim(session, KIND_SCENE, desc)
            await session.commit()
            assert await is_taken(session, KIND_SCENE, desc + "!")
            assert not await is_taken(
                session, KIND_SCENE,
                "techno | under a moonless desert night | through shimmering dust haze",
            )

    async def test_fuzzy_false_is_exact_only(self, prepared_db):
        async with await _session() as session:
            await claim(session, KIND_TITLE, "Neon Cactus After Dark")
            await session.commit()
            assert await is_taken(session, KIND_TITLE, "Neon Cactus After Dark II")
            assert not await is_taken(
                session, KIND_TITLE, "Neon Cactus After Dark II", fuzzy=False
            )

    async def test_claim_is_idempotent(self, prepared_db):
        mid = await _add_mix(title="Owner")
        async with await _session() as session:
            first = await claim(session, KIND_TITLE, "Freight Train Techno")
            again = await claim(session, KIND_TITLE, "freight train TECHNO!", mix_id=mid)
            await session.commit()
            assert first.id == again.id
            assert again.mix_id == mid  # null owner adopts the claimant
            rows = (await session.execute(select(UsedCreative))).scalars().all()
            assert len(rows) == 1

    async def test_empty_values_never_claimed_or_taken(self, prepared_db):
        async with await _session() as session:
            assert await claim(session, KIND_TITLE, "   ") is None
            assert not await is_taken(session, KIND_TITLE, "")

    async def test_release_for_mix_and_exclude_mix_id(self, prepared_db):
        mid = await _add_mix(title="Owner")
        other = await _add_mix(title="Other")
        async with await _session() as session:
            await claim(session, KIND_HOOK, "ROLLERS AT DUSK", mix_id=mid)
            await claim(session, KIND_SCENE, "dnb | dusk | haze", mix_id=mid)
            await claim(session, KIND_TITLE, "Kept Title", mix_id=mid)
            await claim(session, KIND_HOOK, "BASS RITUAL", mix_id=other)
            await session.commit()

            # a mix never collides with its own claims
            assert not await is_taken(
                session, KIND_HOOK, "ROLLERS AT DUSK", exclude_mix_id=mid
            )
            assert await is_taken(
                session, KIND_HOOK, "ROLLERS AT DUSK", exclude_mix_id=other
            )

            released = await release_for_mix(session, mid, KIND_HOOK)
            assert released == 1
            await release_for_mix(session, mid, KIND_SCENE)
            await session.commit()
            assert not await is_taken(session, KIND_HOOK, "ROLLERS AT DUSK")
            # title claim untouched by kind-scoped release; other mix untouched
            assert await is_taken(session, KIND_TITLE, "Kept Title")
            assert await is_taken(session, KIND_HOOK, "BASS RITUAL")

    async def test_recent_values_oldest_first(self, prepared_db):
        async with await _session() as session:
            for t in ("First Title", "Second Title", "Third Title"):
                await claim(session, KIND_TITLE, t)
            await session.commit()
            assert await recent_values(session, KIND_TITLE, limit=2) == [
                "Second Title", "Third Title",
            ]


# ---------------------------------------------------------------------------
# Unique hook generation
# ---------------------------------------------------------------------------


def _mix_obj(mix_id="mix-1", title="Jungle Stampede", genres=None, vibes=None):
    return SimpleNamespace(
        id=mix_id,
        title=title,
        genres=genres or ["drum and bass"],
        vibes=vibes or ["dark"],
    )


DNB_MOTIF = thumbnail_design.GENRE_MOTIFS["drum and bass"]


class TestGenerateUniqueHook:
    async def test_llm_hook_accepted_and_claimed(self, prepared_db):
        gen = SeqGenerator(['"Rollers At Dusk"'])
        async with await _session() as session:
            hook = await thumbnail_design.generate_unique_hook(
                _mix_obj(), DNB_MOTIF, gen, session
            )
            await session.commit()
            assert hook == "ROLLERS\nAT DUSK"
            assert await is_taken(session, KIND_HOOK, "ROLLERS AT DUSK")

    async def test_retries_until_unique_with_used_hooks_in_prompt(self, prepared_db):
        gen = SeqGenerator(["TAKEN HOOK", "TAKEN HOOK", "FRESH CUT"])
        async with await _session() as session:
            await claim(session, KIND_HOOK, "TAKEN HOOK")
            await session.commit()
            hook = await thumbnail_design.generate_unique_hook(
                _mix_obj(), DNB_MOTIF, gen, session
            )
            assert hook == "FRESH\nCUT"
            assert len(gen.calls) == 3
            assert "- TAKEN HOOK" in gen.calls[0]  # used hooks fed to the LLM
            assert "WAS REJECTED" in gen.calls[1]  # rejected draft fed back

    async def test_fallback_is_motif_hook_when_free(self, prepared_db):
        async with await _session() as session:
            hook = await thumbnail_design.generate_unique_hook(
                _mix_obj(), DNB_MOTIF, None, session
            )
            assert hook == "DNB\nOVERDRIVE"
            assert await is_taken(session, KIND_HOOK, "DNB OVERDRIVE")

    async def test_never_bare_motif_hook_when_taken(self, prepared_db):
        """The live bug: same-genre mixes must not all get the motif hook."""
        async with await _session() as session:
            first = await thumbnail_design.generate_unique_hook(
                _mix_obj("mix-1", title="Jungle Stampede"), DNB_MOTIF, None, session
            )
            second = await thumbnail_design.generate_unique_hook(
                _mix_obj("mix-2", title="Midnight Mirage"), DNB_MOTIF, None, session
            )
            assert first == "DNB\nOVERDRIVE"
            assert second != first
            assert "MIDNIGHT" in second  # distinguishing word from the title
            assert await is_taken(session, KIND_HOOK, second.replace("\n", " "))

    async def test_llm_failure_falls_back(self, prepared_db):
        class Broken:
            async def _create_completion(self, prompt, max_tokens, temperature):
                raise RuntimeError("API down")

        async with await _session() as session:
            hook = await thumbnail_design.generate_unique_hook(
                _mix_obj(), DNB_MOTIF, Broken(), session
            )
            assert hook == "DNB\nOVERDRIVE"

    async def test_unusable_llm_output_skipped(self, prepared_db):
        # 1 word, then 5 words, then valid
        gen = SeqGenerator(["BASS", "ONE TWO THREE FOUR FIVE", "DESERT ROLLERS"])
        async with await _session() as session:
            hook = await thumbnail_design.generate_unique_hook(
                _mix_obj(), DNB_MOTIF, gen, session
            )
            assert hook == "DESERT\nROLLERS"

    def test_clean_hook_validation(self):
        assert thumbnail_design._clean_hook(' "rollers at dusk!" ') == "ROLLERS AT DUSK"
        assert thumbnail_design._clean_hook("ONE") is None
        assert thumbnail_design._clean_hook("A B C D E") is None
        assert thumbnail_design._clean_hook("THIS HOOK IS WAY TOO LONG FOR ART") is None

    def test_format_hook_lines_balances(self):
        assert thumbnail_design.format_hook_lines("ROLLERS AT DUSK") == "ROLLERS\nAT DUSK"
        assert thumbnail_design.format_hook_lines("FRESH CUT") == "FRESH\nCUT"
        assert thumbnail_design.format_hook_lines("SOLO") == "SOLO"


# ---------------------------------------------------------------------------
# Scene variation
# ---------------------------------------------------------------------------


class TestSceneVariation:
    def test_vary_scene_deterministic(self):
        a1 = thumbnail_design.vary_scene("mix-1", DNB_MOTIF, "drum and bass")
        a2 = thumbnail_design.vary_scene("mix-1", DNB_MOTIF, "drum and bass")
        assert a1 == a2

    def test_vary_scene_salt_changes_output(self):
        a = thumbnail_design.vary_scene("mix-1", DNB_MOTIF, "drum and bass", salt=0)
        b = thumbnail_design.vary_scene("mix-1", DNB_MOTIF, "drum and bass", salt=1)
        assert a != b

    def test_same_genre_mixes_get_distinct_scenes(self):
        descriptors = {
            thumbnail_design.vary_scene(f"mix-{i}", DNB_MOTIF, "drum and bass")[1]
            for i in range(12)
        }
        assert len(descriptors) > 1  # varied, not one fixed scene per genre

    def test_scene_text_extends_base_scene(self):
        scene, descriptor = thumbnail_design.vary_scene("mix-1", DNB_MOTIF, "drum and bass")
        assert scene.startswith(str(DNB_MOTIF["scene"]))
        assert len(scene) > len(str(DNB_MOTIF["scene"]))
        assert descriptor.startswith("drum and bass |")

    async def test_generate_unique_scene_rerolls_on_collision(self, prepared_db):
        mix_a = _mix_obj("mix-a")
        mix_b = _mix_obj("mix-b")
        async with await _session() as session:
            # Claim mix-b's salt-0 descriptor FOR ANOTHER MIX so b must re-roll.
            _, desc_b0 = thumbnail_design.vary_scene("mix-b", DNB_MOTIF, "drum and bass", 0)
            await claim(session, KIND_SCENE, desc_b0, mix_id="someone-else")
            await session.commit()

            scene_b = await thumbnail_design.generate_unique_scene(
                mix_b, DNB_MOTIF, "drum and bass", session
            )
            text_b0, _ = thumbnail_design.vary_scene("mix-b", DNB_MOTIF, "drum and bass", 0)
            assert scene_b != text_b0  # re-rolled off the taken salt

            scene_a = await thumbnail_design.generate_unique_scene(
                mix_a, DNB_MOTIF, "drum and bass", session
            )
            assert scene_a != scene_b
            rows = (
                (await session.execute(select(UsedCreative).where(UsedCreative.kind == "scene")))
                .scalars().all()
            )
            assert {r.mix_id for r in rows} == {"someone-else", "mix-a", "mix-b"}


# ---------------------------------------------------------------------------
# unique_design_for_mix + regen wiring
# ---------------------------------------------------------------------------


class TestUniqueDesignForMix:
    async def test_release_then_reclaim_never_self_collides(self, prepared_db):
        mid = await _add_mix(title="Roller Season", genres=["drum and bass"])
        async with await _session() as session:
            mix = await session.get(Mix, mid)
            first = await thumbnail_design.unique_design_for_mix(mix, session)
            await session.commit()
            # Regenerating the SAME mix gets the same motif hook back (its own
            # old claim was released), not a suffixed variant.
            second = await thumbnail_design.unique_design_for_mix(mix, session)
            await session.commit()
            assert first["hook"] == second["hook"] == "DNB\nOVERDRIVE"
            hooks = (
                (await session.execute(select(UsedCreative).where(UsedCreative.kind == "hook")))
                .scalars().all()
            )
            assert len(hooks) == 1  # no duplicate claims piling up

    async def test_regen_thumbnails_passes_unique_hook_and_scene(
        self, prepared_db, monkeypatch, tmp_path
    ):
        import app.services.catalog_thumbnails as ctn
        from app.services.catalog_thumbnails import run_regen_thumbnails

        captured = []

        class CapturingArtGenerator:
            async def generate_youtube_thumbnail(self, **kwargs):
                captured.append(kwargs)
                return kwargs["output_path"]

            async def generate_cover_art(self, **kwargs):
                captured.append(kwargs)
                return kwargs["output_path"]

        monkeypatch.setattr(ctn, "get_art_generator", lambda sj: CapturingArtGenerator())
        monkeypatch.setattr(ctn.settings, "OUTPUT_THUMBNAILS_PATH", str(tmp_path / "t"))
        monkeypatch.setattr(ctn.settings, "OUTPUT_COVER_ART_PATH", str(tmp_path / "c"))

        m1 = await _add_mix(
            title="Roller Season", genres=["drum and bass"], youtube_video_id="v1"
        )
        m2 = await _add_mix(
            title="Midnight Mirage", genres=["drum and bass"], youtube_video_id="v2"
        )
        summary = await run_regen_thumbnails([m1, m2])
        assert summary["generated_youtube"] == 2

        by_mix = {k["mix_id"]: k for k in captured}
        h1 = by_mix[m1]["hook_text"]
        h2 = by_mix[m2]["hook_text"]
        assert h1 and h2 and h1 != h2  # never two identical hooks
        assert by_mix[m1]["scene_text"] != by_mix[m2]["scene_text"]

    async def test_regen_force_unique_false_keeps_legacy_motif(
        self, prepared_db, monkeypatch, tmp_path
    ):
        import app.services.catalog_thumbnails as ctn
        from app.services.catalog_thumbnails import run_regen_thumbnails

        captured = []

        class CapturingArtGenerator:
            async def generate_youtube_thumbnail(self, **kwargs):
                captured.append(kwargs)
                return kwargs["output_path"]

            async def generate_cover_art(self, **kwargs):
                captured.append(kwargs)
                return kwargs["output_path"]

        monkeypatch.setattr(ctn, "get_art_generator", lambda sj: CapturingArtGenerator())
        monkeypatch.setattr(ctn.settings, "OUTPUT_THUMBNAILS_PATH", str(tmp_path / "t"))
        monkeypatch.setattr(ctn.settings, "OUTPUT_COVER_ART_PATH", str(tmp_path / "c"))

        mid = await _add_mix(title="Old Way", genres=["drum and bass"], youtube_video_id="v1")
        await run_regen_thumbnails([mid], force_unique=False)
        assert captured[0]["hook_text"] is None  # motif default inside ArtGenerator
        assert captured[0]["scene_text"] is None
        async with await _session() as session:
            rows = (await session.execute(select(UsedCreative))).scalars().all()
            assert rows == []  # nothing claimed in legacy mode


# ---------------------------------------------------------------------------
# Pipeline creative-title uniqueness
# ---------------------------------------------------------------------------


class TestCreativeTitleUniqueness:
    def _generator(self, monkeypatch, texts):
        from app.services.description_generator import DescriptionGenerator

        gen = DescriptionGenerator({"openai_api_key": "test-key"})
        seq = SeqGenerator(texts)
        monkeypatch.setattr(gen, "_create_completion", seq._create_completion)

        async def _noop_usage(session, mix_id, op, in_tok, out_tok):
            pass

        monkeypatch.setattr(gen, "_track_usage", _noop_usage)
        return gen, seq

    async def test_retries_taken_title_then_claims_winner(
        self, prepared_db, monkeypatch
    ):
        mid = await _add_mix(title="Untitled")
        gen, seq = self._generator(
            monkeypatch, ["Desert Frequencies After Dark", "Freight Train Techno"]
        )
        async with await _session() as session:
            await claim(session, KIND_TITLE, "Desert Frequencies After Dark")
            await session.commit()
            title = await gen.generate_creative_title(
                genres=["techno"], vibes=["dark"], session=session, mix_id=mid,
            )
            await session.commit()
            assert title == "Freight Train Techno"
            assert len(seq.calls) == 2
            assert "- Desert Frequencies After Dark" in seq.calls[0]  # used titles fed
            assert "WAS REJECTED" in seq.calls[1]
            assert await is_taken(session, KIND_TITLE, "Freight Train Techno")

    async def test_stubborn_collision_gets_deterministic_suffix(
        self, prepared_db, monkeypatch
    ):
        mid = await _add_mix(title="Untitled")
        gen, _ = self._generator(monkeypatch, ["Same Old Title"] * 3)
        async with await _session() as session:
            await claim(session, KIND_TITLE, "Same Old Title")
            await session.commit()
            title = await gen.generate_creative_title(
                genres=["house"], vibes=["warm"], session=session, mix_id=mid,
            )
            await session.commit()
            # The numeral lands on the hook, ahead of the genre keyword, so
            # the disambiguated title still reads as a title and stays
            # searchable.
            assert title == "Same Old Title II | House Mix"
            assert await is_taken(
                session, KIND_TITLE, "Same Old Title II | House Mix", fuzzy=False
            )

    async def test_no_session_keeps_legacy_single_shot(self, monkeypatch):
        gen, seq = self._generator(monkeypatch, ["Any Title At All"])
        title = await gen.generate_creative_title(genres=["house"], vibes=["warm"])
        # Genre enforcement is unconditional — it does not depend on a session.
        assert title == "Any Title At All | House Mix"
        assert len(seq.calls) == 1


# ---------------------------------------------------------------------------
# Improve-flow registry enforcement
# ---------------------------------------------------------------------------


class TestImproveRegistryGuard:
    async def test_guard_rejects_registry_taken_title(self, prepared_db):
        from app.services.catalog_improve import draft_with_diversity_guard

        titles = iter(["Registered Already Banger", "Something Brand New"])

        class Seq:
            calls = []

            async def _create_completion(self, prompt, max_tokens, temperature):
                Seq.calls.append(prompt)
                return FakeResponse(), json.dumps(
                    {"title": next(titles), "description": "Body."}
                )

            async def _track_usage(self, *a):
                pass

        mid = await _add_mix(title="Raid Train 44")
        async with await _session() as session:
            await claim(session, KIND_TITLE, "Registered Already Banger")
            await session.commit()
            mix = await session.get(Mix, mid)
            draft = await draft_with_diversity_guard(mix, Seq(), session, [])
            # The genre tail is appended by the shared shape policy, and
            # normalize() strips it again for KIND_TITLE — so the claimed
            # "Registered Already Banger" still blocks its shaped form.
            assert draft["title"] == "Something Brand New | Open Format Mix"
            assert "WAS REJECTED" in Seq.calls[1]


# ---------------------------------------------------------------------------
# Stats endpoint
# ---------------------------------------------------------------------------


class TestUniquenessStatsEndpoint:
    async def test_counts_and_recent(self, client):
        async with await _session() as session:
            await claim(session, KIND_TITLE, "Freight Train Techno")
            await claim(session, KIND_HOOK, "ROLLERS AT DUSK", mix_id=None)
            await claim(session, KIND_SCENE, "dnb | dusk | haze")
            await session.commit()

        resp = await client.get("/api/catalog/uniqueness/stats")
        assert resp.status_code == 200
        body = resp.json()
        assert body["counts"] == {"title": 1, "hook": 1, "scene": 1}
        assert body["total"] == 3
        assert len(body["recent"]) == 3
        newest = body["recent"][0]
        assert newest["kind"] == "scene"
        assert newest["value"] == "dnb | dusk | haze"
