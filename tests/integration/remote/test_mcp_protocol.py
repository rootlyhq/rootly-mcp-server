"""MCP protocol smoke tests against a running server (the built container in CI).

Set MCP_SERVER_URL to run them; once set, a server that is down or erroring fails.
"""

import os

import pytest
from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport

pytestmark = [
    pytest.mark.remote,
    pytest.mark.integration,
    pytest.mark.skipif(not os.getenv("MCP_SERVER_URL"), reason="MCP_SERVER_URL not set"),
]

TOKEN = os.getenv("ROOTLY_API_TOKEN") or None


@pytest.fixture
async def client():
    url = os.environ["MCP_SERVER_URL"].rstrip("/") + "/mcp"
    async with Client(StreamableHttpTransport(url, auth=TOKEN)) as mcp_client:
        yield mcp_client


async def test_tools_list_advertises_core_tools(client):
    names = {tool.name for tool in await client.list_tools()}

    assert len(names) >= 20
    assert {"get_current_user", "search_incidents", "list_endpoints"} <= names


async def test_local_tool_call(client):
    """list_endpoints never reaches the Rootly API, so this exercises only the server."""
    result = await client.call_tool("list_endpoints", {})

    assert not result.is_error


@pytest.mark.skipif(not TOKEN, reason="needs a real Rootly API token")
async def test_upstream_tool_call(client):
    result = await client.call_tool("get_current_user", {})

    assert not result.is_error
