"""SSE removal: the old paths answer 410 Gone, and `--transport sse` is refused."""

from __future__ import annotations

import argparse
import json
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import Mock, patch

import httpx2 as httpx
import pytest
from fastmcp import FastMCP
from starlette.middleware import Middleware

from rootly_mcp_server import sse_removal
from rootly_mcp_server.__main__ import build_dual_http_app, main, normalize_transport
from rootly_mcp_server.sse_removal import (
    DEPRECATION_HEADER_VALUE,
    LINK_HEADER_VALUE,
    SSE_SUCCESSOR_URL,
    SUNSET_HEADER_VALUE,
    SSEGoneMiddleware,
)
from rootly_mcp_server.transport import AuthCaptureMiddleware

VALID_AUTH = "Bearer rootly_valid_token"
SSE_REQUESTS = [
    ("GET", "/sse"),
    ("GET", "/sse/"),
    ("POST", "/messages/?session_id=00000000000000000000000000000000"),
    ("POST", "/messages"),
]


def test_header_values_follow_the_rfcs():
    # RFC 9745 Structured Field Date for 2026-10-21T00:00:00Z.
    assert DEPRECATION_HEADER_VALUE == "@1792540800"
    # RFC 8594 HTTP-date.
    assert SUNSET_HEADER_VALUE == "Fri, 20 Nov 2026 00:00:00 GMT"
    assert LINK_HEADER_VALUE == '<https://mcp.rootly.com/mcp>; rel="successor-version"'


@pytest.fixture
def json_log():
    with patch.object(sse_removal._removal_json_logger, "warning") as warning:
        yield warning


@pytest.fixture
def dual_app():
    async def fake_validate(self, auth_header):
        if auth_header == VALID_AUTH:
            return {"id": "1", "email": "user@example.com"}
        return None

    server = FastMCP("Rootly test")

    @server.tool
    def ping() -> str:
        return "pong"

    code_mode = FastMCP("Rootly test Code Mode")
    with patch.object(AuthCaptureMiddleware, "_validate_token_upstream", fake_validate):
        yield build_dual_http_app(
            server,
            middleware=[Middleware(AuthCaptureMiddleware)],
            code_mode_server=code_mode,
            code_mode_path="/mcp-codemode",
        )


async def _request(app, method: str, path: str, **kwargs: Any) -> httpx.Response:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        return await client.request(method, path, **kwargs)


@pytest.mark.parametrize(("method", "path"), SSE_REQUESTS)
@pytest.mark.parametrize("auth", [None, VALID_AUTH])
async def test_sse_paths_answer_gone_before_auth(dual_app, json_log, method, path, auth):
    headers = {"Authorization": auth} if auth else {}
    response = await _request(dual_app, method, path, headers=headers)

    # 410 whether or not the client authenticated: a stuck client must learn
    # the endpoint is gone, not that its token is missing.
    assert response.status_code == 410
    body = response.json()
    assert body["error"] == "sse_transport_removed"
    assert body["successor"] == SSE_SUCCESSOR_URL
    assert "2026-11-20" in body["message"]
    assert "https://mcp.rootly.com/mcp" in body["message"]
    assert response.headers["sunset"] == SUNSET_HEADER_VALUE
    assert response.headers["link"] == LINK_HEADER_VALUE
    assert response.headers["cache-control"] == "no-store"
    assert "www-authenticate" not in response.headers
    json_log.assert_called_once()


async def test_gone_request_is_logged_with_its_client(dual_app, json_log):
    await _request(
        dual_app,
        "GET",
        "/sse",
        headers={"Authorization": VALID_AUTH, "User-Agent": "legacy-sse-client/0.1"},
    )

    event = json.loads(json_log.call_args.args[0])
    assert event["event"] == "sse_removed_request"
    assert event["method"] == "GET"
    assert event["path"] == "/sse"
    assert event["user_agent"] == "legacy-sse-client/0.1"
    assert event["token_fingerprint"]
    assert VALID_AUTH not in json_log.call_args.args[0]


@pytest.mark.parametrize("path", ["/mcp", "/mcp-codemode"])
async def test_streamable_http_surfaces_are_untouched(dual_app, json_log, path):
    response = await _request(dual_app, "POST", path, json={})

    assert response.status_code == 401
    assert "sunset" not in response.headers
    json_log.assert_not_called()


