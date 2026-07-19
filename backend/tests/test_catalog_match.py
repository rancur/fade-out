"""Exhaustive fixture-based tests for the back-catalog matcher (pure logic)."""

from datetime import datetime, timedelta, timezone

from app.services.catalog_match import (
    MatchPlan,
    build_match_plan,
    normalize_title,
    title_similarity,
)

T0 = datetime(2025, 6, 1, 12, 0, tzinfo=timezone.utc)


def yt(id="ytA", title="Desert Frequencies Vol 3", description="", dur=3600.0, published=T0):
    return {
        "platform": "youtube",
        "id": id,
        "title": title,
        "description": description,
        "url": f"https://www.youtube.com/watch?v={id}",
        "duration_seconds": dur,
        "published_at": published,
    }


def sc(id="111", title="Desert Frequencies Vol 3", description="", dur=3600.0,
       published=T0, permalink="https://soundcloud.com/thewillsee/desert-frequencies-vol-3"):
    return {
        "platform": "soundcloud",
        "id": id,
        "title": title,
        "description": description,
        "url": permalink,
        "duration_seconds": dur,
        "published_at": published,
    }


def mix(id="m1", yt_id=None, sc_id=None, yt_url=None, sc_url=None):
    return {
        "id": id,
        "youtube_video_id": yt_id,
        "soundcloud_track_id": sc_id,
        "youtube_url": yt_url,
        "soundcloud_url": sc_url,
    }


# ---------------------------------------------------------------------------
# Normalization
# ---------------------------------------------------------------------------

class TestNormalization:
    def test_lowercase_and_punctuation_stripped(self):
        assert normalize_title("Desert Frequencies, Vol. III!") == "desert frequencies vol iii"

    def test_boilerplate_removed(self):
        assert normalize_title("Official Desert DJ Set Mix") == "desert"

    def test_identical_after_boilerplate_scores_1(self):
        assert title_similarity("Sunrise Sessions (Official Mix)", "Sunrise Sessions DJ Set") == 1.0

    def test_empty_titles_score_0(self):
        assert title_similarity("", "anything") == 0.0
        assert title_similarity("Official Mix", "Official Mix") == 0.0  # all boilerplate


# ---------------------------------------------------------------------------
# Existing mixes claim first
# ---------------------------------------------------------------------------

class TestExistingClaims:
    def test_mix_with_both_ids_claims_both_items(self):
        y, s = yt(id="vidX"), sc(id="42")
        plan = build_match_plan([y], [s], [mix(yt_id="vidX", sc_id="42")])
        assert plan.existing_links == [{"mix_id": "m1", "youtube": y, "soundcloud": s}]
        assert plan.pairs == [] and plan.singles == [] and plan.ambiguous == []

    def test_claim_by_url_when_ids_missing(self):
        y = yt(id="vidY")
        s = sc(id="7", permalink="https://soundcloud.com/thewillsee/night-drive")
        plan = build_match_plan(
            [y], [s],
            [mix(yt_url="https://www.youtube.com/watch?v=vidY",
                 sc_url="https://soundcloud.com/thewillsee/night-drive")],
        )
        assert plan.existing_links[0]["youtube"] is y
        assert plan.existing_links[0]["soundcloud"] is s

    def test_claimed_items_are_not_reused_for_pairing(self):
        # Existing mix owns the YT item; the equally-titled SC item must NOT be
        # paired with the claimed YT item — it goes through the normal flow.
        y, s = yt(id="vidZ"), sc(id="9")
        plan = build_match_plan([y], [s], [mix(yt_id="vidZ")])
        assert plan.existing_links == [{"mix_id": "m1", "youtube": y, "soundcloud": None}]
        assert plan.singles == [s]

    def test_partial_claim_sc_only(self):
        s = sc(id="55")
        plan = build_match_plan([], [s], [mix(sc_id="55")])
        assert plan.existing_links == [{"mix_id": "m1", "youtube": None, "soundcloud": s}]

    def test_mix_with_no_matching_items_produces_no_link(self):
        plan = build_match_plan([yt()], [sc()], [mix(yt_id="other", sc_id="999")])
        assert plan.existing_links == []
        # the untouched items still pair up normally
        assert len(plan.pairs) == 1


# ---------------------------------------------------------------------------
# Cross-link detection
# ---------------------------------------------------------------------------

