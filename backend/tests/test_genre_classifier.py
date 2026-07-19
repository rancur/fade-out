"""Tests for the per-segment genre classifier (fixes global-mean mislabel).

The old classifier scored genres off a single ``mean(bpms)``. librosa reports
drum-and-bass (~174 BPM) at half-tempo (~87), and averaging a multi-genre set
landed that mean inside house's 118-132 band -- so a DnB set was labelled
"house". These tests pin the per-segment + tempo-folding behaviour.
"""

from app.services.genre_utils import (
    classify_genres_from_segments,
    fold_tempo,
)


class TestFoldTempo:
    def test_dnb_half_tempo_folds_up(self):
        # librosa half-tempo of a 174 BPM DnB track.
        assert 160 <= fold_tempo(87.0) <= 180

    def test_double_tempo_folds_down(self):
        # A 124 BPM house track mistakenly doubled to 248.
        assert 118 <= fold_tempo(248.0) <= 132

    def test_in_window_unchanged(self):
        assert fold_tempo(124.0) == 124.0

    def test_zero_is_safe(self):
        assert fold_tempo(0.0) == 0.0


class TestClassifyGenresFromSegments:
    def test_empty_defaults_to_electronic(self):
        assert classify_genres_from_segments([], [], []) == ["electronic"]

    def test_half_tempo_dnb_is_primary_not_house(self):
        # Every segment is a bright, high-centroid DnB track detected at
        # half-tempo. The old mean-based path called this "house".
        bpms = [87.0, 88.0, 86.5, 87.5, 88.5]
        centroids = [3200.0, 3300.0, 3100.0, 3400.0, 3250.0]
        genres = classify_genres_from_segments(bpms, centroids, [])
        assert genres[0] == "drum and bass"
        assert "house" != genres[0]

    def test_true_house_still_classifies_as_house(self):
        bpms = [124.0, 126.0, 125.0, 123.0]
        centroids = [2500.0, 2600.0, 2400.0, 2550.0]
        genres = classify_genres_from_segments(bpms, centroids, [])
        assert genres[0] == "house"

    def test_prevalence_orders_primary_first(self):
        # 4 DnB segments, 1 house segment -> DnB leads.
        bpms = [174.0, 173.0, 175.0, 176.0, 124.0]
        centroids = [3200.0, 3300.0, 3100.0, 3400.0, 2500.0]
        genres = classify_genres_from_segments(bpms, centroids, [])
        assert genres[0] == "drum and bass"
        assert "house" in genres

    def test_keyword_takes_precedence_over_spectral(self):
        # Spectral says house, but an identified track literally says "dubstep".
        bpms = [124.0, 125.0, 126.0]
        centroids = [2500.0, 2600.0, 2400.0]
        genres = classify_genres_from_segments(
            bpms, centroids, ["Skrillex - Dubstep Anthem"]
        )
        assert genres[0] == "dubstep"

    def test_dnb_keyword_beats_house_spectral(self):
        bpms = [124.0, 125.0]
        centroids = [2500.0, 2600.0]
        genres = classify_genres_from_segments(
            bpms, centroids, ["Sub Focus - Liquid DnB Roller"]
        )
        assert genres[0] == "drum and bass"
