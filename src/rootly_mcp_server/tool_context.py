"""Measure how much model context each MCP surface's tool list costs.

Every connected client receives the full ``tools/list`` (names, descriptions,
input and output schemas) and most put all of it in the model's context. One
oversized schema (workflow task ``task_params`` was ~32k tokens per tool) is
paid by every user on every conversation, and nothing in the API diff shows
it. This module measures it so CI can hold the line; see
``tests/unit/test_tool_context_budget.py`` and ``scripts/tool_context_report.py``.

Sizes are serialized JSON characters. ``approx_tokens`` divides by 4, which is
close for prose and undercounts dense JSON (schemas tokenize nearer 2-3 chars
per token), so treat it as a lower bound.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any

# A single tool over this is a design problem, not growth: split it, compact a
# union (spec_transform.compact_large_unions), or move detail behind a lookup.
PER_TOOL_MAX_CHARS = 40_000

# A surface may grow this much over its recorded baseline before the budget
# test fails. Re-record the baseline on purpose when growth is intended.
SURFACE_GROWTH_TOLERANCE = 0.05

SURFACES = ("hosted_full", "hosted_slim", "self_hosted", "code_mode")


@dataclass(frozen=True)
class ToolSize:
    name: str
    chars: int

    @property
    def approx_tokens(self) -> int:
        return self.chars // 4


def tool_definition_chars(tool: Any) -> int:
    """Serialized size of what a client receives for one tool."""
    return len(
        json.dumps(
            {
                "name": tool.name,
                "description": tool.description,
                "inputSchema": tool.inputSchema,
                "outputSchema": tool.outputSchema,
            },
            separators=(",", ":"),
        )
    )


# Settings that change which tools a server registers. Measurements clear
# them so a surface always means its shipped default, whatever the shell has.
_TOOL_LIST_ENV_VARS = (
    "ROOTLY_MCP_ENABLED_TOOLS",
    "ROOTLY_MCP_ENABLE_WRITE_TOOLS",
    "ROOTLY_MCP_HOSTED_TOOL_PROFILE",
)


def _build_surface(surface: str) -> Any:
    from .code_mode import create_rootly_codemode_server
    from .server import create_rootly_mcp_server
    from .server_defaults import DEFAULT_HOSTED_ENABLED_TOOLS

    if surface == "hosted_full":
        return create_rootly_mcp_server(hosted=True, enable_write_tools=True)
    if surface == "hosted_slim":
        return create_rootly_mcp_server(
            hosted=True,
            enable_write_tools=True,
            enabled_tools=set(DEFAULT_HOSTED_ENABLED_TOOLS),
        )
    if surface == "self_hosted":
        return create_rootly_mcp_server(hosted=False, enable_write_tools=True)
    if surface == "code_mode":
        return create_rootly_codemode_server(hosted=True, enable_write_tools=True)
    raise ValueError(f"unknown surface {surface!r}")


async def measure_surface(surface: str) -> list[ToolSize]:
    """Tool sizes for one surface, largest first."""
    from fastmcp import Client

    saved = {name: os.environ.pop(name) for name in _TOOL_LIST_ENV_VARS if name in os.environ}
    os.environ.setdefault("ROOTLY_API_TOKEN", "rootly_tool_context_measurement")
    try:
        server = _build_surface(surface)
    finally:
        os.environ.update(saved)
    async with Client(server) as client:
        tools = await client.list_tools()
    return sorted(
        (ToolSize(tool.name, tool_definition_chars(tool)) for tool in tools),
        key=lambda size: (-size.chars, size.name),
    )
