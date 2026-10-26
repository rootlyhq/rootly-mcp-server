"""Direct tools/call argument repair for curated tools (ToolArgumentAliasMiddleware).

Sentry showed ~43 direct ``list_incidents`` calls a day failing with pydantic's
bare "Unexpected keyword argument": models send the raw Rootly API's query
shapes (``filter[status]``, ``page[size]``, ``filter_started_at={"gte": ...}``,
``filters={...}``) or near-miss names (``search``, ``start_time``). These drive
the production server built from the bundled swagger with the HTTP client
stubbed, so each rewrite is checked end to end: the call succeeds and the
upstream request carries the filter the caller meant.
"""

from __future__ import annotations

import json
import logging
from typing import Annotated, Any

import httpx2
import pytest
from fastmcp import Client, FastMCP
from pydantic import AliasChoices, Field

from rootly_mcp_server.server import (
    ToolArgumentAliasMiddleware,
    _provider_tool_inventory,
    create_rootly_mcp_server,
)
from rootly_mcp_server.tool_arguments import (
    normalize_tool_arguments,
    split_null_arguments,
    unknown_arguments_message,
    validation_aliases,
)

pytestmark = pytest.mark.unit

SWAGGER_PATH = "src/rootly_mcp_server/data/swagger.json"


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
        return httpx2.Response(200, request=request, json={"data": [], "meta": {}})

    # Curated and autogen tools share the same wrapper client.
    next(iter(provider._tools.values()))._client.client.send = fake_send
    return requests


def _text(result: Any) -> str:
    return " ".join(getattr(block, "text", "") for block in result.content)


async def _call(server: FastMCP, tool: str, arguments: dict[str, Any]) -> Any:
    async with Client(server) as client:
        return await client.call_tool(tool, arguments, raise_on_error=False)


def _incident_params(sent: list[httpx2.Request]) -> dict[str, str]:
    requests = [r for r in sent if r.url.path == "/v1/incidents"]
    assert requests, [str(r.url) for r in sent]
    return dict(requests[-1].url.params)


# ---------------------------------------------------------------------------
# Every Sentry shape, end to end against list_incidents
# ---------------------------------------------------------------------------

_TS1 = "2026-10-01T00:00:00Z"
_TS2 = "2026-10-21T00:00:00Z"
_STRICT_AFTER = "started_after is inclusive (>=); there is no strict-after filter"
_STRICT_BEFORE = "started_before is inclusive (<=); there is no strict-before filter"


@pytest.mark.parametrize(
    ("arguments", "expected"),
    [
        ({"filter[status]": "resolved"}, {"filter[status]": "resolved"}),
        ({"filter_status": "resolved"}, {"filter[status]": "resolved"}),
        ({"page[size]": 5}, {"page[size]": "5"}),
        ({"page[number]": 3}, {"page[number]": "3"}),
        ({"filter_started_at_gte": _TS1}, {"filter[started_at][gte]": _TS1}),
        ({"filter[started_at][gte]": _TS1}, {"filter[started_at][gte]": _TS1}),
        (
            {"filter_started_at": {"gte": _TS1, "lte": _TS2}},
            {"filter[started_at][gte]": _TS1, "filter[started_at][lte]": _TS2},
        ),
        (
            {"start_time": _TS1, "end_time": _TS2},
            {"filter[started_at][gte]": _TS1, "filter[started_at][lte]": _TS2},
        ),
        ({"filter_services": ["svc-1", "svc-2"]}, {"filter[service_ids]": "svc-1,svc-2"}),
        ({"filter_service_ids": ["svc-1", "svc-2"]}, {"filter[service_ids]": "svc-1,svc-2"}),
        ({"service_ids": ["svc-1", "svc-2"]}, {"filter[service_ids]": "svc-1,svc-2"}),
        (
            {
                "filters": {
                    "status": "resolved",
                    "started_at": {"gte": _TS1},
                    "service_ids": ["svc-1"],
                }
            },
            {
                "filter[status]": "resolved",
                "filter[started_at][gte]": _TS1,
                "filter[service_ids]": "svc-1",
            },
        ),
        (
            {"filter": {"filter[status]": "mitigated", "page[size]": 7}},
            {"filter[status]": "mitigated", "page[size]": "7"},
        ),
        ({"search": "database"}, {"filter[search]": "database"}),
        ({"max_results": 4}, {"page[size]": "4"}),
    ],
    ids=[
        "filter[status]",
        "filter_status",
        "page[size]",
        "page[number]",
        "filter_started_at_gte",
        "filter[started_at][gte]",
        "filter_started_at-range-dict",
        "start_time+end_time",
        "filter_services-list",
        "filter_service_ids-list",
        "service_ids-list",
        "filters-dict",
        "filter-dict-of-api-keys",
        "search",
        "max_results",
    ],
)
async def test_sentry_shapes_are_rewritten_and_the_call_succeeds(server, sent, arguments, expected):
    result = await _call(server, "list_incidents", arguments)

    assert not result.is_error, _text(result)
    params = _incident_params(sent)
    for key, value in expected.items():
        assert params.get(key) == value, (key, params)


