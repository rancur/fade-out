"""Tests for word-boundary genre keyword matching (fixes 'bass' over-matching)."""

from app.services.genre_utils import keyword_boosts, keyword_matches


class TestKeywordMatches:
    def test_whole_word_matches(self):
        assert keyword_matches("Liquid Drum and Bass Anthem", "bass") is True

    def test_substring_does_not_match(self):
        # The whole point: "bassline" must NOT credit drum & bass.
        assert keyword_matches("Deep Bassline Groove", "bass") is False
        assert keyword_matches("Embassy Nights", "bass") is False

    def test_case_insensitive(self):
        assert keyword_matches("TECHNO WAREHOUSE", "techno") is True

    def test_ampersand_keyword(self):
        assert keyword_matches("classic d&b roller", "d&b") is True

    def test_empty_inputs(self):
        assert keyword_matches("", "bass") is False
        assert keyword_matches("bass", "") is False


class TestKeywordBoosts:
    def test_bassline_no_longer_boosts_dnb(self):
        boosts = keyword_boosts("Progressive Bassline Journey")
        assert "drum and bass" not in boosts

    def test_real_dnb_track_boosts(self):
        boosts = keyword_boosts("Jungle Warfare - Drum and Bass Mix")
        assert boosts.get("drum and bass") == 1.5

    def test_house_keyword(self):
        boosts = keyword_boosts("Deep House Sessions")
        assert boosts.get("house") == 1.5

    def test_custom_boost_value(self):
        boosts = keyword_boosts("Techno Night", boost=3.0)
        assert boosts.get("techno") == 3.0
