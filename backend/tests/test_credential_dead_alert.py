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


class TestConsecutiveDeadThreshold:
    """Fix 2: a single correctly-classified DEAD probe can still be a
    one-off. The notification must wait for DEAD_NOTIFY_THRESHOLD (3)
    consecutive DEAD probes -- /api/health flips to dead immediately
    regardless (that update happens in refresh_all, before
    _track_dead_streak is ever called), only the PAGE waits.
    """

    @pytest.mark.asyncio
    async def test_single_dead_probe_does_not_notify(self, spy):
        svc = PlatformHealth()
        await svc._track_dead_streak("soundcloud", _state(state=OK), _state(state=DEAD))

        assert spy.calls == []

    @pytest.mark.asyncio
    async def test_three_consecutive_dead_probes_notify_exactly_once(self, spy):
        svc = PlatformHealth()
        previous = _state(state=OK)
        for _ in range(3):
            current = _state(state=DEAD)
            await svc._track_dead_streak("soundcloud", previous, current)
            previous = current

        assert len(spy.calls) == 1, "the 3rd consecutive DEAD probe must notify"

        # Further consecutive DEAD probes (~8 more hours) must not re-page.
        for _ in range(50):
            current = _state(state=DEAD)
            await svc._track_dead_streak("soundcloud", previous, current)
            previous = current
        assert len(spy.calls) == 1, "must still page on transition-confirmed only"

    @pytest.mark.asyncio
    async def test_recovery_before_threshold_resets_the_counter(self, spy):
        svc = PlatformHealth()
        # Two DEAD probes -- one short of the threshold.
        await svc._track_dead_streak("soundcloud", _state(state=OK), _state(state=DEAD))
        await svc._track_dead_streak("soundcloud", _state(state=DEAD), _state(state=DEAD))
        assert spy.calls == [], "must not notify before the streak is confirmed"

        # A recovery resets the streak.
        await svc._track_dead_streak("soundcloud", _state(state=DEAD), _state(state=OK))

        # A fresh streak of only 2 more DEAD probes must still stay silent --
        # the counter was reset, not merely paused.
        await svc._track_dead_streak("soundcloud", _state(state=OK), _state(state=DEAD))
        await svc._track_dead_streak("soundcloud", _state(state=DEAD), _state(state=DEAD))
        assert spy.calls == [], "recovery must reset the counter, not pause it"

        # The 3rd consecutive DEAD probe of the NEW streak finally notifies.
        await svc._track_dead_streak("soundcloud", _state(state=DEAD), _state(state=DEAD))
        assert len(spy.calls) == 1

    @pytest.mark.asyncio
    async def test_transient_unknown_never_accumulates_toward_a_page(self, spy):
        """Fix 1 + Fix 2 together: a run of transient (UNKNOWN) probes must
        never notify, no matter how long it runs."""
        svc = PlatformHealth()
        previous = _state(state=OK)
        for _ in range(5):
            current = _state(
                state=UNKNOWN, detail="credential check was inconclusive: 504"
            )
            await svc._track_dead_streak("soundcloud", previous, current)
            previous = current

        assert spy.calls == []
