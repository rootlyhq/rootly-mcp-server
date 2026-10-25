"""The ad-hoc outbound clients are httpx2 and send what legacy httpx sent.

The hosted token probe, the OAuth authorization-server metadata proxy and the
On-Call Health client used to open legacy ``httpx`` clients. They now go
through ``transport.upstream_client``, which applies the same conventions as
the Rootly API client (certifi-first trust roots, pinned Accept-Encoding) and
keeps legacy httpx's default User-Agent. These tests pin the wire format and
that failures still surface as the exception types the callers catch.
"""

from __future__ import annotations

import sys
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import httpx2
import pytest

from rootly_mcp_server import transport
from rootly_mcp_server.och_client import OnCallHealthClient
from rootly_mcp_server.transport import AuthCaptureMiddleware, upstream_client

sys.path.insert(0, str(Path(__file__).parent))

from test_oncall_handoff import FakeMCP, FakeMCPError  # noqa: E402

from rootly_mcp_server.tools.oncall import register_oncall_tools  # noqa: E402

# Exactly what legacy httpx 0.28.1 put on a bare AsyncClient, in wire order.
LEGACY_DEFAULT_HEADERS = [
    ("Accept", "*/*"),
    ("Accept-Encoding", "gzip, deflate, br"),
    ("Connection", "keep-alive"),
    ("User-Agent", "python-httpx/0.28.1"),
]

Handler = Callable[[httpx2.Request], httpx2.Response]


@pytest.fixture
def wire(monkeypatch) -> Iterator[Callable[[Handler], list[httpx2.Request]]]:
    """Route every upstream_client through a MockTransport and record requests.

    Clients built with an explicit transport (e.g. an ASGI test client) are
    left alone, so the app under test can still be driven over httpx2.
    """
    real = httpx2.AsyncClient
    seen: list[httpx2.Request] = []
    handler_box: dict[str, Handler] = {}

    def factory(*args: Any, **kwargs: Any) -> httpx2.AsyncClient:
        if "transport" not in kwargs:

            def record(request: httpx2.Request) -> httpx2.Response:
                seen.append(request)
                return handler_box["handler"](request)

            kwargs["transport"] = httpx2.MockTransport(record)
        return real(*args, **kwargs)

    monkeypatch.setattr(httpx2, "AsyncClient", factory)

    def install(handler: Handler) -> list[httpx2.Request]:
        handler_box["handler"] = handler
        return seen

    yield install


def _raw_headers(request: httpx2.Request) -> list[tuple[str, str]]:
    return [(k.decode(), v.decode()) for k, v in request.headers.raw]


class TestUpstreamClient:
    def test_is_httpx2(self):
        assert isinstance(upstream_client(), httpx2.AsyncClient)

    def test_default_headers_match_legacy_httpx_in_order(self):
        assert _raw_headers_of(upstream_client()) == LEGACY_DEFAULT_HEADERS

    def test_uses_the_certifi_first_ssl_context(self):
        with patch.object(
            transport, "_upstream_ssl_context", wraps=transport._upstream_ssl_context
        ) as ctx:
            upstream_client()
        ctx.assert_called_once_with()

    def test_keyword_arguments_pass_through(self):
        client = upstream_client(timeout=10)
        assert client.timeout == httpx2.Timeout(10)
        # Unchanged defaults from legacy httpx: 5s timeout, no redirects.
        bare = upstream_client()
        assert bare.timeout == httpx2.Timeout(5.0)
        assert bare.follow_redirects is False


def _raw_headers_of(client: httpx2.AsyncClient) -> list[tuple[str, str]]:
    return [(k.decode(), v.decode()) for k, v in client.headers.raw]


@pytest.mark.asyncio
class TestTokenProbe:
    async def test_sends_the_legacy_request(self, wire, monkeypatch):
        monkeypatch.setenv("ROOTLY_BASE_URL", "https://api.rootly.com")
        seen = wire(
            lambda r: httpx2.Response(
                200, json={"data": {"id": "u1", "attributes": {"email": "a@b.c", "name": "A"}}}
            )
        )
        user, rejection = await AuthCaptureMiddleware(app=None)._probe_upstream("Bearer tok")

        assert user == {"id": "u1", "email": "a@b.c", "name": "A"}
        assert rejection is None
        (sent,) = seen
        assert sent.method == "GET"
        assert str(sent.url) == "https://api.rootly.com/v1/users/me"
        assert _raw_headers(sent) == [
            ("Host", "api.rootly.com"),
            ("Accept-Encoding", "gzip, deflate, br"),
            ("Connection", "keep-alive"),
            ("User-Agent", "python-httpx/0.28.1"),
            ("Authorization", "Bearer tok"),
            ("Accept", "application/vnd.api+json"),
        ]
        assert sent.extensions["timeout"] == httpx2.Timeout(10.0).as_dict()

    async def test_rejected_token_returns_none(self, wire):
        wire(lambda r: httpx2.Response(401, json={"errors": []}))
        user, rejection = await AuthCaptureMiddleware(app=None)._probe_upstream("Bearer bad")
        assert user is None
        assert rejection == {"reason": "upstream_rejected", "upstream_status": 401}

    async def test_transport_failure_returns_none(self, wire):
        def boom(request: httpx2.Request) -> httpx2.Response:
            raise httpx2.ConnectError("refused", request=request)

        wire(boom)
        user, rejection = await AuthCaptureMiddleware(app=None)._probe_upstream("Bearer tok")
        assert user is None
        assert rejection is not None and rejection["reason"] == "probe_exception"


