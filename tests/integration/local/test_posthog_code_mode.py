"""PostHog MCP analytics on the real Code Mode server, as main() wires it.

Only the client's send boundary is intercepted, so this fails if the calls
execute makes stop reaching PostHog.
"""

import asyncio
import logging
from collections import Counter
from pathlib import Path
from typing import Any
from unittest.mock import patch

import httpx2
import pytest
from fastmcp import Client
from posthog.mcp.session import derive_session_id_from_mcp_session
from starlette.middleware import Middleware

from rootly_mcp_server.__main__ import build_dual_http_app
from rootly_mcp_server.code_mode import create_rootly_codemode_server
from rootly_mcp_server.posthog_analytics import (
    maybe_enable_posthog_mcp_analytics,
    new_posthog_client,
    posthog_session_middleware,
)
from rootly_mcp_server.transport import AuthCaptureMiddleware

BUNDLED_SPEC = Path(__file__).resolve().parents[3] / "src/rootly_mcp_server/data/swagger.json"


@pytest.mark.integration
async def test_calls_execute_makes_reach_posthog(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DISABLE_DIAGNOSTICS", "true")
    monkeypatch.delenv("SENTRY_DSN", raising=False)
    monkeypatch.delenv("ROOTLY_MCP_ENABLED_TOOLS", raising=False)
    server = create_rootly_codemode_server(
        swagger_path=str(BUNDLED_SPEC),
        hosted=True,
    )
    client = new_posthog_client("phc_test", send=False)
    hook = client.before_send
    assert hook is not None
    tools: list[str] = []

    def record(message: dict[str, Any]) -> dict[str, Any] | None:
        kept = hook(message)
        assert kept is None or isinstance(kept, dict)
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


@pytest.mark.integration
async def test_replayed_http_sessions_group_outer_and_inner_events(monkeypatch):
    monkeypatch.setenv("DISABLE_DIAGNOSTICS", "true")
    monkeypatch.delenv("ROOTLY_MCP_ENABLED_TOOLS", raising=False)
    server = create_rootly_codemode_server(swagger_path=str(BUNDLED_SPEC), hosted=True)
    posthog = new_posthog_client("phc_test", send=False)
    hook = posthog.before_send
    assert hook is not None
    captured: list[dict[str, Any]] = []

    def record(message: dict[str, Any]) -> dict[str, Any] | None:
        kept = hook(message)
        assert kept is None or isinstance(kept, dict)
        if kept and kept["event"] == "$mcp_tool_call":
            captured.append(kept["properties"])
        return kept

    async def authenticated_user(self, auth_header):
        return {"id": "4242"}

    posthog.before_send = record
    maybe_enable_posthog_mcp_analytics(server, posthog, logging.getLogger(__name__))
    app = build_dual_http_app(
        server,
        middleware=[Middleware(AuthCaptureMiddleware), *posthog_session_middleware(posthog)],
    )
    headers = {
        "Authorization": "Bearer rootly_test",
        "Accept": "application/json, text/event-stream",
        "Content-Type": "application/json",
        "MCP-Protocol-Version": "2025-06-18",
    }
    initialize = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "test-client", "version": "1"},
        },
    }
    call = {
        "jsonrpc": "2.0",
        "id": 2,
        "method": "tools/call",
        "params": {
            "name": "execute",
            "arguments": {"code": "return await call_tool('get_server_version', {})"},
        },
    }
    sessions: list[str] = []
    with patch.object(AuthCaptureMiddleware, "_validate_token_upstream", authenticated_user):
        async with app.router.lifespan_context(app):
            async with httpx2.AsyncClient(
                transport=httpx2.ASGITransport(app=app), base_url="http://test"
            ) as http:
                for _ in range(2):
                    response = await http.post("/mcp", json=initialize, headers=headers)
                    assert response.status_code == 200, response.text
                    sessions.append(response.headers["mcp-session-id"])
                assert sessions[0] != sessions[1]
                for session in (sessions[0], sessions[0], sessions[1]):
                    response = await http.post(
                        "/mcp", json=call, headers={**headers, "Mcp-Session-Id": session}
                    )
                    assert response.status_code == 200, response.text
                await asyncio.sleep(0.2)
                posthog.flush()

    expected = Counter(
        {
            derive_session_id_from_mcp_session(f"user:4242:transport:{sessions[0]}"): 2,
            derive_session_id_from_mcp_session(f"user:4242:transport:{sessions[1]}"): 1,
        }
    )
    for tool in ("execute", "get_server_version"):
        events = [event for event in captured if event["$mcp_tool_name"] == tool]
        assert Counter(event["$session_id"] for event in events) == expected
        assert all(event["$mcp_is_error"] is False for event in events)