async def test_max_results_reaches_page_size_on_a_tool_without_its_own_alias(server, sent):
    # list_incident_post_mortems takes page_size but declares no max_results
    # alias, so the generic repair has to cover it.
    result = await _call(server, "list_incident_post_mortems", {"max_results": 5})

    assert not result.is_error, _text(result)
    requests = [r for r in sent if r.url.path == "/v1/post_mortems"]
    assert requests, [str(r.url) for r in sent]
    assert dict(requests[-1].url.params).get("page[size]") == "5"


async def test_max_results_inside_a_filters_dict_reaches_page_size(server, sent):
    # Keys inside filters={...} never see pydantic validation aliases.
    result = await _call(server, "list_incidents", {"filters": {"max_results": 6}})

    assert not result.is_error, _text(result)
    assert _incident_params(sent).get("page[size]") == "6"


async def test_schedule_shift_names_reach_the_tools_own_parameters(server, sent):
    # Production Code Mode calls: schedule_id for the schedule's id, and the
    # list_incidents-style start_time/end_time for the shift range.
    result = await _call(
        server,
        "get_schedule_shifts",
        {"schedule_id": "sched-1", "start_time": _TS1, "end_time": _TS2},
    )

    assert not result.is_error, _text(result)
    requests = [r for r in sent if r.url.path == "/v1/schedules/sched-1/shifts"]
    assert requests, [str(r.url) for r in sent]
    for request in requests:
        assert request.url.params.get("from") == _TS1
        assert request.url.params.get("to") == _TS2


async def test_page_size_reaches_collect_incidents_batch_size(server, sent):
    result = await _call(server, "collect_incidents", {"page_size": 25, "max_results": 25})

    assert not result.is_error, _text(result)
    assert _incident_params(sent).get("page[size]") == "25"


def test_resource_id_maps_to_id_only_when_the_tool_names_that_resource():
    schema = {
        "type": "object",
        "properties": {"id": {"type": "string"}},
        "additionalProperties": False,
    }

    own = normalize_tool_arguments({"incident_id": "i1"}, schema, tool_name="get_incident")
    other = normalize_tool_arguments({"user_id": "u1"}, schema, tool_name="get_incident")
    unnamed = normalize_tool_arguments({"incident_id": "i1"}, schema)

    assert own.arguments == {"id": "i1"}
    assert set(other.unknown) == {"user_id"}
    assert set(unnamed.unknown) == {"incident_id"}


async def test_alias_and_parameter_with_the_same_value_are_deduplicated(server, sent):
    # Sentry ROOTLY-MCP-18: pydantic rejects max_results as unexpected when
    # page_size is also passed, even with the same value.
    result = await _call(server, "list_incidents", {"max_results": 5, "page_size": 5})

    assert not result.is_error, _text(result)
    assert _incident_params(sent).get("page[size]") == "5"


async def test_alias_and_parameter_that_disagree_are_reported(server, sent):
    result = await _call(server, "list_incidents", {"max_results": 5, "page_size": 10})

    assert result.is_error
    assert "'max_results' (conflicts with 'page_size', which is also set)" in _text(result)
    assert not [r for r in sent if r.url.path == "/v1/incidents"]


async def test_bare_id_reaches_the_tools_own_resource_id(server, sent):
    incident_id = "0b9c7d2e-1f3a-4c5b-8d6e-7f8a9b0c1d2e"
    result = await _call(server, "update_incident", {"id": incident_id, "title": "New title"})

    assert not result.is_error, _text(result)
    paths = [r.url.path for r in sent]
    assert f"/v1/incidents/{incident_id}" in paths, paths


