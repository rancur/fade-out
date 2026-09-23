"""A credential going dead must page exactly once, on the transition.

Regression cover for the SoundCloud outage of 2026-09-13: the grant died,
the health probe correctly saw `dead` every ten minutes for six days, and
nobody was told, because platform_health never spoke to the notification
service. The first anyone heard of it was a mix half-publishing on 09-18.
"""
import pytest

from app.services.platform_health import (
    DEAD,
    OK,
    UNKNOWN,
    CredentialState,
    PlatformHealth,
)


class _Spy:
    def __init__(self):
        self.calls = []

    async def notify(self, notification_type, mix_id=None, title="", message="", data=None):
        self.calls.append(
            {"type": notification_type, "title": title, "message": message, "data": data or {}}
        )


@pytest.fixture
def spy(monkeypatch):
    s = _Spy()
    import app.services.notification_service as ns

    monkeypatch.setattr(ns, "get_notification_service", lambda: s)
    return s


def _state(platform="soundcloud", state=DEAD, detail="refresh_token grant rejected"):
    return CredentialState(
        platform=platform, state=state, detail=detail,
        credential="SOUNDCLOUD_REFRESH_TOKEN",
    )


@pytest.mark.asyncio
async def test_pages_when_credential_first_goes_dead(spy):
    await PlatformHealth._announce_if_newly_dead(_state(state=OK), _state(state=DEAD))

    assert len(spy.calls) == 1, "a newly dead credential must page"
    call = spy.calls[0]
    assert call["type"] == "credential_dead"
    assert "soundcloud" in call["title"]
    # The cause must survive into the message, so the page is actionable
    # without going and reading the logs.
    assert "refresh_token grant rejected" in call["message"]
    assert call["data"]["credential"] == "SOUNDCLOUD_REFRESH_TOKEN"
    assert call["data"]["previous_state"] == OK


@pytest.mark.asyncio
async def test_does_not_re_page_while_it_stays_dead(spy):
    """The failing state the old code produced: dead on every probe forever."""
    await PlatformHealth._announce_if_newly_dead(_state(state=OK), _state(state=DEAD))
    for _ in range(50):  # ~8 hours of ten-minute probes
        await PlatformHealth._announce_if_newly_dead(_state(state=DEAD), _state(state=DEAD))

    assert len(spy.calls) == 1, "must page on the transition only, not every probe"


@pytest.mark.asyncio
async def test_pages_again_after_recovery_then_second_failure(spy):
    await PlatformHealth._announce_if_newly_dead(_state(state=OK), _state(state=DEAD))
    await PlatformHealth._announce_if_newly_dead(_state(state=DEAD), _state(state=OK))
    await PlatformHealth._announce_if_newly_dead(_state(state=OK), _state(state=DEAD))

    assert len(spy.calls) == 2, "a second, distinct outage must page again"


@pytest.mark.asyncio
async def test_healthy_and_unknown_states_never_page(spy):
    await PlatformHealth._announce_if_newly_dead(_state(state=DEAD), _state(state=OK))
    await PlatformHealth._announce_if_newly_dead(_state(state=OK), _state(state=UNKNOWN))
    await PlatformHealth._announce_if_newly_dead(None, _state(state=OK))

    assert spy.calls == [], "recovery and unknown must stay silent"


@pytest.mark.asyncio
async def test_alerting_failure_never_breaks_the_probe_loop(monkeypatch):
    """Probing must survive a broken notifier - health matters more than the page."""
    import app.services.notification_service as ns

    def boom():
        raise RuntimeError("notification backend down")

    monkeypatch.setattr(ns, "get_notification_service", boom)
    await PlatformHealth._announce_if_newly_dead(_state(state=OK), _state(state=DEAD))
