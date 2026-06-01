"""Default server constants and recommendation helpers."""

from __future__ import annotations

import logging
import os

from .spec_transform import to_snake_case

# Set up logger
logger = logging.getLogger(__name__)

HOSTED_TOOL_PROFILE_SLIM = "slim"
HOSTED_TOOL_PROFILE_FULL = "full"
_HOSTED_TOOL_PROFILE_ALIASES = {
    "core": HOSTED_TOOL_PROFILE_SLIM,
    "default": HOSTED_TOOL_PROFILE_FULL,
    "slim": HOSTED_TOOL_PROFILE_SLIM,
    "full": HOSTED_TOOL_PROFILE_FULL,
    "all": HOSTED_TOOL_PROFILE_FULL,
}


# Environment variable constants
class EnvVars:
    """Centralized environment variable names for type safety and IDE support."""

    API_TOKEN = "ROOTLY_API_TOKEN"  # nosec B105 - Environment variable name, not password
    BASE_URL = "ROOTLY_BASE_URL"
    SERVER_NAME = "ROOTLY_SERVER_NAME"
    HOSTED = "ROOTLY_HOSTED"
    HOSTED_TOOL_PROFILE = "ROOTLY_MCP_HOSTED_TOOL_PROFILE"
    ENABLE_WRITE_TOOLS = "ROOTLY_MCP_ENABLE_WRITE_TOOLS"
    ENABLED_TOOLS = "ROOTLY_MCP_ENABLED_TOOLS"
    TRANSPORT = "ROOTLY_TRANSPORT"
    ALLOWED_PATHS = "ROOTLY_ALLOWED_PATHS"
    SWAGGER_PATH = "ROOTLY_SWAGGER_PATH"
    LOG_LEVEL = "ROOTLY_LOG_LEVEL"


def _parse_csv_set(raw: str | None) -> set[str] | None:
    """Parse a comma-separated environment value into a normalized set."""
    if raw is None:
        return None
    parsed = {item.strip() for item in raw.split(",") if item.strip()}
    return parsed or None


# Default hosted tool surface tuned from one month of production popularity data.
# This keeps the slim remote/serverless profile near 70 tools while still
# covering the overwhelming majority of observed requests. Operators can always
# override this with ROOTLY_MCP_ENABLED_TOOLS for a narrower or broader surface.
DEFAULT_HOSTED_ENABLED_TOOLS: frozenset[str] = frozenset(
    {
        "collect_incidents",
        "create_incident",
        "create_incident_action_item",
        "create_override_shift",
        "create_schedule",
        "create_workflow",
        "create_workflow_task",
        "find_related_incidents",
        "get_alert",
        "get_alert_by_short_id",
        "get_alert_event",
        "get_current_user",
        "get_escalation_level",
        "get_escalation_policy",
        "get_functionality",
        "get_incident",
        "get_oncall_handoff_summary",
        "get_oncall_schedule_summary",
        "get_schedule",
        "get_schedule_shifts",
        "get_server_version",
        "get_service",
        "get_shift_incidents",
        "get_team",
        "get_user",
        "list_alert_events",
        "list_alert_routes",
        "list_alert_routing_rules",
        "list_alert_urgencies",
        "list_alerts",
        "list_alerts_sources",
        "list_all_incident_action_items",
        "list_endpoints",
        "list_escalation_levels",
        "list_escalation_paths",
        "list_escalation_policies",
        "list_functionalities",
        "list_incident_action_items",
        "list_incident_alerts",
        "list_incident_events",
        "list_incident_form_field_selections",
        "list_incident_types",
        "list_incidents",
        "list_override_shifts",
        "list_schedule_rotation_active_days",
        "list_schedule_rotation_users",
        "list_schedule_rotations",
        "list_schedules",
        "list_services",
        "list_severities",
        "list_shifts",
        "list_teams",
        "list_users",
        "list_workflow_runs",
        "list_workflow_tasks",
        "list_workflows",
        "search_incidents",
        "suggest_solutions",
        "update_escalation_level",
        "update_escalation_path",
        "update_escalation_policy",
        "update_incident",
        "update_incident_type",
        "update_override_shift",
        "update_schedule",
        "update_schedule_rotation",
        "update_service",
        "update_severity",
        "update_team",
        "update_workflow",
        "update_workflow_task",
    }
)