def test_bare_id_is_left_alone_when_the_resource_is_ambiguous():
    schema = {
        "type": "object",
        "properties": {"incident_id": {}, "alert_id": {}},
        "additionalProperties": False,
    }

    result = normalize_tool_arguments({"id": "x"}, schema, tool_name="link_incident_alert")

    assert set(result.unknown) == {"id"}


async def test_shift_field_names_reach_the_shift_range(server, sent):
    result = await _call(
        server, "get_schedule_shifts", {"id": "sched-1", "starts_at": _TS1, "ends_at": _TS2}
    )

    assert not result.is_error, _text(result)
    requests = [r for r in sent if r.url.path == "/v1/schedules/sched-1/shifts"]
    assert requests, [str(r.url) for r in sent]
    for request in requests:
        assert request.url.params.get("from") == _TS1
        assert request.url.params.get("to") == _TS2


@pytest.mark.parametrize(
    ("tool", "arguments", "path", "expected"),
    [
        ("list_audits", {"page_size": 7}, "/v1/audits", {"page[size]": "7"}),
        ("list_incidents", {"page": 3}, "/v1/incidents", {"page[number]": "3"}),
    ],
)
async def test_sentry_tail_names_reach_the_real_parameter(
    server, sent, tool, arguments, path, expected
):
    result = await _call(server, tool, arguments)

    assert not result.is_error, _text(result)
    requests = [r for r in sent if r.url.path == path]
    assert requests, [str(r.url) for r in sent]
    params = dict(requests[0].url.params)
    for key, value in expected.items():
        assert params.get(key) == value, (key, params)


def test_shift_window_names_reach_list_shifts_range():
    schema = {
        "type": "object",
        "properties": {"from_date": {}, "to_date": {}},
        "additionalProperties": False,
    }

    result = normalize_tool_arguments({"starts_after": _TS1, "ends_before": _TS2}, schema)

    assert result.arguments == {"from_date": _TS1, "to_date": _TS2}


def test_ends_before_never_becomes_an_incident_start_bound():
    schema = {
        "type": "object",
        "properties": {"started_after": {}, "started_before": {}},
        "additionalProperties": False,
    }

    result = normalize_tool_arguments({"ends_before": _TS2}, schema)

    assert set(result.unknown) == {"ends_before"}
    assert "started_before" not in result.arguments


def test_start_date_reaches_shift_incidents_start_time():
    # get_shift_incidents takes start_time/end_time, not start_date/end_date.
    schema = {
        "type": "object",
        "properties": {"start_time": {}, "end_time": {}},
        "additionalProperties": False,
    }

    result = normalize_tool_arguments({"start_date": _TS1, "end_date": _TS2}, schema)

    assert result.arguments == {"start_time": _TS1, "end_time": _TS2}
    assert not result.unknown


async def test_explicit_value_is_never_overwritten(server, sent):
    result = await _call(
        server, "list_incidents", {"status": "resolved", "filter[status]": "resolved"}
    )
    assert not result.is_error, _text(result)
    assert _incident_params(sent)["filter[status]"] == "resolved"

    result = await _call(
        server, "list_incidents", {"status": "resolved", "filter_status": "started"}
    )
    text = _text(result)
    assert result.is_error
    assert "'filter_status' (conflicts with 'status', which is also set)" in text
    # Nothing reached the API with either value.
    assert len([r for r in sent if r.url.path == "/v1/incidents"]) == 1


async def test_range_dict_does_not_overwrite_an_explicit_bound(server, sent):
    result = await _call(
        server,
        "list_incidents",
        {"started_after": _TS1, "filter_started_at": {"gte": _TS2, "lte": _TS2}},
    )

    assert result.is_error
    assert "conflicts with 'started_after'" in _text(result)
    assert not sent


