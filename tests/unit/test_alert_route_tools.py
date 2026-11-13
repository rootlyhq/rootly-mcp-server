"""Alert route tools replace the legacy ``/v1/alert_routing_rules`` surface.

Advanced Alert Routing is enabled for every production team, so the legacy
endpoints answer 403 on every call. The legacy tools are gone, and the
``alert_routes`` descriptions carry the rule-id-to-route lookup an agent needs
(an alert's ``data.routing_rules[].id`` holds rule ids, not route ids).
"""

from __future__ import annotations

import pytest

from rootly_mcp_server.server import create_rootly_mcp_server

pytestmark = pytest.mark.unit

LEGACY_ROUTING_RULE_TOOLS = {
    "list_alert_routing_rules",
    "get_alert_routing_rule",
    "create_alert_routing_rule",
    "update_alert_routing_rule",
}


async def _tools(**kwargs) -> dict[str, object]:
    server = create_rootly_mcp_server(hosted=False, **kwargs)
    return {tool.name: tool for tool in await server.list_tools()}


async def test_legacy_routing_rule_tools_are_not_exposed(mock_environment_token):
    tools = await _tools()

    assert not LEGACY_ROUTING_RULE_TOOLS & tools.keys()
    assert {"list_alert_routes", "get_alert_route"} <= tools.keys()


async def test_list_alert_routes_describes_the_rule_id_lookup(mock_environment_token):
    tools = await _tools()

    description = tools["list_alert_routes"].description
    assert "data.routing_rules[].id" in description
    assert "rules[].id" in description


async def test_get_alert_route_warns_rule_ids_are_not_route_ids(mock_environment_token):
    tools = await _tools()

    description = tools["get_alert_route"].description
    assert "not a route id" in description
    assert "list_alert_routes" in description
