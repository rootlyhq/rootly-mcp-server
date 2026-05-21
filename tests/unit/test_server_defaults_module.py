"""Focused tests for server_defaults module."""

from unittest.mock import patch

from rootly_mcp_server.server_defaults import (
    DEFAULT_ALLOWED_PATHS,
    DEFAULT_HOSTED_ENABLED_TOOLS,
    LEGACY_TOOL_ALIASES,
    _generate_recommendation,
    canonicalize_tool_names,
    enabled_tools_from_env,
)


class TestServerDefaultsModule:
    """Direct tests for defaults and recommendation helper."""

    def test_default_allowed_paths_contains_core_endpoints(self):
        assert "/alerts" in DEFAULT_ALLOWED_PATHS
        assert "/incidents/{incident_id}/alerts" in DEFAULT_ALLOWED_PATHS
        assert "/incidents/{incident_id}/form_field_selections" in DEFAULT_ALLOWED_PATHS
        assert "/incident_form_field_selections/{id}" in DEFAULT_ALLOWED_PATHS
        assert "/alerts/{alert_id}/events" in DEFAULT_ALLOWED_PATHS
        assert "/alert_events/{id}" in DEFAULT_ALLOWED_PATHS
        assert "/action_items" in DEFAULT_ALLOWED_PATHS
        assert "/action_items/{id}" in DEFAULT_ALLOWED_PATHS
        assert "/workflows/{workflow_id}/workflow_runs" in DEFAULT_ALLOWED_PATHS
        assert "/workflow_groups" in DEFAULT_ALLOWED_PATHS
        assert "/workflow_groups/{id}" in DEFAULT_ALLOWED_PATHS
        assert "/workflows/{workflow_id}/form_field_conditions" in DEFAULT_ALLOWED_PATHS
        assert "/workflow_form_field_conditions/{id}" in DEFAULT_ALLOWED_PATHS
        assert "/workflows/{workflow_id}/workflow_tasks" in DEFAULT_ALLOWED_PATHS
        assert "/workflow_tasks/{id}" in DEFAULT_ALLOWED_PATHS
        assert "/status-pages" in DEFAULT_ALLOWED_PATHS
        assert "/status-pages/{id}" in DEFAULT_ALLOWED_PATHS
        assert "/status-pages/{status_page_id}/templates" in DEFAULT_ALLOWED_PATHS
        assert "/templates/{id}" in DEFAULT_ALLOWED_PATHS
        assert "/teams/{id}/incidents_chart" in DEFAULT_ALLOWED_PATHS
        assert "/services/{id}/incidents_chart" in DEFAULT_ALLOWED_PATHS
        assert "/services/{id}/uptime_chart" in DEFAULT_ALLOWED_PATHS
        assert "/functionalities/{id}/incidents_chart" in DEFAULT_ALLOWED_PATHS
        assert "/functionalities/{id}/uptime_chart" in DEFAULT_ALLOWED_PATHS
        assert "/alert_groups" in DEFAULT_ALLOWED_PATHS
        assert "/alert_groups/{id}" in DEFAULT_ALLOWED_PATHS
        assert "/alert_routing_rules" in DEFAULT_ALLOWED_PATHS
        assert "/alert_routing_rules/{id}" in DEFAULT_ALLOWED_PATHS
        assert "/alert_sources" in DEFAULT_ALLOWED_PATHS
        assert "/alert_sources/{id}" in DEFAULT_ALLOWED_PATHS
        assert "/alert_urgencies" in DEFAULT_ALLOWED_PATHS
        assert "/alert_urgencies/{id}" in DEFAULT_ALLOWED_PATHS
        assert "/custom_forms" in DEFAULT_ALLOWED_PATHS
        assert "/custom_forms/{id}" in DEFAULT_ALLOWED_PATHS
        assert "/form_fields" in DEFAULT_ALLOWED_PATHS
        assert "/form_fields/{id}" in DEFAULT_ALLOWED_PATHS
        assert "/form_fields/{form_field_id}/options" in DEFAULT_ALLOWED_PATHS
        assert "/form_field_options/{id}" in DEFAULT_ALLOWED_PATHS
        assert "/catalogs" in DEFAULT_ALLOWED_PATHS
        assert "/catalogs/{id}" in DEFAULT_ALLOWED_PATHS
        assert "/catalogs/{catalog_id}/entities" in DEFAULT_ALLOWED_PATHS
        assert "/catalog_entities/{id}" in DEFAULT_ALLOWED_PATHS
        assert "/causes" in DEFAULT_ALLOWED_PATHS
        assert "/causes/{id}" in DEFAULT_ALLOWED_PATHS
        assert "/shifts" in DEFAULT_ALLOWED_PATHS
        assert "/on_call_roles" in DEFAULT_ALLOWED_PATHS

    def test_generate_recommendation_when_no_solutions(self):
        result = _generate_recommendation({"solutions": [], "average_resolution_time": None})
        assert "No similar incidents found" in result

    def test_generate_recommendation_includes_actions_patterns_and_time(self):
        solution_data = {
            "solutions": [{"suggested_actions": ["Restart API", "Purge cache"]}],
            "average_resolution_time": 0.7,
            "common_patterns": ["Database connection saturation"],
        }
        result = _generate_recommendation(solution_data)
        assert "resolve quickly" in result
        assert "Restart API, Purge cache" in result
        assert "Database connection saturation" in result

    def test_generate_recommendation_long_resolution_time(self):
        solution_data = {
            "solutions": [{"suggested_actions": []}],
            "average_resolution_time": 5.0,
            "common_patterns": [],
        }
        result = _generate_recommendation(solution_data)
        assert "require more time" in result

    def test_enabled_tools_from_env_parses_csv(self):
        with patch.dict(
            "os.environ",
            {"ROOTLY_MCP_ENABLED_TOOLS": "list_incidents, getIncident ,listTeams"},
            clear=True,
        ):
            assert enabled_tools_from_env() == {"list_incidents", "getIncident", "listTeams"}

    def test_enabled_tools_from_env_defaults_to_hosted_core_allowlist(self):
        with patch.dict("os.environ", {}, clear=True):
            assert enabled_tools_from_env(hosted=True) == set(DEFAULT_HOSTED_ENABLED_TOOLS)

    def test_enabled_tools_from_env_local_mode_keeps_full_surface_by_default(self):
        with patch.dict("os.environ", {}, clear=True):
            assert enabled_tools_from_env(hosted=False) is None

    def test_canonicalize_passes_through_canonical_names(self):
        assert canonicalize_tool_names({"list_incidents", "getIncident"}) == {
            "list_incidents",
            "getIncident",
        }

    def test_canonicalize_expands_legacy_to_include_canonical(self):
        # Posture A: legacy name stays in the set (so the proxy remains exposed)
        # AND the canonical name is added (so new clients also see it).
        assert canonicalize_tool_names({"listIncidents"}) == {"listIncidents", "list_incidents"}

    def test_canonicalize_handles_mixed_allowlist(self):
        result = canonicalize_tool_names({"listIncidents", "getIncident", "listTeams"})
        assert result == {"listIncidents", "list_incidents", "getIncident", "listTeams"}

    def test_canonicalize_empty_set(self):
        assert canonicalize_tool_names(set()) == set()

    def test_legacy_aliases_contains_list_incidents(self):
        assert LEGACY_TOOL_ALIASES.get("listIncidents") == "list_incidents"
