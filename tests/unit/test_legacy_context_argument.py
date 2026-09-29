"""Tests for LegacyContextArgumentMiddleware.

Callers built against the AgentCat-era schemas still send `context`. These drive
a real FastMCP server so the argument is checked against real tool validation.
"""

from typing import Annotated

import pytest
from fastmcp import Client, FastMCP
from pydantic import Field

from rootly_mcp_server.server import LegacyContextArgumentMiddleware


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