def canonicalize_tool_names(enabled_tools: set[str]) -> set[str]:
    """Normalize an allowlist to the snake_case canonical tool names.

    The exposed tool surface is uniformly snake_case. Operators with legacy
    camelCase entries in ``ROOTLY_MCP_ENABLED_TOOLS`` (or cached configs) still
    get the right tools: each name is converted to its snake_case form so it
    matches the registered/autogen tool names. (The camelCase names also remain
    callable at runtime via the alias middleware, but allowlist filtering keys
    off the canonical snake_case name.)
    """
    if not enabled_tools:
        return enabled_tools
    return {to_snake_case(name) for name in enabled_tools}


def _generate_recommendation(solution_data: dict) -> str:
    """Generate a high-level recommendation based on solution analysis."""
    solutions = solution_data.get("solutions", [])
    avg_time = solution_data.get("average_resolution_time")

    if not solutions:
        return "No similar incidents found. This may be a novel issue requiring escalation."

    recommendation_parts = []

    # Time expectation
    if avg_time:
        if avg_time < 1:
            recommendation_parts.append("Similar incidents typically resolve quickly (< 1 hour).")
        elif avg_time > 4:
            recommendation_parts.append(
                "Similar incidents typically require more time (> 4 hours)."
            )

    # Top solution
    if solutions:
        top_solution = solutions[0]
        if top_solution.get("suggested_actions"):
            actions = top_solution["suggested_actions"][:2]  # Top 2 actions
            recommendation_parts.append(f"Consider trying: {', '.join(actions)}")

    # Pattern insights
    patterns = solution_data.get("common_patterns", [])
    if patterns:
        recommendation_parts.append(f"Common patterns: {patterns[0]}")

    return (
        " ".join(recommendation_parts)
        if recommendation_parts
        else "Review similar incidents above for resolution guidance."
    )


def write_tools_enabled_from_env(default: bool = False) -> bool:
    """Return whether non-destructive write tools should be exposed."""
    raw = os.getenv(EnvVars.ENABLE_WRITE_TOOLS)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def normalize_hosted_tool_profile(
    raw: str | None, *, default: str = HOSTED_TOOL_PROFILE_FULL
) -> str:
    """Normalize hosted tool profile values to `full` or `slim`."""
    if not raw:
        return default
    return _HOSTED_TOOL_PROFILE_ALIASES.get(raw.strip().lower(), default)


def hosted_tool_profile_from_env(default: str = HOSTED_TOOL_PROFILE_FULL) -> str:
    """Return the hosted tool profile requested by the operator environment."""
    raw = os.getenv(EnvVars.HOSTED_TOOL_PROFILE)
    normalized = normalize_hosted_tool_profile(raw, default=default)
    if raw and raw.strip().lower() not in _HOSTED_TOOL_PROFILE_ALIASES:
        logger.warning(
            "Unknown %s=%r; falling back to %r",
            EnvVars.HOSTED_TOOL_PROFILE,
            raw,
            normalized,
        )
    return normalized


def collect_operation_ids(paths: dict) -> set[str]:
    """Return the set of operationIds defined in an OpenAPI paths object."""
    op_ids: set[str] = set()
    for path_data in paths.values():
        if not isinstance(path_data, dict):
            continue
        for method_data in path_data.values():
            if isinstance(method_data, dict) and (operation_id := method_data.get("operationId")):
                op_ids.add(operation_id)
    return op_ids


def enabled_tools_from_env(
    *,
    hosted: bool = False,
    hosted_tool_profile: str = HOSTED_TOOL_PROFILE_FULL,
) -> set[str] | None:
    """Return the configured MCP tool allowlist.

    Precedence:
    1. Explicit ROOTLY_MCP_ENABLED_TOOLS env var
    2. Hosted default profile allowlist
    3. None (expose full tool surface)
    """
    configured = _parse_csv_set(os.getenv(EnvVars.ENABLED_TOOLS))
    if configured is not None:
        return configured
    if hosted:
        if hosted_tool_profile == HOSTED_TOOL_PROFILE_FULL:
            return None
        return set(DEFAULT_HOSTED_ENABLED_TOOLS)
    return None


