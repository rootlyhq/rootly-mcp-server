"""Rejected MCP requests are logged as structured events, and only refused tokens are cached."""

from __future__ import annotations

import json
from collections.abc import Callable
from contextlib import AbstractContextManager
from typing import Any
from unittest.mock import patch

import httpx2
import pytest

from rootly_mcp_server import auth_logging, transport
from rootly_mcp_server.auth_logging import auth_rejection

Handler = Callable[[httpx2.Request], httpx2.Response]


def _upstream(handler: Handler) -> tuple[AbstractContextManager[Any], list[httpx2.Request]]:
    seen: list[httpx2.Request] = []

    def record(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        return handler(request)

    def client(**kwargs):
        return httpx2.AsyncClient(transport=httpx2.MockTransport(record), **kwargs)

    return patch.object(transport, "upstream_client", client), seen


def _connect_error(request: httpx2.Request) -> httpx2.Response:
    raise httpx2.ConnectError("connection reset", request=request)


def _connect_error_echoing_token(request: httpx2.Request) -> httpx2.Response:
    raise httpx2.ConnectError(
        f"connection reset ({request.headers['authorization']})", request=request
    )


async def _call_middleware(headers: list[tuple[bytes, bytes]]) -> tuple[int, list[dict]]:
    async def app(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"OK"})

    sent: list[dict] = []

    async def receive():
        return {"type": "http.request"}

    async def send(message):
        sent.append(message)

    with (
        patch("rootly_mcp_server.utils._MCP_SERVER_URL", "https://mcp.example.com"),
        patch.object(auth_logging._auth_json_logger, "log") as log,
    ):
        await transport.AuthCaptureMiddleware(app)(
            {"type": "http", "path": "/mcp", "headers": headers}, receive, send
        )
    events = [json.loads(call.args[1]) for call in log.call_args_list]
    return sent[0]["status"], events


@pytest.mark.asyncio
class TestTokenValidationCache:
    async def test_refused_token_is_cached(self):
        upstream, seen = _upstream(lambda r: httpx2.Response(401, json={"errors": []}))
        middleware = transport.AuthCaptureMiddleware(app=None)
        with upstream:
            assert await middleware._validate_token_upstream("Bearer bad") is None
            assert auth_rejection.get() == {"reason": "upstream_rejected", "upstream_status": 401}
            assert await middleware._validate_token_upstream("Bearer bad") is None
            assert auth_rejection.get() == {"reason": "negative_cache"}
        assert len(seen) == 1

    @pytest.mark.parametrize(
        ("handler", "reason"),
        [
            (_connect_error, "probe_exception"),
            (lambda r: httpx2.Response(503), "upstream_error"),
        ],
    )
    async def test_failed_probe_is_not_cached(self, handler, reason):
        upstream, seen = _upstream(handler)
        middleware = transport.AuthCaptureMiddleware(app=None)
        with upstream:
            assert await middleware._validate_token_upstream("Bearer tok") is None
            assert (auth_rejection.get() or {}).get("reason") == reason
            assert await middleware._validate_token_upstream("Bearer tok") is None
        assert len(seen) == 2


@pytest.mark.asyncio
class TestAuthRejectedEvent:
    async def test_missing_header_is_not_logged(self):
        # The header-less request before OAuth is routine; it stays below INFO.
        status, events = await _call_middleware([])

        assert status == 401
        assert events == []

    async def test_malformed_header_is_logged(self):
        status, events = await _call_middleware([(b"authorization", b"Basic abc")])

        assert status == 401
        assert events == [
            {
                "event": "mcp_auth_rejected",
                "level": "info",
                "message": "MCP request rejected by auth check",
                "path": "/mcp",
                "transport": "streamable-http",
                "mcp_mode": "classic",
                "auth_header_state": "invalid_format",
                "token_fingerprint": events[0]["token_fingerprint"],
                "reason": "invalid_format",
            }
        ]

    async def test_probe_exception_is_logged_without_the_token(self):
        upstream, _ = _upstream(_connect_error_echoing_token)
        with upstream:
            status, events = await _call_middleware(
                [(b"authorization", b"Bearer rootly_secret123")]
            )

        assert status == 401
        (event,) = events
        assert event["level"] == "warning"
        assert event["reason"] == "probe_exception"
        assert event["exception_type"] == "ConnectError"
        assert event["exception_message"] == "connection reset (Bearer ***REDACTED***)"
        assert "ConnectError" in event["traceback_excerpt"]
        assert len(event["token_fingerprint"]) == 16
        assert "rootly_secret123" not in json.dumps(event)
