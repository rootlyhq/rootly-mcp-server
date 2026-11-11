"""PostHog MCP events carry the real exception of a failed tool call.

FastMCP turns a tool's exception into an `isError` result before posthog.mcp
records the call, so on its own the SDK reports `$mcp_error_type` "Error" and an
`$exception_list` with no frames. These drive a real FastMCP server through the
real posthog.mcp pipeline, intercepting only `capture`.
"""

import asyncio
import logging
from typing import Any
from unittest.mock import patch

import httpx2
from fastmcp import Client, FastMCP
from posthog import Posthog

from rootly_mcp_server import __version__
from rootly_mcp_server.exceptions import GuidedToolError
from rootly_mcp_server.posthog_analytics import (
    ToolExceptionCaptureMiddleware,
    maybe_enable_posthog_mcp_analytics,
    server_build_from_env,
)

FAKE_BEARER = "abcdefghijklm" + "nopqrstuvwxyz123456"


def _server() -> FastMCP:
    server = FastMCP("test")

    @server.tool
    def ok() -> str:
        return "fine"

    @server.tool
    def boom() -> str:
        raise ValueError(f"upstream rejected Bearer {FAKE_BEARER}")

    @server.tool
    def guided() -> str:
        raise GuidedToolError("guided: pass `id`, not `name`")

    @server.tool
    def not_found() -> str:
        request = httpx2.Request("GET", "https://api.rootly.com/v1/users/1")
        response = httpx2.Response(404, request=request)
        raise httpx2.HTTPStatusError(
            "HTTP error 404: Not Found", request=request, response=response
        )

    @server.tool
    def server_error() -> str:
        request = httpx2.Request("GET", "https://api.rootly.com/v1/pulses")
        response = httpx2.Response(500, request=request)
        raise httpx2.HTTPStatusError(
            "HTTP error 500: Internal Server Error", request=request, response=response
        )

    return server


async def _capture(*tools: str) -> list[tuple[str, dict[str, Any]]]:
    server = _server()
    client = Posthog("phc_test", send=False)
    captured: list[tuple[str, dict[str, Any]]] = []
    with patch.object(
        client,
        "capture",
        side_effect=lambda event, **kw: captured.append((event, kw["properties"])),
    ):
        maybe_enable_posthog_mcp_analytics(server, client, logging.getLogger(__name__))
        async with Client(server) as mcp_client:
            for tool in tools:
                await mcp_client.call_tool(tool, {}, raise_on_error=False)
        await asyncio.sleep(0.2)
    return captured


def _events(captured, name: str, tool: str) -> list[dict[str, Any]]:
    return [p for event, p in captured if event == name and p.get("$mcp_tool_name") == tool]


async def test_failed_call_reports_the_real_exception_with_frames():
    captured = await _capture("boom")

    (call,) = _events(captured, "$mcp_tool_call", "boom")
    assert call["$mcp_error_type"] == "ValueError"
    assert call["mcp_error_expected"] is False
    assert "$mcp_error_status" not in call

    (exception,) = _events(captured, "$exception", "boom")
    entries = exception["$exception_list"]
    assert any(entry["type"] == "ValueError" for entry in entries)
    frames = [f for e in entries for f in (e.get("stacktrace") or {}).get("frames", [])]
    assert any(frame.get("function") == "boom" for frame in frames)
    assert not any("code_variables" in frame or "vars" in frame for frame in frames)
    # The rebuilt list still goes through the credential scrubber.
    assert FAKE_BEARER not in repr(captured)


async def test_guided_error_is_marked_expected():
    captured = await _capture("guided")

    (call,) = _events(captured, "$mcp_tool_call", "guided")
    assert call["$mcp_error_type"] == "GuidedToolError"
    assert call["mcp_error_expected"] is True
    assert call["mcp_error_expected_reason"] == "guided"


