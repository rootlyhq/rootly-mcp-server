"""Regressions for ROOTLY-MCP-7S: autogen tools called without their URL path argument.

Production telemetry showed `get_alert(alert_id=...)` (and `get_team(team_id=...)`,
`get_cause(cause_id=...)`, ...) against `/v1/<resource>/{id}` templates. FastMCP's
OpenAPI tools silently drop unknown arguments, so the `{id}` placeholder reached
the transport guard, which failed without naming the argument to pass.
"""

from __future__ import annotations

import re
from types import SimpleNamespace
from typing import Any, cast

import httpx2
import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError

from rootly_mcp_server.server import (
    PathArgumentValidationMiddleware,
    _openapi_path_arguments,
    _provider_tool_inventory,
    _resource_id_alias,
    create_rootly_mcp_server,
)
from rootly_mcp_server.spec_transform import (
    _filter_openapi_spec,
    _is_single_resource_path,
    _path_parameter_description,
    _singular_resource_name,
)

pytestmark = pytest.mark.unit


@pytest.fixture
def server(monkeypatch):
    monkeypatch.setenv("ROOTLY_API_TOKEN", "test-token")
    return create_rootly_mcp_server(hosted=False)


@pytest.fixture
def sent_requests(server) -> list[httpx2.Request]:
    """Capture every upstream request instead of hitting the network."""
    _, provider, _ = _provider_tool_inventory(server)
    sent: list[httpx2.Request] = []

    async def fake_send(request, **kwargs):
        sent.append(request)
        return httpx2.Response(200, request=request, json={"data": {"id": "abc", "attributes": {}}})

    # Every autogen tool shares the same wrapper client.
    next(iter(provider._tools.values()))._client.client.send = fake_send
    return sent


def _error_text(result: Any) -> str:
    assert result.is_error, result
    return " ".join(getattr(block, "text", "") for block in result.content)


async def _schema(server, name: str) -> dict[str, Any]:
    async with Client(server) as client:
        tools = {tool.name: tool for tool in await client.list_tools()}
    return tools[name].input_schema


class TestGetAlertSchema:
    async def test_id_is_required_and_names_itself(self, server):
        schema = await _schema(server, "get_alert")
        assert "id" in schema["required"]
        description = schema["properties"]["id"]["description"]
        assert "Pass it as `id`" in description
        assert "`alert_id`" in description

    async def test_single_resource_get_does_not_advertise_list_arguments(self, server):
        # Pagination/filter arguments made get_alert look like list_alerts; models
        # then called it with `page_size`/`fields_alerts` and no `id`.
        properties = (await _schema(server, "get_alert"))["properties"]
        assert not {"page_size", "page_number"} & set(properties)
        assert not [name for name in properties if name.startswith("filter_")]

    async def test_collection_get_keeps_pagination_and_filters(self, server):
        properties = (await _schema(server, "list_alerts"))["properties"]
        assert {"page_size", "page_number", "filter_status"} <= set(properties)


class TestGetAlertCalls:
    @pytest.mark.parametrize(
        "arguments,rejected",
        [
            ({"short_id": "ABC-1"}, ["short_id"]),
            ({"include": "notified_users"}, []),
            ({}, []),
            ({"id": ""}, []),
            ({"id": None, "include": "notified_users"}, []),
        ],
    )
    async def test_missing_id_is_rejected_before_any_request(
        self, server, sent_requests, arguments, rejected
    ):
        async with Client(server) as client:
            result = await client.call_tool("get_alert", arguments, raise_on_error=False)

        text = _error_text(result)
        assert "get_alert requires the path argument 'id'" in text
        assert "GET /v1/alerts/{id}" in text
        assert 'Retry with {"id": "<value>"}' in text
        for name in rejected:
            assert f"does not accept '{name}'" in text
        assert "unfilled path parameter" not in text
        assert sent_requests == []

    @pytest.mark.parametrize("placeholder", ["null", "None", "undefined", " null "])
    async def test_placeholder_id_is_rejected_before_any_request(
        self, server, sent_requests, placeholder
    ):
        # ROOTLY-MCP-3C: `get_user(id="null")` went out as GET /v1/users/null
        # and came back as a bare 404.
        async with Client(server) as client:
            result = await client.call_tool("get_user", {"id": placeholder}, raise_on_error=False)

        text = _error_text(result)
        assert "get_user requires the path argument 'id' for GET /v1/users/{id}" in text
        assert f"got id={placeholder!r} -- a placeholder, not an ID" in text
        assert "not provided" not in text
        assert sent_requests == []

    @pytest.mark.parametrize("alert_id", ["abc", 123, "none"])
    async def test_id_fills_the_path(self, server, sent_requests, alert_id):
        async with Client(server) as client:
            result = await client.call_tool(
                "get_alert", {"id": alert_id, "include": "notified_users"}, raise_on_error=False
            )

        assert not result.is_error, result
        assert len(sent_requests) == 1
        url = sent_requests[0].url
        assert url.path == f"/v1/alerts/{alert_id}"
        assert url.params.get("include") == "notified_users"