# ---------------------------------------------------------------------------
# What cannot be mapped is explained, never guessed
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("arguments", "expected"),
    [
        (
            {"filter_updated_at": {"gte": _TS1}},
            "'filter_updated_at' (there is no updated-time filter)",
        ),
        (
            {"filter[updated_at][gte]": _TS1},
            "'filter[updated_at][gte]' (there is no updated-time filter)",
        ),
        (
            {"filters": {"updated_at": {"gte": _TS1}}},
            "'filters.updated_at' (there is no updated-time filter)",
        ),
        ({"include": "services"}, "'include' (related records cannot be side-loaded"),
        (
            {"filter_started_at": _TS1},
            "'filter_started_at' (use 'started_after' and/or 'started_before'",
        ),
        ({"since": _TS1}, "'since' (did you mean 'started_after'?)"),
        ({"bogus": 1}, "list_incidents does not accept 'bogus'."),
        # Strict bounds: started_after/started_before are inclusive, so a
        # gt/lt cannot be rewritten without widening the range.
        ({"filter[started_at][gt]": _TS1}, f"'filter[started_at][gt]' ({_STRICT_AFTER}"),
        ({"filter[started_at][lt]": _TS2}, f"'filter[started_at][lt]' ({_STRICT_BEFORE}"),
        ({"filter_started_at_gt": _TS1}, f"'filter_started_at_gt' ({_STRICT_AFTER}"),
        ({"started_at_lt": _TS2}, f"'started_at_lt' ({_STRICT_BEFORE}"),
        ({"filter_started_at": {"gt": _TS1}}, f"'filter_started_at' ({_STRICT_AFTER}"),
        (
            {"filter_started_at": {"gte": _TS1, "lt": _TS2}},
            f"'filter_started_at' ({_STRICT_BEFORE}",
        ),
        ({"filters": {"started_at": {"gt": _TS1}}}, f"'filters.started_at' ({_STRICT_AFTER}"),
    ],
)
async def test_unmappable_arguments_get_a_legible_error(server, sent, arguments, expected):
    result = await _call(server, "list_incidents", arguments)

    text = _text(result)
    assert result.is_error
    assert text.startswith("list_incidents does not accept "), text
    assert expected in text
    assert "Valid parameters for list_incidents (* = required): query, teams," in text
    assert "errors.pydantic.dev" not in text
    assert not sent


async def test_updated_filter_points_at_the_sort_that_exists(server, sent):
    result = await _call(server, "list_incidents", {"filter_updated_at": {"gte": _TS1}})

    assert "pass sort='-updated_at'" in _text(result)


async def test_created_filter_points_at_the_sort_that_exists(server, sent):
    result = await _call(
        server, "list_incidents", {"filter_created_at_gte": _TS1, "filter_created_at_lte": _TS2}
    )

    text = _text(result)
    assert "'filter_created_at_gte' (there is no created-time filter)" in text
    assert "pass sort='-created_at'" in text


async def test_created_filter_on_a_tool_that_has_one_names_its_bounds(server, sent):
    result = await _call(server, "list_incident_post_mortems", {"created_at": _TS1})

    text = _text(result)
    assert "'created_at' (use 'created_after' and/or 'created_before'" in text
    assert "no created-time filter" not in text


async def test_short_unrelated_names_get_no_suggestion(server, sent):
    # difflib's default cutoff paired types with teams; there is no type filter.
    result = await _call(server, "list_incidents", {"types": "x"})

    text = _text(result)
    assert "'types'" in text
    assert "did you mean" not in text


async def test_partially_mappable_call_is_rejected_whole(server, sent):
    result = await _call(server, "list_incidents", {"filter[status]": "resolved", "include": "x"})

    text = _text(result)
    assert result.is_error
    assert "'include'" in text
    assert "filter[status]" not in text
    assert not sent


async def test_required_parameters_are_starred(server, sent):
    result = await _call(server, "get_incident", {"incident_id": "1", "bogus": True})

    text = _text(result)
    assert result.is_error
    assert "get_incident does not accept 'bogus'." in text
    assert "Valid parameters for get_incident (* = required): incident_id*" in text


# ---------------------------------------------------------------------------
# Names a tool already accepts through pydantic validation aliases
# ---------------------------------------------------------------------------


def _rejected_by_middleware(text: str) -> bool:
    return " does not accept " in text or "Unexpected keyword argument" in text


def _sample_value(spec: dict[str, Any], name: str) -> Any:
    if spec.get("type") == "integer":
        return 2
    if spec.get("type") == "number":
        return 0.5
    if spec.get("type") == "boolean":
        return False
    if "date" in name or name.endswith(("_at", "_after", "_before")) or "time" in name:
        return _TS1
    return "1"


