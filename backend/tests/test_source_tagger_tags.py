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
            {"title": "One", "artist": "A", "timestamp": "00:00"},
            {"title": "Two", "artist": "B", "timestamp": "05:30"},
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
    assert "00:00 A - One" in desc
    assert "05:30 B - Two" in desc


def test_url_prefers_soundcloud_then_youtube():
    assert build_tags(_mix())["URL"].startswith("https://soundcloud.com/")
    assert build_tags(_mix(soundcloud_url=None))["URL"].startswith("https://www.youtube.com/")


def test_missing_optional_fields_are_omitted_not_blank():
    tags = build_tags(_mix(genres=None, tracklist=None, soundcloud_url=None, youtube_url=None))
    assert "GENRE" not in tags
    assert "DESCRIPTION" not in tags
    assert "URL" not in tags
    assert tags["ARTIST"] == "Will See"
