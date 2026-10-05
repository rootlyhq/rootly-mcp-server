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
from rootly_mcp_server.transport import _session_authenticated_user


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

    # Keep the real SDK middleware, injection, event construction and
    # redaction pipeline, but intercept the send boundary so no test event or
    # Rootly data reaches an external service. `publish_event` would be the
    # wrong tap: it runs before the redaction hooks, so what it sees is not
    # what the exporter receives. The queue's `add` is made synchronous by
    # pointing it at the worker's own `_process_event`.
    events: list[Any] = []
    event_queue = queue_module.event_queue
    monkeypatch.setattr(event_queue, "_send_event", lambda event, retries=0: events.append(event))
    monkeypatch.setattr(event_queue, "add", event_queue._process_event)
    create_server = create_rootly_codemode_server if code_mode else create_rootly_mcp_server
    server = create_server(
        swagger_path=str(Path(__file__).resolve().parents[3] / "swagger.json"),
        hosted=True,
        enable_write_tools=True,
        enabled_tools=set(DEFAULT_HOSTED_ENABLED_TOOLS) if profile == "slim" else None,
    )
    assert maybe_enable_mcpcat_tracking(
        server, "proj_test_directory_review", logging.getLogger(__name__)
    )
    # The in-memory client carries no HTTP request, so stand in for the hosted
    # auth middleware: the session resolver derives the session from this user.
    reset = _session_authenticated_user.set({"id": "user-directory-review"})

    async with Client(server) as client:
        tools = await client.list_tools()
        names = {tool.name for tool in tools}
        assert tools
        assert "get_more_tools" not in names
        for tool in tools:
            properties = tool.inputSchema.get("properties", {})
            required = tool.inputSchema.get("required", [])
            # No telemetry handle is a parameter of any tool: the SDK runs in
            # session hook mode and intent capture is off.
            for injected in ("context", "session_id", "agent_id"):
                assert injected not in properties, (tool.name, injected)
                assert injected not in required, (tool.name, injected)
            schema_text = json.dumps(tool.inputSchema)
            assert "analytics and user intent tracking" not in schema_text, tool.name
            assert "YOU MUST provide 15-25 words" not in schema_text, tool.name
            assert "Session continuity handle" not in schema_text, tool.name
            assert "Never invent" not in schema_text, tool.name

        tool_name = "execute" if code_mode else "get_server_version"
        arguments: dict[str, Any] = {}
        if code_mode:
            arguments["code"] = 'return await call_tool("get_server_version", {})'
        result = await client.call_tool(tool_name, arguments)
        assert not result.is_error
        # Hook mode mirrors no handle into the result either.
        assert "session_id" not in json.dumps(result.structured_content or {})

        # A client holding a pre-#223 or pre-hook-mode tool list still sends
        # the handles, `context` with the user's goal spelled out. The call
        # must keep working, and the text must not reach the exporter.
        intent = "user wants to know whether the acme outage is still ongoing"
        legacy = await client.call_tool(
            tool_name, {**arguments, "session_id": "start", "context": intent}
        )
        assert not legacy.is_error
    _session_authenticated_user.reset(reset)

    call_events = [event for event in events if event.resource_name == tool_name]
    assert len(call_events) >= 2, "AgentCat stopped producing tool-call telemetry"
    clean_event, legacy_event = call_events[-2:]
    for event in (clean_event, legacy_event):
        assert event.event_type == "mcp:tools/call"
        assert event.user_intent is None
        assert "context" not in event.parameters["arguments"]
        assert "session_id" not in event.parameters["arguments"]
        assert intent not in json.dumps(event.model_dump(mode="json"), default=str)
        assert event.session_id.startswith("ses_"), "hook mode must still correlate sessions"
        assert event.is_error is False
        assert event.duration is not None
        assert event.response is not None
    # Both calls came from the same user in the same hour, so hook mode
    # correlates them into one session rather than minting one per call.
    assert clean_event.session_id == legacy_event.session_id
