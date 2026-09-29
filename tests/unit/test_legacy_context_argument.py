"""Tests for LegacyContextArgumentMiddleware.

Callers built against the AgentCat-era schemas still send `context`. These drive
a real FastMCP server so the argument is checked against real tool validation.
"""

from typing import Annotated

import pytest
from fastmcp import Client, FastMCP
from pydantic import Field

from rootly_mcp_server.server import (
    RETIRED_INJECTED_PARAMS,
    LegacyContextArgumentMiddleware,
)


def _server() -> FastMCP:
    server = FastMCP("test")
    server.add_middleware(LegacyContextArgumentMiddleware())

    @server.tool
    def list_incidents(page_size: int = 10) -> str:
        return f"page_size={page_size}"

    @server.tool
    def annotate(context: Annotated[str, Field(description="Own parameter")]) -> str:
        return f"context={context}"

    return server


@pytest.mark.asyncio
async def test_undeclared_context_is_dropped():
    async with Client(_server()) as client:
        result = await client.call_tool(
            "list_incidents", {"page_size": 5, "context": "agent intent text"}
        )

    assert result.data == "page_size=5"


@pytest.mark.asyncio
async def test_declared_context_is_kept():
    async with Client(_server()) as client:
        result = await client.call_tool("annotate", {"context": "keep me"})

    assert result.data == "context=keep me"


@pytest.mark.asyncio
async def test_other_unexpected_arguments_still_fail():
    async with Client(_server()) as client:
        result = await client.call_tool("list_incidents", {"bogus": 1}, raise_on_error=False)

    assert result.is_error


@pytest.mark.asyncio
async def test_context_stays_out_of_advertised_schemas():
    async with Client(_server()) as client:
        tools = {tool.name: tool for tool in await client.list_tools()}

    assert "context" not in tools["list_incidents"].inputSchema["properties"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tool", "arguments"),
    [("tool_search", {"query": "incidents"}), ("list_tools", {}), ("tags", {})],
)
async def test_code_mode_server_tolerates_legacy_context(monkeypatch, tool, arguments):
    """The production factory stack plus the Code Mode transform, not a bare server.

    Discovery tools never reach the Rootly API, so no HTTP mocking is needed.
    """
    from rootly_mcp_server.code_mode import create_rootly_codemode_server

    monkeypatch.setenv("ROOTLY_API_TOKEN", "test-token")
    server = create_rootly_codemode_server()

    async with Client(server) as client:
        result = await client.call_tool(
            tool, {**arguments, "context": "legacy intent"}, raise_on_error=False
        )

    assert not result.is_error, result.content


def test_live_agentcat_parameters_are_not_retired():
    """`session_id`/`agent_id` must never be listed as retired.

    This middleware is registered by the factory; `agentcat.track()` adds
    AgentCat's middleware afterwards, so ours runs first. Dropping a parameter
    AgentCat still injects would remove it before AgentCat reads it --
    `session_id` is its correlation key, and billing counts sessions.
    """
    assert "session_id" not in RETIRED_INJECTED_PARAMS
    assert "agent_id" not in RETIRED_INJECTED_PARAMS


@pytest.mark.asyncio
async def test_every_retired_parameter_is_dropped(monkeypatch):
    """The drop generalizes past `context` -- one entry or several."""
    monkeypatch.setattr("rootly_mcp_server.server.RETIRED_INJECTED_PARAMS", ("context", "intent"))
    async with Client(_server()) as client:
        result = await client.call_tool(
            "list_incidents", {"page_size": 7, "context": "c", "intent": "i"}
        )

    assert result.data == "page_size=7"


@pytest.mark.asyncio
async def test_retired_parameter_declared_by_a_tool_is_kept(monkeypatch):
    """A tool that owns the name still receives it."""
    monkeypatch.setattr("rootly_mcp_server.server.RETIRED_INJECTED_PARAMS", ("context", "intent"))
    async with Client(_server()) as client:
        result = await client.call_tool("annotate", {"context": "kept"})

    assert result.data == "context=kept"
