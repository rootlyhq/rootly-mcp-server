"""Hold the tool-list context cost of each MCP surface to a budget.

Every connected client pays for the whole tools/list on every conversation. In
2026-11 a 153-variant union made create_workflow_task ~32k tokens on its own
and a customer disabled the server over it; nothing failed. These tests fail
instead:

* any one tool over PER_TOOL_MAX_CHARS, on any surface;
* any surface more than SURFACE_GROWTH_TOLERANCE over its recorded baseline.

Intended growth (new tools): re-record with
``uv run python scripts/tool_context_report.py --update`` and commit the
baseline, so the increase is visible in review.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from rootly_mcp_server.tool_context import (
    PER_TOOL_MAX_CHARS,
    SURFACE_GROWTH_TOLERANCE,
    SURFACES,
    measure_surface,
)

BASELINE = json.loads((Path(__file__).parent / "tool_context_baseline.json").read_text())
FIX_HINT = (
    "Shrink it (compact a large union with spec_transform.compact_large_unions, trim "
    "descriptions, move detail behind a lookup tool), or if the growth is intended run "
    "`uv run python scripts/tool_context_report.py --update` and commit the baseline."
)


@pytest.fixture(scope="module")
def measured():
    import asyncio

    return {surface: asyncio.run(measure_surface(surface)) for surface in SURFACES}


def test_every_surface_has_a_baseline():
    assert set(BASELINE) == set(SURFACES)


@pytest.mark.parametrize("surface", SURFACES)
def test_no_single_tool_exceeds_the_cap(measured, surface):
    over = [s for s in measured[surface] if s.chars > PER_TOOL_MAX_CHARS]
    assert not over, (
        f"{surface}: tools over {PER_TOOL_MAX_CHARS:,} chars: "
        + ", ".join(f"{s.name} ({s.chars:,} chars, ~{s.approx_tokens:,} tokens)" for s in over)
        + f". {FIX_HINT}"
    )


@pytest.mark.parametrize("surface", SURFACES)
def test_surface_stays_within_its_baseline(measured, surface):
    chars = sum(s.chars for s in measured[surface])
    budget = int(BASELINE[surface]["chars"] * (1 + SURFACE_GROWTH_TOLERANCE))
    largest = ", ".join(f"{s.name} ({s.chars:,})" for s in measured[surface][:5])
    assert chars <= budget, (
        f"{surface} tool list is {chars:,} chars (~{chars // 4:,} tokens), over its budget "
        f"of {budget:,} (baseline {BASELINE[surface]['chars']:,} + "
        f"{SURFACE_GROWTH_TOLERANCE:.0%}). Largest: {largest}. {FIX_HINT}"
    )


def test_workflow_task_tools_list_task_types_not_their_fields(measured):
    sizes = {s.name: s.chars for s in measured["hosted_full"]}
    # Was ~130k chars each with the 153-variant union inlined.
    assert sizes["create_workflow_task"] < 10_000
    assert sizes["update_workflow_task"] < 10_000
    assert "get_workflow_task_params_schema" in sizes


async def test_measurement_ignores_tool_list_settings_in_the_environment(monkeypatch, measured):
    from rootly_mcp_server.tool_context import measure_surface

    monkeypatch.setenv("ROOTLY_MCP_ENABLE_WRITE_TOOLS", "false")
    monkeypatch.setenv("ROOTLY_MCP_ENABLED_TOOLS", "get_user")

    sizes = await measure_surface("hosted_full")

    assert [s.name for s in sizes] == [s.name for s in measured["hosted_full"]]
    # Restored after measuring.
    assert os.environ["ROOTLY_MCP_ENABLED_TOOLS"] == "get_user"


def test_core_toolset_is_small(measured):
    # ?toolsets=core is the small surface we recommend; keep it small.
    chars = sum(s.chars for s in measured["hosted_core"])
    assert chars < 60_000, f"core toolset is {chars:,} chars"
    assert len(measured["hosted_core"]) <= 35
