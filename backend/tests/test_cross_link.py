"""Tests for the cross_link handler's local-update logic (push stays disabled).

CROSS_LINK_PUSH_ENABLED defaults to False, so these exercise the description
mutation logic without any network calls.
"""

from types import SimpleNamespace

import pytest

from app.services.handlers import handle_cross_link


class FakeSession:
    """Minimal stand-in for AsyncSession.get()."""

    def __init__(self, mix):
        self._mix = mix

    async def get(self, model, key):
        return self._mix


def _mix(**kwargs):
    base = dict(
        soundcloud_url="https://soundcloud.com/thewillsee/mix",
        youtube_url="https://www.youtube.com/watch?v=abc123",
        description_soundcloud="SC body",
        description_youtube="YT body",
    )
    base.update(kwargs)
    return SimpleNamespace(**base)


class TestHandleCrossLink:
    async def test_links_both_descriptions_locally(self):
        mix = _mix()
        out = await handle_cross_link("m1", FakeSession(mix))
        assert set(out["updated_descriptions"]) == {
            "soundcloud_description",
            "youtube_description",
        }
        assert out["pushed_platforms"] == []
        assert mix.youtube_url in mix.description_soundcloud
        assert mix.soundcloud_url in mix.description_youtube

    async def test_skips_when_one_url_missing(self):
        mix = _mix(youtube_url=None)
        out = await handle_cross_link("m1", FakeSession(mix))
        assert out["updated_descriptions"] == []
        assert out["pushed_platforms"] == []

    async def test_idempotent_no_double_link(self):
        mix = _mix()
        await handle_cross_link("m1", FakeSession(mix))
        # Second run should not append the links again.
        out = await handle_cross_link("m1", FakeSession(mix))
        assert out["updated_descriptions"] == []
        assert mix.description_youtube.count(mix.soundcloud_url) == 1
        assert mix.description_soundcloud.count(mix.youtube_url) == 1
