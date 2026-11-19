"""submit_feedback: registered only with PostHog on, scrubbed, capped, rate-limited."""

from __future__ import annotations

from typing import Any, cast

import pytest
from fastmcp import Client, FastMCP

from rootly_mcp_server import feedback, toolsets
from rootly_mcp_server.feedback import (
    FEEDBACK_EVENT,
    FEEDBACK_TOOL_NAME,
    FIELD_MAX_LENGTH,
    SUMMARY_MAX_LENGTH,
    FeedbackRateLimiter,
    clean_text,
    register_feedback_tool,
)
from rootly_mcp_server.server import create_rootly_mcp_server
from rootly_mcp_server.toolsets import CORE, ToolSelection

USER = {
    "id": "42",
    "email": "someone@example.com",
    "name": "Someone",
    "organization_id": "7",
}


class FakePostHog:
    def __init__(self, error: Exception | None = None) -> None:
        self.events: list[dict[str, Any]] = []
        self.error = error

    def capture(self, event: str, **kwargs: Any) -> None:
        if self.error is not None:
            raise self.error
        self.events.append({"event": event, **kwargs})


@pytest.fixture
def signed_in(monkeypatch):
    monkeypatch.setattr(feedback, "get_hosted_authenticated_user", lambda: USER)
    monkeypatch.setattr(feedback, "current_session_id", lambda: "session-1")


def _server(client: Any, **kwargs: Any) -> FastMCP:
    server = FastMCP("test")
    register_feedback_tool(server, client, **kwargs)
    return server


async def _call(server: FastMCP, **arguments: Any) -> dict[str, Any]:
    async with Client(server) as client:
        result = await client.call_tool(FEEDBACK_TOOL_NAME, arguments)
    return result.structured_content or {}


async def _names(server: FastMCP) -> set[str]:
    async with Client(server) as client:
        return {tool.name for tool in await client.list_tools()}


async def test_not_registered_without_posthog():
    assert FEEDBACK_TOOL_NAME not in await _names(_server(None))


async def test_records_one_event_tied_to_the_user_and_session(signed_in, monkeypatch):
    monkeypatch.setenv("ROOTLY_MCP_BUILD", "abc1234")
    posthog = FakePostHog()

    result = await _call(
        _server(posthog),
        summary="update_incident drops the reason for a 422",
        category="confusing_output",
        tool_name="update_incident",
        attempted="Set severity on INC-1",
        expected="The validation message",
        proposed_fix="Surface the API error detail",
    )

    assert result["status"] == "recorded"
    [event] = posthog.events
    assert event["event"] == FEEDBACK_EVENT
    assert event["distinct_id"] == "42"
    assert event["groups"] == {"organization": "7"}
    properties = event["properties"]
    assert properties["feedback_summary"] == "update_incident drops the reason for a 422"
    assert properties["feedback_category"] == "confusing_output"
    assert properties["feedback_tool_name"] == "update_incident"
    assert properties["feedback_attempted"] == "Set severity on INC-1"
    assert properties["feedback_expected"] == "The validation message"
    assert properties["feedback_proposed_fix"] == "Surface the API error detail"
    assert properties["$session_id"] == "session-1"
    assert properties["$mcp_server_build"] == "abc1234"
    assert properties["$set"] == {"email": "someone@example.com", "name": "Someone"}


async def test_optional_fields_are_left_out(signed_in):
    posthog = FakePostHog()

    await _call(_server(posthog), summary="Need a bulk resolve tool", category="missing_capability")

    properties = posthog.events[0]["properties"]
    for key in ("feedback_tool_name", "feedback_attempted", "feedback_expected"):
        assert key not in properties


async def test_credentials_are_scrubbed(signed_in):
    posthog = FakePostHog()

    await _call(
        _server(posthog),
        summary="Auth fails",
        category="bug",
        attempted="Called with ROOTLY_API_TOKEN=rootly_9f8e7d",
    )

    assert "rootly_9f8e7d" not in posthog.events[0]["properties"]["feedback_attempted"]


def test_long_text_is_capped():
    assert len(clean_text("x" * 10_000, FIELD_MAX_LENGTH) or "") == FIELD_MAX_LENGTH
    assert len(clean_text("y" * 500, SUMMARY_MAX_LENGTH) or "") == SUMMARY_MAX_LENGTH
    assert clean_text("   ", FIELD_MAX_LENGTH) is None
    assert clean_text(None, FIELD_MAX_LENGTH) is None


async def test_rate_limited_per_user(signed_in):
    posthog = FakePostHog()
    server = _server(posthog, rate_limiter=FeedbackRateLimiter(limit=2))

    statuses = [
        (await _call(server, summary=f"report {n}", category="other"))["status"] for n in range(3)
    ]

    assert statuses == ["recorded", "recorded", "not_recorded"]
    assert len(posthog.events) == 2


def test_rate_limit_window_rolls():
    now = [0.0]
    limiter = FeedbackRateLimiter(limit=1, window=60, clock=lambda: now[0])

    assert limiter.allow("a")
    assert not limiter.allow("a")
    assert limiter.allow("b")
    now[0] = 60.0
    assert limiter.allow("a")


async def test_anonymous_callers_are_not_recorded(monkeypatch):
    monkeypatch.setattr(feedback, "get_hosted_authenticated_user", lambda: None)
    posthog = FakePostHog()

    result = await _call(_server(posthog), summary="x", category="bug")

    assert result["status"] == "not_recorded"
    assert posthog.events == []


async def test_capture_failure_does_not_raise(signed_in):
    result = await _call(
        _server(FakePostHog(error=RuntimeError("down"))), summary="x", category="bug"
    )

    assert result["status"] == "not_recorded"


async def test_visible_under_any_toolset_selection(monkeypatch):
    monkeypatch.setenv("ROOTLY_API_TOKEN", "rootly_test")
    monkeypatch.delenv("ROOTLY_MCP_TOOLSETS", raising=False)
    monkeypatch.delenv("ROOTLY_MCP_ENABLED_TOOLS", raising=False)
    server = create_rootly_mcp_server(hosted=True, enable_write_tools=True)
    register_feedback_tool(server, cast(Any, FakePostHog()))

    for selection in (
        ToolSelection(frozenset({CORE})),
        ToolSelection(frozenset({"alerts"})),
        ToolSelection(frozenset({CORE}), readonly=True),
    ):
        reset = toolsets.requested_selection.set(selection)
        try:
            assert FEEDBACK_TOOL_NAME in await _names(server)
        finally:
            toolsets.requested_selection.reset(reset)


def test_naming_it_in_a_tool_list_is_not_an_invalid_name(monkeypatch, caplog):
    # main() registers it after the server applies the list, so the list
    # check must not report it as unknown.
    monkeypatch.setenv("ROOTLY_API_TOKEN", "rootly_test")
    with caplog.at_level("WARNING"):
        create_rootly_mcp_server(enabled_tools={"list_incidents", FEEDBACK_TOOL_NAME})

    assert "Invalid tool names" not in caplog.text


async def test_a_tool_list_of_only_feedback_starts(monkeypatch):
    monkeypatch.setenv("ROOTLY_API_TOKEN", "rootly_test")
    server = create_rootly_mcp_server(enabled_tools={FEEDBACK_TOOL_NAME})
    register_feedback_tool(server, cast(Any, FakePostHog()))

    assert await _names(server) == {FEEDBACK_TOOL_NAME}
