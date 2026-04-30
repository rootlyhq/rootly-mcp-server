"""MCP resource registration for Rootly MCP server."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
from typing import Any, Protocol

from .incidents import _resolve_incident_reference_to_uuid

JsonDict = dict[str, Any]
MakeAuthenticatedRequest = Callable[..., Awaitable[Any]]
StripHeavyNestedData = Callable[[JsonDict], JsonDict]


class MCPErrorLike(Protocol):
    """Protocol for MCP error categorization used by resource handlers."""

    @staticmethod
    def categorize_error(exception: Exception) -> tuple[str, str]: ...


def register_resource_handlers(
    mcp: Any,
    make_authenticated_request: MakeAuthenticatedRequest,
    strip_heavy_nested_data: StripHeavyNestedData,
    mcp_error: MCPErrorLike,
) -> None:
    """Register MCP resources for incidents, teams, and operational context."""

    @mcp.resource("incident://{incident_id}")
    async def get_incident_resource(incident_id: str) -> JsonDict:
        """Expose incident details as an MCP resource for easy reference and context."""
        try:
            resolved_incident_id = await _resolve_incident_reference_to_uuid(
                incident_id, make_authenticated_request
            )
            response = await make_authenticated_request(
                "GET", f"/v1/incidents/{resolved_incident_id}"
            )
            response.raise_for_status()
            incident_data = strip_heavy_nested_data({"data": [response.json().get("data", {})]})

            incident = incident_data.get("data", [{}])[0]
            attributes = incident.get("attributes", {})

            text_content = f"""Incident Reference: {incident_id}
Resolved Incident ID: {resolved_incident_id}
Title: {attributes.get("title", "N/A")}
Status: {attributes.get("status", "N/A")}
Severity: {attributes.get("severity", "N/A")}
Created: {attributes.get("created_at", "N/A")}
Updated: {attributes.get("updated_at", "N/A")}
Summary: {attributes.get("summary", "N/A")}
URL: {attributes.get("url", "N/A")}"""

            return {
                "uri": f"incident://{incident_id}",
                "name": f"Incident {incident_id}",
                "text": text_content,
                "mimeType": "text/plain",
            }
        except Exception as e:
            error_type, error_message = mcp_error.categorize_error(e)
            return {
                "uri": f"incident://{incident_id}",
                "name": f"Incident {incident_id} (Error)",
                "text": f"Error ({error_type}): {error_message}",
                "mimeType": "text/plain",
            }

    @mcp.resource("team://{team_id}")
    async def get_team_resource(team_id: str) -> JsonDict:
        """Expose team details as an MCP resource for easy reference and context."""
        try:
            response = await make_authenticated_request("GET", f"/v1/teams/{team_id}")
            response.raise_for_status()
            team_data = response.json()

            team = team_data.get("data", {})
            attributes = team.get("attributes", {})

            text_content = f"""Team #{team_id}
