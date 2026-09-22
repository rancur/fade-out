"""The SoundCloud redirect URI must be pinned, and identical at both call sites.

Regression cover for 2026-09-18. SoundCloud string-matches redirect_uri against
the OAuth app's registration, so `localhost` and `127.0.0.1` are different URIs
and only the registered one works. The old code built the URI from the request
Host header at two separate call sites, so reaching the container by any other
name produced a URI that could never match -- and SoundCloud signals that by
rendering a BLANK authorize page, not an error. Hours went into browser,
session and bot-detection theories before the one-word cause surfaced.
"""
import pytest

from app.routers.auth import soundcloud_redirect_uri

REGISTERED = "http://127.0.0.1:8500/api/auth/soundcloud/callback"


class _Req:
    def __init__(self, host=None):
        self.headers = {"host": host} if host else {}


def test_configured_value_wins_over_request_host():
    uri = soundcloud_redirect_uri(
        _Req("fade-out.local:8500"), {"soundcloud_redirect_uri": REGISTERED}
    )
    assert uri == REGISTERED, "a configured URI must not be overridden by Host"


def test_explicit_override_wins_over_everything():
    uri = soundcloud_redirect_uri(
        _Req("fade-out.local:8500"),
        {"soundcloud_redirect_uri": REGISTERED},
        override="http://example.test/cb",
    )
    assert uri == "http://example.test/cb"


def test_localhost_host_is_rewritten_to_127_0_0_1():
    """THE bug: `localhost` silently produced an unusable URI."""
    uri = soundcloud_redirect_uri(_Req("localhost:8500"), {})
    assert uri == REGISTERED
    assert "localhost" not in uri


def test_missing_host_header_defaults_to_the_registered_form():
    assert soundcloud_redirect_uri(_Req(), {}) == REGISTERED


def test_both_call_sites_resolve_identically():
    """The exchange fails if it differs by one character from the auth URL's."""
    req = _Req("localhost:8500")
    sj = {}
    url_side = soundcloud_redirect_uri(req, sj, override=None)
    exchange_side = soundcloud_redirect_uri(req, sj)
    assert url_side == exchange_side


@pytest.mark.parametrize("host", ["localhost:8500", "127.0.0.1:8500", "nas:8500", None])
def test_never_emits_a_localhost_uri_regardless_of_host(host):
    uri = soundcloud_redirect_uri(_Req(host), {})
    assert "localhost" not in uri, f"Host {host!r} leaked a localhost redirect URI"
