"""task_params compaction (spec_transform) and its lookup tool."""

from __future__ import annotations

import json
import os
from typing import Any

import pytest
from fastmcp import Client

from rootly_mcp_server.server import create_rootly_mcp_server
from rootly_mcp_server.spec_transform import (
    WORKFLOW_TASK_PARAMS_SCHEMA_TOOL,
    compact_large_unions,
)


def _variant(task_type: str, **fields: Any) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "task_type": {"type": "string", "enum": [task_type]},
            **{name: {"type": "string", "description": "x" * 60} for name in fields},
        },
        "required": sorted(fields),
    }


def _spec(variants: dict[str, dict[str, Any]], *, extra: list | None = None) -> dict[str, Any]:
    refs = [{"$ref": f"#/components/schemas/{name}"} for name in variants]
    return {
        "paths": {},
        "components": {
            "schemas": {
                **variants,
                "new_workflow_task": {
                    "type": "object",
                    "properties": {
                        "task_params": {
                            "oneOf": refs + (extra or []),
                            "description": "Task params.",
                        }
                    },
                },
            }
        },
    }


def _task_params(spec: dict[str, Any]) -> dict[str, Any]:
    return spec["components"]["schemas"]["new_workflow_task"]["properties"]["task_params"]


def test_large_union_is_replaced_by_its_discriminator_enum():
    variants = {f"t{i}_task_params": _variant(f"t{i}", a="", b="") for i in range(10)}
    spec = _spec(variants, extra=[{"type": "object", "additionalProperties": True}])

    found = compact_large_unions(spec, max_chars=500)

    compacted = _task_params(spec)
    assert "oneOf" not in compacted
    assert compacted["properties"]["task_type"]["enum"] == sorted(f"t{i}" for i in range(10))
    assert compacted["additionalProperties"] is True
    assert WORKFLOW_TASK_PARAMS_SCHEMA_TOOL in compacted["description"]
    assert compacted["description"].startswith("Task params. ")
    # The patched placeholder for a broken $ref is not a variant.
    assert set(found["task_type"]) == {f"t{i}" for i in range(10)}
    assert found["task_type"]["t3"]["required"] == ["a", "b"]


def test_small_union_stays_inline():
    spec = _spec({"t_task_params": _variant("t", a=""), "u_task_params": _variant("u", a="")})

    assert compact_large_unions(spec) == {}
    assert "oneOf" in _task_params(spec)


def test_union_without_a_lookup_tool_stays_inline():
    variants = {
        f"r{i}": {
            "type": "object",
            "properties": {"rule_type": {"enum": [f"r{i}"]}, "x": {"description": "y" * 200}},
        }
        for i in range(10)
    }
    spec = _spec(variants)

    assert compact_large_unions(spec, max_chars=100) == {}
    assert "oneOf" in _task_params(spec)


def test_variants_sharing_a_value_are_merged_not_dropped():
    # The real spec declares task_type create_google_docs_page on both the
    # Google Docs and the Quip variant.
    spec = _spec(
        {
            "docs_task_params": _variant("create_google_docs_page", title="", drive_id=""),
            "quip_task_params": _variant("create_google_docs_page", title="", quip_folder=""),
        }
    )

    found = compact_large_unions(spec, max_chars=100)

    merged = found["task_type"]["create_google_docs_page"]
    assert set(merged["properties"]) == {"task_type", "title", "drive_id", "quip_folder"}
    assert merged["required"] == ["title"]


@pytest.fixture
async def client():
    os.environ.setdefault("ROOTLY_API_TOKEN", "rootly_test")
    async with Client(create_rootly_mcp_server(hosted=True)) as c:
        yield c


async def test_bundled_workflow_task_tools_list_types_only(client):
    tools = {t.name: t for t in await client.list_tools()}
    for name in ("create_workflow_task", "update_workflow_task"):
        task_params = tools[name].inputSchema["properties"]["data"]["properties"]["attributes"][
            "properties"
        ]["task_params"]
        assert "send_slack_message" in task_params["properties"]["task_type"]["enum"]
        assert len(json.dumps(tools[name].inputSchema)) < 10_000


async def test_lookup_returns_one_types_fields(client):
    result = await client.call_tool(
        WORKFLOW_TASK_PARAMS_SCHEMA_TOOL, {"task_type": " Send_Slack_Message "}
    )

    body = result.structured_content
    assert body["task_type"] == "send_slack_message"
    assert "channels" in body["fields"]
    assert "text" in body["required"]


async def test_lookup_suggests_close_task_types(client):
    result = await client.call_tool(
        WORKFLOW_TASK_PARAMS_SCHEMA_TOOL, {"task_type": "send_slak_message"}
    )

    body = result.structured_content
    assert body["error"] is True
    assert "Did you mean: send_slack_message" in body["message"]
    assert "send_slack_message" in body["task_types"]


@pytest.mark.parametrize("allowlisted", ["create_workflow_task", "update_workflow_task"])
async def test_lookup_comes_with_a_workflow_task_tool_allowlist(allowlisted):
    os.environ.setdefault("ROOTLY_API_TOKEN", "rootly_test")
    server = create_rootly_mcp_server(hosted=True, enabled_tools={allowlisted, "get_user"})
    async with Client(server) as c:
        names = {t.name for t in await c.list_tools()}

    assert WORKFLOW_TASK_PARAMS_SCHEMA_TOOL in names


async def test_lookup_absent_without_workflow_task_tools():
    os.environ.setdefault("ROOTLY_API_TOKEN", "rootly_test")
    server = create_rootly_mcp_server(hosted=True, enabled_tools={"get_user"})
    async with Client(server) as c:
        names = {t.name for t in await c.list_tools()}

    assert WORKFLOW_TASK_PARAMS_SCHEMA_TOOL not in names
