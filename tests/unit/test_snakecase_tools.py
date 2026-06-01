"""Tests for snake_case tool-name normalization and the camelCase alias bridge.

Covers:
- `to_snake_case`: camelCase/PascalCase -> snake_case conversion rules
- `snakecase_operation_ids`: in-place spec rewrite + camel->snake mapping
- `CamelCaseAliasMiddleware`: routes deprecated camelCase calls to canonical
  snake_case names without listing the aliases
"""

from types import SimpleNamespace

import pytest

from rootly_mcp_server.server import CamelCaseAliasMiddleware
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


@pytest.mark.asyncio
class TestCamelCaseAliasMiddleware:
    async def _run(self, middleware, name):
        captured = {}

        async def call_next(context):
            captured["name"] = context.message.name
            return "ok"

        context = SimpleNamespace(message=SimpleNamespace(name=name, arguments={}))
        result = await middleware.on_call_tool(context, call_next)
        return result, captured["name"]

    async def test_rewrites_camelcase_to_canonical_snake_case(self):
        mw = CamelCaseAliasMiddleware({"getScheduleShifts": "get_schedule_shifts"})
        result, dispatched = await self._run(mw, "getScheduleShifts")
        assert result == "ok"
        assert dispatched == "get_schedule_shifts"

    async def test_passes_through_unknown_and_snake_names_untouched(self):
        mw = CamelCaseAliasMiddleware({"getScheduleShifts": "get_schedule_shifts"})
        _, dispatched = await self._run(mw, "get_schedule_shifts")
        assert dispatched == "get_schedule_shifts"
        _, dispatched = await self._run(mw, "some_other_tool")
        assert dispatched == "some_other_tool"

    async def test_identity_mapping_is_a_harmless_no_op(self):
        # An identity entry rewrites the name to itself — behaviorally a no-op.
        mw = CamelCaseAliasMiddleware({"tool_search": "tool_search"})
        _, dispatched = await self._run(mw, "tool_search")
        assert dispatched == "tool_search"