class TestCrossLink:
    def test_yt_description_contains_sc_permalink(self):
        s = sc(id="10", title="totally different name", dur=100.0,
               permalink="https://soundcloud.com/thewillsee/foggy-morning")
        y = yt(id="v1", title="unrelated", dur=9999.0,
               description="Listen on SoundCloud: https://soundcloud.com/thewillsee/foggy-morning")
        plan = build_match_plan([y], [s], [])
        assert plan.pairs == [(y, s, 1.0, "cross-link")]

    def test_sc_description_contains_watch_url(self):
        y = yt(id="v2abc", title="x", dur=1.0)
        s = sc(id="11", title="y", dur=9999.0,
               description="Video: https://www.youtube.com/watch?v=v2abc")
        plan = build_match_plan([y], [s], [])
        assert plan.pairs == [(y, s, 1.0, "cross-link")]

    def test_sc_description_contains_youtu_be_url(self):
        y = yt(id="v3short", title="x", dur=1.0)
        s = sc(id="12", title="y", dur=9999.0,
               description="watch: https://youtu.be/v3short")
        plan = build_match_plan([y], [s], [])
        assert plan.pairs[0][3] == "cross-link"

    def test_bidirectional_cross_link(self):
        s = sc(id="13", permalink="https://soundcloud.com/thewillsee/dawn")
        s["description"] = "https://youtu.be/v4both"
        y = yt(id="v4both", description="https://soundcloud.com/thewillsee/dawn")
        plan = build_match_plan([y], [s], [])
        assert plan.pairs == [(y, s, 1.0, "cross-link")]

    def test_wrong_video_id_does_not_cross_link(self):
        y = yt(id="correct1234", title="a", dur=1.0)
        s = sc(id="14", title="b", dur=9999.0,
               description="https://www.youtube.com/watch?v=different99")
        plan = build_match_plan([y], [s], [])
        assert plan.pairs == []
        assert set(map(id, plan.singles)) == {id(y), id(s)}


# ---------------------------------------------------------------------------
# Title + duration
# ---------------------------------------------------------------------------

class TestTitleDuration:
    def test_same_title_close_duration_pairs(self):
        y = yt(title="Neon Cactus After Dark", dur=3600)
        s = sc(title="Neon Cactus After Dark (Official Mix)", dur=3650)
        plan = build_match_plan([y], [s], [])
        assert len(plan.pairs) == 1
        _, _, conf, reason = plan.pairs[0]
        assert reason == "title+duration"
        assert conf >= 0.8

    def test_duration_outside_tolerance_blocks_title_match(self):
        y = yt(title="Neon Cactus After Dark", dur=3600, published=T0)
        s = sc(title="Neon Cactus After Dark", dur=3600 + 91, published=T0 + timedelta(days=30))
        plan = build_match_plan([y], [s], [])
        assert plan.pairs == []
        assert len(plan.singles) == 2

    def test_dissimilar_titles_do_not_pair(self):
        y = yt(title="Deep House Sunrise", dur=3600, published=T0)
        s = sc(title="Jungle Fever Chapter 9", dur=3600, published=T0 + timedelta(days=60))
        plan = build_match_plan([y], [s], [])
        assert plan.pairs == []

    def test_best_similarity_wins_no_double_assign(self):
        # Two YT videos with near-identical titles; only one SC track. The
        # closer title must claim it, the other goes single.
        y1 = yt(id="vA", title="Silk and Static Vol 1", dur=3600)
        y2 = yt(id="vB", title="Silk and Static Vol 2", dur=3600)
        s1 = sc(id="20", title="Silk and Static Vol 2", dur=3610)
        plan = build_match_plan([y1, y2], [s1], [])
        assert len(plan.pairs) == 1
        assert plan.pairs[0][0] is y2
        assert plan.singles == [y1]

    def test_two_pairs_assign_correctly(self):
        ya = yt(id="vA", title="Alpha Journey", dur=3600)
        yb = yt(id="vB", title="Beta Voyage", dur=5400)
        sa = sc(id="21", title="Alpha Journey", dur=3620)
        sb = sc(id="22", title="Beta Voyage", dur=5380)
        plan = build_match_plan([ya, yb], [sb, sa], [])
        assigned = {(p[0]["id"], p[1]["id"]) for p in plan.pairs}
        assert assigned == {("vA", "21"), ("vB", "22")}