async def test_upstream_status_is_recorded_and_4xx_answers_are_expected():
    captured = await _capture("not_found", "server_error")

    (not_found,) = _events(captured, "$mcp_tool_call", "not_found")
    assert not_found["$mcp_error_type"] == "HTTPStatusError"
    assert not_found["$mcp_error_status"] == "404"
    assert not_found["mcp_error_expected"] is True
    assert not_found["mcp_error_expected_reason"] == "upstream_answer"

    (server_error,) = _events(captured, "$mcp_tool_call", "server_error")
    assert server_error["$mcp_error_status"] == "500"
    assert server_error["mcp_error_expected"] is False


async def test_a_success_after_a_failure_carries_no_error_detail():
    captured = await _capture("boom", "ok")

    (call,) = _events(captured, "$mcp_tool_call", "ok")
    assert not call.get("$mcp_is_error")
    for key in ("$mcp_error_type", "$mcp_error_status", "mcp_error_expected"):
        assert key not in call


async def test_events_carry_this_servers_version():
    captured = await _capture("ok")

    assert captured
    assert {p["$mcp_server_version"] for _, p in captured} == {__version__}


def test_capture_middleware_wraps_every_other_middleware():
    server = _server()
    server.add_middleware(ToolExceptionCaptureMiddleware())  # stand-in for ours
    maybe_enable_posthog_mcp_analytics(
        server, Posthog("phc_test", send=False), logging.getLogger(__name__)
    )

    assert isinstance(server.middleware[0], ToolExceptionCaptureMiddleware)


def test_a_quoted_credential_past_the_cut_is_scrubbed_first():
    from rootly_mcp_server.posthog_analytics import _exception_list

    secret = "two words " + "x" * 3000
    try:
        raise ValueError('upstream said {"password": "' + secret + '"}')
    except ValueError as exc:
        entries = _exception_list(exc)

    (entry,) = entries
    assert len(entry["value"]) <= 2048
    assert "two words" not in entry["value"]
    assert "xxxxxxxx" not in entry["value"]


def test_server_build_comes_from_the_image():
    with patch.dict("os.environ", {"ROOTLY_MCP_BUILD": " abc123 "}, clear=True):
        assert server_build_from_env() == "abc123"
    with patch.dict("os.environ", {}, clear=True):
        assert server_build_from_env() is None


async def test_expected_failures_open_no_error_tracking_issue():
    captured = await _capture("guided", "not_found", "server_error", "boom")

    # Expected: the call is counted, but no $exception (no Error Tracking issue).
    for tool in ("guided", "not_found"):
        assert _events(captured, "$mcp_tool_call", tool)
        assert _events(captured, "$exception", tool) == []
    # Unexpected: both events, as before.
    for tool in ("server_error", "boom"):
        assert _events(captured, "$mcp_tool_call", tool)
        assert _events(captured, "$exception", tool)


def _sdk_event(name: str, **properties: Any) -> dict[str, Any]:
    return {"event": name, "properties": {"$mcp_is_error": True, **properties}}


def test_expected_is_read_from_the_event_when_the_exception_was_not_seen():
    # As after a posthog.mcp release that records the exception itself.
    from rootly_mcp_server.posthog_analytics import enrich_posthog_mcp_event

    guided = _sdk_event(
        "$exception",
        **{"$exception_list": [{"type": "ToolError"}, {"type": "GuidedToolError"}]},
    )
    assert enrich_posthog_mcp_event(guided) is None

    not_found = _sdk_event(
        "$mcp_tool_call",
        **{"$mcp_error_message": "Error calling tool 'get_user': HTTP error 404: Not Found"},
    )
    properties = enrich_posthog_mcp_event(not_found)["properties"]
    assert properties["$mcp_error_status"] == "404"
    assert properties["mcp_error_expected_reason"] == "upstream_answer"

    # A status quoted inside the body is not the response's own.
    quoted = _sdk_event(
        "$exception",
        **{
            "$exception_list": [
                {"type": "Error", "value": "HTTP error 422: x - {'d': 'HTTP error 404'}"}
            ]
        },
    )
    kept = enrich_posthog_mcp_event(quoted)
    assert kept is not None
    assert kept["properties"]["$mcp_error_status"] == "422"
    assert kept["properties"]["mcp_error_expected"] is False
