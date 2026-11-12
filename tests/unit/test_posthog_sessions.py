"""Tests for the caller session key PostHog MCP analytics groups events by."""

from datetime import UTC, datetime

import pytest

from rootly_mcp_server import posthog_sessions
from rootly_mcp_server.posthog_sessions import current_session_key, derive_session_key

_HOUR = datetime(2026, 10, 5, 19, 2, tzinfo=UTC)


def test_session_key_prefers_the_transport_session_within_the_caller():
    key = derive_session_key(
        {"mcp-session-id": " abc123 "}, {"id": "user-1"}, "Bearer tok", now=_HOUR
    )
    assert key == "user:user-1:transport:abc123"


def test_session_key_never_crosses_callers_on_a_shared_transport_session():
    headers = {"mcp-session-id": "abc123"}
    first = derive_session_key(headers, {"id": "user-1"}, "Bearer a", now=_HOUR)
    second = derive_session_key(headers, {"id": "user-2"}, "Bearer b", now=_HOUR)
    anonymous = derive_session_key(headers, None, "Bearer c", now=_HOUR)
    assert len({first, second, anonymous}) == 3


def test_session_key_buckets_the_authenticated_user_by_hour():
    key = derive_session_key({}, {"id": "user-1"}, "Bearer tok", now=_HOUR)
    assert key == "user:user-1:hour:2026100519"
    assert derive_session_key({}, {"id": "user-1"}, "Bearer tok", now=_HOUR.replace(hour=20)) != key


def test_session_key_falls_back_to_a_token_digest_not_the_token():
    key = derive_session_key({}, None, "Bearer secret-token", now=_HOUR)
    assert key is not None
    assert key.startswith("token:")
    assert key.endswith(":hour:2026100519")
    assert "secret-token" not in key
    assert key == derive_session_key({}, None, "Bearer secret-token", now=_HOUR)
    assert key != derive_session_key({}, None, "Bearer other-token", now=_HOUR)


def test_session_key_is_none_without_any_caller_identity():
    assert derive_session_key({}, None, "", now=_HOUR) is None
    assert derive_session_key(None, {}, "  ", now=_HOUR) is None
    assert derive_session_key({"mcp-session-id": "abc"}, None, "", now=_HOUR) is None


def test_current_session_key_reads_hosted_request_context(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(posthog_sessions, "get_hosted_authenticated_user", lambda: {"id": "user-9"})
    key = current_session_key()
    assert key is not None
    assert key.startswith("user:user-9:hour:")


def test_current_session_key_is_none_outside_hosted_requests(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(posthog_sessions, "get_hosted_authenticated_user", lambda: None)
    token = posthog_sessions._session_auth_token.set("")
    try:
        assert current_session_key() is None
    finally:
        posthog_sessions._session_auth_token.reset(token)
