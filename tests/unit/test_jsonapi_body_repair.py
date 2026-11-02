"""JSON:API envelope repair for autogen write tools (JsonApiBodyRepairMiddleware).

Sentry showed ``attach_alert`` failing with Rootly's 400 "Invalid payload:
alert_ids must be nested under data.attributes" and "data: Expected hash":
models put attributes straight on ``data``, wrap it twice, or send it as a
JSON string. These drive the production server built from the bundled
swagger with the HTTP client stubbed and check the body that goes upstream.
"""

from __future__ import annotations

import json
from typing import Any

import httpx2
import pytest
from fastmcp import Client, FastMCP

from rootly_mcp_server.server import _provider_tool_inventory, create_rootly_mcp_server
from rootly_mcp_server.tool_arguments import repair_jsonapi_body

pytestmark = pytest.mark.unit

SWAGGER_PATH = "src/rootly_mcp_server/data/swagger.json"
INCIDENT_ID = "0b9c7d2e-1f3a-4c5b-8d6e-7f8a9b0c1d2e"
EXPECTED_BODY = {"data": {"type": "alerts", "attributes": {"alert_ids": ["a1", "a2"]}}}


@pytest.fixture
def server(monkeypatch) -> FastMCP:
    monkeypatch.setenv("ROOTLY_API_TOKEN", "test-token")
    return create_rootly_mcp_server(swagger_path=SWAGGER_PATH, hosted=False)


@pytest.fixture
def sent(server) -> list[httpx2.Request]:
    """Capture every upstream request instead of hitting the network."""
    _, provider, _ = _provider_tool_inventory(server)
    requests: list[httpx2.Request] = []

    async def fake_send(request, **kwargs):
        requests.append(request)
        return httpx2.Response(200, request=request, json={"data": {}})

    next(iter(provider._tools.values()))._client.client.send = fake_send
    return requests


def _text(result: Any) -> str:
    return " ".join(getattr(block, "text", "") for block in result.content)


async def _attach(server: FastMCP, data: Any) -> Any:
    async with Client(server) as client:
        return await client.call_tool(
            "attach_alert", {"incident_id": INCIDENT_ID, "data": data}, raise_on_error=False
        )


def _sent_body(sent: list[httpx2.Request]) -> dict[str, Any]:
    requests = [r for r in sent if r.url.path == f"/v1/incidents/{INCIDENT_ID}/alerts"]
    assert requests, [str(r.url) for r in sent]
    return json.loads(requests[-1].content)


@pytest.mark.parametrize(
    "data",
    [
        pytest.param({"alert_ids": ["a1", "a2"]}, id="attributes-on-data"),
        pytest.param({"type": "alerts", "alert_ids": ["a1", "a2"]}, id="typed-attributes-on-data"),
        pytest.param({"data": EXPECTED_BODY["data"]}, id="wrapped-twice"),
        pytest.param(json.dumps(EXPECTED_BODY["data"]), id="json-string"),
        pytest.param({"attributes": {"alert_ids": ["a1", "a2"]}}, id="missing-type"),
    ],
)
async def test_misshapen_attach_alert_bodies_are_repaired(server, sent, data):
    result = await _attach(server, data)

    assert not result.is_error, _text(result)
    assert _sent_body(sent) == EXPECTED_BODY


async def test_malformed_json_string_names_where_it_broke(server, sent):
    # ROOTLY-MCP-2MF: a stringified body one closing brace short went upstream
    # unchanged and came back as Rootly's "data: Expected hash".
    data = json.dumps(EXPECTED_BODY["data"])[:-1]

    result = await _attach(server, data)

    assert result.is_error
    text = _text(result)
    assert "attach_alert: `data` must be a JSON object" in text
    assert f"at character {len(data)} of {len(data)}" in text
    assert '{"type": "alerts", "attributes": {}}' in text
    assert sent == []


async def test_well_formed_body_is_sent_unchanged(server, sent):
    result = await _attach(server, EXPECTED_BODY["data"])

    assert not result.is_error, _text(result)
    assert _sent_body(sent) == EXPECTED_BODY


