"""PostHog's send_feedback tool: on with analytics, scrubbed, rate-limited, always listed."""

from __future__ import annotations

import asyncio
import logging
from typing import Any
from unittest.mock import patch

import pytest
from fastmcp import Client, FastMCP
from posthog import Posthog

from rootly_mcp_server import posthog_analytics, toolsets
from rootly_mcp_server.posthog_analytics import (
    FEEDBACK_EVENT,
    FEEDBACK_TOOL_DESCRIPTION,
    FeedbackRateLimiter,
    maybe_enable_posthog_mcp_analytics,
    prepare_posthog_mcp_event,
)
from rootly_mcp_server.server import create_rootly_mcp_server
from rootly_mcp_server.toolsets import CORE, ToolSelection

FEEDBACK_TOOL = "send_feedback"
FAKE_TOKEN = "rootly_" + "9f8e7d6c5b4a39281706"


@pytest.fixture(autouse=True)
def fresh_limiter(monkeypatch):
    monkeypatch.setattr(posthog_analytics, "feedback_rate_limiter", FeedbackRateLimiter())


def _instrumented(server: FastMCP) -> tuple[FastMCP, list[tuple[str, dict[str, Any]]]]:
    client = Posthog("phc_test", send=False)
    captured: list[tuple[str, dict[str, Any]]] = []
    patcher = patch.object(
        client, "capture", side_effect=lambda event, **kw: captured.append((event, kw))
    )
    patcher.start()
    maybe_enable_posthog_mcp_analytics(server, client, logging.getLogger(__name__))
    return server, captured


async def _send(server: FastMCP, arguments: dict[str, Any]) -> Any:
    async with Client(server) as client:
        result = await client.call_tool(FEEDBACK_TOOL, arguments, raise_on_error=False)
    await asyncio.sleep(0.2)  # capture is fire-and-forget
    return result


def _feedback_events(captured: list[tuple[str, dict[str, Any]]]) -> list[dict[str, Any]]:
    return [kw for event, kw in captured if event == FEEDBACK_EVENT]


async def test_listed_with_our_description():
    server, _ = _instrumented(FastMCP("test"))

    async with Client(server) as client:
        tools = {tool.name: tool for tool in await client.list_tools()}

    assert tools[FEEDBACK_TOOL].description == FEEDBACK_TOOL_DESCRIPTION


async def test_not_listed_without_posthog():
    server = FastMCP("test")
    maybe_enable_posthog_mcp_analytics(server, None, logging.getLogger(__name__))

    async with Client(server) as client:
        assert FEEDBACK_TOOL not in {tool.name for tool in await client.list_tools()}


async def test_report_is_captured_and_scrubbed():
    server, captured = _instrumented(FastMCP("test"))

    result = await _send(
        server,
        {
            "feedback_type": "missing_capability",
            "summary": f"No bulk resolve tool; tried api_token={FAKE_TOKEN}",
            "details": f"ROOTLY_API_TOKEN={FAKE_TOKEN}",
            "suggested_improvement": "Add resolve_alerts taking a list of ids",
        },
    )

    assert not result.is_error
    [event] = _feedback_events(captured)
    properties = event["properties"]
    assert properties["$mcp_feedback_type"] == "missing_capability"
    assert properties["$mcp_feedback_suggested_improvement"] == (
        "Add resolve_alerts taking a list of ids"
    )
    # The SDK copies the report into $mcp_intent too; neither copy keeps the token.
    assert FAKE_TOKEN not in repr(captured)


async def test_reports_are_rate_limited_per_caller(monkeypatch):
    monkeypatch.setattr(posthog_analytics, "feedback_rate_limiter", FeedbackRateLimiter(limit=2))
    server, captured = _instrumented(FastMCP("test"))

    for n in range(3):
        await _send(server, {"feedback_type": "issue", "summary": f"report {n}"})

    assert len(_feedback_events(captured)) == 2


def test_rate_limit_drops_only_feedback_events(monkeypatch):
    monkeypatch.setattr(posthog_analytics, "feedback_rate_limiter", FeedbackRateLimiter(limit=0))

    assert prepare_posthog_mcp_event({"event": FEEDBACK_EVENT, "properties": {}}) is None
    assert prepare_posthog_mcp_event({"event": "$mcp_tool_call", "properties": {}}) is not None


def test_rate_limit_window_rolls():
    now = [0.0]
    limiter = FeedbackRateLimiter(limit=1, window=60, clock=lambda: now[0])

    assert limiter.allow("a")
    assert not limiter.allow("a")
    assert limiter.allow("b")
    now[0] = 60.0
    assert limiter.allow("a")


async def test_listed_and_callable_under_any_toolset_selection(monkeypatch):
    monkeypatch.setenv("ROOTLY_API_TOKEN", "rootly_test")
    monkeypatch.delenv("ROOTLY_MCP_TOOLSETS", raising=False)
    monkeypatch.delenv("ROOTLY_MCP_ENABLED_TOOLS", raising=False)
    server, captured = _instrumented(create_rootly_mcp_server(hosted=True))

    for selection in (
        ToolSelection(frozenset({CORE})),
        ToolSelection(frozenset({"alerts"})),
        ToolSelection(frozenset({CORE}), readonly=True),
    ):
        reset = toolsets.requested_selection.set(selection)
        try:
            async with Client(server) as client:
                assert FEEDBACK_TOOL in {tool.name for tool in await client.list_tools()}
            result = await _send(server, {"feedback_type": "other", "summary": "x"})
            assert not result.is_error
        finally:
            toolsets.requested_selection.reset(reset)

    assert len(_feedback_events(captured)) == 3


async def test_not_listed_when_turned_off():
    server = FastMCP("test")
    client = Posthog("phc_test", send=False)
    maybe_enable_posthog_mcp_analytics(
        server, client, logging.getLogger(__name__), collect_feedback=False
    )

    async with Client(server) as mcp_client:
        assert FEEDBACK_TOOL not in {tool.name for tool in await mcp_client.list_tools()}


def test_naming_it_in_a_tool_list_is_valid(monkeypatch, caplog):
    # PostHog analytics adds it after the server applies the list, so the
    # list check must not report it as unknown.
    monkeypatch.setenv("ROOTLY_API_TOKEN", "rootly_test")
    with caplog.at_level("WARNING"):
        create_rootly_mcp_server(enabled_tools={"list_incidents", FEEDBACK_TOOL})

    assert "Invalid tool names" not in caplog.text


async def test_a_tool_list_of_only_feedback_starts(monkeypatch):
    monkeypatch.setenv("ROOTLY_API_TOKEN", "rootly_test")
    server, _ = _instrumented(create_rootly_mcp_server(enabled_tools={FEEDBACK_TOOL}))

    async with Client(server) as client:
        assert {tool.name for tool in await client.list_tools()} == {FEEDBACK_TOOL}