Name: {attributes.get("name", "N/A")}
Color: {attributes.get("color", "N/A")}
Slug: {attributes.get("slug", "N/A")}
Created: {attributes.get("created_at", "N/A")}
Updated: {attributes.get("updated_at", "N/A")}"""

            return {
                "uri": f"team://{team_id}",
                "name": f"Team: {attributes.get('name', team_id)}",
                "text": text_content,
                "mimeType": "text/plain",
            }
        except Exception as e:
            error_type, error_message = mcp_error.categorize_error(e)
            return {
                "uri": f"team://{team_id}",
                "name": f"Team #{team_id} (Error)",
                "text": f"Error ({error_type}): {error_message}",
                "mimeType": "text/plain",
            }

    @mcp.resource("rootly://incidents")
    async def list_incidents_resource() -> JsonDict:
        """List recent incidents as an MCP resource for quick reference."""
        try:
            response = await make_authenticated_request(
                "GET",
                "/v1/incidents",
                params={
                    "page[size]": 10,
                    "page[number]": 1,
                    "include": "",
                    "fields[incidents]": "id,title,status",
                },
            )
            response.raise_for_status()
            data = strip_heavy_nested_data(response.json())

            incidents = data.get("data", [])
            text_lines = ["Recent Incidents:\n"]

            for incident in incidents:
                attrs = incident.get("attributes", {})
                text_lines.append(
                    f"• #{incident.get('id', 'N/A')} - {attrs.get('title', 'N/A')} [{attrs.get('status', 'N/A')}]"
                )

            return {
                "uri": "rootly://incidents",
                "name": "Recent Incidents",
                "text": "\n".join(text_lines),
                "mimeType": "text/plain",
            }
        except Exception as e:
            error_type, error_message = mcp_error.categorize_error(e)
            return {
                "uri": "rootly://incidents",
                "name": "Recent Incidents (Error)",
                "text": f"Error ({error_type}): {error_message}",
                "mimeType": "text/plain",
            }

    @mcp.resource("rootly://oncall-status")
    async def get_oncall_status_resource() -> JsonDict:
        """Show current on-call status across teams - critical for incident response context."""
        try:
            # Get current schedules
            schedules_response = await make_authenticated_request("GET", "/v1/schedules")
            schedules_response.raise_for_status()
            schedules_data = schedules_response.json().get("data", [])

            status_lines = ["🚨 CURRENT ON-CALL STATUS", "=" * 40]

            if not schedules_data:
                status_lines.append("No schedules found")
            else:
                for schedule in schedules_data[:10]:  # Limit to first 10 schedules
                    attrs = schedule.get("attributes", {})
                    name = attrs.get("name", "Unknown Schedule")
                    status_lines.append(f"\n📅 {name}")

                    # Get current shifts for this schedule
                    schedule_id = schedule.get("id")
                    if schedule_id:
                        now = datetime.utcnow()
                        shifts_response = await make_authenticated_request(
                            "GET",
                            f"/v1/schedules/{schedule_id}/shifts",
                            params={
                                "from": (now - timedelta(hours=1)).isoformat(),
                                "to": (now + timedelta(hours=1)).isoformat(),
                            },
                        )
                        if shifts_response.status_code == 200:
                            shifts = shifts_response.json().get("data", [])
                            if shifts:
                                for shift in shifts[:3]:  # Show up to 3 current shifts
                                    shift_attrs = shift.get("attributes", {})
                                    user_name = "Unknown User"
                                    user_data = shift_attrs.get("user", {})
                                    if isinstance(user_data, dict):
                                        user_name = user_data.get("name", "Unknown User")
                                    status_lines.append(f"  👤 {user_name}")
                            else:
                                status_lines.append("  ⚠️  No active shifts")
                        else:
                            status_lines.append("  ❌ Could not fetch shifts")

            status_lines.append(
                f"\n🕐 Updated: {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')} UTC"
            )

            return {
                "uri": "rootly://oncall-status",
                "name": "Current On-Call Status",
                "text": "\n".join(status_lines),
                "mimeType": "text/plain",
            }
        except Exception as e:
            error_type, error_message = mcp_error.categorize_error(e)
            return {
                "uri": "rootly://oncall-status",
                "name": "On-Call Status (Error)",
                "text": f"Error ({error_type}): {error_message}",
                "mimeType": "text/plain",
            }

    @mcp.resource("rootly://workflow-guide")
    async def get_workflow_guide_resource() -> JsonDict:
        """Provide workflow guidance for common Rootly operations."""
        guide_content = """🎯 ROOTLY WORKFLOW GUIDE

🚨 INCIDENT RESPONSE WORKFLOW:
1️⃣ Check ongoing incidents: listIncidents(status="open,investigating")
2️⃣ Create new incident: createIncident(title="...", summary="...")
3️⃣ Find similar past incidents: find_related_incidents(incident_description="...")
4️⃣ Get solution suggestions: suggest_solutions(incident_id="...")
5️⃣ Add action items: createIncidentActionItem(incident_id="...", description="...")
6️⃣ Check on-call status: get_oncall_handoff_summary()

📅 SCHEDULE MANAGEMENT WORKFLOW:
1️⃣ View schedules: listSchedules()
2️⃣ Check current shifts: getScheduleShifts(schedule_id="...")
3️⃣ Create override: createOverrideShift(schedule_id="...", user_id="...", start_time="...", end_time="...")
4️⃣ Review metrics: get_oncall_shift_metrics(start_date="...", end_date="...")

📊 MONITORING SETUP WORKFLOW:
1️⃣ Review alerts: listAlerts()
2️⃣ Update alert configuration: updateAlert(id="...", ...)
3️⃣ Create dashboard: createDashboard(name="...", description="...")
4️⃣ Set up heartbeats: createHeartbeat(name="...", url="...")

💡 BEST PRACTICES:
• Use find_related_incidents early in incident response
• Check oncall status before escalating
• Review recent incidents for patterns: rootly://incidents resource
• Use team resources for context: team://{team_id}
"""

        return {
            "uri": "rootly://workflow-guide",
            "name": "Rootly Workflow Guide",
            "text": guide_content,
            "mimeType": "text/plain",
        }