@pytest.mark.parametrize("path", ["/ssefoo", "/messages-archive", "/mcp/sse"])
async def test_only_the_sse_paths_are_gone(json_log, path):
    async def app(scope, receive, send):
        await send({"type": "http.response.start", "status": 204, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    response = await _request(SSEGoneMiddleware(app), "GET", path)

    assert response.status_code == 204
    json_log.assert_not_called()


async def test_custom_sse_paths_are_honored(json_log):
    async def app(scope, receive, send):  # pragma: no cover - must not be reached
        raise AssertionError("reached the app")

    middleware = SSEGoneMiddleware(app, sse_path="/legacy-sse", message_path="/legacy-msg")

    assert (await _request(middleware, "GET", "/legacy-sse")).status_code == 410
    assert (await _request(middleware, "POST", "/legacy-msg/?session_id=x")).status_code == 410


@pytest.mark.parametrize(
    ("live", "path"),
    [("/sse", "/sse"), ("/messages", "/messages/?session_id=x"), ("/messages", "/messages")],
)
async def test_a_live_mcp_path_is_never_answered_gone(json_log, live, path):
    # A self-hosted server may serve Streamable HTTP at an old SSE path
    # (FASTMCP_STREAMABLE_HTTP_PATH=/sse); that endpoint must keep working.
    async def app(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"live"})

    middleware = SSEGoneMiddleware(app, live_paths=(live, None))

    response = await _request(middleware, "POST", path)
    assert response.status_code == 200
    assert response.text == "live"
    json_log.assert_not_called()


def test_dual_app_serving_streamable_http_at_sse_keeps_it_live():
    import fastmcp

    server = FastMCP("Rootly test")
    with patch.object(fastmcp.settings, "streamable_http_path", "/sse"):
        app = build_dual_http_app(server)

    gone = next(m for m in app.user_middleware if m.cls is SSEGoneMiddleware)
    assert "/sse" in cast(tuple[str, ...], gone.kwargs["live_paths"])


def test_dual_app_no_longer_advertises_sse(dual_app):
    assert "/sse" not in dual_app.state.path
    assert dual_app.state.mcp_endpoints.streamable == "/mcp"


# --- CLI ---------------------------------------------------------------------


@pytest.mark.parametrize("value", ["sse", "SSE", " sse "])
def test_sse_transport_is_refused_with_the_replacement(value):
    with pytest.raises(argparse.ArgumentTypeError) as excinfo:
        normalize_transport(value)

    message = str(excinfo.value)
    assert "removed on 2026-11-20" in message
    assert "--transport streamable-http" in message


@pytest.mark.parametrize("value", ["both", "dual", "streamable+sse", "sse+streamable"])
def test_combined_transport_names_still_start_the_http_server(value):
    assert normalize_transport(value) == "both"


def _cli_args(transport: str) -> SimpleNamespace:
    return SimpleNamespace(
        swagger_path=None,
        log_level="ERROR",
        name="Rootly",
        transport=transport,
        debug=False,
        base_url=None,
        allowed_paths=None,
        hosted=False,
        enable_code_mode=False,
        enable_write_tools=True,
        enabled_tools=None,
        list_tools=False,
        code_mode_path=None,
        host=False,
    )


def test_self_hosted_streamable_http_answers_gone_on_sse_paths():
    main_server = SimpleNamespace(run=Mock())
    with patch.dict("os.environ", {"ROOTLY_API_TOKEN": "rootly_test"}, clear=True):
        with patch(
            "rootly_mcp_server.__main__.parse_args", return_value=_cli_args("streamable-http")
        ):
            with patch("rootly_mcp_server.__main__.setup_logging"):
                with patch("rootly_mcp_server.__main__.check_api_token"):
                    with patch(
                        "rootly_mcp_server.__main__.create_rootly_mcp_server",
                        return_value=main_server,
                    ):
                        with patch(
                            "rootly_mcp_server.__main__.get_hosted_auth_middleware",
                            return_value=None,
                        ):
                            main()

    run_kwargs = main_server.run.call_args.kwargs
    assert run_kwargs["transport"] == "streamable-http"
    [gone] = run_kwargs["middleware"]
    assert gone.cls is SSEGoneMiddleware
    assert gone.kwargs["live_paths"] == ("/mcp",)
