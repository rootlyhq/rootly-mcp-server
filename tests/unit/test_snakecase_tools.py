"""Tests for snake_case tool-name normalization and the camelCase alias bridge.

Covers:
- `to_snake_case`: camelCase/PascalCase -> snake_case conversion rules
- `snakecase_operation_ids`: in-place spec rewrite + camel->snake mapping
- `CamelCaseAliasMiddleware`: routes deprecated camelCase calls to canonical
  snake_case names without listing the aliases
"""

from types import SimpleNamespace

import pytest

from rootly_mcp_server.server import ArgumentNormalizationMiddleware, CamelCaseAliasMiddleware
from rootly_mcp_server.spec_transform import snakecase_operation_ids, to_snake_case


class TestToSnakeCase:
    @pytest.mark.parametrize(
        ("camel", "expected"),
        [
            ("getIncident", "get_incident"),
            ("listIncidents", "list_incidents"),
            ("getScheduleShifts", "get_schedule_shifts"),
            ("getWorkflowTask", "get_workflow_task"),
            ("listAlertsSources", "list_alerts_sources"),
            ("listAllIncidentActionItems", "list_all_incident_action_items"),
            ("ListWorkflowRuns", "list_workflow_runs"),  # PascalCase
            ("createIncidentFormFieldSelection", "create_incident_form_field_selection"),
        ],
    )
    def test_converts_camel_and_pascal_case(self, camel, expected):
        assert to_snake_case(camel) == expected

    def test_already_snake_case_is_idempotent(self):
        for name in ("list_incidents", "search_incidents", "get_alert_by_short_id"):
            assert to_snake_case(name) == name


class TestSnakecaseOperationIds:
    def test_rewrites_operation_ids_in_place_and_returns_mapping(self):
        spec = {
            "paths": {
                "/incidents": {"get": {"operationId": "listIncidents"}},
                "/incidents/{id}": {
                    "get": {"operationId": "getIncident"},
                    "patch": {"operationId": "updateIncident"},
                },
                "/already_snake": {"get": {"operationId": "list_incidents"}},
            }
        }

        mapping = snakecase_operation_ids(spec)

        assert spec["paths"]["/incidents"]["get"]["operationId"] == "list_incidents"
        assert spec["paths"]["/incidents/{id}"]["get"]["operationId"] == "get_incident"
        assert spec["paths"]["/incidents/{id}"]["patch"]["operationId"] == "update_incident"
        # Already-snake names are untouched and excluded from the mapping.
        assert spec["paths"]["/already_snake"]["get"]["operationId"] == "list_incidents"
        assert mapping == {
            "listIncidents": "list_incidents",
            "getIncident": "get_incident",
            "updateIncident": "update_incident",
        }

    def test_ignores_non_operation_keys(self):
        spec = {
            "paths": {
                "/x": {
                    "parameters": [{"name": "id"}],  # not an HTTP method
                    "get": {"operationId": "getThing"},
                }
            }
        }
        mapping = snakecase_operation_ids(spec)
        assert mapping == {"getThing": "get_thing"}


async def _run_middleware(middleware, name, arguments=None):
    """Drive a single on_call_tool invocation and return (result, captured context)."""
    if arguments is None:
        arguments = {}
    captured = {}

    async def call_next(context):
        captured["name"] = context.message.name
        captured["args"] = dict(context.message.arguments) if context.message.arguments else {}
        return "ok"

    context = SimpleNamespace(message=SimpleNamespace(name=name, arguments=arguments))
    result = await middleware.on_call_tool(context, call_next)
    return result, captured


