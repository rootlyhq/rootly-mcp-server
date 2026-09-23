"""Verify the client-visible tool surface with the hosted AgentCat SDK loaded.

CI installs the SDK version pinned in Dockerfile. Base installations without
the optional telemetry package skip these tests.
"""

import json
import logging
from pathlib import Path
from typing import Any

import pytest
from fastmcp import Client

from rootly_mcp_server.__main__ import maybe_enable_mcpcat_tracking
from rootly_mcp_server.code_mode import create_rootly_codemode_server
from rootly_mcp_server.server import create_rootly_mcp_server
from rootly_mcp_server.server_defaults import DEFAULT_HOSTED_ENABLED_TOOLS


@pytest.mark.integration
@pytest.mark.parametrize("profile", ["full", "slim"])
@pytest.mark.parametrize("code_mode", [False, True], ids=["standard", "code-mode"])
async def test_agentcat_keeps_call_telemetry_without_requesting_intent(
    monkeypatch: pytest.MonkeyPatch, profile: str, code_mode: bool
) -> None:
    monkeypatch.setenv("DISABLE_DIAGNOSTICS", "true")
    monkeypatch.delenv("SENTRY_DSN", raising=False)
    monkeypatch.delenv("ROOTLY_MCP_ENABLED_TOOLS", raising=False)
    pytest.importorskip("agentcat")
    queue_module = pytest.importorskip("agentcat.modules.event_queue")

    # Keep real SDK middleware/injection/event construction, but intercept the
    # publish boundary so no test event or Rootly data reaches an external service.
    events: list[Any] = []
    monkeypatch.setattr(queue_module, "publish_event", lambda _server, event: events.append(event))
    create_server = create_rootly_codemode_server if code_mode else create_rootly_mcp_server
    server = create_server(
        swagger_path=str(Path(__file__).resolve().parents[3] / "swagger.json"),
        hosted=True,
        enable_write_tools=True,
        enabled_tools=set(DEFAULT_HOSTED_ENABLED_TOOLS) if profile == "slim" else None,
    )
    maybe_enable_mcpcat_tracking(server, "proj_test_directory_review", logging.getLogger(__name__))

    async with Client(server) as client:
        tools = await client.list_tools()
        names = {tool.name for tool in tools}
        assert tools
        assert "get_more_tools" not in names
        for tool in tools:
            assert "context" not in tool.inputSchema.get("properties", {}), tool.name
            assert "context" not in tool.inputSchema.get("required", []), tool.name
            schema_text = json.dumps(tool.inputSchema)
            assert "analytics and user intent tracking" not in schema_text, tool.name
            assert "YOU MUST provide 15-25 words" not in schema_text, tool.name

        # session_id is still advertised by the SDK for tracing; only the
        # analytics intent field and feedback tool should disappear.
        tool_name = "execute" if code_mode else "get_server_version"
        tool = next(tool for tool in tools if tool.name == tool_name)
        assert "session_id" in tool.inputSchema["properties"]
        arguments: dict[str, Any] = {"session_id": "start"}
        if code_mode:
            arguments["code"] = 'return await call_tool("get_server_version", {})'
        result = await client.call_tool(tool_name, arguments)
        assert not result.is_error

    call_events = [event for event in events if event.resource_name == tool_name]
    assert call_events, "AgentCat stopped producing tool-call telemetry"
    event = call_events[-1]
    assert event.event_type == "mcp:tools/call"
    assert event.user_intent is None
    assert "context" not in event.parameters["arguments"]
    assert event.is_error is False
    assert event.duration is not None
    assert event.response is not None
