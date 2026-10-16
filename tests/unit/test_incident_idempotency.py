"""Incident retry keys survive MCP validation, authentication and HTTP transport."""

import json

import httpx
import pytest
from fastmcp import Client

from rootly_mcp_server import server as server_module

pytestmark = pytest.mark.unit


@pytest.fixture
async def incident_server(monkeypatch, request):
    auth_source = getattr(request, "param", "hosted_header")
    monkeypatch.setenv("ROOTLY_API_TOKEN", "test-token")
    monkeypatch.setattr(
        "fastmcp.server.dependencies.get_http_headers",
        lambda: {"authorization": "Bearer test-token"} if auth_source == "hosted_header" else {},
    )
    monkeypatch.setattr(
        server_module,
        "_load_swagger_spec",
        lambda _: {
            "openapi": "3.0.0",
            "info": {"title": "Test", "version": "1"},
            "paths": {
                "/v1/incidents": {
                    "post": {
                        "operationId": "createIncident",
                        "responses": {"201": {"description": "Created"}},
                    }
                }
            },
        },
    )
    hosted = auth_source != "local"
    http_client = server_module.AuthenticatedHTTPXClient(hosted=hosted)
    monkeypatch.setattr(server_module, "AuthenticatedHTTPXClient", lambda **kwargs: http_client)
    token = server_module._session_auth_token.set(
        "Bearer test-token" if auth_source == "hosted_session" else ""
    )
    try:
        yield server_module.create_rootly_mcp_server(
            hosted=hosted, enable_write_tools=True, enabled_tools={"create_incident"}
        )
    finally:
        server_module._session_auth_token.reset(token)
        await http_client.client.aclose()


@pytest.mark.parametrize(
    "incident_server", ["local", "hosted_header", "hosted_session"], indirect=True
)
async def test_header_and_replay_survive_full_tool_path(incident_server, respx_mock):
    incident = {"data": {"id": "inc-1", "type": "incidents", "attributes": {"title": "Outage"}}}
    route = respx_mock.post("https://api.rootly.com/v1/incidents").mock(
        side_effect=[httpx.Response(status, json=incident) for status in (201, 200, 201)]
    )
    key = "k" * 255
    async with Client(incident_server) as client:
        for name in ("create_incident", "createIncident"):
            result = await client.call_tool(name, {"title": "Outage", "idempotency_key": key})
            assert not result.is_error
            assert result.data == incident
        await client.call_tool("create_incident", {"title": "Outage"})

    assert route.call_count == 3
    for call in route.calls:
        assert call.request.headers["authorization"] == "Bearer test-token"
        assert call.request.headers["content-type"] == "application/vnd.api+json"
        assert call.request.headers["accept"] == "application/vnd.api+json"
        assert json.loads(call.request.content) == {
            "data": {"type": "incidents", "attributes": {"title": "Outage"}}
        }
    assert [call.request.headers.get("idempotency-key") for call in route.calls] == [key, key, None]


async def test_schema_advertises_optional_key_and_retry_window(incident_server):
    async with Client(incident_server) as client:
        tools = {tool.name: tool for tool in await client.list_tools()}
    tool = tools["create_incident"]
    schema = tool.inputSchema
    field = schema["properties"]["idempotency_key"]
    assert {"type": "string"} in field["anyOf"]
    assert "idempotency_key" not in schema.get("required", [])
    assert field["default"] is None
    assert "Idempotency-Key" in field["description"]
    assert "24 hours" in field["description"]
    assert tool.annotations is not None and tool.annotations.idempotentHint is False


@pytest.mark.parametrize(
    "key",
    [
        "",
        "   ",
        "x" * 256,
        "clé-1",
        "key\r\nInjected: value",
        "key\x00",
        "key\x7f",
        "\nkey",
        "key\r",
        "\tkey",
        "\u00a0key",
    ],
)
async def test_invalid_keys_fail_without_sending_request(incident_server, respx_mock, key):
    async with Client(incident_server) as client:
        result = await client.call_tool(
            "create_incident", {"title": "Outage", "idempotency_key": key}
        )
    assert result.data["error_type"] == "validation_error"
    assert not respx_mock.calls


async def test_surrounding_spaces_are_trimmed(incident_server, respx_mock):
    route = respx_mock.post("https://api.rootly.com/v1/incidents").respond(201, json={"data": {}})
    async with Client(incident_server) as client:
        await client.call_tool(
            "create_incident", {"title": "Outage", "idempotency_key": "  key-1  "}
        )
    assert route.calls[0].request.headers["idempotency-key"] == "key-1"