def test_undeclared_keys_on_data_are_left_for_the_api():
    schema = {
        "type": "object",
        "properties": {
            "data": {
                "type": "object",
                "properties": {
                    "type": {"enum": ["alerts"]},
                    "attributes": {"type": "object", "properties": {"alert_ids": {}}},
                },
            }
        },
    }

    repaired, rewrites = repair_jsonapi_body({"data": {"alert_ids": ["a1"], "bogus": 1}}, schema)

    assert repaired == {"data": {"alert_ids": ["a1"], "bogus": 1}}
    assert rewrites == []


def test_conflicting_attribute_is_not_overwritten():
    schema = {
        "type": "object",
        "properties": {
            "data": {
                "type": "object",
                "properties": {
                    "type": {"enum": ["alerts"]},
                    "attributes": {"type": "object", "properties": {"alert_ids": {}}},
                },
            }
        },
    }
    data = {"alert_ids": ["a1"], "attributes": {"alert_ids": ["a2"]}}

    repaired, rewrites = repair_jsonapi_body({"data": data}, schema)

    assert repaired["data"] == data
    assert rewrites == []


def test_tools_without_a_jsonapi_body_are_untouched():
    arguments = {"data": '{"x": 1}'}

    repaired, rewrites = repair_jsonapi_body(arguments, {"properties": {"data": {}}})

    assert repaired == arguments
    assert rewrites == []


# create_workflow_run: data.attributes is an anyOf (one branch per trigger kind)
# with no top-level properties, so the repair used to find no attributes and
# skip the tool entirely (ROOTLY-MCP-2XG).

WORKFLOW_ID = "5f0c8a1e-2b3d-4e5f-9a0b-1c2d3e4f5a6b"


def _sent_workflow_run_body(sent: list[httpx2.Request]) -> dict[str, Any]:
    requests = [r for r in sent if r.url.path == f"/v1/workflows/{WORKFLOW_ID}/workflow_runs"]
    assert requests, [str(r.url) for r in sent]
    return json.loads(requests[-1].content)


async def test_workflow_run_attributes_on_data_are_nested(server, sent):
    async with Client(server) as client:
        result = await client.call_tool(
            "create_workflow_run",
            {"workflow_id": WORKFLOW_ID, "data": {"incident_id": INCIDENT_ID, "immediate": True}},
            raise_on_error=False,
        )
    assert not result.is_error, _text(result)
    assert _sent_workflow_run_body(sent) == {
        "data": {
            "type": "workflow_runs",
            "attributes": {"incident_id": INCIDENT_ID, "immediate": True},
        }
    }


async def test_workflow_run_unknown_key_is_left_for_the_api(server, sent):
    # Only keys some anyOf branch declares are moved.
    async with Client(server) as client:
        await client.call_tool(
            "create_workflow_run",
            {"workflow_id": WORKFLOW_ID, "data": {"incident_id": INCIDENT_ID, "bogus": 1}},
            raise_on_error=False,
        )
    assert _sent_workflow_run_body(sent)["data"] == {"incident_id": INCIDENT_ID, "bogus": 1}


# A single resource sent as a one-item array reached Rootly unchanged and came
# back as the opaque "data: Expected hash" (ROOTLY-MCP-1FE).


@pytest.mark.parametrize(
    "data",
    [
        [{"type": "alerts", "attributes": {"alert_ids": ["a1", "a2"]}}],
        json.dumps([{"type": "alerts", "attributes": {"alert_ids": ["a1", "a2"]}}]),
        [{"alert_ids": ["a1", "a2"]}],
    ],
)
async def test_one_item_array_is_unwrapped(server, sent, data):
    result = await _attach(server, data)
    assert not result.is_error, _text(result)
    assert _sent_body(sent) == EXPECTED_BODY


@pytest.mark.parametrize(
    ("data", "kind"),
    [
        ([{"alert_ids": ["a1"]}, {"alert_ids": ["a2"]}], "an array of 2 items"),
        ("[]", "an array of 0 items"),
        ("42", "a number"),
    ],
)
async def test_non_object_data_is_rejected_before_the_api(server, sent, data, kind):
    result = await _attach(server, data)
    assert result.is_error
    text = _text(result)
    assert "must be a single JSON object" in text
    assert kind in text
    assert sent == []
