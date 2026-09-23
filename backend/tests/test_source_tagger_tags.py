"""Tag construction is pure, so it is tested without touching a disk."""
from datetime import datetime
from types import SimpleNamespace

from app.services.source_tagger import build_tags


def _mix(**kw):
    base = dict(
        id="m1",
        title="Lunar Desert Groove | Trance Mix",
        audio_file_path="/watch/audio/2026-09-18 16-50-12.flac",
        video_file_path=None,
        genres=["Trance", "Progressive"],
        tracklist=[
            {"title": "One", "artist": "A", "timestamp_seconds": 0.0, "timestamp_formatted": "0:00"},
            {"title": "Two", "artist": "B", "timestamp_seconds": 330.0, "timestamp_formatted": "5:30"},
        ],
        cover_art_path="/output/cover-art/m1.jpg",
        soundcloud_url="https://soundcloud.com/thewillsee/lunar-desert-groove-trance-mix",
        youtube_url="https://www.youtube.com/watch?v=28j2P14Qp_g",
        created_at=datetime(2026, 9, 18, 16, 50, 12),
        source="watch",
        pipeline_status="completed",
    )
    base.update(kw)
    return SimpleNamespace(**base)


def test_artist_and_album_are_fixed():
    tags = build_tags(_mix())
    assert tags["ARTIST"] == "Will See"
    assert tags["ALBUM"] == "Will See Mixes"


def test_title_comes_from_the_mix():
    assert build_tags(_mix())["TITLE"] == "Lunar Desert Groove | Trance Mix"


def test_date_prefers_the_filename_token():
    assert build_tags(_mix())["DATE"] == "2026-09-18"


def test_genres_are_joined():
    assert build_tags(_mix())["GENRE"] == "Trance; Progressive"


def test_tracklist_becomes_a_readable_description():
    desc = build_tags(_mix())["DESCRIPTION"]
    assert "0:00 A - One" in desc
    assert "5:30 B - Two" in desc


def test_url_prefers_soundcloud_then_youtube():
    assert build_tags(_mix())["URL"].startswith("https://soundcloud.com/")
    assert build_tags(_mix(soundcloud_url=None))["URL"].startswith("https://www.youtube.com/")


def test_missing_optional_fields_are_omitted_not_blank():
    tags = build_tags(_mix(genres=None, tracklist=None, soundcloud_url=None, youtube_url=None))
    assert "GENRE" not in tags
    assert "DESCRIPTION" not in tags
    assert "URL" not in tags
    assert tags["ARTIST"] == "Will See"


def test_empty_genres_are_omitted_not_blank():
    """Genres list with only empty strings must not emit a GENRE tag."""
    tags = build_tags(_mix(genres=["", None]))
    assert "GENRE" not in tags


def test_tracklist_fallback_to_timestamp_seconds():
    """Entry with only timestamp_seconds (no timestamp_formatted) still renders timestamp."""
    tags = build_tags(_mix(
        tracklist=[
            {"title": "Three", "artist": "C", "timestamp_seconds": 125.5},
        ]
    ))
    desc = tags["DESCRIPTION"]
    # format_timestamp(125.5) -> int(125) = 125 -> 2:05
    assert "2:05 C - Three" in desc


def test_tracklist_no_timestamp_renders_without_leading_space():
    """Entry with no timestamp data renders artist and title without leading space."""
    tags = build_tags(_mix(
        tracklist=[
            {"title": "Four", "artist": "D"},
        ]
    ))
    desc = tags["DESCRIPTION"]
    assert desc == "D - Four"
