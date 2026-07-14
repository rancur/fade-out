"""Tests for description_generator pure helpers (no OpenAI / DB / network)."""

from types import SimpleNamespace

from app.services.description_generator import (
    _format_chapter_block,
    _response_text,
    price_for_model,
)


def _fake_response(content):
    """Build a minimal object shaped like an OpenAI chat completion."""
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=content))]
    )


class TestPriceForModel:
    def test_gpt_4o(self):
        # 1M input @ $2.50 + 1M output @ $10.00
        assert price_for_model("gpt-4o", 1_000_000, 1_000_000) == 12.50

    def test_gpt_4o_mini_cheaper(self):
        assert price_for_model("gpt-4o-mini", 1_000_000, 0) == 0.15

    def test_dated_snapshot_prefix_match(self):
        # A dated snapshot resolves to the base model's rate.
        assert price_for_model("gpt-4o-2024-08-06", 1_000_000, 1_000_000) == 12.50

    def test_mini_prefix_not_swallowed_by_base(self):
        # "gpt-4o-mini-..." must match the mini rate, not gpt-4o.
        assert price_for_model("gpt-4o-mini-2024-07-18", 1_000_000, 0) == 0.15

    def test_unknown_model_falls_back_to_gpt_4o(self):
        assert price_for_model("some-future-model", 1_000_000, 1_000_000) == 12.50

    def test_case_insensitive(self):
        assert price_for_model("GPT-4O", 1_000_000, 0) == 2.50

    def test_zero_tokens(self):
        assert price_for_model("gpt-4o", 0, 0) == 0.0


class TestResponseText:
    def test_normal_content(self):
        assert _response_text(_fake_response("  hello  ")) == "hello"

    def test_none_content(self):
        assert _response_text(_fake_response(None)) == ""

    def test_empty_content(self):
        assert _response_text(_fake_response("")) == ""

    def test_no_choices(self):
        assert _response_text(SimpleNamespace(choices=[])) == ""

    def test_malformed(self):
        assert _response_text(SimpleNamespace()) == ""


class TestFormatChapterBlock:
    def test_too_few_returns_empty(self):
        tracks = [
            {"artist": "A", "title": "T", "timestamp_seconds": 0},
            {"artist": "B", "title": "U", "timestamp_seconds": 60},
        ]
        assert _format_chapter_block(tracks) == ""

    def test_none_returns_empty(self):
        assert _format_chapter_block(None) == ""

    def test_renders_chapters_with_intro(self):
        tracks = [
            {"artist": "A", "title": "T", "timestamp_seconds": 30},
            {"artist": "B", "title": "U", "timestamp_seconds": 90},
            {"artist": "C", "title": "V", "timestamp_seconds": 150},
        ]
        block = _format_chapter_block(tracks)
        lines = block.splitlines()
        assert lines[0] == "Tracklist:"
        # Synthetic intro anchors chapters at 0:00 (required by YouTube).
        assert lines[1] == "0:00 Intro"
        assert "0:30 A - T" in block
        assert "2:30 C - V" in block

    def test_unidentified_renders_as_id_dash_id(self):
        tracks = [
            {"artist": "", "title": "", "timestamp_seconds": 0},
            {"artist": "B", "title": "U", "timestamp_seconds": 60},
            {"artist": "C", "title": "V", "timestamp_seconds": 120},
        ]
        block = _format_chapter_block(tracks)
        assert "0:00 ID - ID" in block