@pytest.mark.asyncio
class TestCamelCaseAliasMiddleware:
    async def test_rewrites_camelcase_to_canonical_snake_case(self):
        mw = CamelCaseAliasMiddleware({"getScheduleShifts": "get_schedule_shifts"})
        result, ctx = await _run_middleware(mw, "getScheduleShifts")
        assert result == "ok"
        assert ctx["name"] == "get_schedule_shifts"

    async def test_passes_through_unknown_and_snake_names_untouched(self):
        mw = CamelCaseAliasMiddleware({"getScheduleShifts": "get_schedule_shifts"})
        _, ctx = await _run_middleware(mw, "get_schedule_shifts")
        assert ctx["name"] == "get_schedule_shifts"
        _, ctx = await _run_middleware(mw, "some_other_tool")
        assert ctx["name"] == "some_other_tool"

    async def test_identity_mapping_is_a_harmless_no_op(self):
        mw = CamelCaseAliasMiddleware({"tool_search": "tool_search"})
        _, ctx = await _run_middleware(mw, "tool_search")
        assert ctx["name"] == "tool_search"


@pytest.mark.asyncio
class TestArgumentNormalizationMiddleware:
    async def _run(self, name, arguments):
        _, ctx = await _run_middleware(ArgumentNormalizationMiddleware(), name, arguments)
        return "ok", ctx["args"]

    async def test_renames_from_to_from_date_for_list_shifts(self):
        _, args = await self._run(
            "list_shifts",
            {"from": "2026-01-01", "to": "2026-01-07", "page_size": 25},
        )
        assert args["from_date"] == "2026-01-01"
        assert args["to_date"] == "2026-01-07"
        assert "from" not in args
        assert "to" not in args

    async def test_no_rename_when_canonical_already_present(self):
        _, args = await self._run(
            "list_shifts",
            {"from": "old", "from_date": "correct", "to_date": "also_correct"},
        )
        assert args["from_date"] == "correct"
        assert args["from"] == "old"

    async def test_renames_max_tokens_to_max_results_for_search_incidents(self):
        _, args = await self._run(
            "search_incidents",
            {"query": "outage", "max_tokens": "3000"},
        )
        assert args["max_results"] == "3000"
        assert "max_tokens" not in args

    @pytest.mark.parametrize(
        ("tool", "old_key", "new_key"),
        [
            ("search_incidents", "limit", "max_results"),
            ("search_incidents", "search_term", "query"),
            ("search_incidents", "pattern", "query"),
            ("list_incidents", "declared_after", "started_after"),
            ("list_incidents", "created_at_gte", "started_after"),
            ("list_incidents", "per_page", "page_size"),
            ("list_incidents", "description", "query"),
            ("collect_incidents", "max_incidents", "max_results"),
            ("collect_incidents", "start_time", "started_after"),
            ("collect_incidents", "end", "started_before"),
            ("find_related_incidents", "query", "incident_description"),
            ("find_related_incidents", "alert_summary", "incident_description"),
            ("find_related_incidents", "limit", "max_results"),
            ("suggest_solutions", "description", "incident_description"),
            ("get_incident", "id", "incident_id"),
        ],
    )
    async def test_renames_common_llm_argument_variants(self, tool, old_key, new_key):
        _, args = await self._run(tool, {old_key: "value"})
        assert args[new_key] == "value"
        assert old_key not in args

    async def test_converts_list_schedule_ids_to_csv(self):
        _, args = await self._run(
            "list_shifts",
            {"from_date": "2026-01-01", "to_date": "2026-01-07", "schedule_ids": ["abc", "def"]},
        )
        assert args["schedule_ids"] == "abc,def"

    async def test_leaves_string_schedule_ids_alone(self):
        _, args = await self._run(
            "list_shifts",
            {"from_date": "2026-01-01", "to_date": "2026-01-07", "schedule_ids": "abc,def"},
        )
        assert args["schedule_ids"] == "abc,def"

    async def test_empty_list_left_unchanged(self):
        _, args = await self._run(
            "list_shifts",
            {"from_date": "2026-01-01", "to_date": "2026-01-07", "schedule_ids": []},
        )
        assert args["schedule_ids"] == []

    async def test_no_op_for_unrelated_tools(self):
        _, args = await self._run("get_incident", {"incident_id": "123"})
        assert args == {"incident_id": "123"}