async def _alias_cases(server: FastMCP) -> list[tuple[str, str, str, dict[str, Any]]]:
    """(tool, alias, parameter, arguments) for every curated tool's every alias."""
    curated, _, _ = _provider_tool_inventory(server)
    cases: list[tuple[str, str, str, dict[str, Any]]] = []
    for name in sorted(curated):
        tool = await server.get_tool(name)
        if tool is None:
            continue
        schema = tool.parameters or {}
        properties = schema.get("properties") or {}
        required = {
            param: _sample_value(properties.get(param) or {}, param)
            for param in schema.get("required") or []
        }
        for alias, parameter in validation_aliases(getattr(tool, "fn", None)).items():
            arguments = {k: v for k, v in required.items() if k != parameter}
            arguments[alias] = _sample_value(properties.get(parameter) or {}, parameter)
            cases.append((name, alias, parameter, arguments))
    return cases


async def test_every_declared_validation_alias_is_accepted(server, sent):
    cases = await _alias_cases(server)
    # The aliases declared in tools/ today; a new one is picked up automatically.
    declared = {(tool, alias) for tool, alias, _, _ in cases}
    assert {
        ("get_schedule_shifts", "from_date"),
        ("get_schedule_shifts", "to"),
        ("list_shifts", "start_date"),
        ("list_shifts", "end_date"),
        ("get_incident_meeting_transcripts", "id"),
        ("list_incidents", "max_results"),
        ("list_incidents", "limit"),
        ("suggest_solutions", "max_results"),
    } <= declared

    rejected = []
    async with Client(server) as client:
        for tool, alias, parameter, arguments in cases:
            result = await client.call_tool(tool, arguments, raise_on_error=False)
            if result.is_error and _rejected_by_middleware(_text(result)):
                rejected.append((tool, alias, parameter, _text(result)))
    assert rejected == []


@pytest.mark.parametrize(
    ("tool", "arguments"),
    [
        ("get_schedule_shifts", {"id": "s1", "from_date": _TS1, "to_date": _TS2}),
        ("list_shifts", {"start_date": _TS1, "end_date": _TS2}),
        ("get_incident_meeting_transcripts", {"id": "inc-1"}),
    ],
)
async def test_reported_alias_calls_are_not_rejected(server, sent, tool, arguments):
    result = await _call(server, tool, arguments)

    assert not _rejected_by_middleware(_text(result)), _text(result)
    assert sent, "the call should reach the API"


async def test_accepted_alias_is_neither_rewritten_nor_overridden(server, sent):
    # `limit` is list_incidents' own alias of page_size: kept as sent, and a
    # page[size] alias for the same parameter cannot overwrite it.
    result = await _call(server, "list_incidents", {"limit": 3})
    assert not result.is_error, _text(result)
    assert _incident_params(sent)["page[size]"] == "3"

    result = await _call(server, "list_incidents", {"limit": 3, "page[size]": 9})
    assert result.is_error
    assert "'page[size]' (conflicts with 'page_size', which is also set)" in _text(result)


def test_validation_aliases_reads_annotated_field_metadata():
    def tool(
        start: Annotated[str, Field(validation_alias=AliasChoices("start", "from"))] = "",
        size: Annotated[int, Field(alias="limit")] = 1,
        plain: str = "",
    ) -> str:
        return ""

    assert validation_aliases(tool) == {"from": "start", "limit": "size"}
    assert validation_aliases(None) == {}


# ---------------------------------------------------------------------------
# Scope: curated tools only, no double handling
# ---------------------------------------------------------------------------


async def test_openapi_tools_are_untouched(server, sent):
    # Autogen tools take the API's own filter names; this middleware must not
    # rename them nor reject what FastMCP's OpenAPI layer accepts.
    result = await _call(server, "list_services", {"filter_name": "api", "bogus": 1})

    assert not result.is_error, _text(result)
    services = [r for r in sent if r.url.path == "/v1/services"]
    assert services and services[-1].url.params.get("filter[name]") == "api"


async def test_openapi_missing_path_argument_keeps_its_own_error(server, sent):
    # incident_id names another resource than /v1/action_items/{id}, so it stays unmapped.
    result = await _call(server, "get_incident_action_items", {"incident_id": "i1"})

    text = _text(result)
    assert result.is_error
    assert text.startswith("get_incident_action_items requires the path argument 'id'"), text