# Default allowed API paths
DEFAULT_ALLOWED_PATHS = [
    "/incidents",
    "/incidents/{incident_id}/alerts",
    "/alerts",
    "/alerts/{id}",
    "/severities",
    "/severities/{severity_id}",
    "/teams",
    "/teams/{team_id}",
    "/services",
    "/services/{service_id}",
    "/functionalities",
    "/functionalities/{functionality_id}",
    # Incident types
    "/incident_types",
    "/incident_types/{incident_type_id}",
    # Action items (all, by id, by incident)
    "/action_items",
    "/action_items/{id}",
    "/incidents/{incident_id}/action_items",
    # Incident form field selections (used for incident custom field values)
    "/incidents/{incident_id}/form_field_selections",
    "/incident_form_field_selections/{id}",
    # Workflows
    "/workflows",
    "/workflows/{workflow_id}",
    "/workflows/{workflow_id}/workflow_tasks",
    "/workflow_tasks/{id}",
    # Workflow reads
    "/workflows/{workflow_id}/workflow_runs",
    "/workflow_groups",
    "/workflow_groups/{id}",
    "/workflows/{workflow_id}/form_field_conditions",
    "/workflow_form_field_conditions/{id}",
    # Environments
    "/environments",
    "/environments/{environment_id}",
    # Users
    "/users",
    "/users/{user_id}",
    "/users/me",
    # Status pages
    "/status-pages",
    "/status-pages/{id}",
    "/status-pages/{status_page_id}/templates",
    "/templates/{id}",
    # Incident and uptime charts
    "/teams/{id}/incidents_chart",
    "/services/{id}/incidents_chart",
    "/services/{id}/uptime_chart",
    "/functionalities/{id}/incidents_chart",
    "/functionalities/{id}/uptime_chart",
    # Alert configuration visibility (read-only by default)
    "/alert_groups",
    "/alert_groups/{id}",
    "/alert_routing_rules",
    "/alert_routing_rules/{id}",
    # Advanced alert routing — the successor to alert_routing_rules.  When a
    # tenant has the Advanced Alert Routing feature enabled, `/alert_routing_rules`
    # returns 403 and this endpoint is the replacement.  Both are exposed so the
    # model can fall back automatically based on the per-tenant feature flag.
    "/alert_routes",
    "/alert_routes/{id}",
    "/alert_sources",
    "/alert_sources/{id}",
    "/alert_urgencies",
    "/alert_urgencies/{id}",
    # Form metadata
    "/custom_forms",
    "/custom_forms/{id}",
    "/form_fields",
    "/form_fields/{id}",
    "/form_fields/{form_field_id}/options",
    "/form_field_options/{id}",
    # Catalog and cause metadata
    "/catalogs",
    "/catalogs/{id}",
    "/catalogs/{catalog_id}/entities",
    "/catalog_entities/{id}",
    "/causes",
    "/causes/{id}",
    # Alert events
    "/alerts/{alert_id}/events",
    "/alert_events/{id}",
    # On-call schedules and shifts
    "/schedules",
    "/schedules/{schedule_id}",
    "/schedules/{schedule_id}/shifts",
    "/schedules/{schedule_id}/schedule_rotations",
    "/shifts",
    "/schedule_rotations/{schedule_rotation_id}",
    "/schedule_rotations/{schedule_rotation_id}/schedule_rotation_users",
    "/schedule_rotations/{schedule_rotation_id}/schedule_rotation_active_days",
    # Escalation policies and paths
    "/escalation_policies",
    "/escalation_policies/{escalation_policy_id}",
    "/escalation_policies/{escalation_policy_id}/escalation_paths",
    "/escalation_policies/{escalation_policy_id}/escalation_levels",
    "/escalation_paths/{escalation_policy_path_id}",
    "/escalation_paths/{escalation_policy_path_id}/escalation_levels",
    "/escalation_levels/{escalation_level_id}",
    # On-call overrides
    "/schedules/{schedule_id}/override_shifts",
    "/override_shifts/{override_shift_id}",
    # On-call shadows and roles
    "/schedules/{schedule_id}/on_call_shadows",
    "/on_call_shadows/{on_call_shadow_id}",
    "/on_call_roles",
    "/on_call_roles/{on_call_role_id}",
    # Communications management
    "/communications_groups",
    "/communications_groups/{id}",
    "/communications_stages",
    "/communications_stages/{id}",
    "/communications_templates",
    "/communications_templates/{id}",
    "/communications_types",
    "/communications_types/{id}",
    # Dashboards and analytics
    "/dashboards",
    "/dashboards/{id}",
    "/dashboard_panels",
    "/dashboard_panels/{id}",
    # Playbooks and runbooks
    "/playbooks",
    "/playbooks/{id}",
    "/playbook_tasks",
    "/playbook_tasks/{id}",
    # Post-incident reviews and retrospectives
    "/post_incident_reviews",
    "/post_incident_reviews/{id}",
    "/retrospective_processes",
    "/retrospective_processes/{id}",
    "/retrospective_process_groups",
    "/retrospective_process_groups/{id}",
    "/retrospective_steps",
    "/retrospective_steps/{id}",
    # Postmortem templates
    "/postmortem_templates",
    "/postmortem_templates/{id}",
    # Heartbeat monitoring
    "/heartbeats",
    "/heartbeats/{id}",
    # Live call routing
    "/live_call_routers",
    "/live_call_routers/{id}",
    # Pulse checks and health monitoring
    "/pulses",
    "/pulses/{id}",
    # Safe user operations (notification preferences only)
    "/users/{user_id}/notification_rules",
    "/user_notification_rules/{id}",
    "/users/{user_id}/email_addresses",
    "/user_email_addresses/{id}",
    "/users/{user_id}/phone_numbers",
    "/user_phone_numbers/{id}",
    # Incident-related expanded endpoints
    "/incidents/{incident_id}/events",
    "/incident_events/{id}",
    "/incidents/{incident_id}/custom_field_selections",
    "/incident_custom_field_selections/{id}",
    "/incidents/{incident_id}/postmortems",
    "/incident_postmortems/{id}",
    "/incidents/{incident_id}/retrospective_steps",
    "/incident_retrospective_steps/{id}",
    "/incidents/{incident_id}/status_pages",
    "/incident_status_pages/{id}",
    # Advanced form management
    "/custom_fields",
    "/custom_fields/{id}",
    "/custom_field_options",
    "/custom_field_options/{id}",
    "/form_sets",
    "/form_sets/{id}",
    "/form_field_placements",
    "/form_field_placements/{id}",
    "/form_field_placement_conditions",
    "/form_field_placement_conditions/{id}",
    "/form_set_conditions",
    "/form_set_conditions/{id}",
    # Status page templates
    "/status_page_templates",
    "/status_page_templates/{id}",
    # Sub-status management
    "/sub_statuses",
    "/sub_statuses/{id}",
    "/incident_sub_statuses",
    "/incident_sub_statuses/{id}",
]

