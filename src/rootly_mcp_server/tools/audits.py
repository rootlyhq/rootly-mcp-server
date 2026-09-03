"""Audit log tool registration for Rootly MCP server."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Annotated, Any, Protocol

from mcp.types import ToolAnnotations
from pydantic import Field

JsonDict = dict[str, Any]
MakeAuthenticatedRequest = Callable[..., Awaitable[Any]]

ADJUSTMENTS_KEY = "argument_adjustments"

MAX_RESULTS_CEILING = 25
DEFAULT_MAX_RESULTS = 10
# Each record can carry a large `object` (full object state) and
# `object_changes` (before/after per field). Long values are trimmed so a
# page of records cannot crowd out the rest of a conversation.
VALUE_CHARS = 200
CHANGED_FIELDS_SHOWN = 25
OBJECT_FIELDS_SHOWN = 40

# Resource types the API documents for filter[item_type]. Used only to warn
# when a filter returns nothing: an unrecognised item_type is accepted and
# silently matches zero records, which reads identically to "nothing changed".
# Never used to reject input, so a newly instrumented type still works.
KNOWN_ITEM_TYPES = frozenset(
    {
        "AlertRoute",
        "AlertRoutingRule",
        "Alerts::Source",
        "ApiKey",
        "Catalog",
        "CatalogEntity",
        "CatalogEntityProperty",
        "CatalogField",
        "Cause",
        "CustomField",
        "CustomFieldOption",
        "CustomForm",
        "Dashboard",
        "EdgeConnector",
        "EdgeConnector::Action",
        "Environment",
        "EscalationPolicy",
        "EscalationPolicyPath",
        "ExportJob",
        "FormField",
        "Functionality",
        "GeniusWorkflow",
        "GeniusWorkflowGroup",
        "GeniusWorkflowRun",
        "Group",
        "GroupUser",
        "Heartbeat",
        "Incident",
        "IncidentActionItem",
        "IncidentEvent",
        "IncidentFormFieldSelection",
        "IncidentFormFieldSelectionUser",
        "IncidentPermissionSet",
        "IncidentPostMortem",
        "IncidentRoleAssignment",
        "IncidentRoleTask",
        "IncidentStatusPageEvent",
        "IncidentTask",
        "IncidentType",
        "Integrations::DatadogAccount",
        "Integrations::GithubAccount",
        "Integrations::GoogleMeetAccount",
        "Integrations::JiraAccount",
        "Integrations::MicrosoftTeamsAccount",
        "Integrations::NotionAccount",
        "Integrations::OpsgenieAccount",
        "Integrations::PagerdutyAccount",
        "Integrations::ServiceNowAccount",
        "Integrations::SlackAccount",
        "Integrations::StatusPageIoAccount",
        "Integrations::ZendeskAccount",
        "Integrations::ZoomAccount",
        "LiveCallRouter",
        "LoginActivity",
        "Membership",
        "OnCallRole",
        "Playbook",
        "PlaybookTask",
        "Role",
        "Schedule",
        "Secret",
        "Service",
        "Severity",
        "StatusPage",
    }
)

# Configuration lives in child records for these types, and those children are
# not instrumented: filtering for them returns zero rows, and the parent's
# object_changes carries only the parent's own columns. Verified against
# production. Callers asking about them are told so rather than being handed an
# empty result to misread.
UNAUDITED_CHILD_TYPES = {
    "ScheduleRotation": "Schedule",
    "ScheduleRotationUser": "Schedule",
    "ScheduleRotationActiveDay": "Schedule",
    "Shift": "Schedule",
    "OverrideShift": "Schedule",
    "EscalationLevel": "EscalationPolicy",
    "GeniusWorkflowTask": "GeniusWorkflow",
    "WorkflowTask": "GeniusWorkflow",
}


class MCPErrorLike(Protocol):
    """Protocol for MCP error helpers used by audit tools."""

    @staticmethod
    def tool_error(
        error_message: str,
        error_type: str = "execution_error",
        details: dict[str, Any] | None = None,
    ) -> JsonDict: ...

    @staticmethod
    def categorize_error(exception: Exception) -> tuple[str, str]: ...


def _clamp(name: str, value: int, notes: list[str], *, minimum: int, maximum: int) -> int:
    used = max(minimum, min(maximum, value))
    if used != value:
        notes.append(f"{name}={value} is outside the supported range; used {used} instead.")
    return used


def _trim(value: Any) -> Any:
    """Shorten long scalars so one verbose field cannot dominate the result."""
    if isinstance(value, str) and len(value) > VALUE_CHARS:
        return value[:VALUE_CHARS] + f"... [{len(value)} chars]"
    if isinstance(value, dict | list):
        rendered = str(value)
        if len(rendered) > VALUE_CHARS:
            return rendered[:VALUE_CHARS] + f"... [{len(rendered)} chars]"
    return value


def _trim_object(value: Any) -> Any:
    """Bound an object's size without collapsing it into a string.

    `_trim` stringifies a large dict, which is the wrong answer for a caller
    that explicitly asked for structured object state. Each field is trimmed
    individually instead, and the field count is capped, so the result stays a
    mapping the caller can read.
    """
    if not isinstance(value, dict):
        return _trim(value)
    trimmed: JsonDict = {}
    for key in list(value)[:OBJECT_FIELDS_SHOWN]:
        trimmed[key] = _trim(value[key])
    omitted = len(value) - len(trimmed)
    if omitted > 0:
        trimmed["_fields_omitted"] = omitted
    return trimmed


def _summarize_changes(object_changes: Any) -> tuple[JsonDict, int]:
    """Render object_changes as field -> {from, to}, trimmed and capped."""
    if not isinstance(object_changes, dict):
        return {}, 0
    # PaperTrail records each field as [before, after]; humanized payloads may
    # already be a mapping. Both shapes are handled.
    summary: JsonDict = {}
    for field, change in list(object_changes.items())[:CHANGED_FIELDS_SHOWN]:
        if isinstance(change, list) and len(change) == 2:
            summary[field] = {"from": _trim(change[0]), "to": _trim(change[1])}
        elif isinstance(change, dict) and ("from" in change or "to" in change):
            summary[field] = {
                "from": _trim(change.get("from")),
                "to": _trim(change.get("to")),
            }
        else:
            summary[field] = _trim(change)
    return summary, len(object_changes)


def register_audit_tools(
    mcp: Any,
    make_authenticated_request: MakeAuthenticatedRequest,
    mcp_error: MCPErrorLike,
) -> None:
    """Register audit log tools on the MCP server."""

    @mcp.tool(
        annotations=ToolAnnotations(
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=True,
        ),
    )
    async def list_audits(
        item_type: Annotated[
            str,
            Field(
                description=(
                    "Restrict to one resource type, e.g. 'Schedule', 'GeniusWorkflow', "
                    "'EscalationPolicy', 'ApiKey', 'Role', 'Membership'. Leave blank "
                    "for every type."
                )
            ),
        ] = "",
        user_id: Annotated[
            str,
            Field(description="Only changes made by this Rootly user ID."),
        ] = "",
        api_key_id: Annotated[
            str,
            Field(description="Only changes made with this API key ID."),
        ] = "",
        source: Annotated[
            str,
            Field(
                description=(
                    "Where the change came from: 'web', 'api', 'mobile', 'slack', "
                    "'scim' or 'oauth'."
                )
            ),
        ] = "",
        since: Annotated[
            str,
            Field(description="Only changes at or after this ISO 8601 timestamp or date."),
        ] = "",
        until: Annotated[
            str,
            Field(description="Only changes at or before this ISO 8601 timestamp or date."),
        ] = "",
        max_results: Annotated[
            int,
            Field(description=f"Maximum records to return. Max: {MAX_RESULTS_CEILING}."),
        ] = DEFAULT_MAX_RESULTS,
        include_full_object: Annotated[
            bool,
            Field(
                description=(
                    "Include each record's full object state alongside the diff. "
                    "Off by default because it is the bulk of the payload."
                )
            ),
        ] = False,
    ) -> JsonDict:
        """Read the Rootly audit log: who changed which configuration object, when, and what changed.

        Each record carries the action, the acting user, the source, and the
        before-and-after value of every modified field. Use it to answer "who
        changed this escalation policy", "what did this workflow look like
        before", or to collect change evidence for a compliance review.

        Covers roughly 60 resource types at the object level and is retained
        indefinitely. Two limits worth knowing: schedule rotations, escalation
        levels and shift overrides are not audited, and sign-in events are kept
        separately from this log.

        Returns a single page of at most 25 records, newest first, alongside
        `total_matching` so a wider search can be narrowed with the filters. It
        does not walk the whole log, so a full compliance export belongs on the
        REST endpoint rather than here.

        Requires an API token whose role has the "Audits - read" permission.
        """
        notes: list[str] = []
        try:
            capped = _clamp(
                "max_results", max_results, notes, minimum=1, maximum=MAX_RESULTS_CEILING
            )

            params: dict[str, Any] = {
                "page[size]": capped,
                "page[number]": 1,
            }
            requested_type = item_type.strip()
            if requested_type:
                params["filter[item_type]"] = requested_type
            if user_id.strip():
                params["filter[user_id]"] = user_id.strip()
            if api_key_id.strip():
                params["filter[api_key_id]"] = api_key_id.strip()
            if source.strip():
                params["filter[source]"] = source.strip().lower()
            if since.strip():
                params["filter[created_at][gte]"] = since.strip()
            if until.strip():
                params["filter[created_at][lte]"] = until.strip()

            response = await make_authenticated_request("GET", "/v1/audits", params=params)

            # This endpoint answers 404 for a role without audit access as well
            # as for a genuinely missing record, so the bare status would send
            # callers looking for a feature that is present but not permitted.
            if response.status_code == 404:
                return mcp_error.tool_error(
                    "Cannot read the audit log. This endpoint returns 404 both when "
                    "a record is missing and when the token's role lacks audit "
                    "access, and the latter is the usual cause. Enable the "
                    "'Audits - read' permission on the role under Configuration -> "
                    "Roles & Permissions, or use a token whose role already has it.",
                    "permission_error",
                )
            response.raise_for_status()

            body = response.json() or {}
            records = body.get("data") or []
            meta = body.get("meta") or {}

            audits: list[JsonDict] = []
            for record in records[:capped]:
                attrs = record.get("attributes") or {}
                changes, total_changed = _summarize_changes(attrs.get("object_changes"))
                entry: JsonDict = {
                    "id": record.get("id"),
                    "occurred_at": attrs.get("created_at"),
                    "action": attrs.get("event"),
                    "item_type": attrs.get("item_type"),
                    "item_type_display": attrs.get("item_type_display"),
                    "item_id": attrs.get("item_id"),
                    "actor": {
                        "user_id": attrs.get("user_id"),
                        "name": attrs.get("user_name"),
                        "email": attrs.get("user_email"),
                    },
                    "source": attrs.get("source"),
                    "changed_fields": changes,
                }
                if total_changed > len(changes):
                    entry["changed_fields_omitted"] = total_changed - len(changes)
                for optional in ("ip_address", "user_agent", "request_id"):
                    if attrs.get(optional):
                        entry[optional] = attrs[optional]
                if include_full_object:
                    entry["object"] = _trim_object(attrs.get("object"))
                audits.append(entry)

            result: JsonDict = {
                "audits": audits,
                "returned": len(audits),
                "total_matching": meta.get("total_count"),
            }

            # An unrecognised item_type is accepted and matches nothing, so an
            # empty result is ambiguous without saying which case it was.
            if not audits and requested_type:
                if requested_type in UNAUDITED_CHILD_TYPES:
                    parent = UNAUDITED_CHILD_TYPES[requested_type]
                    result["note"] = (
                        f"'{requested_type}' is not audited, so this is not evidence that "
                        f"nothing changed. Changes to it are not recorded under its own "
                        f"type, and its parent '{parent}' records only the parent's own "
                        f"fields. Auditing the underlying configuration change is not "
                        f"currently possible through this log."
                    )
                elif requested_type not in KNOWN_ITEM_TYPES:
                    result["note"] = (
                        f"No records matched, and '{requested_type}' is not one of the "
                        f"item_types this tool knows about. The filter accepts any value "
                        f"and silently matches nothing, so this may be a misspelling "
                        f"(values are CamelCase model names such as 'Schedule' or "
                        f"'GeniusWorkflow') or a type added since this list was written "
                        f"— either way, do not read it as no changes."
                    )

            if notes:
                result[ADJUSTMENTS_KEY] = notes
            return result

        except Exception as e:
            error_type, error_message = mcp_error.categorize_error(e)
            return mcp_error.tool_error(f"Failed to list audits: {error_message}", error_type)