def test_scope_is_the_registered_curated_tools(server):
    middleware = next(m for m in server.middleware if isinstance(m, ToolArgumentAliasMiddleware))
    curated, _, autogen = _provider_tool_inventory(server)

    scope = middleware.tool_names
    assert scope is not None
    assert scope == frozenset(curated)
    assert "list_incidents" in scope
    assert not set(scope) & autogen


async def test_valid_call_passes_through_unchanged(server, sent):
    result = await _call(server, "list_incidents", {"status": "resolved", "page_size": 2})

    assert not result.is_error, _text(result)
    params = _incident_params(sent)
    assert params["filter[status]"] == "resolved"
    assert params["page[size]"] == "2"


# ---------------------------------------------------------------------------
# Measurement: rewrites are logged
# ---------------------------------------------------------------------------


async def test_rewrites_are_recorded_in_tool_usage_log(server, sent, caplog, monkeypatch):
    monkeypatch.setenv("ROOTLY_TOOL_USAGE_LOGGING", "true")
    records: list[str] = []
    usage_logger = logging.getLogger("rootly_mcp_server.tool_usage_json")

    class _Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record.getMessage())

    handler = _Capture()
    usage_logger.addHandler(handler)
    try:
        with caplog.at_level(logging.DEBUG, logger="rootly_mcp_server.server"):
            result = await _call(
                server, "list_incidents", {"filter[status]": "resolved", "page[size]": 3}
            )
    finally:
        usage_logger.removeHandler(handler)

    assert not result.is_error, _text(result)
    assert any(
        "Normalized list_incidents arguments: filter[status]->status, page[size]->page_size"
        in message
        for message in caplog.messages
    )
    events = [json.loads(message) for message in records if '"mcp_tool_call"' in message]
    assert events, records
    # The raw keys the caller sent, next to what they were rewritten to.
    assert events[-1]["tool_arg_keys"] == ["filter[status]", "page[size]"]
    assert events[-1]["tool_arg_rewrites"] == [
        "filter[status]->status",
        "page[size]->page_size",
    ]


# ---------------------------------------------------------------------------
# Unit: the normalizer against a schema
# ---------------------------------------------------------------------------

_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "query": {"type": "string"},
        "status": {"type": "string"},
        "service_ids": {"type": "string", "description": "Comma-separated service IDs"},
        "labels": {"type": "string", "description": "Free text"},
        "started_after": {"type": "string"},
        "page_size": {"type": "integer"},
    },
}


def test_normalizer_only_targets_declared_parameters():
    result = normalize_tool_arguments(
        {"filter_started_at": {"gte": _TS1, "lte": _TS2}, "page[number]": 2}, _SCHEMA
    )

    # started_before and page_number are not declared: nothing is invented.
    assert set(result.unknown) == {"filter_started_at", "page[number]"}
    assert "started_after" not in result.arguments


def test_normalizer_coerces_lists_only_for_comma_separated_strings():
    result = normalize_tool_arguments({"service_ids": ["a", 1], "labels": ["x", "y"]}, _SCHEMA)

    assert result.arguments["service_ids"] == "a,1"
    assert result.arguments["labels"] == ["x", "y"]
    assert result.rewrites == ["service_ids:list->csv"]


def test_normalizer_keeps_declared_names_verbatim():
    schema = {**_SCHEMA, "properties": {**_SCHEMA["properties"], "filter_status": {}}}
    result = normalize_tool_arguments({"filter_status": "x"}, schema)

    assert result.arguments == {"filter_status": "x"}
    assert not result.rewrites and not result.unknown


# ---------------------------------------------------------------------------
# Without a curated-name scope, any non-OpenAPI tool is handled
# ---------------------------------------------------------------------------


async def test_bare_server_handles_every_function_tool():
    server = FastMCP("test")
    server.add_middleware(ToolArgumentAliasMiddleware())

    @server.tool
    def list_things(query: str = "", page_size: int = 10) -> str:
        return f"query={query} page_size={page_size}"

    async with Client(server) as client:
        result = await client.call_tool("list_things", {"search": "db", "page[size]": 3})

    assert result.data == "query=db page_size=3"


