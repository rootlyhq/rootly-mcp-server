"""Generated /v1/incidents/{...} tools accept incident numbers as well as UUIDs.

Models pass "5883", "INC-5883" or "#5883" where the nested endpoints need the
incident's UUID; the API answered 404 (about 470 calls a week). These drive the
production server built from the bundled swagger with the HTTP client stubbed.
"""

from __future__ import annotations

from typing import Any

import httpx2
import pytest
from fastmcp import Client, FastMCP

from rootly_mcp_server.server import _provider_tool_inventory, create_rootly_mcp_server

pytestmark = pytest.mark.unit

SWAGGER_PATH = "src/rootly_mcp_server/data/swagger.json"
UUID = "0b9c7d2e-1f3a-4c5b-8d6e-7f8a9b0c1d2e"


@pytest.fixture
def server(monkeypatch) -> FastMCP:
    monkeypatch.setenv("ROOTLY_API_TOKEN", "test-token")
    return create_rootly_mcp_server(swagger_path=SWAGGER_PATH, hosted=False)


@pytest.fixture
def sent(server) -> list[httpx2.Request]:
    """Capture upstream requests; answer the sequential-id lookup for 5883 only."""
    _, provider, _ = _provider_tool_inventory(server)
    requests: list[httpx2.Request] = []

    async def fake_send(request, **kwargs):
        requests.append(request)
        if request.url.path == "/v1/incidents" and "filter[sequential_id]" in request.url.params:
            number = request.url.params["filter[sequential_id]"]
            data = (
                [{"id": UUID, "type": "incidents", "attributes": {"sequential_id": 5883}}]
                if number == "5883"
                else []
            )
            return httpx2.Response(200, request=request, json={"data": data})
        return httpx2.Response(200, request=request, json={"data": []})

    next(iter(provider._tools.values()))._client.client.send = fake_send
    return requests


async def _call(server: FastMCP, tool: str, arguments: dict[str, Any]) -> Any:
    async with Client(server) as client:
        return await client.call_tool(tool, arguments, raise_on_error=False)


def _text(result: Any) -> str:
    return " ".join(getattr(block, "text", "") for block in result.content)


def _paths(sent: list[httpx2.Request]) -> list[str]:
    return [request.url.path for request in sent]


@pytest.mark.parametrize("reference", ["5883", "INC-5883", "inc-5883", "#5883", 5883])
async def test_incident_number_is_resolved_to_the_uuid(server, sent, reference):
    result = await _call(server, "list_incident_events", {"incident_id": reference})

    assert not result.is_error, _text(result)
    assert _paths(sent)[-1] == f"/v1/incidents/{UUID}/events"
    assert sent[0].url.params["filter[sequential_id]"] == "5883"


@pytest.mark.parametrize(
    "tool", ["list_incident_action_items", "list_incident_form_field_selections"]
)
async def test_other_nested_incident_tools_resolve_too(server, sent, tool):
    result = await _call(server, tool, {"incident_id": "INC-5883"})

    assert not result.is_error, _text(result)
    assert _paths(sent)[-1].startswith(f"/v1/incidents/{UUID}/")


@pytest.mark.parametrize("reference", [UUID, "122-prod-pags"])
async def test_uuid_and_slug_pass_through_without_a_lookup(server, sent, reference):
    result = await _call(server, "list_incident_events", {"incident_id": reference})

    assert not result.is_error, _text(result)
    assert _paths(sent) == [f"/v1/incidents/{reference}/events"]


async def test_unknown_number_is_explained_before_the_api(server, sent):
    result = await _call(server, "list_incident_events", {"incident_id": "INC-404"})

    assert result.is_error
    text = _text(result)
    assert "Incident reference not found: INC-404" in text
    assert "pass the incident's UUID" in text
    assert _paths(sent) == ["/v1/incidents"]


async def test_tools_outside_incidents_are_untouched(server, sent):
    result = await _call(server, "get_alert", {"id": "5883"})

    assert not result.is_error, _text(result)
    assert _paths(sent) == ["/v1/alerts/5883"]


async def test_code_mode_calls_resolve_too(monkeypatch):
    from rootly_mcp_server.code_mode import create_rootly_codemode_server

    monkeypatch.setenv("ROOTLY_API_TOKEN", "test-token")
    code_mode = create_rootly_codemode_server(swagger_path=SWAGGER_PATH, hosted=False)
    _, provider, _ = _provider_tool_inventory(code_mode)
    sent: list[httpx2.Request] = []

    async def fake_send(request, **kwargs):
        sent.append(request)
        if "filter[sequential_id]" in request.url.params:
            data = [{"id": UUID, "type": "incidents", "attributes": {"sequential_id": 5883}}]
            return httpx2.Response(200, request=request, json={"data": data})
        return httpx2.Response(200, request=request, json={"data": []})

    next(iter(provider._tools.values()))._client.client.send = fake_send
    code = "return await call_tool('list_incident_events', {'incident_id': 'INC-5883'})"
    result = await _call(code_mode, "execute", {"code": code})

    assert not result.is_error, _text(result)
    assert _paths(sent)[-1] == f"/v1/incidents/{UUID}/events"
