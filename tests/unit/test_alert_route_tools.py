"""Alert route tools replace the legacy ``/v1/alert_routing_rules`` surface.

The legacy endpoints reject requests for organizations using Advanced Alert
Routing. The default tool surface uses ``alert_routes`` and explains how to
distinguish a matched rule ID from the route that owns it.
"""

from __future__ import annotations

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError
from mcp.types import Tool

from rootly_mcp_server import server_defaults
from rootly_mcp_server.server import create_rootly_mcp_server

pytestmark = pytest.mark.unit

LEGACY_ROUTING_RULE_TOOLS = {
    "list_alert_routing_rules",
    "get_alert_routing_rule",
    "create_alert_routing_rule",
    "update_alert_routing_rule",
}


async def _tools(**kwargs) -> dict[str, Tool]:
    server = create_rootly_mcp_server(**kwargs)
    async with Client(server) as client:
        return {tool.name: tool for tool in await client.list_tools()}


@pytest.mark.parametrize("profile", ["self_hosted", "hosted", "slim", "microsoft"])
@pytest.mark.parametrize("enable_write_tools", [False, True])
async def test_legacy_routing_rule_tools_are_not_exposed(
    mock_environment_token, monkeypatch, profile, enable_write_tools
):
    monkeypatch.delenv("ROOTLY_MCP_ENABLED_TOOLS", raising=False)
    monkeypatch.delenv("ROOTLY_MCP_TOOLSETS", raising=False)
    monkeypatch.setenv("ROOTLY_MCP_HOSTED_TOOL_PROFILE", "slim" if profile == "slim" else "full")
    if profile == "microsoft":
        monkeypatch.setenv(
            "ROOTLY_MCP_TOOLSETS",
            "core,incidents,alerts,oncall,workflows,workflow_tasks,catalog,"
            "status_pages,config,users,investigations,audit",
        )
    hosted = profile != "self_hosted"
    enabled_tools = server_defaults.enabled_tools_from_env(
        hosted=hosted,
        hosted_tool_profile=server_defaults.hosted_tool_profile_from_env(),
    )
    server = create_rootly_mcp_server(
        hosted=hosted, enable_write_tools=enable_write_tools, enabled_tools=enabled_tools
    )
    async with Client(server) as client:
        tools = {tool.name for tool in await client.list_tools()}
        assert not LEGACY_ROUTING_RULE_TOOLS & tools
        with pytest.raises(ToolError, match="[Uu]nknown tool|not found"):
            await client.call_tool("list_alert_routing_rules", {})

    assert "list_alert_routes" in tools
    if profile == "slim":
        assert not {"get_alert_route", "create_alert_route", "update_alert_route"} & tools
    else:
        assert "get_alert_route" in tools
        writes = {"create_alert_route", "update_alert_route"}
        if enable_write_tools:
            assert writes <= tools
        else:
            assert not writes & tools


async def test_list_alert_routes_describes_the_rule_id_lookup(mock_environment_token):
    tools = await _tools()

    description = tools["list_alert_routes"].description or ""
    assert "data.attributes.data.routing_rules[].id" in description
    assert "data[].attributes.rules[].id" in description


async def test_get_alert_route_warns_rule_ids_are_not_route_ids(mock_environment_token):
    tools = await _tools()

    description = tools["get_alert_route"].description or ""
    assert "not a route id" in description
    assert "list_alert_routes" in description
