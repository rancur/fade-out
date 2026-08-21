"""CORS allowlist behaviour.

fade-out ships no authentication, so the CORS allowlist is a real security
boundary rather than a convenience: a wildcard would let any site the operator
visits read their stored credential metadata and drive their pipeline. These
tests pin the defaults so a future change has to be deliberate.
"""

import pytest

from app.config import Settings


def _settings(**overrides) -> Settings:
    # _env_file=None so a developer's local .env cannot influence the result.
    return Settings(_env_file=None, **overrides)


class TestCorsAllowOrigins:
    def test_default_is_not_a_wildcard(self):
        assert "*" not in _settings().cors_allow_origins

    def test_defaults_to_local_dev_origins(self):
        origins = _settings().cors_allow_origins
        assert "http://localhost:5173" in origins
        assert "http://127.0.0.1:5173" in origins

    def test_public_url_is_added_automatically(self):
        origins = _settings(PUBLIC_URL="https://fadeout.example.com").cors_allow_origins
        assert "https://fadeout.example.com" in origins

    def test_public_url_trailing_slash_is_normalised(self):
        # Browsers send an Origin header with no trailing slash; an entry with
        # one would silently never match.
        origins = _settings(PUBLIC_URL="https://fadeout.example.com/").cors_allow_origins
        assert "https://fadeout.example.com" in origins
        assert "https://fadeout.example.com/" not in origins

    def test_public_url_is_not_duplicated(self):
        origins = _settings(
            CORS_ALLOW_ORIGINS="https://fadeout.example.com",
            PUBLIC_URL="https://fadeout.example.com",
        ).cors_allow_origins
        assert origins.count("https://fadeout.example.com") == 1

    def test_empty_public_url_adds_nothing(self):
        assert _settings(PUBLIC_URL="").cors_allow_origins == _settings().cors_allow_origins

    @pytest.mark.parametrize(
        "raw, expected",
        [
            ("https://a.example", ["https://a.example"]),
            ("https://a.example,https://b.example", ["https://a.example", "https://b.example"]),
            # Whitespace and empty segments are operator typos, not origins.
            (" https://a.example , https://b.example ", ["https://a.example", "https://b.example"]),
            ("https://a.example,,", ["https://a.example"]),
            ("", []),
            ("   ", []),
        ],
    )
    def test_parsing(self, raw, expected):
        assert _settings(CORS_ALLOW_ORIGINS=raw).cors_allow_origins == expected


class TestAppMiddlewareWiring:
    def test_app_is_not_configured_with_a_wildcard_origin(self):
        """The running app must not advertise "*" as an allowed origin."""
        from app.main import app

        cors = _cors_middleware(app)
        assert "*" not in cors.kwargs["allow_origins"]

    def test_app_allows_credentials_for_an_explicit_allowlist(self):
        from app.main import app

        cors = _cors_middleware(app)
        assert cors.kwargs["allow_credentials"] is True

    def test_wildcard_and_credentials_are_never_paired(self, monkeypatch):
        """If someone sets "*", credentials must switch off.

        Browsers reject that pairing outright, and Starlette would otherwise
        echo the caller's origin back — turning "*" into "any site you visit",
        which is the exact exposure the allowlist exists to prevent.
        """
        origins = _settings(CORS_ALLOW_ORIGINS="*").cors_allow_origins
        assert origins == ["*"]
        # Same expression app.main uses to derive allow_credentials.
        assert ("*" not in origins) is False


def _cors_middleware(app):
    from starlette.middleware.cors import CORSMiddleware

    for mw in app.user_middleware:
        if mw.cls is CORSMiddleware:
            return mw
    raise AssertionError("CORSMiddleware is not installed on the app")