class TestPathArgumentDotSegments:
    """fastmcp 4.1 rejects `.`/`..` path segments (PrefectHQ/fastmcp#5457).

    Rootly path IDs are UUIDs or slugs matching ^[a-z0-9_-]+$, so no real ID is
    a dot segment; a traversal attempt must fail readably and send nothing.
    """

    @pytest.mark.parametrize("alert_id", ["9f1c2d3e-4b5a-6c7d-8e9f-0a1b2c3d4e5f", "my-alert_1"])
    async def test_real_ids_still_fill_the_path(self, server, sent_requests, alert_id):
        async with Client(server) as client:
            result = await client.call_tool("get_alert", {"id": alert_id}, raise_on_error=False)

        assert not result.is_error, result
        assert [request.url.path for request in sent_requests] == [f"/v1/alerts/{alert_id}"]

    @pytest.mark.parametrize("alert_id", ["..", ".", "../users", "%2E%2E", "%252e%252e"])
    async def test_dot_segments_are_rejected_before_any_request(
        self, server, sent_requests, alert_id
    ):
        async with Client(server) as client:
            result = await client.call_tool("get_alert", {"id": alert_id}, raise_on_error=False)

        text = _error_text(result)
        assert "GET /v1/alerts/{id}" in text
        assert "Path parameter 'id' cannot contain dot segments" in text
        assert sent_requests == []


@pytest.mark.parametrize(
    "tool_name,arguments,path",
    [
        # Argument shapes observed in production (Sentry, 2026-10-23).
        ("get_alert", {"alert_id": "abc", "include": "notified_users"}, "/v1/alerts/abc"),
        ("get_team", {"team_id": "t-1"}, "/v1/teams/t-1"),
        ("get_cause", {"cause_id": "c-1"}, "/v1/causes/c-1"),
        ("get_schedule", {"schedule_id": "s-1"}, "/v1/schedules/s-1"),
        ("get_service", {"service_id": "svc-1"}, "/v1/services/svc-1"),
        ("get_user", {"user_id": 42}, "/v1/users/42"),
    ],
)
async def test_resource_named_id_fills_the_path(server, sent_requests, tool_name, arguments, path):
    async with Client(server) as client:
        result = await client.call_tool(tool_name, arguments, raise_on_error=False)

    assert not result.is_error, result
    assert [request.url.path for request in sent_requests] == [path]


async def test_other_resources_id_is_still_rejected(server, sent_requests):
    # /v1/action_items/{id}: an incident_id names a different resource.
    async with Client(server) as client:
        result = await client.call_tool(
            "get_incident_action_items", {"incident_id": "i-1"}, raise_on_error=False
        )

    text = _error_text(result)
    assert "get_incident_action_items requires the path argument 'id'" in text
    assert "does not accept 'incident_id'" in text
    assert sent_requests == []


async def test_explicit_id_wins_over_resource_named_id(server, sent_requests):
    async with Client(server) as client:
        result = await client.call_tool(
            "get_alert", {"id": "real", "alert_id": "other"}, raise_on_error=False
        )

    assert not result.is_error, result
    assert [request.url.path for request in sent_requests] == ["/v1/alerts/real"]


async def test_nested_path_argument_is_reported_by_name(server, sent_requests):
    async with Client(server) as client:
        result = await client.call_tool("list_incident_events", {"id": "i-1"}, raise_on_error=False)

    text = _error_text(result)
    assert "requires the path argument 'incident_id'" in text
    assert "/v1/incidents/{incident_id}/events" in text
    assert sent_requests == []


