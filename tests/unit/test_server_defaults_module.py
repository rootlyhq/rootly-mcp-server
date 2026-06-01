"""Focused tests for server_defaults module."""

from unittest.mock import patch

from rootly_mcp_server.server_defaults import (
    DEFAULT_ALLOWED_PATHS,
    DEFAULT_HOSTED_ENABLED_TOOLS,
    HOSTED_TOOL_PROFILE_FULL,
    HOSTED_TOOL_PROFILE_SLIM,
    _generate_recommendation,
    enabled_tools_from_env,
    hosted_tool_profile_from_env,
    normalize_hosted_tool_profile,
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
            {"ROOTLY_MCP_ENABLED_TOOLS": "list_incidents, get_incident ,list_teams"},
            clear=True,
        ):
            assert enabled_tools_from_env() == {"list_incidents", "get_incident", "list_teams"}

    def test_enabled_tools_from_env_defaults_to_hosted_full_surface(self):
        with patch.dict("os.environ", {}, clear=True):
            assert enabled_tools_from_env(hosted=True) is None

    def test_enabled_tools_from_env_returns_slim_hosted_profile_when_requested(self):
        with patch.dict("os.environ", {}, clear=True):
            assert enabled_tools_from_env(
                hosted=True,
                hosted_tool_profile=HOSTED_TOOL_PROFILE_SLIM,
            ) == set(DEFAULT_HOSTED_ENABLED_TOOLS)

    def test_default_hosted_enabled_tools_targets_curated_70_tool_profile(self):
        assert len(DEFAULT_HOSTED_ENABLED_TOOLS) >= 60

    def test_enabled_tools_from_env_local_mode_keeps_full_surface_by_default(self):
        with patch.dict("os.environ", {}, clear=True):
            assert enabled_tools_from_env(hosted=False) is None

    def test_normalize_hosted_tool_profile_accepts_aliases(self):
        assert normalize_hosted_tool_profile("slim") == HOSTED_TOOL_PROFILE_SLIM
        assert normalize_hosted_tool_profile("core") == HOSTED_TOOL_PROFILE_SLIM
        assert normalize_hosted_tool_profile("default") == HOSTED_TOOL_PROFILE_FULL
        assert normalize_hosted_tool_profile("all") == HOSTED_TOOL_PROFILE_FULL

    def test_hosted_tool_profile_from_env_defaults_to_full(self):
        with patch.dict("os.environ", {}, clear=True):
            assert hosted_tool_profile_from_env() == HOSTED_TOOL_PROFILE_FULL

    def test_default_hosted_enabled_tools_are_all_snake_case(self):
        # Hard cutover: the entire tool surface is snake_case. No camelCase
        # entries should survive in the curated hosted allowlist.
        for name in DEFAULT_HOSTED_ENABLED_TOOLS:
            assert name == name.lower(), f"{name!r} is not snake_case"
            assert "-" not in name
