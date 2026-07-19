"""Will-approved thumbnail design system: motifs, prompts, and the overlay."""

import os

import pytest
from PIL import Image

from app.services import thumbnail_design as td
from app.services.thumbnail_design import (
    GENRE_MOTIFS,
    build_scene_prompt,
    compose_thumbnail,
    resolve_genre_key,
)

EXPECTED_KEYS = {
    "drum and bass", "dubstep", "trap", "house", "deep house", "tech house",
    "trance", "techno", "edm", "garage", "organic", "open format",
}

BRAND_HEXES = ["#D9A05B", "#B4552D", "#7A9E7E", "#F2E3C6", "#3E2C23", "#E8B4A0"]

APPROVED_ACCENTS = {
    (217, 160, 91), (180, 85, 45), (122, 158, 126), (232, 180, 160),
}


class TestGenreMotifs:
    def test_all_genre_buckets_present(self):
        assert set(GENRE_MOTIFS) == EXPECTED_KEYS

    def test_every_motif_is_complete(self):
        for key, motif in GENRE_MOTIFS.items():
            assert isinstance(motif["scene"], str) and len(motif["scene"]) > 40, key
            accent = motif["accent"]
            assert isinstance(accent, tuple) and len(accent) == 3, key
            assert accent in APPROVED_ACCENTS, key
            assert isinstance(motif["hook"], str) and motif["hook"], key

    def test_hooks_are_max_three_words_and_unique(self):
        hooks = [str(m["hook"]) for m in GENRE_MOTIFS.values()]
        for hook in hooks:
            assert len(hook.replace("\n", " ").split()) <= 3, hook
            assert hook == hook.upper(), hook  # pixel-font hooks are all-caps
        assert len(set(hooks)) == len(hooks)  # distinct per genre

    def test_scenes_are_unique_and_carry_the_eye_motif(self):
        scenes = [str(m["scene"]) for m in GENRE_MOTIFS.values()]
        assert len(set(scenes)) == len(scenes)
        for scene in scenes:
            assert "eye" in scene.lower(), scene

    def test_approved_prototypes_ported_verbatim(self):
        assert GENRE_MOTIFS["drum and bass"]["hook"] == "DNB\nOVERDRIVE"
        assert "cybernetic desert coyote" in GENRE_MOTIFS["drum and bass"]["scene"]
        assert GENRE_MOTIFS["dubstep"]["hook"] == "BASS\nRITUAL"
        assert "sandstone totem skull" in GENRE_MOTIFS["dubstep"]["scene"]
        assert GENRE_MOTIFS["house"]["hook"] == "HOUSE\nTHERAPY"
        assert "sunflower" in GENRE_MOTIFS["house"]["scene"]
        assert GENRE_MOTIFS["trance"]["hook"] == "3AM\nTRANCE"
        assert "planet-eye" in GENRE_MOTIFS["trance"]["scene"]
        assert GENRE_MOTIFS["edm"]["hook"] == "FESTIVAL\nFUEL"
        assert "phoenix" in GENRE_MOTIFS["edm"]["scene"]


class TestResolveGenreKey:
    @pytest.mark.parametrize(
        ("genres", "expected"),
        [
            (["drum and bass"], "drum and bass"),
            (["dnb"], "drum and bass"),
            (["jungle", "house"], "drum and bass"),
            (["dubstep"], "dubstep"),
            (["riddim"], "dubstep"),
            (["trap"], "trap"),
            (["deep house"], "deep house"),
            (["tech house"], "tech house"),
            (["house"], "house"),
            (["trance"], "trance"),
            (["techno"], "techno"),
            (["edm"], "edm"),
            (["big room"], "edm"),
            (["garage"], "garage"),
            (["breakbeat"], "garage"),
            (["ambient"], "organic"),
            (["melodic techno"], "organic"),  # "melodic" alias checked first
        ],
    )
    def test_alias_mapping(self, genres, expected):
        assert resolve_genre_key(genres) == expected

    def test_exact_key_wins_over_alias_order(self):
        # A later mix genre that IS a motif key beats alias substrings of the
        # first genre.
        assert resolve_genre_key(["unknown style", "techno"]) == "techno"

    def test_fallbacks(self):
        assert resolve_genre_key([]) == "open format"
        assert resolve_genre_key(None) == "open format"
        assert resolve_genre_key(["electronic"]) == "open format"

    def test_first_matching_genre_wins(self):
        assert resolve_genre_key(["house", "techno"]) == "house"


