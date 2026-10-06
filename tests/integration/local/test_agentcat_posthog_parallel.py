"""AgentCat and PostHog MCP analytics on the same server, as main() wires them.

Drives the real server through the real AgentCat (Dockerfile pin) and PostHog
SDKs with only their publish boundaries intercepted. Base installs without
AgentCat skip.
"""

import asyncio
import logging
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from fastmcp import Client

from rootly_mcp_server.__main__ import maybe_enable_mcpcat_tracking
from rootly_mcp_server.posthog_analytics import (
    build_posthog_client,
    maybe_enable_posthog_mcp_analytics,
)
from rootly_mcp_server.server import create_rootly_mcp_server


@pytest.mark.integration
async def test_agentcat_and_posthog_both_capture_without_breaking_tools(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DISABLE_DIAGNOSTICS", "true")
    monkeypatch.setenv("POSTHOG_PROJECT_TOKEN", "phc_test")
    monkeypatch.setenv("POSTHOG_ALONGSIDE_AGENTCAT", "true")
    monkeypatch.delenv("SENTRY_DSN", raising=False)
    monkeypatch.delenv("ROOTLY_MCP_ENABLED_TOOLS", raising=False)
    pytest.importorskip("agentcat")
    queue_module = pytest.importorskip("agentcat.modules.event_queue")
    agentcat_events: list[Any] = []
    monkeypatch.setattr(
        queue_module, "publish_event", lambda _server, event: agentcat_events.append(event)
    )
    logger = logging.getLogger(__name__)
    server = create_rootly_mcp_server(
        swagger_path=str(Path(__file__).resolve().parents[3] / "swagger.json"),
        hosted=True,
        enable_write_tools=True,
    )

    agentcat_enabled = maybe_enable_mcpcat_tracking(server, "proj_test_parallel", logger)
    posthog_client = build_posthog_client(logger, agentcat_enabled=agentcat_enabled)
    assert agentcat_enabled
    assert posthog_client is not None

    posthog_events: list[str] = []
    with patch.object(
        posthog_client, "capture", side_effect=lambda event, **_kw: posthog_events.append(event)
    ):
        maybe_enable_posthog_mcp_analytics(server, posthog_client, logger)
        async with Client(server) as client:
            tools = await client.list_tools()
            for tool in tools:
                properties = tool.inputSchema.get("properties", {})
                assert not {"context", "conversation_id", "llm_model"} & set(properties), tool.name
            result = await client.call_tool("get_server_version", {"session_id": "start"})
        await asyncio.sleep(0.2)

    assert not result.is_error
    assert any(e.resource_name == "get_server_version" for e in agentcat_events)
    assert {"$mcp_tools_list", "$mcp_tool_call"} <= set(posthog_events)