# Paths explicitly excluded for security reasons - these contain sensitive operations
# that should not be exposed through MCP even if they exist in the OpenAPI spec
SECURITY_EXCLUDED_PATHS = [
    # Authentication and API management
    "/api_keys",
    "/api_keys/{id}",
    "/authorizations",
    "/authorizations/{id}",
    "/secrets",
    "/secrets/{id}",
    # User account management (creation/deletion should be done through proper IAM)
    "/users/create",
    "/users/{user_id}/delete",
    # Role and permission management
    "/roles",
    "/roles/{id}",
    "/permissions",
    "/permissions/{id}",
    "/incident_permission_sets",
    "/incident_permission_set_booleans",
    "/incident_permission_set_resources",
    # Webhook and integration management (potential for data exfiltration)
    "/webhooks_endpoints",
    "/webhooks_endpoints/{id}",
    # Financial and billing operations
    "/on_call_pay_reports",
    "/on_call_pay_reports/{id}",
    # Global configuration that could affect system behavior
    "/retrospective_configurations",
]

# Non-destructive write operations are only exposed for these path families when
# write tools are explicitly enabled. This keeps the default surface focused on
# read-only workflows and avoids exposing broader admin/config writes.
DEFAULT_WRITE_ALLOWED_PATHS = [
    # Core incident and infrastructure - create + update
    "/alerts",
    "/alerts/{id}",
    "/incidents/{incident_id}/alerts",
    "/environments",
    "/environments/{environment_id}",
    "/functionalities",
    "/functionalities/{functionality_id}",
    "/incident_types",
    "/incident_types/{incident_type_id}",
    "/services",
    "/services/{service_id}",
    "/severities",
    "/severities/{severity_id}",
    "/teams",
    "/teams/{team_id}",
    # Alert management - create + update
    "/alerts/{alert_id}/events",
    "/alert_groups",
    "/alert_groups/{id}",
    "/alert_routes",
    "/alert_routes/{id}",
    "/alert_routing_rules",
    "/alert_routing_rules/{id}",
    "/alert_urgencies",
    "/alert_urgencies/{id}",
    "/alert_sources",
    "/alert_sources/{id}",
    # Incident mutations
    "/alert_events/{id}",
    "/action_items/{id}",
    "/incidents/{incident_id}/action_items",
    "/incidents/{incident_id}/custom_field_selections",
    "/incidents/{incident_id}/events",
    "/incidents/{incident_id}/form_field_selections",
    "/incident_form_field_selections/{id}",
    # On-call schedules - create + update
    "/schedules",
    "/schedules/{schedule_id}",
    "/schedules/{schedule_id}/schedule_rotations",
    "/schedule_rotations/{schedule_rotation_id}",
    "/schedule_rotations/{schedule_rotation_id}/schedule_rotation_users",
    "/schedule_rotations/{schedule_rotation_id}/schedule_rotation_active_days",
    "/schedules/{schedule_id}/override_shifts",
    "/override_shifts/{override_shift_id}",
    "/schedules/{schedule_id}/on_call_shadows",
    "/on_call_shadows/{on_call_shadow_id}",
    "/on_call_roles",
    "/on_call_roles/{on_call_role_id}",
    # Escalation policies - create + update
    "/escalation_policies",
    "/escalation_policies/{escalation_policy_id}",
    "/escalation_policies/{escalation_policy_id}/escalation_paths",
    "/escalation_policies/{escalation_policy_id}/escalation_levels",
    "/escalation_paths/{escalation_policy_path_id}",
    "/escalation_paths/{escalation_policy_path_id}/escalation_levels",
    "/escalation_levels/{escalation_level_id}",
    # Workflows - create + update
    "/workflows",
    "/workflows/{workflow_id}",
    "/workflows/{workflow_id}/workflow_runs",
    "/workflow_groups",
    "/workflow_groups/{id}",
    "/workflows/{workflow_id}/workflow_tasks",
    "/workflow_tasks/{id}",
    "/workflows/{workflow_id}/form_field_conditions",
    "/workflow_form_field_conditions/{id}",
    # Dashboards - create + update
    "/dashboards",
    "/dashboards/{id}",
    "/dashboards/{dashboard_id}/panels",
    "/dashboard_panels/{id}",
    # Forms - create + update
    "/custom_forms",
    "/custom_forms/{id}",
    "/form_fields",
    "/form_fields/{id}",
    "/form_fields/{form_field_id}/options",
    "/form_field_options/{id}",
    # Playbooks - create + update
    "/playbooks",
    "/playbooks/{id}",
    "/playbooks/{playbook_id}/playbook_tasks",
    "/playbook_tasks/{id}",
    # Monitoring - create + update
    "/heartbeats",
    "/heartbeats/{id}",
    "/pulses",
    "/pulses/{id}",
    "/live_call_routers",
    "/live_call_routers/{id}",
    # Post-incident and retrospectives - create + update
    "/post_incident_reviews",
    "/post_incident_reviews/{id}",
    "/retrospective_processes",
    "/retrospective_processes/{id}",
    "/retrospective_processes/{retrospective_process_id}/groups",
    "/retrospective_process_groups/{id}",
    "/retrospective_processes/{retrospective_process_id}/retrospective_steps",
    "/retrospective_steps/{id}",
    "/postmortem_templates",
    "/postmortem_templates/{id}",
    # Status page templates
    "/status-pages/{status_page_id}/templates",
    "/templates/{id}",
    # Communications - create + update
    "/communications_groups",
    "/communications_groups/{id}",
    "/communications_stages",
    "/communications_stages/{id}",
    "/communications_templates",
    "/communications_templates/{id}",
    "/communications_types",
    "/communications_types/{id}",
    # Causes and catalog - create + update
    "/causes",
    "/causes/{id}",
    "/catalogs",
    "/catalogs/{id}",
    "/catalogs/{catalog_id}/entities",
    "/catalog_entities/{id}",
    # Sub-statuses - create + update
    "/sub_statuses",
    "/sub_statuses/{id}",
    "/incident_sub_statuses",
    "/incident_sub_statuses/{id}",
    # User notification preferences - create + update
    "/users/{user_id}/notification_rules",
    "/user_notification_rules/{id}",
    "/users/{user_id}/email_addresses",
    "/user_email_addresses/{id}",
    "/users/{user_id}/phone_numbers",
    "/user_phone_numbers/{id}",
    # Extended incident management
    "/incident_events/{id}",
    "/incident_custom_field_selections/{id}",
    "/incident_postmortems/{id}",
    "/incident_retrospective_steps/{id}",
    "/incident_status_pages/{id}",
    # Form and field management - create + update
    "/custom_fields",
    "/custom_fields/{id}",
    "/custom_field_options",
    "/custom_field_options/{id}",
    "/form_sets",
    "/form_sets/{id}",
    "/form_field_placements/{id}",
    "/form_field_placement_conditions/{id}",
    "/form_set_conditions/{id}",
    # Status pages - create + update
    "/status-pages",
    "/status-pages/{id}",
    # Status page templates
    "/status_page_templates",
    "/status_page_templates/{id}",
]

# DELETE operations are only exposed for these high-priority screenshot families.
# All other DELETE operations remain disabled in MCP by default.
DEFAULT_DELETE_ALLOWED_PATHS = [
    "/schedules/{schedule_id}",
    "/schedule_rotations/{schedule_rotation_id}",
    "/escalation_policies/{escalation_policy_id}",
    "/escalation_paths/{escalation_policy_path_id}",
    "/escalation_levels/{escalation_level_id}",
]
