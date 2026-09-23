"""Hosted deployments must not expose or execute individual health-risk profiling."""

from pathlib import Path
from typing import Any
from unittest.mock import Mock

import pytest
from fastmcp import Client

from rootly_mcp_server.code_mode import create_rootly_codemode_server
from rootly_mcp_server.server import create_rootly_mcp_server
from rootly_mcp_server.server_defaults import DEFAULT_HOSTED_ENABLED_TOOLS

HEALTH_TOOL = "check_oncall_health_risk"


@pytest.mark.integration
@pytest.mark.parametrize("profile", ["full", "slim", "custom-allowlist"])
@pytest.mark.parametrize("surface", ["sse", "streamable-http", "code-mode"])
async def test_hosted_health_risk_tool_cannot_be_discovered_or_called(
    monkeypatch: pytest.MonkeyPatch, profile: str, surface: str
) -> None:
    # Neither a configured key nor an explicit allowlist may enable profiling.
    monkeypatch.setenv("ONCALLHEALTH_API_KEY", "test_key_must_not_be_used")
    monkeypatch.delenv("ROOTLY_MCP_ENABLED_TOOLS", raising=False)
    if profile == "custom-allowlist":
        monkeypatch.setenv(
            "ROOTLY_MCP_ENABLED_TOOLS",
            f"{HEALTH_TOOL},get_server_version,get_oncall_schedule_summary",
        )
    och_client = Mock(side_effect=AssertionError("Hosted code reached On-Call Health"))
    monkeypatch.setattr("rootly_mcp_server.tools.oncall.OnCallHealthClient", och_client)

    options: dict[str, Any] = {
        "swagger_path": str(Path(__file__).resolve().parents[3] / "swagger.json"),
        "hosted": True,
        "enabled_tools": set(DEFAULT_HOSTED_ENABLED_TOOLS) if profile == "slim" else None,
    }
    if surface == "code-mode":
        server = create_rootly_codemode_server(**options)
    else:
        server = create_rootly_mcp_server(**options, transport=surface)

    async with Client(server) as client:
        tools = await client.list_tools()
        assert HEALTH_TOOL not in {tool.name for tool in tools}

        # Check direct calls as well as the advertised list: hiding a tool
        # without removing its handler would leave it callable by name.
        result = await client.call_tool(
            HEALTH_TOOL,
            {"start_date": "2026-09-01", "end_date": "2026-09-07"},
            raise_on_error=False,
        )
        assert result.is_error

        if surface == "code-mode":
            catalog = await client.call_tool("list_tools", {"detail": "full"})
            assert HEALTH_TOOL not in str(catalog.data)
            search = await client.call_tool("tool_search", {"query": HEALTH_TOOL})
            assert HEALTH_TOOL not in str(search.data)
            schema = await client.call_tool("get_schema", {"tools": [HEALTH_TOOL]})
            assert schema.data == f"Tools not found: {HEALTH_TOOL}"

            for name in (HEALTH_TOOL, f"rootly:{HEALTH_TOOL}"):
                result = await client.call_tool(
                    "execute",
                    {
                        "code": f'return await call_tool("{name}", '
                        '{"start_date": "2026-09-01", "end_date": "2026-09-07"})'
                    },
                    raise_on_error=False,
                )
                assert result.is_error

            version = await client.call_tool(
                "execute", {"code": 'return await call_tool("get_server_version", {})'}
            )
        else:
            assert "get_oncall_schedule_summary" in {tool.name for tool in tools}
            version = await client.call_tool("get_server_version", {})
        assert not version.is_error

    och_client.assert_not_called()