class TestBuildScenePrompt:
    def test_contains_palette_scene_and_no_text_clause(self):
        prompt = build_scene_prompt("drum and bass")
        for hexcode in BRAND_HEXES:
            assert hexcode in prompt
        assert GENRE_MOTIFS["drum and bass"]["scene"] in prompt
        assert "Absolutely no text, letters or numbers anywhere." in prompt
        assert "right two-thirds" in prompt
        assert "left third darker" in prompt

    def test_square_variant_reserves_the_upper_third(self):
        prompt = build_scene_prompt("house", aspect="square")
        assert "lower two-thirds" in prompt
        assert "upper third darker" in prompt

    def test_unknown_key_falls_back_to_open_format(self):
        prompt = build_scene_prompt("polka")
        assert GENRE_MOTIFS["open format"]["scene"] in prompt

    def test_brand_settings_can_override_a_scene(self):
        class Brand:
            genre_visual_modifiers = {"techno": "a custom techno scene"}

        prompt = build_scene_prompt("techno", Brand())
        assert "a custom techno scene" in prompt
        assert GENRE_MOTIFS["techno"]["scene"] not in prompt
        # other keys unaffected
        assert GENRE_MOTIFS["house"]["scene"] in build_scene_prompt("house", Brand())


class TestComposeThumbnail:
    def test_bundled_font_resolves(self):
        path = td._font_path()
        assert path is not None and path.endswith("PressStart2P-Regular.ttf")
        assert os.path.exists(path)

    def _art(self, tmp_path, size=(640, 360)):
        art = str(tmp_path / "scene.jpg")
        Image.new("RGB", size, (150, 90, 50)).save(art)
        return art

    def test_wide_overlay_writes_target_size(self, tmp_path):
        art = self._art(tmp_path)
        out = str(tmp_path / "wide.jpg")
        result = compose_thumbnail(art, "DNB\nOVERDRIVE", (180, 85, 45), out)
        assert result == out
        img = Image.open(out)
        assert img.size == (1280, 720)

    def test_wide_overlay_darkens_left_scrim_side(self, tmp_path):
        art = self._art(tmp_path)
        out = str(tmp_path / "wide.jpg")
        compose_thumbnail(art, "HOOK", (217, 160, 91), out)
        img = Image.open(out).convert("RGB")
        # Sample a scrim-side pixel vs an untouched right-side pixel.
        left = img.getpixel((30, 600))
        right = img.getpixel((1250, 600))
        assert sum(left) < sum(right)

    def test_square_overlay_writes_target_size_and_top_scrim(self, tmp_path):
        art = self._art(tmp_path, size=(1400, 1400))
        out = str(tmp_path / "sq.jpg")
        compose_thumbnail(art, "DEEP\nDIVE", (122, 158, 126), out, size=(1400, 1400))
        img = Image.open(out).convert("RGB")
        assert img.size == (1400, 1400)
        top = img.getpixel((700, 30))
        bottom = img.getpixel((700, 900))
        assert sum(top) < sum(bottom)

    def test_can_compose_in_place(self, tmp_path):
        art = self._art(tmp_path)
        compose_thumbnail(art, "IN\nPLACE", (180, 85, 45), art)
        assert Image.open(art).size == (1280, 720)

    def test_long_hook_still_fits(self, tmp_path):
        art = self._art(tmp_path)
        out = str(tmp_path / "long.jpg")
        compose_thumbnail(art, "MELODIC\nPROGRESSIVE", (122, 158, 126), out)
        assert Image.open(out).size == (1280, 720)