# ---------------------------------------------------------------------------
# Code Mode: call_tool reaches the same middleware chain
# ---------------------------------------------------------------------------


async def test_code_mode_call_tool_gets_the_same_rewrites(monkeypatch):
    from rootly_mcp_server.code_mode import create_rootly_codemode_server

    monkeypatch.setenv("ROOTLY_API_TOKEN", "test-token")
    server = create_rootly_codemode_server(swagger_path=SWAGGER_PATH, hosted=False)
    _, provider, _ = _provider_tool_inventory(server)
    requests: list[httpx2.Request] = []

    async def fake_send(request, **kwargs):
        requests.append(request)
        return httpx2.Response(200, request=request, json={"data": [], "meta": {}})

    next(iter(provider._tools.values()))._client.client.send = fake_send
    code = (
        "return await call_tool('list_incidents', "
        f"{{'filter[status]': 'resolved', 'start_time': '{_TS1}'}})"
    )

    result = await _call(server, "execute", {"code": code})

    assert not result.is_error, _text(result)
    params = _incident_params(requests)
    assert params["filter[status]"] == "resolved"
    assert params["filter[started_at][gte]"] == _TS1


def test_search_incidents_rejection_points_at_the_filtering_tools():
    """ROOTLY-MCP-2M1 / 2KC: filters sent to the free-text search tool."""
    message = unknown_arguments_message(
        "search_incidents", {"properties": {"query": {}}}, {"status": "", "started_after": ""}
    )

    assert message.startswith("search_incidents does not accept 'status', 'started_after'.")
    assert "use list_incidents (one page) or collect_incidents (many pages)" in message


# ---------------------------------------------------------------------------
# JSON null and placeholder arguments (Sentry, 2026-10-26)
# ---------------------------------------------------------------------------


async def test_nulls_on_optional_parameters_mean_unset(server, sent):
    # collect_incidents failed with "12 validation errors" when every optional
    # parameter came as null; list_incidents with an undeclared start_time=null.
    collect = await _call(
        server, "collect_incidents", {"query": None, "status": None, "max_results": None}
    )
    listed = await _call(
        server, "list_incidents", {"status": "resolved", "start_time": None, "sort": None}
    )

    assert not collect.is_error, _text(collect)
    assert not listed.is_error, _text(listed)
    params = _incident_params(sent)
    assert params["filter[status]"] == "resolved"
    assert params["sort"] == "-created_at"


@pytest.mark.parametrize(
    "arguments",
    [
        {"filters": {"status": None, "severity": "sev1"}},
        {"filter_started_at": {"gte": None, "lte": _TS2}, "filter[status]": None},
    ],
    ids=["filters-dict", "range-dict"],
)
async def test_nulls_inside_filter_objects_mean_unset(server, sent, arguments):
    # Greptile on #278: these nulls only become parameters during normalization.
    result = await _call(server, "list_incidents", arguments)

    assert not result.is_error, _text(result)
    params = _incident_params(sent)
    assert "filter[status]" not in params
    assert "filter[started_at][gte]" not in params


@pytest.mark.parametrize("value", [None, "null", "undefined", "None"])
async def test_empty_required_argument_is_named(server, sent, value):
    # list_incident_roles(incident_id=null) got pydantic's
    # "Input should be a valid string" 13 times in a week.
    result = await _call(server, "list_incident_roles", {"incident_id": value})

    text = _text(result)
    assert result.is_error
    received = "incident_id=null" if value is None else f"incident_id={value!r}"
    assert (
        f"list_incident_roles requires the argument 'incident_id', but got {received} "
        "-- a placeholder, not a value."
    ) in text
    assert 'Retry with {"incident_id": "<value>"}' in text
    assert "valid string" not in text
    assert not sent


def test_nullable_parameters_keep_their_null():
    schema = {
        "properties": {
            "a": {"anyOf": [{"type": "string"}, {"type": "null"}]},
            "b": {"type": ["integer", "null"]},
            "c": {"type": "string"},
        },
        "required": [],
    }

    kept, dropped, empty_required = split_null_arguments(
        {"a": None, "b": None, "c": None, "d": None}, schema
    )

    assert kept == {"a": None, "b": None}
    assert dropped == ["c", "d"]
    assert empty_required == []