# ---------------------------------------------------------------------------
# Ambiguous (LLM judge candidates)
# ---------------------------------------------------------------------------

class TestAmbiguous:
    def test_duration_and_date_close_but_titles_differ(self):
        y = yt(title="Raid Train 2024-05-01", dur=7200, published=T0)
        s = sc(title="Phantom Groove Theory", dur=7150, published=T0 + timedelta(days=3))
        plan = build_match_plan([y], [s], [])
        assert plan.pairs == []
        assert plan.ambiguous == [(y, s)]
        assert plan.singles == []

    def test_date_outside_window_is_not_ambiguous(self):
        y = yt(title="Raid Train 2024-05-01", dur=7200, published=T0)
        s = sc(title="Phantom Groove Theory", dur=7150, published=T0 + timedelta(days=15))
        plan = build_match_plan([y], [s], [])
        assert plan.ambiguous == []
        assert len(plan.singles) == 2

    def test_missing_published_date_is_not_ambiguous(self):
        y = yt(title="one", dur=7200, published=None)
        s = sc(title="two", dur=7150, published=T0)
        plan = build_match_plan([y], [s], [])
        assert plan.ambiguous == []

    def test_matcher_never_calls_llm(self):
        # The module must not import an LLM client at all.
        import app.services.catalog_match as m
        src_names = dir(m)
        assert "openai" not in src_names
        assert not hasattr(m, "DescriptionGenerator")


# ---------------------------------------------------------------------------
# Singles + full-plan integration
# ---------------------------------------------------------------------------

class TestSinglesAndIntegration:
    def test_all_leftovers_become_singles(self):
        y = yt(id="only", title="YT only", dur=100, published=T0)
        s = sc(id="30", title="SC only", dur=90000, published=T0 - timedelta(days=100))
        plan = build_match_plan([y], [s], [])
        assert plan.pairs == [] and plan.ambiguous == []
        assert plan.singles == [y, s]

    def test_empty_inputs(self):
        plan = build_match_plan([], [], [])
        assert plan == MatchPlan()

    def test_full_scenario_no_double_assignment(self):
        # existing claim + cross-link + title/duration + ambiguous + singles
        y_claimed = yt(id="claimed", title="Old Upload", dur=3600)
        y_cross = yt(id="crossv", title="whatever", dur=1.0,
                     description="https://soundcloud.com/thewillsee/crossed")
        y_title = yt(id="titlev", title="Four Decks and a Prayer", dur=5000)
        y_amb = yt(id="ambv", title="Stream 2024-01-05", dur=7000, published=T0)
        y_single = yt(id="lonev", title="Lone Video", dur=123, published=None)

        s_claimed = sc(id="900", title="Old Upload SC", dur=3600)
        s_cross = sc(id="901", title="different", dur=2.0,
                     permalink="https://soundcloud.com/thewillsee/crossed")
        s_title = sc(id="902", title="Four Decks & a Prayer", dur=5050)
        s_amb = sc(id="903", title="Cosmic Drift IV", dur=7040, published=T0 + timedelta(days=2))
        s_single = sc(id="904", title="Lone Track", dur=55555, published=None)

        plan = build_match_plan(
            [y_claimed, y_cross, y_title, y_amb, y_single],
            [s_claimed, s_cross, s_title, s_amb, s_single],
            [mix(id="mx", yt_id="claimed", sc_id="900")],
        )

        assert plan.existing_links == [
            {"mix_id": "mx", "youtube": y_claimed, "soundcloud": s_claimed}
        ]
        reasons = {p[3] for p in plan.pairs}
        assert reasons == {"cross-link", "title+duration"}
        assert (y_cross, s_cross, 1.0, "cross-link") in plan.pairs
        assert plan.ambiguous == [(y_amb, s_amb)]
        assert set(map(id, plan.singles)) == {id(y_single), id(s_single)}

        # Every input item lands in exactly one bucket.
        seen = []
        for link in plan.existing_links:
            seen += [link["youtube"], link["soundcloud"]]
        for p in plan.pairs:
            seen += [p[0], p[1]]
        for a in plan.ambiguous:
            seen += [a[0], a[1]]
        seen += plan.singles
        seen = [x for x in seen if x is not None]
        assert len(seen) == 10
        assert len({id(x) for x in seen}) == 10
