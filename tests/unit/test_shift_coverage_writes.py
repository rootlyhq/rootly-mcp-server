"""Coverage requests use the real API contracts and the ordinary write gate."""

import json

import httpx2
import pytest
from fastmcp import Client

from rootly_mcp_server import toolsets
from rootly_mcp_server.server import _provider_tool_inventory, create_rootly_mcp_server
from rootly_mcp_server.spec_transform import _filter_openapi_spec
from rootly_mcp_server.toolsets import ToolSelection

SWAGGER_PATH = "src/rootly_mcp_server/data/swagger.json"
WRITE_TOOLS = {"create_shift_coverage_request", "delete_shift_coverage_request"}
READ_TOOLS = {"list_shift_coverage_requests", "get_shift_coverage_request"}


@pytest.fixture(autouse=True)
def configuration(monkeypatch):
    monkeypatch.setenv("ROOTLY_API_TOKEN", "test-token")
    for name in (
        "ROOTLY_MCP_ENABLED_TOOLS",
        "ROOTLY_MCP_TOOLSETS",
        "ROOTLY_MCP_HOSTED_TOOL_PROFILE",
    ):
        monkeypatch.delenv(name, raising=False)


@pytest.mark.parametrize("profile", ["full", "slim", "core"])
@pytest.mark.parametrize("writes", [True, False])
async def test_hosted_profiles_expose_coverage_writes_only_when_enabled(
    monkeypatch, profile, writes
):
    monkeypatch.setenv("ROOTLY_MCP_HOSTED_TOOL_PROFILE", profile)
    server = create_rootly_mcp_server(
        swagger_path=SWAGGER_PATH, hosted=True, enable_write_tools=writes
    )
    async with Client(server) as client:
        tools = {tool.name: tool for tool in await client.list_tools()}

    assert READ_TOOLS <= tools.keys()
    if writes:
        assert WRITE_TOOLS <= tools.keys()
        create_annotations = tools["create_shift_coverage_request"].annotations
        delete_annotations = tools["delete_shift_coverage_request"].annotations
        assert create_annotations is not None
        assert delete_annotations is not None
        assert create_annotations.read_only_hint is False
        assert create_annotations.idempotent_hint is False
        assert delete_annotations.destructive_hint is True
        assert delete_annotations.idempotent_hint is True
    else:
        assert not WRITE_TOOLS & tools.keys()


@pytest.mark.parametrize("tool", sorted(WRITE_TOOLS))
async def test_readonly_connection_cannot_discover_or_call_coverage_writes(tool):
    server = create_rootly_mcp_server(swagger_path=SWAGGER_PATH, hosted=False)
    reset = toolsets.requested_selection.set(ToolSelection(frozenset({"all"}), readonly=True))
    try:
        async with Client(server) as client:
            names = {tool.name for tool in await client.list_tools()}
            result = await client.call_tool(tool, {}, raise_on_error=False)
    finally:
        toolsets.requested_selection.reset(reset)

    assert not WRITE_TOOLS & names
    assert READ_TOOLS <= names
    assert result.is_error
    assert "read-only" in str(result.content)


@pytest.mark.parametrize("targeted", [False, True])
async def test_create_and_delete_use_the_real_paths_and_payloads(targeted):
    server = create_rootly_mcp_server(swagger_path=SWAGGER_PATH, hosted=False)
    _, provider, generated = _provider_tool_inventory(server)
    assert WRITE_TOOLS <= generated
    assert provider is not None
    sent = []
    created = [{"id": "request-1", "type": "shift_coverage_requests"}]

    async def send(request, **kwargs):
        sent.append(request)
        return httpx2.Response(
            201 if request.method == "POST" else 200,
            request=request,
            json={"data": created if request.method == "POST" else created[0]},
        )

    next(iter(provider._tools.values()))._client.client.send = send
    attributes = {
        "starts_at": "2026-12-01T08:00:00Z",
        "ends_at": "2026-12-01T16:00:00Z",
        "user_id": 123,
    }
    if targeted:
        attributes["recipient_user_ids"] = [456, 789]
    body = {"type": "shift_coverage_requests", "attributes": attributes}
    async with Client(server) as client:
        result = await client.call_tool(
            "create_shift_coverage_request", {"schedule_id": "schedule-1", "data": body}
        )
        assert not result.is_error
        assert result.structured_content is not None
        assert result.structured_content["data"] == created
        deleted = await client.call_tool("delete_shift_coverage_request", {"id": "request-1"})
        assert not deleted.is_error

    assert [(req.method, req.url.path) for req in sent] == [
        ("POST", "/v1/schedules/schedule-1/shift_coverage_requests"),
        ("DELETE", "/v1/shift_coverage_requests/request-1"),
    ]
    assert json.loads(sent[0].content) == {"data": body}


async def test_explicit_empty_delete_allowlist_still_disables_cancellation():
    server = create_rootly_mcp_server(
        swagger_path=SWAGGER_PATH, hosted=False, delete_allowed_paths=[]
    )
    _, _, generated = _provider_tool_inventory(server)

    assert "create_shift_coverage_request" in generated
    assert "delete_shift_coverage_request" not in generated
    assert "delete_schedule" not in generated


def test_disabled_writes_override_an_explicit_delete_allowlist():
    path = "/v1/shift_coverage_requests/{id}"
    spec = {
        "paths": {
            path: {"get": {"operationId": "getRequest"}, "delete": {"operationId": "deleteRequest"}}
        }
    }
    filtered = _filter_openapi_spec(
        spec, [path], delete_allowed_paths=[path], enable_write_tools=False
    )

    assert "get" in filtered["paths"][path]
    assert "delete" not in filtered["paths"][path]
