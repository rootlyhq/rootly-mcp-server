"""Calls to tools removed with their deprecated endpoints name the replacement."""

from types import SimpleNamespace

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError

from rootly_mcp_server import toolsets
from rootly_mcp_server.server import RemovedToolMiddleware, create_rootly_mcp_server
from rootly_mcp_server.spec_transform import deprecated_operation_guidance
from rootly_mcp_server.toolsets import ToolSelection


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


@pytest.fixture
def mcp(monkeypatch):
    monkeypatch.setenv("ROOTLY_API_TOKEN", "rootly_test_value")
    return create_rootly_mcp_server()


@pytest.mark.asyncio
async def test_removed_tool_call_names_the_replacement(mcp):
    async with Client(mcp) as client:
        names = {tool.name for tool in await client.list_tools()}
        assert "list_custom_fields" not in names

        # Both the snake_case name and the historical camelCase one.
        for name in ("list_custom_fields", "listCustomFields"):
            with pytest.raises(ToolError, match="was removed.*form field endpoints"):
                await client.call_tool(name, {})

        with pytest.raises(ToolError, match="Unknown tool: 'not_a_tool'"):
            await client.call_tool("not_a_tool", {})


@pytest.mark.asyncio
async def test_read_only_connection_gets_the_replacement_not_a_read_only_refusal(mcp):
    reset = toolsets.requested_selection.set(ToolSelection(frozenset(), readonly=True))
    try:
        async with Client(mcp) as client:
            with pytest.raises(ToolError) as excinfo:
                await client.call_tool("list_custom_fields", {})
    finally:
        toolsets.requested_selection.reset(reset)
    assert "form field endpoints" in str(excinfo.value)
    assert "readonly" not in str(excinfo.value)


@pytest.mark.asyncio
async def test_a_registered_tool_is_never_shadowed():
    middleware = RemovedToolMiddleware({"list_things": "Use other things.", "gone": ""})
    middleware.exclude({"list_things"})
    context = SimpleNamespace(message=SimpleNamespace(name="list_things"))

    async def call_next(_context):
        return "called"

    assert await middleware.on_call_tool(context, call_next) == "called"  # type: ignore[arg-type]
    context.message.name = "gone"
    with pytest.raises(ToolError, match="Tool 'gone' was removed"):
        await middleware.on_call_tool(context, call_next)  # type: ignore[arg-type]
