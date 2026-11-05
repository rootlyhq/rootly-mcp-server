"""Calls to tools removed with their deprecated endpoints name the replacement."""

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError

from rootly_mcp_server.server import create_rootly_mcp_server
from rootly_mcp_server.spec_transform import deprecated_operation_guidance


def test_guidance_keeps_only_the_replacement_sentence():
    spec = {
        "paths": {
            "/v1/custom_fields": {
                "get": {
                    "operationId": "list_custom_fields",
                    "deprecated": True,
                    "description": "[DEPRECATED] Use form field endpoints instead. List Custom fields",
                },
                "post": {"operationId": "create_custom_field", "description": "Kept"},
            },
            "/v1/props": {
                "get": {
                    "operationId": "list_props",
                    "deprecated": True,
                    "description": "**Deprecated:** Use `include=fields` instead.\n\nList props.",
                }
            },
        }
    }

    assert deprecated_operation_guidance(spec) == {
        "list_custom_fields": "Use form field endpoints instead.",
        "list_props": "Use `include=fields` instead.",
    }


@pytest.mark.asyncio
async def test_removed_tool_call_names_the_replacement(monkeypatch):
    monkeypatch.setenv("ROOTLY_API_TOKEN", "rootly_test_value")
    mcp = create_rootly_mcp_server()
    async with Client(mcp) as client:
        names = {tool.name for tool in await client.list_tools()}
        assert "list_custom_fields" not in names

        # Both the snake_case name and the historical camelCase one.
        for name in ("list_custom_fields", "listCustomFields"):
            with pytest.raises(ToolError, match="was removed.*form field endpoints"):
                await client.call_tool(name, {})

        with pytest.raises(ToolError, match="Unknown tool: 'not_a_tool'"):
            await client.call_tool("not_a_tool", {})
