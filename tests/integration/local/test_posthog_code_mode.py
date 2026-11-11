"""PostHog MCP analytics on the real Code Mode server, as main() wires it.

Only the client's send boundary is intercepted, so this fails if the calls
execute makes stop reaching PostHog.
"""

import asyncio
import logging
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from fastmcp import Client

from rootly_mcp_server.code_mode import create_rootly_codemode_server
from rootly_mcp_server.posthog_analytics import (
    maybe_enable_posthog_mcp_analytics,
    new_posthog_client,
)


@pytest.mark.integration
async def test_calls_execute_makes_reach_posthog(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DISABLE_DIAGNOSTICS", "true")
    monkeypatch.delenv("SENTRY_DSN", raising=False)
    monkeypatch.delenv("ROOTLY_MCP_ENABLED_TOOLS", raising=False)
    server = create_rootly_codemode_server(
        swagger_path=str(Path(__file__).resolve().parents[3] / "swagger.json"),
        hosted=True,
    )
    client = new_posthog_client("phc_test", send=False)
    hook = client.before_send
    tools: list[str] = []

    def record(message: dict[str, Any]) -> dict[str, Any] | None:
        kept = hook(message)
        if kept and kept["event"] == "$mcp_tool_call":
            tools.append(kept["properties"]["$mcp_tool_name"])
        return kept

    client.before_send = record
    with patch(
        "rootly_mcp_server.posthog_analytics.get_hosted_authenticated_user",
        return_value={"id": "4242"},
    ):
        maybe_enable_posthog_mcp_analytics(server, client, logging.getLogger(__name__))
        async with Client(server) as mcp_client:
            result = await mcp_client.call_tool(
                "execute", {"code": "return await call_tool('get_server_version', {})"}
            )
        await asyncio.sleep(0.2)
        client.flush()

    assert not result.is_error
    assert sorted(tools) == ["execute", "get_server_version"]