@pytest.mark.asyncio
class TestOAuthMetadataProxy:
    @staticmethod
    def _app():
        from rootly_mcp_server.server import create_rootly_mcp_server

        spec = {"openapi": "3.0.0", "info": {"title": "t", "version": "1"}, "paths": {}}
        with patch("rootly_mcp_server.server._load_swagger_spec", return_value=spec):
            return create_rootly_mcp_server(
                base_url="https://api.rootly.com", hosted=True
            ).http_app()

    @staticmethod
    async def _get(app, path: str) -> httpx2.Response:
        async with httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app=app), base_url="http://mcp.test"
        ) as client:
            return await client.get(path)

    async def test_proxies_with_the_legacy_request(self, wire):
        seen = wire(lambda r: httpx2.Response(200, json={"issuer": "https://rootly.com"}))
        resp = await self._get(self._app(), "/.well-known/oauth-authorization-server")

        assert resp.status_code == 200
        assert resp.json() == {"issuer": "https://rootly.com"}
        (sent,) = seen
        assert str(sent.url) == "https://rootly.com/.well-known/oauth-authorization-server"
        assert _raw_headers(sent) == [("Host", "rootly.com"), *LEGACY_DEFAULT_HEADERS]
        assert sent.extensions["timeout"] == httpx2.Timeout(10).as_dict()

    async def test_upstream_error_is_a_502(self, wire):
        # raise_for_status() must raise an exception the handler's catch sees.
        wire(lambda r: httpx2.Response(500, json={}))
        resp = await self._get(self._app(), "/.well-known/oauth-authorization-server")
        assert resp.status_code == 502
        assert resp.json() == {"error": "authorization_server_metadata_unavailable"}


ANALYSIS = {
    "id": 7,
    "analysis_data": {
        "team_analysis": {
            "members": [
                {"user_name": "A", "rootly_user_id": 1, "och_score": 80, "burnout_score": 9}
            ]
        }
    },
}


@pytest.mark.asyncio
class TestOnCallHealthClient:
    async def test_get_analysis_sends_the_legacy_request(self, wire):
        seen = wire(lambda r: httpx2.Response(200, json=ANALYSIS))
        client = OnCallHealthClient(api_key="och-key", base_url="https://och.test")

        assert await client.get_analysis(42) == ANALYSIS
        (sent,) = seen
        assert str(sent.url) == "https://och.test/analyses/42"
        assert _raw_headers(sent) == [
            ("Host", "och.test"),
            *LEGACY_DEFAULT_HEADERS,
            ("X-API-Key", "och-key"),
        ]
        assert sent.extensions["timeout"] == httpx2.Timeout(30.0).as_dict()

    async def test_get_latest_analysis_sends_the_legacy_request(self, wire):
        seen = wire(lambda r: httpx2.Response(200, json={"analyses": [ANALYSIS]}))
        client = OnCallHealthClient(api_key="och-key", base_url="https://och.test")

        assert await client.get_latest_analysis() == ANALYSIS
        (sent,) = seen
        assert str(sent.url) == "https://och.test/analyses?limit=1"
        assert sent.headers["x-api-key"] == "och-key"
        assert sent.extensions["timeout"] == httpx2.Timeout(30.0).as_dict()

    async def test_http_errors_are_httpx2_status_errors(self, wire):
        wire(lambda r: httpx2.Response(404, json={"detail": "nope"}))
        client = OnCallHealthClient(api_key="k", base_url="https://och.test")
        with pytest.raises(httpx2.HTTPStatusError):
            await client.get_analysis(1)

    @pytest.mark.parametrize("status", [404, 503])
    async def test_tool_reports_an_upstream_status_as_a_connection_error(
        self, wire, monkeypatch, status
    ):
        # check_oncall_health_risk catches the client's HTTPStatusError and
        # re-raises it as ConnectionError; a stale legacy-httpx catch would let
        # the httpx2 exception through as a generic failure instead.
        monkeypatch.setenv("ONCALLHEALTH_API_KEY", "k")
        monkeypatch.setenv("ONCALLHEALTH_API_URL", "https://och.test")
        wire(lambda r: httpx2.Response(status, json={}))
        mcp = FakeMCP()
        register_oncall_tools(
            mcp=mcp, make_authenticated_request=AsyncMock(), mcp_error=FakeMCPError()
        )

        result = await mcp.tools["check_oncall_health_risk"](
            start_date="2026-02-09", end_date="2026-02-15", och_analysis_id=1
        )

        assert result["error_type"] == "connection_error"
        assert "Failed to fetch On-Call Health data" in result["message"]
        assert str(status) in result["message"]