async def test_every_path_argument_is_required_and_described(server):
    """Systemic guard: no autogen tool may expose an undocumented or optional path arg."""
    _, provider, names = _provider_tool_inventory(server)
    async with Client(server) as client:
        listed = {tool.name: tool for tool in await client.list_tools()}

    checked = 0
    for name in sorted(names & set(listed)):
        route_info = _openapi_path_arguments(provider._tools[name])
        if route_info is None:
            continue
        _, path, path_args = route_info
        assert len(path_args) == len(re.findall(r"\{[^}]+\}", path)), (name, path)
        schema = listed[name].input_schema
        for arg in path_args:
            assert arg in schema["required"], (name, arg)
            assert schema["properties"][arg].get("description"), (name, arg)
        checked += 1
    assert checked > 100


def test_curated_tools_are_not_route_checked():
    assert _openapi_path_arguments(object()) is None


class TestSpecHelpers:
    @pytest.mark.parametrize(
        "path,expected",
        [
            ("/v1/alerts/{id}", True),
            ("/v1/alerts/{id}/", True),
            ("/v1/alerts", False),
            ("/v1/incidents/{incident_id}/alerts", False),
        ],
    )
    def test_is_single_resource_path(self, path, expected):
        assert _is_single_resource_path(path) is expected

    @pytest.mark.parametrize(
        "segment,expected",
        [
            ("alerts", "alert"),
            ("escalation_policies", "escalation policy"),
            ("status-pages", "status page"),
            ("alert_urgencies", "alert urgency"),
            ("sub_statuses", "sub status"),
            ("dashboard_panels", "dashboard panel"),
        ],
    )
    def test_singular_resource_name(self, segment, expected):
        assert _singular_resource_name(segment) == expected

    def test_descriptions(self):
        assert _path_parameter_description("/v1/teams/{id}", "id") == (
            "Required. ID of the team (fills `{id}` in /v1/teams/{id}). "
            "Pass it as `id` -- this tool has no `team_id` argument."
        )
        assert _path_parameter_description("/v1/incidents/{incident_id}/events", "incident_id") == (
            "Required. ID of the incident (fills `{incident_id}` in /v1/incidents/{incident_id}/events)."
        )

    def test_existing_descriptions_are_kept(self):
        spec = {
            "openapi": "3.0.0",
            "info": {"title": "t", "version": "1"},
            "paths": {
                "/alerts/{id}": {
                    "parameters": [
                        {"name": "id", "in": "path", "required": True, "description": "Mine"}
                    ],
                    "get": {
                        "operationId": "getAlert",
                        "parameters": [{"name": "other", "in": "path", "required": True}],
                    },
                }
            },
        }
        filtered = _filter_openapi_spec(spec, ["/alerts/{id}"])
        path_item = filtered["paths"]["/alerts/{id}"]
        assert path_item["parameters"][0]["description"] == "Mine"
        assert path_item["get"]["parameters"][0]["description"].startswith("Required.")
        names = {param["name"] for param in path_item["get"]["parameters"]}
        assert "page[size]" not in names
        assert "filter[status]" not in names


def test_several_candidate_ids_are_not_chosen_between():
    # causes -> cause / caus: two different nonblank ids mean nothing is guessed.
    assert _resource_id_alias("/v1/causes/{id}", "id", {"cause_id": "a", "caus_id": "b"}) is None
    assert _resource_id_alias("/v1/causes/{id}", "id", {"cause_id": "a", "caus_id": ""}) == (
        "cause_id"
    )


async def test_declared_resource_id_parameter_is_never_consumed():
    """A tool that declares `alert_id` itself keeps it; the path error still fires."""
    route = SimpleNamespace(
        path="/v1/alerts/{id}",
        method="GET",
        parameter_map={"id": {"location": "path"}, "alert_id": {"location": "query"}},
    )
    tool = SimpleNamespace(
        _route=route,
        parameters={"properties": {"id": {}, "alert_id": {}}},
    )

    async def get_tool(_name):
        return tool

    message = SimpleNamespace(name="get_alert", arguments={"alert_id": "a1"})
    context = SimpleNamespace(
        message=message,
        fastmcp_context=SimpleNamespace(fastmcp=SimpleNamespace(get_tool=get_tool)),
    )

    async def call_next(context: Any) -> Any:
        raise AssertionError("the call must not proceed without its path argument")

    with pytest.raises(ToolError, match="requires the path argument 'id'"):
        await PathArgumentValidationMiddleware().on_call_tool(cast(Any, context), call_next)
    assert message.arguments == {"alert_id": "a1"}
