"""Tests for tag generation, formatting, and the YouTube 500-char budget."""

from app.services.tag_generator import TagGenerator


class TestGenerate:
    def setup_method(self):
        self.gen = TagGenerator()

    def test_brand_tags_first(self):
        tags = self.gen.generate(genres=["house"], vibes=["groovy"])
        assert tags[0] == "Will See"

    def test_respects_max(self):
        tags = self.gen.generate(genres=["house", "techno"], vibes=["energetic"], max_tags=10)
        assert len(tags) == 10

    def test_no_duplicates_case_insensitive(self):
        tags = self.gen.generate(genres=["house"], vibes=["groovy"])
        lowered = [t.lower() for t in tags]
        assert len(lowered) == len(set(lowered))

    def test_includes_genre_and_vibe(self):
        tags = self.gen.generate(genres=["techno"], vibes=["intense"], max_tags=30)
        joined = " ".join(t.lower() for t in tags)
        assert "techno" in joined
        assert "dark" in joined  # from intense vibe tags

    def test_artist_tags_from_tracklist(self):
        tracks = [{"artist": "Boris Brejcha", "title": "X", "timestamp_seconds": 1}]
        tags = self.gen.generate(genres=["techno"], vibes=["intense"], tracklist=tracks)
        assert any("brejcha" in t.lower() for t in tags)

    def test_unknown_artist_excluded(self):
        tracks = [{"artist": "Unknown", "title": "X", "timestamp_seconds": 1}]
        tags = self.gen.generate(genres=["techno"], vibes=["intense"], tracklist=tracks)
        assert "unknown" not in [t.lower() for t in tags]


class TestPrimaryGenreTag:
    def setup_method(self):
        self.gen = TagGenerator()

    def test_mapped(self):
        assert self.gen.get_primary_genre_tag(["drum and bass"]) == "Drum & Bass"
        assert self.gen.get_primary_genre_tag(["house"]) == "House"

    def test_default(self):
        assert self.gen.get_primary_genre_tag(["polka"]) == "Electronic"

    def test_first_mappable_wins(self):
        assert self.gen.get_primary_genre_tag(["polka", "techno"]) == "Techno"


class TestSoundCloudFormat:
    def test_quotes_multiword(self):
        out = TagGenerator().format_for_soundcloud(["house", "deep house"])
        assert out == 'house "deep house"'


class TestYouTubeBudget:
    def setup_method(self):
        self.gen = TagGenerator()

    def test_within_budget_untouched(self):
        tags = ["house", "techno", "deep house"]
        out = self.gen.format_for_youtube(tags)
        assert out == tags

    def test_trims_to_500_chars(self):
        tags = [f"tag_number_{i:03d}" for i in range(200)]
        out = self.gen.format_for_youtube(tags)
        # Aggregate cost (tag lengths + separators) must stay within 500.
        cost = sum(len(t) for t in out) + max(0, len(out) - 1)
        assert cost <= 500
        assert len(out) < len(tags)

    def test_preserves_priority_order(self):
        tags = ["first", "second", "third"]
        out = self.gen.format_for_youtube(tags)
        assert out == tags

    def test_quoted_multiword_costs_two_extra(self):
        # 249-char tag with a space -> quoted cost 251; a second same tag would be
        # 251 + 1 separator + 251 = 503 > 500, so only one fits.
        long_tag = "a " + "b" * 247  # len 249, contains a space
        out = self.gen.format_for_youtube([long_tag, long_tag + "c"])
        assert out == [long_tag]

    def test_skips_empty_tags(self):
        out = self.gen.format_for_youtube(["", "  ", "house"])
        assert out == ["house"]
