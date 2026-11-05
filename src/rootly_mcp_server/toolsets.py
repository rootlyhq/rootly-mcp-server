"""Toolsets: named groups of tools a client selects per request.

The model other large MCP servers settled on (GitHub ``X-MCP-Toolsets`` /
``/readonly``, Datadog ``?toolsets=``, Supabase ``?features=``): every tool
belongs to one toolset and a client asks for the toolsets it needs. Every
client pays for the whole ``tools/list`` on every conversation; the full
surface is ~125k tokens, ``?toolsets=core`` ~10k. Opt-in: with no selection a
client still gets every tool, so existing configurations are unchanged.

Selection, per request (hosted) or per process (self-hosted):

* ``?toolsets=core,alerts`` or ``X-MCP-Toolsets: core,alerts``; ``all`` for
  everything. Unknown names are ignored.
* ``?readonly=true`` or ``X-MCP-Readonly: true`` keeps read-only tools only.
* ``ROOTLY_MCP_TOOLSETS`` sets the default when a request names none
  (``all`` unless set).

Generated tools are assigned by their OpenAPI tag; curated tools by name. A
tool with no toolset is a test failure (tests/unit/test_toolsets.py), so a new
tag or curated tool has to be placed deliberately.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterable, Mapping
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any

ALL = "all"
CORE = "core"

# name -> one-line description, shown in server instructions and errors.
TOOLSETS: dict[str, str] = {
    CORE: "Everyday incident response: find and update incidents, alerts, who is on call.",
    "incidents": "Incidents in depth: action items, events, roles, retrospectives, form fields.",
    "alerts": "Alerts, alert sources, routes and routing rules, urgencies, heartbeats.",
    "oncall": "Schedules, rotations, shifts, overrides, escalation policies and paths.",
    "workflows": "Workflows, workflow tasks and runs, playbooks.",
    "catalog": "Services, functionalities, teams, environments, catalogs.",
    "status_pages": "Status pages, templates and pulses.",
    "config": "Severities, causes, incident types, forms, retrospective processes, dashboards.",
    "users": "Users, notification rules, contact methods.",
    "ai": "AI SRE investigations, AI automations and skills.",
    "observability": "Rootly Observability logs and patterns.",
    "audit": "Audit log.",
}

# The default hosted surface: what an assistant needs for most incident work.
# Kept small on purpose (clients degrade past ~30-50 loaded tools); anything
# else is one ?toolsets= away and named in the server instructions.
CORE_TOOLS: frozenset[str] = frozenset(
    {
        # Incidents
        "search_incidents",
        "list_incidents",
        "get_incident",
        "create_incident",
        "update_incident",
        "find_related_incidents",
        "suggest_solutions",
        "get_incident_retrospective",
        "list_incident_action_items",
        "create_incident_action_item",
        "list_incident_events",
        # Alerts
        "get_alert_by_short_id",
        "list_alerts",
        "get_alert",
        "list_incident_alerts",
        # On-call
        "get_oncall_handoff_summary",
        "get_oncall_schedule_summary",
        "list_shifts",
        "check_responder_availability",
        "list_schedules",
        "list_escalation_policies",
        # Lookups
        "get_current_user",
        "list_users",
        "get_user",
        "list_teams",
        "list_services",
        "list_severities",
        "get_server_version",
        # Discovery: which API endpoints exist, so a model can name the
        # toolset it needs. Local, no API call.
        "list_endpoints",
    }
)

# OpenAPI tag (first tag of an operation) -> toolset.
TAG_TOOLSETS: dict[str, str] = {
    # incidents
    "Incidents": "incidents",
    "IncidentActionItems": "incidents",
    "IncidentEvents": "incidents",
    "IncidentFormFieldSelections": "incidents",
    "IncidentRetrospectives": "incidents",
    "IncidentRetrospectiveSteps": "incidents",
    "IncidentRoles": "incidents",
    "IncidentSubStatuses": "incidents",
    "Meeting Recordings": "incidents",
    # alerts
    "Alerts": "alerts",
    "AlertEvents": "alerts",
    "AlertGroups": "alerts",
    "AlertRoutes": "alerts",
    "AlertRoutingRules": "alerts",
    "AlertSources": "alerts",
    "AlertUrgencies": "alerts",
    "Heartbeats": "alerts",
    "LiveCallRouters": "alerts",
    # on-call
    "Schedules": "oncall",
    "ScheduleRotations": "oncall",
    "ScheduleRotationActiveDays": "oncall",
    "ScheduleRotationUsers": "oncall",
    "Shifts": "oncall",
    "OverrideShifts": "oncall",
    "OnCallShadows": "oncall",
    "OnCallRoles": "oncall",
    "ShiftCoverageRequests": "oncall",
    "EscalationPolicies": "oncall",
    "EscalationPaths": "oncall",
    "EscalationLevels": "oncall",
    "EscalationLevelsPolicies": "oncall",
    "EscalationLevelsPath": "oncall",
    # workflows
    "Workflows": "workflows",
    "WorkflowTasks": "workflows",
    "WorkflowRuns": "workflows",
    "WorkflowGroups": "workflows",
    "WorkflowFormFieldConditions": "workflows",
    "Playbooks": "workflows",
    "PlaybookTasks": "workflows",
    # catalog
    "Services": "catalog",
    "Functionalities": "catalog",
    "Teams": "catalog",
    "Environments": "catalog",
    "Catalogs": "catalog",
    "CatalogEntities": "catalog",
    "CatalogProperties": "catalog",
    # status pages
    "StatusPages": "status_pages",
    "StatusPageTemplates": "status_pages",
    "Pulses": "status_pages",
    # config
    "Severities": "config",
    "Causes": "config",
    "IncidentTypes": "config",
    "SubStatuses": "config",
    "FormFields": "config",
    "FormFieldOptions": "config",
    "FormFieldPlacements": "config",
    "FormFieldPlacementConditions": "config",
    "FormSets": "config",
    "FormSetConditions": "config",
    "CustomForms": "config",
    "RetrospectiveProcesses": "config",
    "RetrospectiveProcessGroups": "config",
    "RetrospectiveSteps": "config",
    "Dashboards": "config",
    "DashboardPanels": "config",
    # users
    "Users": "users",
    "UserNotificationRules": "users",
    "UserEmailAddresses": "users",
    "UserPhoneNumbers": "users",
    # ai
    "AIAutomations": "ai",
    "AISkills": "ai",
    "Investigations": "ai",
    # observability
    "Observability": "observability",
    # audit
    "Audits": "audit",
}

# Curated (hand-written) tools -> toolset.
CURATED_TOOLSETS: dict[str, str] = {
    "list_endpoints": "config",
    "get_server_version": "config",
    "search_incidents": "incidents",
    "collect_incidents": "incidents",
    "list_incidents": "incidents",
    "get_incident": "incidents",
    "create_incident": "incidents",
    "update_incident": "incidents",
    "find_related_incidents": "incidents",
    "suggest_solutions": "incidents",
    "get_incident_retrospective": "incidents",
    "get_incident_meeting_transcripts": "incidents",
    "list_incident_post_mortems": "incidents",
    "list_incident_roles": "incidents",
    "get_alert_by_short_id": "alerts",
    "get_oncall_handoff_summary": "oncall",
    "get_oncall_schedule_summary": "oncall",
    "get_oncall_shift_metrics": "oncall",
    "get_shift_incidents": "oncall",
    "get_schedule_shifts": "oncall",
    "list_shifts": "oncall",
    "check_responder_availability": "oncall",
    "create_override_recommendation": "oncall",
    "check_oncall_health_risk": "oncall",
    "get_workflow_task_params_schema": "workflows",
    "list_investigations": "ai",
    "get_investigation": "ai",
    "get_investigation_progress": "ai",
    "start_investigation": "ai",
    "cancel_investigation": "ai",
    "list_audits": "audit",
}

# Old hosted profile names, mapped onto toolsets. ``slim`` keeps its own
# prebuilt server (see __main__), so it is not a toolset here.
PROFILE_TOOLSETS: dict[str, frozenset[str]] = {
    "full": frozenset({ALL}),
    "all": frozenset({ALL}),
    "default": frozenset({ALL}),
}

TOOLSETS_ENV = "ROOTLY_MCP_TOOLSETS"
TOOLSETS_QUERY_PARAM = "toolsets"
TOOLSETS_HEADER = "x-mcp-toolsets"
READONLY_QUERY_PARAM = "readonly"
READONLY_HEADER = "x-mcp-readonly"
TOOL_PROFILE_QUERY_PARAM = "tool_profile"
TOOL_PROFILE_HEADER = "x-rootly-tool-profile"


@dataclass(frozen=True)
class ToolSelection:
    """Which tools a request may see and call."""

    toolsets: frozenset[str]
    readonly: bool = False

    @property
    def everything(self) -> bool:
        return ALL in self.toolsets and not self.readonly

    def allows(self, name: str, toolset: str | None, read_only_hint: bool | None) -> bool:
        if self.readonly and read_only_hint is not True:
            return False
        if ALL in self.toolsets:
            return True
        if CORE in self.toolsets and name in CORE_TOOLS:
            return True
        return toolset is not None and toolset in self.toolsets


# Set per HTTP request by ToolSelectionCaptureMiddleware (transport layer);
# unset for stdio, where the process default applies.
requested_selection: ContextVar[ToolSelection | None] = ContextVar(
    "rootly_requested_tool_selection", default=None
)


def parse_toolsets(raw: str | None) -> frozenset[str] | None:
    """``"core, alerts"`` -> ``{"core", "alerts"}``; unknown names dropped.

    Returns None when nothing usable was given, so the caller's default holds.
    """
    if not raw:
        return None
    names = {part.strip().lower().replace("-", "_") for part in raw.split(",")}
    known = {name for name in names if name == ALL or name in TOOLSETS}
    return frozenset(known) or None


# Fail closed, as GitHub's X-MCP-Readonly does: only an explicit "off" value
# (or absence) leaves writes on, so a typo like readonly=enabled is read-only.
_READONLY_OFF = frozenset({"", "false", "f", "no", "n", "0", "off"})


def _readonly_requested(raw: str | None) -> bool:
    return raw is not None and raw.strip().lower() not in _READONLY_OFF


def default_toolsets(*, hosted: bool) -> frozenset[str]:
    """The selection when a request names none: everything, unless the operator
    sets ROOTLY_MCP_TOOLSETS.

    Toolsets are opt-in. PostHog (30 days, classic hosted server) showed `core`
    alone fully covers 42% of users, so a narrower default would break the
    rest; clients choose ``?toolsets=core`` for the small surface instead.
    """
    del hosted  # same default on both; kept so a future per-deployment default is one line
    configured = parse_toolsets(os.getenv(TOOLSETS_ENV))
    if configured is not None:
        return configured
    return frozenset({ALL})


def selection_from_request(
    query_params: Mapping[str, str], headers: Mapping[str, str]
) -> ToolSelection | None:
    """What an HTTP request asked for, or None to use the server default."""
    toolsets = parse_toolsets(query_params.get(TOOLSETS_QUERY_PARAM)) or parse_toolsets(
        headers.get(TOOLSETS_HEADER)
    )
    if toolsets is None:
        # ?tool_profile=full predates toolsets and keeps meaning everything.
        profile = (
            query_params.get(TOOL_PROFILE_QUERY_PARAM) or headers.get(TOOL_PROFILE_HEADER) or ""
        )
        toolsets = PROFILE_TOOLSETS.get(profile.strip().lower())
    readonly = _readonly_requested(query_params.get(READONLY_QUERY_PARAM)) or _readonly_requested(
        headers.get(READONLY_HEADER)
    )
    if toolsets is None and not readonly:
        return None
    return ToolSelection(toolsets or frozenset(), readonly)


def toolset_for(name: str, tag: str | None) -> str | None:
    """The toolset a tool belongs to (curated name first, then OpenAPI tag)."""
    if name in CURATED_TOOLSETS:
        return CURATED_TOOLSETS[name]
    if tag is not None:
        return TAG_TOOLSETS.get(tag)
    return None


def describe_toolsets(names: Iterable[str] = ()) -> str:
    """``name (description)`` lines for instructions and error messages."""
    wanted = list(names) or list(TOOLSETS)
    return "; ".join(f"{name}: {TOOLSETS[name]}" for name in wanted if name in TOOLSETS)


def server_instructions(default: Iterable[str] = (CORE,)) -> str:
    """MCP ``instructions``: what is loaded and how to get the rest.

    Sent once per session. A model that needs a tool outside the selection can
    tell the user the exact change instead of concluding Rootly cannot do it.
    ``default`` is the server's default selection (``ROOTLY_MCP_TOOLSETS``).
    """
    loaded = sorted(default)
    groups = "; ".join(f"`{name}` ({description})" for name, description in TOOLSETS.items())
    return (
        "Rootly incident management tools are grouped into toolsets. Unless the "
        "connection URL says otherwise, only "
        + ", ".join(f"`{name}`" for name in loaded)
        + f" {'is' if len(loaded) == 1 else 'are'} loaded. All toolsets: "
        f"{groups}. To use others, the user adds ?toolsets=<names> to the server URL "
        "(e.g. https://mcp.rootly.com/mcp?toolsets=core,workflows, or "
        "?toolsets=all for everything) or sends the X-MCP-Toolsets header, and "
        "reconnects. ?readonly=true limits any selection to read-only tools."
    )


TOOL_PROFILE_DEPRECATED_EVENT = "tool_profile_deprecated"
_MAX_USER_AGENT_CHARS = 256
_deprecation_logger: Any = None


def tool_profile_in_request(
    query_params: Mapping[str, str], headers: Mapping[str, str]
) -> tuple[str, str] | None:
    """``(source, value)`` when a request still selects a legacy tool profile."""
    if value := query_params.get(TOOL_PROFILE_QUERY_PARAM):
        return "query", value
    if value := headers.get(TOOL_PROFILE_HEADER):
        return "header", value
    return None


def log_tool_profile_deprecated(
    source: str, value: str, headers: Mapping[str, str], token_fingerprint: str
) -> None:
    """One structured warning per request that uses ?tool_profile= / X-Rootly-Tool-Profile.

    Both still work (``full`` -> all toolsets, ``slim`` -> the slim list); the
    event is how the remaining users are found before removal, as with SSE.
    """
    global _deprecation_logger
    if _deprecation_logger is None:
        from .utils import json_line_logger

        _deprecation_logger = json_line_logger("rootly_mcp_server.tool_profile_deprecation_json")
    event = {
        "event": TOOL_PROFILE_DEPRECATED_EVENT,
        "level": "warning",
        "message": "tool_profile is deprecated; use ?toolsets= / X-MCP-Toolsets",
        "source": source,
        "tool_profile": value[:32],
        "user_agent": headers.get("user-agent", "")[:_MAX_USER_AGENT_CHARS],
        "token_fingerprint": token_fingerprint,
    }
    _deprecation_logger.warning(
        json.dumps({k: v for k, v in event.items() if v != ""}, separators=(",", ":"))
    )
