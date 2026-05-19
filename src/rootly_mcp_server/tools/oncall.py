"""On-call tool registration for Rootly MCP server."""

from __future__ import annotations

import asyncio
import os
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Annotated, Any, Protocol, cast

import httpx
from pydantic import Field

from ..och_client import OnCallHealthClient
from ..validators import validate_page_params

JsonDict = dict[str, Any]
MakeAuthenticatedRequest = Callable[..., Awaitable[Any]]
SHIFT_INCIDENT_QUERY_FIELDS = (
    "title,status,started_at,resolved_at,created_at,summary,"
    "customer_impact_summary,mitigation,severity,url"
)
DEFAULT_MAX_SHIFT_INCIDENT_RESULTS = 100


def _truncate_text(value: Any, max_length: int = 280) -> str | None:
    """Keep large narrative fields compact enough for MCP clients."""
    if not value:
        return None

    if not isinstance(value, str):
        value = str(value)

    value = value.strip()
    if not value:
        return None

    if len(value) <= max_length:
        return str(value)

    return f"{value[: max_length - 1].rstrip()}…"


def _normalize_incident_severity(severity: Any) -> str:
    """Normalize incident severity to a stable string for display/grouping."""
    if severity is None:
        return "unknown"

    if isinstance(severity, str):
        normalized = severity.strip()
        return normalized or "unknown"

    if isinstance(severity, list):
        for item in severity:
            normalized = _normalize_incident_severity(item)
            if normalized != "unknown":
                return normalized
        return "unknown"

    if isinstance(severity, dict):
        for key in ("name", "slug", "label", "value", "severity", "title", "id"):
            value = severity.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()

        # Common nested payloads from related resources.
        for nested_key in ("attributes", "data"):
            nested_value = severity.get(nested_key)
            normalized = _normalize_incident_severity(nested_value)
            if normalized != "unknown":
                return normalized

        # Last-resort: pick first non-empty string in dict values.
        for value in severity.values():
            if isinstance(value, str) and value.strip():
                return value.strip()

        return "unknown"

    return str(severity)


class MCPErrorLike(Protocol):
    """Protocol for MCP error helper methods used by tool modules."""

    @staticmethod
    def tool_error(
        error_message: str,
        error_type: str = "execution_error",
        details: dict[str, Any] | None = None,
    ) -> JsonDict: ...

    @staticmethod
    def categorize_error(exception: Exception) -> tuple[str, str]: ...


def register_oncall_tools(
    mcp: Any,
    make_authenticated_request: MakeAuthenticatedRequest,
    mcp_error: MCPErrorLike,
) -> None:
    """Register on-call analysis and scheduling tools on the MCP server."""

    @mcp.tool()
    async def get_oncall_shift_metrics(
        start_date: Annotated[
            str,
            Field(
                description="Start date for metrics (ISO 8601 format, e.g., '2025-10-01' or '2025-10-01T00:00:00Z')"
            ),
        ],
        end_date: Annotated[
            str,
            Field(
                description="End date for metrics (ISO 8601 format, e.g., '2025-10-31' or '2025-10-31T23:59:59Z')"
            ),
        ],
        user_ids: Annotated[
            str, Field(description="Comma-separated list of user IDs to filter by (optional)")
        ] = "",
        schedule_ids: Annotated[
            str, Field(description="Comma-separated list of schedule IDs to filter by (optional)")
        ] = "",
        team_ids: Annotated[
            str,
            Field(
                description="Comma-separated list of team IDs to filter by (requires querying schedules first)"
            ),
        ] = "",
        group_by: Annotated[
            str, Field(description="Group results by: 'user', 'schedule', 'team', or 'none'")
        ] = "user",
    ) -> dict:
        """
        📊 Get on-call workload metrics and shift statistics - ESSENTIAL for fairness and planning.

        WHEN TO USE:
        • Monthly/quarterly reviews of on-call distribution
        • Before adjusting schedules to ensure fair workload
        • For management reporting on on-call burden
        • When investigating potential on-call burnout

        Returns shift counts, total hours, and statistics grouped by user, schedule, or team.

        Examples:
        - Monthly report: start_date='2025-10-01', end_date='2025-10-31'
        - Specific user: start_date='2025-10-01', end_date='2025-10-31', user_ids='123,456'
        - Specific team: team_ids='team-1' (will query schedules for that team first)
        """
        try:
            from collections import defaultdict
            from datetime import datetime, timedelta

            # Build query parameters
            params: dict[str, Any] = {
                "from": start_date,
                "to": end_date,
            }

            # Fetch schedules (schedules don't have team relationship, they have owner_group_ids)
            schedules_response = await make_authenticated_request(
                "GET", "/v1/schedules", params={"page[size]": 100}
            )

            if schedules_response is None:
                return mcp_error.tool_error(
                    "Failed to get schedules: API request returned None", "execution_error"
                )

            schedules_response.raise_for_status()
            schedules_data = schedules_response.json()

            all_schedules = schedules_data.get("data", [])

            # Collect all unique team IDs from schedules' owner_group_ids
            team_ids_set = set()
            for schedule in all_schedules:
                owner_group_ids = schedule.get("attributes", {}).get("owner_group_ids", [])
                team_ids_set.update(owner_group_ids)

            # Fetch all teams
            teams_map = {}
            if team_ids_set:
                teams_response = await make_authenticated_request(
                    "GET", "/v1/teams", params={"page[size]": 100}
                )
                if teams_response and teams_response.status_code == 200:
                    teams_data = teams_response.json()
                    for team in teams_data.get("data", []):
                        teams_map[team.get("id")] = team

            # Build schedule -> team mapping
            schedule_to_team_map = {}
            for schedule in all_schedules:
                schedule_id = schedule.get("id")
                schedule_name = schedule.get("attributes", {}).get("name", "Unknown")
                owner_group_ids = schedule.get("attributes", {}).get("owner_group_ids", [])

                # Use the first owner group as the primary team
                if owner_group_ids:
                    team_id = owner_group_ids[0]
                    team_attrs = teams_map.get(team_id, {}).get("attributes", {})
                    team_name = team_attrs.get("name", "Unknown Team")
                    schedule_to_team_map[schedule_id] = {
                        "team_id": team_id,
                        "team_name": team_name,
                        "schedule_name": schedule_name,
                    }

            # Handle team filtering (requires multi-step query)
            target_schedule_ids = []
            if team_ids:
                team_id_list = [tid.strip() for tid in team_ids.split(",") if tid.strip()]

                # Filter schedules by team
                for schedule_id, team_info in schedule_to_team_map.items():
                    if str(team_info["team_id"]) in team_id_list:
                        target_schedule_ids.append(schedule_id)

            # Apply schedule filtering
            if schedule_ids:
                schedule_id_list = [sid.strip() for sid in schedule_ids.split(",") if sid.strip()]
                target_schedule_ids.extend(schedule_id_list)

            if target_schedule_ids:
                params["schedule_ids[]"] = target_schedule_ids

            # Apply user filtering
            if user_ids:
                user_id_list = [uid.strip() for uid in user_ids.split(",") if uid.strip()]
                params["user_ids[]"] = user_id_list

            # Include relationships for richer data
            params["include"] = "user,shift_override,on_call_role,schedule_rotation"

            # Query shifts
            try:
                shifts_response = await make_authenticated_request(
                    "GET", "/v1/shifts", params=params
                )

                if shifts_response is None:
                    return mcp_error.tool_error(
                        "Failed to get shifts: API request returned None", "execution_error"
                    )

                shifts_response.raise_for_status()
                shifts_data = shifts_response.json()

                if shifts_data is None:
                    return mcp_error.tool_error(
                        "Failed to get shifts: API returned null/empty response",
                        "execution_error",
                        details={"status": shifts_response.status_code},
                    )

                shifts = shifts_data.get("data", [])
                included = shifts_data.get("included", [])
            except AttributeError as e:
                return mcp_error.tool_error(
                    f"Failed to get shifts: Response object error - {str(e)}",
                    "execution_error",
                    details={"params": params},
                )
            except Exception as e:
                return mcp_error.tool_error(
                    f"Failed to get shifts: {str(e)}",
                    "execution_error",
                    details={"params": params, "error_type": type(e).__name__},
                )

            # Build lookup maps for included resources
            users_map = {}
            on_call_roles_map = {}
            for resource in included:
                if resource.get("type") == "users":
                    users_map[resource.get("id")] = resource
                elif resource.get("type") == "on_call_roles":
                    on_call_roles_map[resource.get("id")] = resource

            # Calculate metrics
            metrics: dict[str, dict[str, Any]] = defaultdict(
                lambda: {
                    "shift_count": 0,
                    "total_hours": 0.0,
                    "override_count": 0,
                    "regular_count": 0,
                    "primary_count": 0,
                    "secondary_count": 0,
                    "primary_hours": 0.0,
                    "secondary_hours": 0.0,
                    "unknown_role_count": 0,
                    "unique_days": set(),
                    "shifts": [],
                }
            )

            for shift in shifts:
                attrs = shift.get("attributes", {})
                relationships = shift.get("relationships", {})

                # Parse timestamps
                starts_at = attrs.get("starts_at")
                ends_at = attrs.get("ends_at")
                is_override = attrs.get("is_override", False)
                schedule_id = attrs.get("schedule_id")

                # Calculate shift duration in hours and track unique days
                duration_hours = 0.0
                shift_days = set()
                if starts_at and ends_at:
                    try:
                        start_dt = datetime.fromisoformat(starts_at.replace("Z", "+00:00"))
                        end_dt = datetime.fromisoformat(ends_at.replace("Z", "+00:00"))
                        duration_hours = (end_dt - start_dt).total_seconds() / 3600

                        # Track all unique calendar days this shift spans
                        shift_start_date = start_dt.date()
                        shift_end_date = end_dt.date()
                        while shift_start_date <= shift_end_date:
                            shift_days.add(shift_start_date)
                            shift_start_date += timedelta(days=1)
                    except (ValueError, AttributeError):
                        pass

                # Get user info
                user_rel = relationships.get("user", {}).get("data") or {}
                user_id = user_rel.get("id")
                user_name = "Unknown"
                user_email = ""

                if user_id and user_id in users_map:
                    user_attrs = users_map[user_id].get("attributes", {})
                    user_name = user_attrs.get("full_name") or user_attrs.get("email", "Unknown")
                    user_email = user_attrs.get("email", "")

                # Get on-call role info (primary vs secondary)
                role_rel = relationships.get("on_call_role", {}).get("data") or {}
                role_id = role_rel.get("id")
                role_name = "unknown"
                is_primary = False

                if role_id and role_id in on_call_roles_map:
                    role_attrs = on_call_roles_map[role_id].get("attributes", {})
                    role_name = role_attrs.get("name", "").lower()
                    # Typically primary roles contain "primary" and secondary contain "secondary"
                    # Common patterns: "Primary", "Secondary", "L1", "L2", etc.
                    is_primary = "primary" in role_name or role_name == "l1" or role_name == "p1"

                # Determine grouping key
                if group_by == "user":
                    key = f"{user_id}|{user_name}"
                elif group_by == "schedule":
                    schedule_info = schedule_to_team_map.get(schedule_id, {})
                    schedule_name = schedule_info.get("schedule_name", f"schedule_{schedule_id}")
                    key = f"{schedule_id}|{schedule_name}"
                elif group_by == "team":
                    team_info = schedule_to_team_map.get(schedule_id, {})
                    if team_info:
                        team_id = team_info["team_id"]
                        team_name = team_info["team_name"]
                        key = f"{team_id}|{team_name}"
                    else:
                        key = "unknown_team|Unknown Team"
                else:
                    key = "all"

                # Update metrics
                metrics[key]["shift_count"] += 1
                metrics[key]["total_hours"] += duration_hours

                if is_override:
                    metrics[key]["override_count"] += 1
                else:
                    metrics[key]["regular_count"] += 1

                # Track primary vs secondary
                if role_id:
                    if is_primary:
                        metrics[key]["primary_count"] += 1
                        metrics[key]["primary_hours"] += duration_hours
                    else:
                        metrics[key]["secondary_count"] += 1
                        metrics[key]["secondary_hours"] += duration_hours
                else:
                    metrics[key]["unknown_role_count"] += 1

                # Track unique days
                metrics[key]["unique_days"].update(shift_days)

                metrics[key]["shifts"].append(
                    {
                        "shift_id": shift.get("id"),
                        "starts_at": starts_at,
                        "ends_at": ends_at,
                        "duration_hours": round(duration_hours, 2),
                        "is_override": is_override,
                        "schedule_id": schedule_id,
                        "user_id": user_id,
                        "user_name": user_name,
                        "user_email": user_email,
                        "role_name": role_name,
                        "is_primary": is_primary,
                    }
                )

            # Format results
            results = []
            for key, data in metrics.items():
                if group_by == "user":
                    user_id, user_name = key.split("|", 1)
                    result = {
                        "user_id": user_id,
                        "user_name": user_name,
                        "shift_count": data["shift_count"],
                        "days_on_call": len(data["unique_days"]),
                        "total_hours": round(data["total_hours"], 2),
                        "regular_shifts": data["regular_count"],
                        "override_shifts": data["override_count"],
                        "primary_shifts": data["primary_count"],
                        "secondary_shifts": data["secondary_count"],
                        "primary_hours": round(data["primary_hours"], 2),
                        "secondary_hours": round(data["secondary_hours"], 2),
                        "unknown_role_shifts": data["unknown_role_count"],
                    }
                elif group_by == "schedule":
                    schedule_id, schedule_name = key.split("|", 1)
                    result = {
                        "schedule_id": schedule_id,
                        "schedule_name": schedule_name,
                        "shift_count": data["shift_count"],
                        "days_on_call": len(data["unique_days"]),
                        "total_hours": round(data["total_hours"], 2),
                        "regular_shifts": data["regular_count"],
                        "override_shifts": data["override_count"],
                        "primary_shifts": data["primary_count"],
                        "secondary_shifts": data["secondary_count"],
                        "primary_hours": round(data["primary_hours"], 2),
                        "secondary_hours": round(data["secondary_hours"], 2),
                        "unknown_role_shifts": data["unknown_role_count"],
                    }
                elif group_by == "team":
                    team_id, team_name = key.split("|", 1)
                    result = {
                        "team_id": team_id,
                        "team_name": team_name,
                        "shift_count": data["shift_count"],
                        "days_on_call": len(data["unique_days"]),
                        "total_hours": round(data["total_hours"], 2),
                        "regular_shifts": data["regular_count"],
                        "override_shifts": data["override_count"],
                        "primary_shifts": data["primary_count"],
                        "secondary_shifts": data["secondary_count"],
                        "primary_hours": round(data["primary_hours"], 2),
                        "secondary_hours": round(data["secondary_hours"], 2),
                        "unknown_role_shifts": data["unknown_role_count"],
                    }
                else:
                    result = {
                        "group_key": key,
                        "shift_count": data["shift_count"],
                        "days_on_call": len(data["unique_days"]),
                        "total_hours": round(data["total_hours"], 2),
                        "regular_shifts": data["regular_count"],
                        "override_shifts": data["override_count"],
                        "primary_shifts": data["primary_count"],
                        "secondary_shifts": data["secondary_count"],
                        "primary_hours": round(data["primary_hours"], 2),
                        "secondary_hours": round(data["secondary_hours"], 2),
                        "unknown_role_shifts": data["unknown_role_count"],
                    }

                results.append(result)

            # Sort by shift count descending
            results.sort(key=lambda x: x["shift_count"], reverse=True)

            return {
                "period": {"start_date": start_date, "end_date": end_date},
                "total_shifts": len(shifts),
                "grouped_by": group_by,
                "metrics": results,
                "summary": {
                    "total_hours": round(sum(m["total_hours"] for m in results), 2),
                    "total_regular_shifts": sum(m["regular_shifts"] for m in results),
                    "total_override_shifts": sum(m["override_shifts"] for m in results),
                    "unique_people": len(results) if group_by == "user" else None,
                },
            }

        except Exception as e:
            import traceback

            error_type, error_message = mcp_error.categorize_error(e)
            return mcp_error.tool_error(
                f"Failed to get on-call shift metrics: {error_message}",
                error_type,
                details={
                    "params": {"start_date": start_date, "end_date": end_date},
                    "exception_type": type(e).__name__,
                    "exception_str": str(e),
                    "traceback": traceback.format_exc(),
                },
            )

    @mcp.tool()
    async def get_oncall_handoff_summary(
        team_ids: Annotated[
            str,
            Field(description="Comma-separated list of team IDs to filter schedules (optional)"),
        ] = "",
        schedule_ids: Annotated[
            str, Field(description="Comma-separated list of schedule IDs (optional)")
        ] = "",
        timezone: Annotated[
            str,
            Field(
                description="Timezone to use for display and filtering (e.g., 'America/Los_Angeles', 'Europe/London', 'Asia/Tokyo'). IMPORTANT: If user mentions a city, location, or region (e.g., 'Toronto', 'APAC', 'my time'), infer the appropriate IANA timezone. Defaults to UTC if not specified."
            ),
        ] = "UTC",
        filter_by_region: Annotated[
            bool,
            Field(
                description="If True, only show on-call for people whose shifts are during business hours (9am-5pm) in the specified timezone. Defaults to False."
            ),
        ] = False,
        include_incidents: Annotated[
            bool,
            Field(
                description="If True, fetch incidents for each shift (slower). If False, only show on-call info (faster). Defaults to False for better performance."
            ),
        ] = False,
    ) -> dict:
        """
        Get current on-call handoff summary. Shows who's currently on-call and who's next.
        Optionally fetch incidents (set include_incidents=True, but slower).

        Timezone handling: If user mentions their location/timezone, infer it (e.g., "Toronto" → "America/Toronto",
        "my time" → ask clarifying question or use a common timezone).

        Regional filtering: Use timezone + filter_by_region=True to see only people on-call
        during business hours in that region (e.g., timezone='Asia/Tokyo', filter_by_region=True
        shows only APAC on-call during APAC business hours).

        Performance: By default, incidents are NOT fetched for faster response. Set include_incidents=True
        to fetch incidents for each shift (slower, may timeout with many schedules).

        Useful for:
        - Quick on-call status checks
        - Daily handoff meetings
        - Regional on-call status (APAC, EU, Americas)
        - Team coordination across timezones
        """
        try:
            from datetime import datetime, timedelta
            from zoneinfo import ZoneInfo

            # Validate and set timezone
            try:
                tz = ZoneInfo(timezone)
            except Exception:
                tz = ZoneInfo("UTC")  # Fallback to UTC if invalid timezone

            now = datetime.now(tz)

            def convert_to_timezone(iso_string: str) -> str:
                """Convert ISO timestamp to target timezone."""
                if not iso_string:
                    return iso_string
                try:
                    dt = datetime.fromisoformat(iso_string.replace("Z", "+00:00"))
                    dt_converted = dt.astimezone(tz)
                    return dt_converted.isoformat()
                except (ValueError, AttributeError):
                    return iso_string  # Return original if conversion fails

            # Fetch schedules with team info. Page 1 first to learn total_pages,
            # then parallel-fetch any remaining pages with a small concurrency cap
            # to avoid hammering the upstream API.
            max_pages = 5  # Schedules shouldn't have many pages
            request_semaphore = asyncio.Semaphore(10)

            first_response = await make_authenticated_request(
                "GET", "/v1/schedules", params={"page[size]": 100, "page[number]": 1}
            )
            if not first_response:
                return mcp_error.tool_error(
                    "Failed to fetch schedules - no response from API", "execution_error"
                )
            if first_response.status_code != 200:
                return mcp_error.tool_error(
                    f"Failed to fetch schedules - API returned status {first_response.status_code}",
                    "execution_error",
                    details={"status_code": first_response.status_code},
                )

            first_data = first_response.json()
            all_schedules = list(first_data.get("data", []))
            total_pages = min(int(first_data.get("meta", {}).get("total_pages", 1)), max_pages)

            if total_pages > 1:

                async def _fetch_schedule_page(page_number: int) -> list[dict]:
                    async with request_semaphore:
                        page_response = await make_authenticated_request(
                            "GET",
                            "/v1/schedules",
                            params={"page[size]": 100, "page[number]": page_number},
                        )
                    if not page_response or page_response.status_code != 200:
                        return []
                    return list(page_response.json().get("data", []))

                rest_pages = await asyncio.gather(
                    *(_fetch_schedule_page(p) for p in range(2, total_pages + 1)),
                    return_exceptions=True,
                )
                for page_schedules in rest_pages:
                    if isinstance(page_schedules, BaseException):
                        # One transient page error shouldn't abort the whole handler.
                        continue
                    all_schedules.extend(page_schedules)

            # Build team mapping
            team_ids_set = set()
            for schedule in all_schedules:
                owner_group_ids = schedule.get("attributes", {}).get("owner_group_ids", [])
                team_ids_set.update(owner_group_ids)

            teams_map = {}
            if team_ids_set:
                teams_response = await make_authenticated_request(
                    "GET", "/v1/teams", params={"page[size]": 100}
                )
                if teams_response and teams_response.status_code == 200:
                    teams_data = teams_response.json()
                    for team in teams_data.get("data", []):
                        teams_map[team.get("id")] = team

            # Filter schedules
            target_schedules = []
            team_filter = (
                [tid.strip() for tid in team_ids.split(",") if tid.strip()] if team_ids else []
            )
            schedule_filter = (
                [sid.strip() for sid in schedule_ids.split(",") if sid.strip()]
                if schedule_ids
                else []
            )

            for schedule in all_schedules:
                schedule_id = schedule.get("id")
                owner_group_ids = schedule.get("attributes", {}).get("owner_group_ids", [])

                # Apply filters
                if schedule_filter and schedule_id not in schedule_filter:
                    continue
                if team_filter and not any(str(tgid) in team_filter for tgid in owner_group_ids):
                    continue

                target_schedules.append(schedule)

            # Fetch shifts for all target schedules in parallel (capped by
            # request_semaphore). Was: N sequential awaits, the dominant
            # cost in this tool's p95 latency.
            shifts_starts_gte = (now - timedelta(days=1)).isoformat()
            shifts_starts_lte = (now + timedelta(days=7)).isoformat()

            async def _fetch_shifts_for_schedule(schedule_id: str):
                async with request_semaphore:
                    return await make_authenticated_request(
                        "GET",
                        "/v1/shifts",
                        params={
                            "schedule_ids[]": [schedule_id],
                            "filter[starts_at][gte]": shifts_starts_gte,
                            "filter[starts_at][lte]": shifts_starts_lte,
                            "include": "user,on_call_role",
                            "page[size]": 50,
                        },
                    )

            shift_responses = await asyncio.gather(
                *(_fetch_shifts_for_schedule(s.get("id")) for s in target_schedules),
                return_exceptions=True,
            )

            handoff_data = []
            for schedule, shifts_response in zip(target_schedules, shift_responses, strict=True):
                if isinstance(shifts_response, BaseException) or not shifts_response:
                    continue

                schedule_id = schedule.get("id")
                schedule_attrs = schedule.get("attributes", {})
                schedule_name = schedule_attrs.get("name", "Unknown Schedule")
                owner_group_ids = schedule_attrs.get("owner_group_ids", [])

                # Get team info
                team_name = "No Team"
                if owner_group_ids:
                    team_id = owner_group_ids[0]
                    team_attrs = teams_map.get(team_id, {}).get("attributes", {})
                    team_name = team_attrs.get("name", "Unknown Team")

                shifts_data = shifts_response.json()
                shifts = shifts_data.get("data", [])
                included = shifts_data.get("included", [])

                # Build user and role maps
                users_map = {}
                roles_map = {}
                for resource in included:
                    if resource.get("type") == "users":
                        users_map[resource.get("id")] = resource
                    elif resource.get("type") == "on_call_roles":
                        roles_map[resource.get("id")] = resource

                # Find current and next shifts
                current_shift = None
                next_shift = None

                for shift in sorted(
                    shifts, key=lambda s: s.get("attributes", {}).get("starts_at", "")
                ):
                    attrs = shift.get("attributes", {})
                    starts_at_str = attrs.get("starts_at")
                    ends_at_str = attrs.get("ends_at")

                    if not starts_at_str or not ends_at_str:
                        continue

                    try:
                        starts_at = datetime.fromisoformat(starts_at_str.replace("Z", "+00:00"))
                        ends_at = datetime.fromisoformat(ends_at_str.replace("Z", "+00:00"))

                        # Current shift: ongoing now
                        if starts_at <= now <= ends_at:
                            current_shift = shift
                        # Next shift: starts after now and no current shift found yet
                        elif starts_at > now and not next_shift:
                            next_shift = shift

                    except (ValueError, AttributeError):
                        continue

                # Build response for this schedule
                schedule_info = {
                    "schedule_id": schedule_id,
                    "schedule_name": schedule_name,
                    "team_name": team_name,
                    "current_oncall": None,
                    "next_oncall": None,
                }

                if current_shift:
                    current_attrs = current_shift.get("attributes", {})
                    current_rels = current_shift.get("relationships", {})
                    user_data = current_rels.get("user", {}).get("data") or {}
                    user_id = user_data.get("id")
                    role_data = current_rels.get("on_call_role", {}).get("data") or {}
                    role_id = role_data.get("id")

                    user_name = "Unknown"
                    if user_id and user_id in users_map:
                        user_attrs = users_map[user_id].get("attributes", {})
                        user_name = user_attrs.get("full_name") or user_attrs.get(
                            "email", "Unknown"
                        )

                    role_name = "Unknown Role"
                    if role_id and role_id in roles_map:
                        role_attrs = roles_map[role_id].get("attributes", {})
                        role_name = role_attrs.get("name", "Unknown Role")

                    schedule_info["current_oncall"] = {
                        "user_name": user_name,
                        "user_id": user_id,
                        "role": role_name,
                        "starts_at": convert_to_timezone(current_attrs.get("starts_at")),
                        "ends_at": convert_to_timezone(current_attrs.get("ends_at")),
                        "is_override": current_attrs.get("is_override", False),
                    }

                if next_shift:
                    next_attrs = next_shift.get("attributes", {})
                    next_rels = next_shift.get("relationships", {})
                    user_data = next_rels.get("user", {}).get("data") or {}
                    user_id = user_data.get("id")
                    role_data = next_rels.get("on_call_role", {}).get("data") or {}
                    role_id = role_data.get("id")

                    user_name = "Unknown"
                    if user_id and user_id in users_map:
                        user_attrs = users_map[user_id].get("attributes", {})
                        user_name = user_attrs.get("full_name") or user_attrs.get(
                            "email", "Unknown"
                        )

                    role_name = "Unknown Role"
                    if role_id and role_id in roles_map:
                        role_attrs = roles_map[role_id].get("attributes", {})
                        role_name = role_attrs.get("name", "Unknown Role")

                    schedule_info["next_oncall"] = {
                        "user_name": user_name,
                        "user_id": user_id,
                        "role": role_name,
                        "starts_at": convert_to_timezone(next_attrs.get("starts_at")),
                        "ends_at": convert_to_timezone(next_attrs.get("ends_at")),
                        "is_override": next_attrs.get("is_override", False),
                    }

                handoff_data.append(schedule_info)

            # Filter by region if requested
            if filter_by_region:
                # Define business hours (9am-5pm) in the target timezone
                business_start_hour = 9
                business_end_hour = 17

                # Create datetime objects for today's business hours in target timezone
                today_business_start = now.replace(
                    hour=business_start_hour, minute=0, second=0, microsecond=0
                )
                today_business_end = now.replace(
                    hour=business_end_hour, minute=0, second=0, microsecond=0
                )

                # Filter schedules where current shift overlaps with business hours
                filtered_data = []
                for schedule_info in handoff_data:
                    current_oncall = schedule_info.get("current_oncall")
                    if current_oncall:
                        # Parse shift times (already in target timezone)
                        shift_start_str = current_oncall.get("starts_at")
                        shift_end_str = current_oncall.get("ends_at")

                        if shift_start_str and shift_end_str:
                            try:
                                shift_start = datetime.fromisoformat(
                                    shift_start_str.replace("Z", "+00:00")
                                )
                                shift_end = datetime.fromisoformat(
                                    shift_end_str.replace("Z", "+00:00")
                                )

                                # Check if shift overlaps with today's business hours
                                # Shift overlaps if: shift_start < business_end AND shift_end > business_start
                                if (
                                    shift_start < today_business_end
                                    and shift_end > today_business_start
                                ):
                                    filtered_data.append(schedule_info)
                            except (ValueError, AttributeError):
                                # Skip if we can't parse times
                                continue

                handoff_data = filtered_data

            # Fetch incidents for each current shift in parallel when requested.
            if include_incidents:

                async def _fetch_shift_incidents_for(schedule_info: dict):
                    current_oncall = schedule_info.get("current_oncall")
                    if not current_oncall:
                        return None
                    async with request_semaphore:
                        return await _fetch_shift_incidents_internal(
                            start_time=current_oncall["starts_at"],
                            end_time=current_oncall["ends_at"],
                            schedule_ids="",
                            severity="",
                            status="",
                            tags="",
                            include_preexisting_active=True,
                            max_incidents=25,
                        )

                incidents_results = await asyncio.gather(
                    *(_fetch_shift_incidents_for(s) for s in handoff_data),
                    return_exceptions=True,
                )

                for schedule_info, incidents_result in zip(
                    handoff_data, incidents_results, strict=True
                ):
                    if isinstance(incidents_result, BaseException) or incidents_result is None:
                        schedule_info["shift_incidents"] = None
                    elif incidents_result.get("success"):
                        schedule_info["shift_incidents"] = incidents_result
                    else:
                        schedule_info["shift_incidents"] = None
            else:
                # Skip incident fetching for better performance
                for schedule_info in handoff_data:
                    schedule_info["shift_incidents"] = None

            return {
                "success": True,
                "timestamp": now.isoformat(),
                "timezone": timezone,
                "schedules": handoff_data,
                "summary": {
                    "total_schedules": len(handoff_data),
                    "schedules_with_current_oncall": sum(
                        1 for s in handoff_data if s["current_oncall"]
                    ),
                    "schedules_with_next_oncall": sum(1 for s in handoff_data if s["next_oncall"]),
                    "total_incidents": sum(
                        s.get("shift_incidents", {}).get("summary", {}).get("total_incidents", 0)
                        for s in handoff_data
                        if s.get("shift_incidents")
                    ),
                },
            }

        except Exception as e:
            import traceback

            error_type, error_message = mcp_error.categorize_error(e)
            return mcp_error.tool_error(
                f"Failed to get on-call handoff summary: {error_message}",
                error_type,
                details={
                    "exception_type": type(e).__name__,
                    "exception_str": str(e),
                    "traceback": traceback.format_exc(),
                },
            )

    async def _fetch_shift_incidents_internal(
        start_time: str,
        end_time: str,
        schedule_ids: str = "",
        severity: str = "",
        status: str = "",
        tags: str = "",
        *,
        include_preexisting_active: bool = False,
        max_incidents: int | None = None,
    ) -> dict:
        """Internal helper to fetch incidents - used by both get_shift_incidents and get_oncall_handoff_summary."""
        try:
            from datetime import datetime

            # Build query parameters
            # Fetch incidents that:
            # 1. Were created during the shift (created_at in range)
            # 2. OR are currently active/unresolved (started but not resolved yet)
            params = {
                "page[size]": 100,
                "sort": "-created_at",
                "fields[incidents]": SHIFT_INCIDENT_QUERY_FIELDS,
            }

            # Fetch incidents that started before the shift ended, then use
            # in-memory filtering to keep incidents that were created, started,
            # or resolved during the shift. We cannot safely apply a lower
            # started_at bound here without dropping incidents that began
            # before the shift and resolved during it.
            params["filter[started_at][lte]"] = end_time  # Started before shift ended

            # Add severity filter if provided
            if severity:
                params["filter[severity]"] = severity.lower()

            # Add status filter if provided
            if status:
                params["filter[status]"] = status.lower()

            # Add tags filter if provided
            if tags:
                tag_list = [t.strip() for t in tags.split(",") if t.strip()]
                if tag_list:
                    params["filter[tags][]"] = tag_list

            # Query incidents with pagination
            all_incidents = []
            page = 1
            max_pages = 10  # Safety limit to prevent infinite loops

            while page <= max_pages:
                params["page[number]"] = page
                incidents_response = await make_authenticated_request(
                    "GET", "/v1/incidents", params=params
                )

                if not incidents_response:
                    return mcp_error.tool_error(
                        "Failed to fetch incidents - no response from API", "execution_error"
                    )

                if incidents_response.status_code != 200:
                    return mcp_error.tool_error(
                        f"Failed to fetch incidents - API returned status {incidents_response.status_code}",
                        "execution_error",
                        details={
                            "status_code": incidents_response.status_code,
                            "time_range": f"{start_time} to {end_time}",
                        },
                    )

                incidents_data = incidents_response.json()
                page_incidents = incidents_data.get("data", [])

                if not page_incidents:
                    break  # No more data

                all_incidents.extend(page_incidents)

                # Check if there are more pages
                meta = incidents_data.get("meta", {})
                total_pages = meta.get("total_pages", 1)

                if page >= total_pages:
                    break  # Reached the last page

                page += 1

            # Filter incidents to include:
            # 1. Created during shift (created_at between start_time and end_time)
            # 2. Currently active (started but not resolved, regardless of when created)
            shift_start_dt = datetime.fromisoformat(start_time.replace("Z", "+00:00"))
            shift_end_dt = datetime.fromisoformat(end_time.replace("Z", "+00:00"))
            now_dt = datetime.now(UTC)

            # Format incidents for handoff summary
            incidents_summary = []
            for incident in all_incidents:
                incident_id = incident.get("id")
                attrs = incident.get("attributes", {})

                # Check if incident is relevant to this shift
                created_at = attrs.get("created_at")
                started_at = attrs.get("started_at")
                resolved_at = attrs.get("resolved_at")

                # Parse timestamps
                try:
                    created_dt = (
                        datetime.fromisoformat(created_at.replace("Z", "+00:00"))
                        if created_at
                        else None
                    )
                    started_dt = (
                        datetime.fromisoformat(started_at.replace("Z", "+00:00"))
                        if started_at
                        else None
                    )
                    resolved_dt = (
                        datetime.fromisoformat(resolved_at.replace("Z", "+00:00"))
                        if resolved_at
                        else None
                    )
                except (ValueError, AttributeError):
                    continue  # Skip if we can't parse dates

                # Include incident if:
                # 1. Created during shift
                # 2. Started during shift
                # 3. Resolved during shift
                # 4. Currently active (not resolved and started before now)
                include_incident = False

                if created_dt and shift_start_dt <= created_dt <= shift_end_dt:
                    include_incident = True  # Created during shift

                if started_dt and shift_start_dt <= started_dt <= shift_end_dt:
                    include_incident = True  # Started during shift

                if resolved_dt and shift_start_dt <= resolved_dt <= shift_end_dt:
                    include_incident = True  # Resolved during shift

                if (
                    include_preexisting_active
                    and not resolved_dt
                    and started_dt
                    and started_dt <= now_dt
                ):
                    include_incident = True  # Currently active

                if not include_incident:
                    continue

                # Calculate duration if resolved
                duration_minutes = None
                if started_dt and resolved_dt:
                    duration_minutes = int((resolved_dt - started_dt).total_seconds() / 60)

                # Build narrative summary
                narrative_parts = []

                # What happened
                title = attrs.get("title", "Untitled Incident")
                severity = _normalize_incident_severity(attrs.get("severity"))
                summary_text = _truncate_text(attrs.get("summary"))
                impact_text = _truncate_text(attrs.get("customer_impact_summary"))
                mitigation_text = _truncate_text(attrs.get("mitigation"))
                narrative_parts.append(f"[{severity.upper()}] {title}")

                # When and duration
                if started_at:
                    narrative_parts.append(f"Started at {started_at}")
                if resolved_at:
                    narrative_parts.append(f"Resolved at {resolved_at}")
                    if duration_minutes:
                        narrative_parts.append(f"Duration: {duration_minutes} minutes")
                elif attrs.get("status"):
                    narrative_parts.append(f"Status: {attrs.get('status')}")

                # What was the issue
                if summary_text:
                    narrative_parts.append(f"Details: {summary_text}")

                # Impact
                if impact_text:
                    narrative_parts.append(f"Impact: {impact_text}")

                # Resolution (if available)
                if mitigation_text:
                    narrative_parts.append(f"Resolution: {mitigation_text}")
                elif attrs.get("action_items_count") and attrs.get("action_items_count") > 0:
                    narrative_parts.append(
                        f"Action items created: {attrs.get('action_items_count')}"
                    )

                narrative = _truncate_text(" | ".join(narrative_parts), max_length=400)

                incidents_summary.append(
                    {
                        "incident_id": incident_id,
                        "title": attrs.get("title", "Untitled Incident"),
                        "severity": severity,
                        "status": attrs.get("status"),
                        "started_at": started_at,
                        "resolved_at": resolved_at,
                        "duration_minutes": duration_minutes,
                        "summary": summary_text,
                        "impact": impact_text,
                        "mitigation": mitigation_text,
                        "narrative": narrative,
                        "incident_url": attrs.get("incident_url") or attrs.get("url"),
                    }
                )

            # Group by severity
            by_severity: dict[str, list[dict[str, Any]]] = {}
            for inc in incidents_summary:
                sev = inc["severity"] or "unknown"
                if sev not in by_severity:
                    by_severity[sev] = []
                by_severity[sev].append(inc)

            # Calculate statistics
            total_incidents = len(incidents_summary)
            resolved_count = sum(1 for inc in incidents_summary if inc["resolved_at"])
            ongoing_count = total_incidents - resolved_count

            avg_resolution_time = None
            durations = [
                inc["duration_minutes"] for inc in incidents_summary if inc["duration_minutes"]
            ]
            if durations:
                avg_resolution_time = int(sum(durations) / len(durations))

            returned_incidents = incidents_summary
            truncated_count = 0
            if max_incidents is not None and total_incidents > max_incidents:
                returned_incidents = incidents_summary[:max_incidents]
                truncated_count = total_incidents - max_incidents

            return {
                "success": True,
                "period": {"start_time": start_time, "end_time": end_time},
                "summary": {
                    "total_incidents": total_incidents,
                    "resolved": resolved_count,
                    "ongoing": ongoing_count,
                    "average_resolution_minutes": avg_resolution_time,
                    "by_severity": {k: len(v) for k, v in by_severity.items()},
                },
                "incidents": returned_incidents,
                "returned_incidents": len(returned_incidents),
                "truncated_incidents": truncated_count,
                "results_truncated": truncated_count > 0,
            }

        except Exception as e:
            import traceback

            error_type, error_message = mcp_error.categorize_error(e)
            return mcp_error.tool_error(
                f"Failed to get shift incidents: {error_message}",
                error_type,
                details={
                    "params": {"start_time": start_time, "end_time": end_time},
                    "exception_type": type(e).__name__,
                    "exception_str": str(e),
                    "traceback": traceback.format_exc(),
                },
            )

    @mcp.tool()
    async def get_shift_incidents(
        start_time: Annotated[
            str,
            Field(
                description="Start time for incident search (ISO 8601 format, e.g., '2025-10-01T00:00:00Z')"
            ),
        ],
        end_time: Annotated[
            str,
            Field(
                description="End time for incident search (ISO 8601 format, e.g., '2025-10-01T23:59:59Z')"
            ),
        ],
        schedule_ids: Annotated[
            str,
            Field(
                description="Comma-separated list of schedule IDs to filter incidents (optional)"
            ),
        ] = "",
        severity: Annotated[
            str,
            Field(description="Filter by severity: 'critical', 'high', 'medium', 'low' (optional)"),
        ] = "",
        status: Annotated[
            str,
            Field(
                description="Filter by status: 'started', 'detected', 'acknowledged', 'investigating', 'identified', 'monitoring', 'resolved', 'cancelled' (optional)"
            ),
        ] = "",
        tags: Annotated[
            str,
            Field(description="Comma-separated list of tag slugs to filter incidents (optional)"),
        ] = "",
    ) -> dict:
        """
        Get incidents and alerts that occurred during a specific shift or time period.

        Useful for:
        - Shift handoff summaries showing what happened during the shift
        - Post-shift debriefs and reporting
        - Incident analysis by time period
        - Understanding team workload during specific shifts

        Returns incident details including severity, status, duration, and basic summary.
        """
        return await _fetch_shift_incidents_internal(
            start_time,
            end_time,
            schedule_ids,
            severity,
            status,
            tags,
            include_preexisting_active=False,
            max_incidents=DEFAULT_MAX_SHIFT_INCIDENT_RESULTS,
        )

    # Cache for lookup maps (TTL: 5 minutes)
    _lookup_maps_cache: dict[str, Any] = {
        "data": None,
        "timestamp": 0.0,
        "ttl_seconds": 300,  # 5 minutes
    }
    _lookup_maps_lock = asyncio.Lock()

    # Helper function to fetch users and schedules for enrichment
    async def _fetch_users_and_schedules_maps() -> tuple[
        dict[str, Any], dict[str, Any], dict[str, Any]
    ]:
        """Fetch all users, schedules, and teams to build lookup maps.

        Results are cached for 5 minutes to avoid repeated API calls.
        """
        import time

        # Check cache (fast path without lock)
        now = time.time()
        if (
            _lookup_maps_cache["data"] is not None
            and (now - _lookup_maps_cache["timestamp"]) < _lookup_maps_cache["ttl_seconds"]
        ):
            return cast(
                tuple[dict[str, Any], dict[str, Any], dict[str, Any]],
                _lookup_maps_cache["data"],
            )

        # Acquire lock to prevent concurrent fetches
        async with _lookup_maps_lock:
            # Re-check cache after acquiring lock
            now = time.time()
            if (
                _lookup_maps_cache["data"] is not None
                and (now - _lookup_maps_cache["timestamp"]) < _lookup_maps_cache["ttl_seconds"]
            ):
                return cast(
                    tuple[dict[str, Any], dict[str, Any], dict[str, Any]],
                    _lookup_maps_cache["data"],
                )

            users_map = {}
            schedules_map = {}
            teams_map = {}

            # Fetch all users with pagination
            page = 1
            while page <= 10:
                users_response = await make_authenticated_request(
                    "GET", "/v1/users", params={"page[size]": 100, "page[number]": page}
                )
                if users_response and users_response.status_code == 200:
                    users_data = users_response.json()
                    for user in users_data.get("data", []):
                        users_map[user.get("id")] = user
                    if len(users_data.get("data", [])) < 100:
                        break
                    page += 1
                else:
                    break

            # Fetch all schedules with pagination
            page = 1
            while page <= 10:
                schedules_response = await make_authenticated_request(
                    "GET", "/v1/schedules", params={"page[size]": 100, "page[number]": page}
                )
                if schedules_response and schedules_response.status_code == 200:
                    schedules_data = schedules_response.json()
                    for schedule in schedules_data.get("data", []):
                        schedules_map[schedule.get("id")] = schedule
                    if len(schedules_data.get("data", [])) < 100:
                        break
                    page += 1
                else:
                    break

            # Fetch all teams with pagination
            page = 1
            while page <= 10:
                teams_response = await make_authenticated_request(
                    "GET", "/v1/teams", params={"page[size]": 100, "page[number]": page}
                )
                if teams_response and teams_response.status_code == 200:
                    teams_data = teams_response.json()
                    for team in teams_data.get("data", []):
                        teams_map[team.get("id")] = team
                    if len(teams_data.get("data", [])) < 100:
                        break
                    page += 1
                else:
                    break

            # Cache the result
            result = (users_map, schedules_map, teams_map)
            _lookup_maps_cache["data"] = result
            _lookup_maps_cache["timestamp"] = now

            return result

    @mcp.tool()
    async def list_shifts(
        from_date: Annotated[
            str,
            Field(
                description="Start date/time for shift query (ISO 8601 format, e.g., '2026-02-09T00:00:00Z')"
            ),
        ],
        to_date: Annotated[
            str,
            Field(
                description="End date/time for shift query (ISO 8601 format, e.g., '2026-02-15T23:59:59Z')"
            ),
        ],
        user_ids: Annotated[
            str,
            Field(
                description="Comma-separated list of user IDs to filter by (e.g., '2381,94178'). Only returns shifts for these users."
            ),
        ] = "",
        schedule_ids: Annotated[
            str,
            Field(description="Comma-separated list of schedule IDs to filter by (optional)"),
        ] = "",
        include_user_details: Annotated[
            bool,
            Field(description="Include user name and email in response (default: True)"),
        ] = True,
        page_size: Annotated[
            int,
            Field(
                description="Number of enriched shifts to return per page (max: 100). This paginates the MCP response even when the upstream shifts API does not."
            ),
        ] = 25,
        page_number: Annotated[
            int,
            Field(
                description="Page number of enriched shifts to return (1-indexed). Use this instead of relying on the generated listShifts pagination."
            ),
        ] = 1,
    ) -> dict:
        """
        List on-call shifts with reliable filtering, enrichment, and MCP-level pagination.

        Unlike the raw API, this tool:
        - Actually filters by user_ids (client-side filtering)
        - Includes user_name, user_email, schedule_name, team_name
        - Calculates total_hours for each shift
        - Returns a predictable paginated result for MCP clients

        Use this instead of the auto-generated listShifts when you need user filtering
        or dependable pagination on large tenants.
        """
        try:
            from datetime import datetime

            page_size, page_number = validate_page_params(page_size, page_number)
            if page_number == 0:
                return mcp_error.tool_error(
                    "page_number must be >= 1 for list_shifts",
                    "validation_error",
                    details={"page_number": page_number},
                )
            # Build query parameters
            params: dict[str, Any] = {
                "from": from_date,
                "to": to_date,
                "include": "user,on_call_role,schedule_rotation",
                "page[size]": 100,
            }

            if schedule_ids:
                schedule_id_list = [sid.strip() for sid in schedule_ids.split(",") if sid.strip()]
                params["schedule_ids[]"] = schedule_id_list

            # Parse user_ids for filtering
            user_id_filter = set()
            if user_ids:
                user_id_filter = {uid.strip() for uid in user_ids.split(",") if uid.strip()}

            # Fetch lookup maps for enrichment
            users_map, schedules_map, teams_map = await _fetch_users_and_schedules_maps()

            # Build schedule -> team mapping
            schedule_to_team = {}
            for schedule_id, schedule in schedules_map.items():
                owner_group_ids = schedule.get("attributes", {}).get("owner_group_ids", [])
                if owner_group_ids:
                    team_id = owner_group_ids[0]
                    team = teams_map.get(team_id, {})
                    schedule_to_team[schedule_id] = {
                        "team_id": team_id,
                        "team_name": team.get("attributes", {}).get("name", "Unknown Team"),
                    }

            # Fetch all shifts with pagination
            all_shifts = []
            page = 1
            while page <= 10:
                params["page[number]"] = page
                shifts_response = await make_authenticated_request(
                    "GET", "/v1/shifts", params=params
                )

                if shifts_response is None:
                    break

                shifts_response.raise_for_status()
                shifts_data = shifts_response.json()

                shifts = shifts_data.get("data", [])
                included = shifts_data.get("included", [])

                # Update users_map from included data
                for resource in included:
                    if resource.get("type") == "users":
                        users_map[resource.get("id")] = resource

                if not shifts:
                    break

                all_shifts.extend(shifts)

                meta = shifts_data.get("meta", {})
                total_pages = meta.get("total_pages", 1)
                if page >= total_pages:
                    break
                page += 1

            # Process and filter shifts
            enriched_shifts = []
            for shift in all_shifts:
                attrs = shift.get("attributes", {})
                relationships = shift.get("relationships", {})

                # Get user info
                user_rel = relationships.get("user", {}).get("data") or {}
                user_id = user_rel.get("id")

                # Skip shifts without a user
                if not user_id:
                    continue

                # Apply user_ids filter
                if user_id_filter and str(user_id) not in user_id_filter:
                    continue

                user_info = users_map.get(user_id, {})
                user_attrs = user_info.get("attributes", {})
                user_name = user_attrs.get("full_name") or user_attrs.get("name") or "Unknown"
                user_email = user_attrs.get("email", "")

                # Get schedule info
                schedule_id = attrs.get("schedule_id")
                schedule_info = schedules_map.get(schedule_id, {})
                schedule_name = schedule_info.get("attributes", {}).get("name", "Unknown Schedule")

                # Get team info
                team_info = schedule_to_team.get(schedule_id, {})
                team_name = team_info.get("team_name", "Unknown Team")

                # Calculate total hours
                starts_at = attrs.get("starts_at")
                ends_at = attrs.get("ends_at")
                total_hours = 0.0
                if starts_at and ends_at:
                    try:
                        start_dt = datetime.fromisoformat(starts_at.replace("Z", "+00:00"))
                        end_dt = datetime.fromisoformat(ends_at.replace("Z", "+00:00"))
                        total_hours = round((end_dt - start_dt).total_seconds() / 3600, 2)
                    except (ValueError, AttributeError):
                        pass

                enriched_shift = {
                    "shift_id": shift.get("id"),
                    "user_id": user_id,
                    "schedule_id": schedule_id,
                    "starts_at": starts_at,
                    "ends_at": ends_at,
                    "is_override": attrs.get("is_override", False),
                    "total_hours": total_hours,
                }

                if include_user_details:
                    enriched_shift["user_name"] = user_name
                    enriched_shift["user_email"] = user_email
                    enriched_shift["schedule_name"] = schedule_name
                    enriched_shift["team_name"] = team_name

                enriched_shifts.append(enriched_shift)

            total_matching_shifts = len(enriched_shifts)
            start_index = (page_number - 1) * page_size
            end_index = start_index + page_size
            paginated_shifts = enriched_shifts[start_index:end_index]
            has_more = end_index < total_matching_shifts

            return {
                "period": {"from": from_date, "to": to_date},
                "total_shifts": total_matching_shifts,
                "returned_shifts": len(paginated_shifts),
                "filters_applied": {
                    "user_ids": list(user_id_filter) if user_id_filter else None,
                    "schedule_ids": schedule_ids if schedule_ids else None,
                },
                "meta": {
                    "page_size": page_size,
                    "page_number": page_number,
                    "total_matching_shifts": total_matching_shifts,
                    "returned_shifts": len(paginated_shifts),
                    "has_more": has_more,
                    "next_page": page_number + 1 if has_more else None,
                },
                "shifts": paginated_shifts,
            }

        except Exception as e:
            import traceback

            error_type, error_message = mcp_error.categorize_error(e)
            return mcp_error.tool_error(
                f"Failed to list shifts: {error_message}",
                error_type,
                details={
                    "params": {"from": from_date, "to": to_date},
                    "exception_type": type(e).__name__,
                    "traceback": traceback.format_exc(),
                },
            )

    @mcp.tool()
    async def get_oncall_schedule_summary(
        start_date: Annotated[
            str,
            Field(description="Start date (ISO 8601, e.g., '2026-02-09')"),
        ],
        end_date: Annotated[
            str,
            Field(description="End date (ISO 8601, e.g., '2026-02-15')"),
        ],
        schedule_ids: Annotated[
            str,
            Field(description="Comma-separated schedule IDs to filter (optional)"),
        ] = "",
        team_ids: Annotated[
            str,
            Field(description="Comma-separated team IDs to filter (optional)"),
        ] = "",
        include_user_ids: Annotated[
            bool,
            Field(description="Include numeric user IDs for cross-platform correlation"),
        ] = True,
    ) -> dict:
        """
        Get compact on-call schedule summary for a date range.

        Returns one entry per user per schedule (not raw shifts), with
        aggregated hours. Optimized for AI agent context windows.

        Use this instead of listShifts when you need:
        - Aggregated hours per responder
        - Schedule coverage overview
        - Responder load analysis with warnings
        """
        try:
            from collections import defaultdict
            from datetime import datetime

            # Parse filter IDs
            schedule_id_filter = set()
            if schedule_ids:
                schedule_id_filter = {sid.strip() for sid in schedule_ids.split(",") if sid.strip()}

            team_id_filter = set()
            if team_ids:
                team_id_filter = {tid.strip() for tid in team_ids.split(",") if tid.strip()}

            # Fetch lookup maps
            users_map, schedules_map, teams_map = await _fetch_users_and_schedules_maps()

            # Build schedule -> team mapping and apply team filter
            schedule_to_team = {}
            filtered_schedule_ids = set()
            for schedule_id, schedule in schedules_map.items():
                owner_group_ids = schedule.get("attributes", {}).get("owner_group_ids", [])
                team_id = owner_group_ids[0] if owner_group_ids else None
                team = teams_map.get(team_id, {}) if team_id else {}
                team_name = team.get("attributes", {}).get("name", "Unknown Team")

                schedule_to_team[schedule_id] = {
                    "team_id": team_id,
                    "team_name": team_name,
                    "schedule_name": schedule.get("attributes", {}).get("name", "Unknown Schedule"),
                }

                # Apply filters
                if schedule_id_filter and schedule_id not in schedule_id_filter:
                    continue
                if team_id_filter and (not team_id or team_id not in team_id_filter):
                    continue
                filtered_schedule_ids.add(schedule_id)

            # If no filters, include all schedules
            if not schedule_id_filter and not team_id_filter:
                filtered_schedule_ids = set(schedules_map.keys())

            # Fetch shifts
            params: dict[str, Any] = {
                "from": f"{start_date}T00:00:00Z" if "T" not in start_date else start_date,
                "to": f"{end_date}T23:59:59Z" if "T" not in end_date else end_date,
                "include": "user,on_call_role",
                "page[size]": 100,
            }

            all_shifts = []
            page = 1
            while page <= 10:
                params["page[number]"] = page
                shifts_response = await make_authenticated_request(
                    "GET", "/v1/shifts", params=params
                )

                if shifts_response is None:
                    break

                shifts_response.raise_for_status()
                shifts_data = shifts_response.json()

                shifts = shifts_data.get("data", [])
                included = shifts_data.get("included", [])

                # Update users_map from included data
                for resource in included:
                    if resource.get("type") == "users":
                        users_map[resource.get("id")] = resource

                if not shifts:
                    break

                all_shifts.extend(shifts)

                meta = shifts_data.get("meta", {})
                total_pages = meta.get("total_pages", 1)
                if page >= total_pages:
                    break
                page += 1

            # Aggregate by schedule and user
            schedule_coverage: dict[str, dict] = defaultdict(
                lambda: {
                    "schedule_name": "",
                    "team_name": "",
                    "responders": defaultdict(
                        lambda: {
                            "user_name": "",
                            "user_id": None,
                            "total_hours": 0.0,
                            "shift_count": 0,
                            "is_override": False,
                        }
                    ),
                }
            )

            responder_load: dict[str, dict] = defaultdict(
                lambda: {
                    "user_name": "",
                    "user_id": None,
                    "total_hours": 0.0,
                    "schedules": set(),
                }
            )

            for shift in all_shifts:
                attrs = shift.get("attributes", {})
                schedule_id = attrs.get("schedule_id")

                # Apply schedule filter
                if filtered_schedule_ids and schedule_id not in filtered_schedule_ids:
                    continue

                # Get user info
                relationships = shift.get("relationships", {})
                user_rel = relationships.get("user", {}).get("data") or {}
                user_id = user_rel.get("id")

                # Skip shifts without a user
                if not user_id:
                    continue

                user_info = users_map.get(user_id, {})
                user_attrs = user_info.get("attributes", {})
                user_name = user_attrs.get("full_name") or user_attrs.get("name") or "Unknown"

                # Get schedule/team info
                sched_info = schedule_to_team.get(schedule_id, {})
                schedule_name = sched_info.get("schedule_name", "Unknown Schedule")
                team_name = sched_info.get("team_name", "Unknown Team")

                # Calculate hours
                starts_at = attrs.get("starts_at")
                ends_at = attrs.get("ends_at")
                hours = 0.0
                if starts_at and ends_at:
                    try:
                        start_dt = datetime.fromisoformat(starts_at.replace("Z", "+00:00"))
                        end_dt = datetime.fromisoformat(ends_at.replace("Z", "+00:00"))
                        hours = (end_dt - start_dt).total_seconds() / 3600
                    except (ValueError, AttributeError):
                        pass

                is_override = attrs.get("is_override", False)

                # Update schedule coverage
                sched_data = schedule_coverage[schedule_id]
                sched_data["schedule_name"] = schedule_name
                sched_data["team_name"] = team_name

                user_key = str(user_id)
                sched_data["responders"][user_key]["user_name"] = user_name
                sched_data["responders"][user_key]["user_id"] = user_id
                sched_data["responders"][user_key]["total_hours"] += hours
                sched_data["responders"][user_key]["shift_count"] += 1
                if is_override:
                    sched_data["responders"][user_key]["is_override"] = True

                # Update responder load
                responder_load[user_key]["user_name"] = user_name
                responder_load[user_key]["user_id"] = user_id
                responder_load[user_key]["total_hours"] += hours
                responder_load[user_key]["schedules"].add(schedule_name)

            # Format schedule coverage
            formatted_coverage = []
            for _schedule_id, sched_data in schedule_coverage.items():
                responders_list = []
                for _user_key, resp_data in sched_data["responders"].items():
                    responder = {
                        "user_name": resp_data["user_name"],
                        "total_hours": round(resp_data["total_hours"], 1),
                        "shift_count": resp_data["shift_count"],
                        "is_override": resp_data["is_override"],
                    }
                    if include_user_ids:
                        responder["user_id"] = resp_data["user_id"]
                    responders_list.append(responder)

                # Sort by hours descending
                responders_list.sort(key=lambda x: x["total_hours"], reverse=True)

                formatted_coverage.append(
                    {
                        "schedule_name": sched_data["schedule_name"],
                        "team_name": sched_data["team_name"],
                        "responders": responders_list,
                    }
                )

            # Format responder load with warnings
            formatted_load = []
            for _user_key, load_data in responder_load.items():
                schedules_list = list(load_data["schedules"])
                hours = round(load_data["total_hours"], 1)

                responder_entry = {
                    "user_name": load_data["user_name"],
                    "total_hours": hours,
                    "schedules": schedules_list,
                }
                if include_user_ids:
                    responder_entry["user_id"] = load_data["user_id"]

                # Add warnings for high load
                if len(schedules_list) >= 4:
                    responder_entry["warning"] = (
                        f"High load: {len(schedules_list)} concurrent schedules"
                    )
                elif hours >= 168:  # 7 days * 24 hours
                    responder_entry["warning"] = f"High load: {hours} hours in period"

                formatted_load.append(responder_entry)

            # Sort by hours descending
            formatted_load.sort(key=lambda x: x["total_hours"], reverse=True)
            formatted_coverage.sort(key=lambda x: x["schedule_name"])

            return {
                "period": {"start": start_date, "end": end_date},
                "total_schedules": len(formatted_coverage),
                "total_responders": len(formatted_load),
                "schedule_coverage": formatted_coverage,
                "responder_load": formatted_load,
            }

        except Exception as e:
            import traceback

            error_type, error_message = mcp_error.categorize_error(e)
            return mcp_error.tool_error(
                f"Failed to get on-call schedule summary: {error_message}",
                error_type,
                details={
                    "params": {"start_date": start_date, "end_date": end_date},
                    "exception_type": type(e).__name__,
                    "traceback": traceback.format_exc(),
                },
            )

    @mcp.tool()
    async def check_responder_availability(
        start_date: Annotated[
            str,
            Field(description="Start date (ISO 8601, e.g., '2026-02-09')"),
        ],
        end_date: Annotated[
            str,
            Field(description="End date (ISO 8601, e.g., '2026-02-15')"),
        ],
        user_ids: Annotated[
            str,
            Field(
                description="Comma-separated Rootly user IDs to check (e.g., '2381,94178,27965')"
            ),
        ],
    ) -> dict:
        """
        Check if specific users are scheduled for on-call in a date range.

        Use this to verify if at-risk users (from On-Call Health) are scheduled,
        or to check availability before assigning new shifts.

        Returns scheduled users with their shifts and total hours,
        plus users who are not scheduled.
        """
        try:
            from datetime import datetime

            if not user_ids:
                return mcp_error.tool_error(
                    "user_ids parameter is required",
                    "validation_error",
                )

            # Parse user IDs
            user_id_list = [uid.strip() for uid in user_ids.split(",") if uid.strip()]
            user_id_set = set(user_id_list)

            # Fetch lookup maps
            users_map, schedules_map, teams_map = await _fetch_users_and_schedules_maps()

            # Build schedule -> team mapping
            schedule_to_team = {}
            for schedule_id, schedule in schedules_map.items():
                owner_group_ids = schedule.get("attributes", {}).get("owner_group_ids", [])
                if owner_group_ids:
                    team_id = owner_group_ids[0]
                    team = teams_map.get(team_id, {})
                    schedule_to_team[schedule_id] = {
                        "schedule_name": schedule.get("attributes", {}).get("name", "Unknown"),
                        "team_name": team.get("attributes", {}).get("name", "Unknown Team"),
                    }

            # Fetch shifts
            params: dict[str, Any] = {
                "from": f"{start_date}T00:00:00Z" if "T" not in start_date else start_date,
                "to": f"{end_date}T23:59:59Z" if "T" not in end_date else end_date,
                "include": "user,on_call_role",
                "page[size]": 100,
            }

            all_shifts = []
            page = 1
            while page <= 10:
                params["page[number]"] = page
                shifts_response = await make_authenticated_request(
                    "GET", "/v1/shifts", params=params
                )

                if shifts_response is None:
                    break

                shifts_response.raise_for_status()
                shifts_data = shifts_response.json()

                shifts = shifts_data.get("data", [])
                included = shifts_data.get("included", [])

                # Update users_map from included data
                for resource in included:
                    if resource.get("type") == "users":
                        users_map[resource.get("id")] = resource

                if not shifts:
                    break

                all_shifts.extend(shifts)

                meta = shifts_data.get("meta", {})
                total_pages = meta.get("total_pages", 1)
                if page >= total_pages:
                    break
                page += 1

            # Group shifts by user
            user_shifts: dict[str, list] = {uid: [] for uid in user_id_list}
            user_hours: dict[str, float] = dict.fromkeys(user_id_list, 0.0)

            for shift in all_shifts:
                attrs = shift.get("attributes", {})
                relationships = shift.get("relationships", {})

                user_rel = relationships.get("user", {}).get("data") or {}
                raw_user_id = user_rel.get("id")

                # Skip shifts without a user
                if not raw_user_id:
                    continue

                user_id = str(raw_user_id)

                if user_id not in user_id_set:
                    continue

                schedule_id = attrs.get("schedule_id")
                sched_info = schedule_to_team.get(schedule_id, {})

                starts_at = attrs.get("starts_at")
                ends_at = attrs.get("ends_at")
                hours = 0.0
                if starts_at and ends_at:
                    try:
                        start_dt = datetime.fromisoformat(starts_at.replace("Z", "+00:00"))
                        end_dt = datetime.fromisoformat(ends_at.replace("Z", "+00:00"))
                        hours = round((end_dt - start_dt).total_seconds() / 3600, 1)
                    except (ValueError, AttributeError):
                        pass

                user_shifts[user_id].append(
                    {
                        "schedule_name": sched_info.get("schedule_name", "Unknown"),
                        "starts_at": starts_at,
                        "ends_at": ends_at,
                        "hours": hours,
                    }
                )
                user_hours[user_id] += hours

            # Format results
            scheduled = []
            not_scheduled = []

            for user_id in user_id_list:
                user_info = users_map.get(user_id, {})
                user_attrs = user_info.get("attributes", {})
                user_name = user_attrs.get("full_name") or user_attrs.get("name") or "Unknown"

                shifts = user_shifts.get(user_id, [])
                if shifts:
                    scheduled.append(
                        {
                            "user_id": int(user_id) if user_id.isdigit() else user_id,
                            "user_name": user_name,
                            "total_hours": round(user_hours[user_id], 1),
                            "shifts": shifts,
                        }
                    )
                else:
                    not_scheduled.append(
                        {
                            "user_id": int(user_id) if user_id.isdigit() else user_id,
                            "user_name": user_name,
                        }
                    )

            # Sort scheduled by hours descending
            scheduled.sort(key=lambda x: x["total_hours"], reverse=True)

            return {
                "period": {"start": start_date, "end": end_date},
                "checked_users": len(user_id_list),
                "scheduled": scheduled,
                "not_scheduled": not_scheduled,
            }

        except Exception as e:
            import traceback

            error_type, error_message = mcp_error.categorize_error(e)
            return mcp_error.tool_error(
                f"Failed to check responder availability: {error_message}",
                error_type,
                details={
                    "params": {
                        "start_date": start_date,
                        "end_date": end_date,
                        "user_ids": user_ids,
                    },
                    "exception_type": type(e).__name__,
                    "traceback": traceback.format_exc(),
                },
            )

    @mcp.tool()
    async def create_override_recommendation(
        schedule_id: Annotated[
            str,
            Field(description="Schedule ID to create override for"),
        ],
        original_user_id: Annotated[
            int,
            Field(description="User ID being replaced"),
        ],
        start_date: Annotated[
            str,
            Field(description="Override start (ISO 8601, e.g., '2026-02-09')"),
        ],
        end_date: Annotated[
            str,
            Field(description="Override end (ISO 8601, e.g., '2026-02-15')"),
        ],
        exclude_user_ids: Annotated[
            str,
            Field(description="Comma-separated user IDs to exclude (e.g., other at-risk users)"),
        ] = "",
    ) -> dict:
        """
        Recommend replacement responders for an override shift.

        Finds users in the same schedule rotation who are not already
        heavily loaded during the period.

        Returns recommended replacements sorted by current load (lowest first),
        plus a ready-to-use override payload for the top recommendation.
        """
        try:
            from datetime import datetime

            # Parse exclusions
            exclude_set = set()
            if exclude_user_ids:
                exclude_set = {uid.strip() for uid in exclude_user_ids.split(",") if uid.strip()}
            exclude_set.add(str(original_user_id))

            # Fetch lookup maps
            users_map, schedules_map, teams_map = await _fetch_users_and_schedules_maps()

            # Get schedule info
            schedule = schedules_map.get(schedule_id, {})
            schedule_name = schedule.get("attributes", {}).get("name", "Unknown Schedule")

            # Get original user info
            original_user = users_map.get(str(original_user_id), {})
            original_user_attrs = original_user.get("attributes", {})
            original_user_name = (
                original_user_attrs.get("full_name") or original_user_attrs.get("name") or "Unknown"
            )

            # Fetch schedule rotations to find rotation users
            rotation_users = set()

            # First, get the schedule to find its rotations
            schedule_response = await make_authenticated_request(
                "GET", f"/v1/schedules/{schedule_id}"
            )

            if schedule_response and schedule_response.status_code == 200:
                import asyncio

                schedule_data = schedule_response.json()
                schedule_obj = schedule_data.get("data", {})
                relationships = schedule_obj.get("relationships", {})

                # Get schedule rotations
                rotations = relationships.get("schedule_rotations", {}).get("data", [])
                rotation_ids = [r.get("id") for r in rotations if r.get("id")]

                # Fetch all rotation users in parallel
                if rotation_ids:

                    async def fetch_rotation_users(rotation_id: str):
                        response = await make_authenticated_request(
                            "GET",
                            f"/v1/schedule_rotations/{rotation_id}/schedule_rotation_users",
                            params={"page[size]": 100},
                        )
                        if response and response.status_code == 200:
                            return response.json().get("data", [])
                        return []

                    # Execute all rotation user fetches in parallel
                    rotation_results = await asyncio.gather(
                        *[fetch_rotation_users(rid) for rid in rotation_ids], return_exceptions=True
                    )

                    # Process results
                    for result in rotation_results:
                        if isinstance(result, list):
                            for ru in result:
                                user_rel = (
                                    ru.get("relationships", {}).get("user", {}).get("data", {})
                                )
                                user_id = user_rel.get("id")
                                if user_id:
                                    rotation_users.add(str(user_id))

            # Fetch shifts to calculate current load for rotation users
            params: dict[str, Any] = {
                "from": f"{start_date}T00:00:00Z" if "T" not in start_date else start_date,
                "to": f"{end_date}T23:59:59Z" if "T" not in end_date else end_date,
                "include": "user",
                "page[size]": 100,
            }

            all_shifts = []
            page = 1
            while page <= 10:
                params["page[number]"] = page
                shifts_response = await make_authenticated_request(
                    "GET", "/v1/shifts", params=params
                )

                if shifts_response is None:
                    break

                shifts_response.raise_for_status()
                shifts_data = shifts_response.json()

                shifts = shifts_data.get("data", [])
                included = shifts_data.get("included", [])

                for resource in included:
                    if resource.get("type") == "users":
                        users_map[resource.get("id")] = resource

                if not shifts:
                    break

                all_shifts.extend(shifts)

                meta = shifts_data.get("meta", {})
                total_pages = meta.get("total_pages", 1)
                if page >= total_pages:
                    break
                page += 1

            # Calculate load per user
            user_load: dict[str, float] = {}
            for shift in all_shifts:
                attrs = shift.get("attributes", {})
                relationships = shift.get("relationships", {})

                user_rel = relationships.get("user", {}).get("data") or {}
                raw_user_id = user_rel.get("id")

                # Skip shifts without a user
                if not raw_user_id:
                    continue

                user_id = str(raw_user_id)

                starts_at = attrs.get("starts_at")
                ends_at = attrs.get("ends_at")
                hours = 0.0
                if starts_at and ends_at:
                    try:
                        start_dt = datetime.fromisoformat(starts_at.replace("Z", "+00:00"))
                        end_dt = datetime.fromisoformat(ends_at.replace("Z", "+00:00"))
                        hours = (end_dt - start_dt).total_seconds() / 3600
                    except (ValueError, AttributeError):
                        pass

                user_load[user_id] = user_load.get(user_id, 0.0) + hours

            # Find recommendations from rotation users
            recommendations = []
            for user_id in rotation_users:
                if user_id in exclude_set:
                    continue

                user_info = users_map.get(user_id, {})
                user_attrs = user_info.get("attributes", {})
                user_name = user_attrs.get("full_name") or user_attrs.get("name") or "Unknown"

                current_hours = round(user_load.get(user_id, 0.0), 1)

                # Generate reason based on load
                if current_hours == 0:
                    reason = "Already in rotation, no current load"
                elif current_hours < 24:
                    reason = "Already in rotation, low load"
                elif current_hours < 48:
                    reason = "Same team, moderate availability"
                else:
                    reason = "In rotation, but higher load"

                recommendations.append(
                    {
                        "user_id": int(user_id) if user_id.isdigit() else user_id,
                        "user_name": user_name,
                        "current_hours_in_period": current_hours,
                        "reason": reason,
                    }
                )

            # Sort by load (lowest first)
            recommendations.sort(key=lambda x: x["current_hours_in_period"])

            # Build override payload for top recommendation
            override_payload = None
            if recommendations:
                top_rec = recommendations[0]
                # Format dates for API
                override_starts = f"{start_date}T00:00:00Z" if "T" not in start_date else start_date
                override_ends = f"{end_date}T23:59:59Z" if "T" not in end_date else end_date

                override_payload = {
                    "schedule_id": schedule_id,
                    "user_id": top_rec["user_id"],
                    "starts_at": override_starts,
                    "ends_at": override_ends,
                }

            # Build response with optional warning
            response = {
                "schedule_name": schedule_name,
                "original_user": {
                    "id": original_user_id,
                    "name": original_user_name,
                },
                "period": {
                    "start": start_date,
                    "end": end_date,
                },
                "recommended_replacements": recommendations[:5],  # Top 5
                "override_payload": override_payload,
            }

            # Add warning if no recommendations available
            if not rotation_users:
                response["warning"] = (
                    "No rotation users found for this schedule. The schedule may not have any rotations configured."
                )
            elif not recommendations:
                response["warning"] = (
                    "All rotation users are either excluded or the original user. No recommendations available."
                )

            return response

        except Exception as e:
            import traceback

            error_type, error_message = mcp_error.categorize_error(e)
            return mcp_error.tool_error(
                f"Failed to create override recommendation: {error_message}",
                error_type,
                details={
                    "params": {
                        "schedule_id": schedule_id,
                        "original_user_id": original_user_id,
                    },
                    "exception_type": type(e).__name__,
                    "traceback": traceback.format_exc(),
                },
            )

    @mcp.tool()
    async def check_oncall_health_risk(
        start_date: Annotated[
            str,
            Field(description="Start date for the on-call period (ISO 8601, e.g., '2026-02-09')"),
        ],
        end_date: Annotated[
            str,
            Field(description="End date for the on-call period (ISO 8601, e.g., '2026-02-15')"),
        ],
        och_analysis_id: Annotated[
            int | None,
            Field(
                description="On-Call Health analysis ID. If not provided, uses the latest analysis"
            ),
        ] = None,
        och_threshold: Annotated[
            float,
            Field(description="OCH score threshold for at-risk classification (default: 50.0)"),
        ] = 50.0,
        include_replacements: Annotated[
            bool,
            Field(description="Include recommended replacement responders (default: true)"),
        ] = True,
    ) -> dict:
        """Check if any at-risk responders (based on On-Call Health analysis) are scheduled for on-call.

        Integrates with On-Call Health (oncallhealth.ai) to identify responders with elevated
        workload health risk and checks if they are scheduled during the specified period.
        Optionally recommends safe replacement responders.

        Requires ONCALLHEALTH_API_KEY environment variable.
        """
        try:
            # Validate OCH API key is configured
            if not os.environ.get("ONCALLHEALTH_API_KEY"):
                raise PermissionError(
                    "ONCALLHEALTH_API_KEY environment variable required. "
                    "Get your key from oncallhealth.ai/settings/api-keys"
                )

            och_client = OnCallHealthClient()

            # 1. Get OCH analysis (by ID or latest)
            try:
                if och_analysis_id:
                    analysis = await och_client.get_analysis(och_analysis_id)
                else:
                    analysis = await och_client.get_latest_analysis()
                    och_analysis_id = analysis.get("id")
            except httpx.HTTPStatusError as e:
                raise ConnectionError(f"Failed to fetch On-Call Health data: {e}")
            except ValueError as e:
                raise ValueError(str(e))

            # 2. Extract at-risk and safe users
            at_risk_users, safe_users = och_client.extract_at_risk_users(
                analysis, threshold=och_threshold
            )

            if not at_risk_users:
                return {
                    "period": {"start": start_date, "end": end_date},
                    "och_analysis_id": och_analysis_id,
                    "och_threshold": och_threshold,
                    "at_risk_scheduled": [],
                    "at_risk_not_scheduled": [],
                    "recommended_replacements": [],
                    "summary": {
                        "total_at_risk": 0,
                        "at_risk_scheduled": 0,
                        "action_required": False,
                        "message": "No users above health risk threshold.",
                    },
                }

            # 3. Get shifts for the period
            all_shifts = []
            users_map = {}
            schedules_map = {}

            # Fetch lookup maps
            lookup_users, lookup_schedules, lookup_teams = await _fetch_users_and_schedules_maps()
            users_map.update({str(k): v for k, v in lookup_users.items()})
            schedules_map.update({str(k): v for k, v in lookup_schedules.items()})

            # Fetch shifts
            page = 1
            while page <= 10:
                shifts_response = await make_authenticated_request(
                    "GET",
                    "/v1/shifts",
                    params={
                        "filter[starts_at_lte]": (
                            end_date if "T" in end_date else f"{end_date}T23:59:59Z"
                        ),
                        "filter[ends_at_gte]": (
                            start_date if "T" in start_date else f"{start_date}T00:00:00Z"
                        ),
                        "page[size]": 100,
                        "page[number]": page,
                        "include": "user,schedule",
                    },
                )
                if shifts_response is None:
                    break
                shifts_response.raise_for_status()
                shifts_data = shifts_response.json()

                shifts = shifts_data.get("data", [])
                included = shifts_data.get("included", [])

                for resource in included:
                    if resource.get("type") == "users":
                        users_map[str(resource.get("id"))] = resource
                    elif resource.get("type") == "schedules":
                        schedules_map[str(resource.get("id"))] = resource

                if not shifts:
                    break

                all_shifts.extend(shifts)

                meta = shifts_data.get("meta", {})
                total_pages = meta.get("total_pages", 1)
                if page >= total_pages:
                    break
                page += 1

            # 5. Correlate: which at-risk users are scheduled?
            at_risk_scheduled = []
            at_risk_not_scheduled = []

            for user in at_risk_users:
                rootly_id = user.get("rootly_user_id")
                if not rootly_id:
                    continue

                rootly_id_str = str(rootly_id)

                # Find shifts for this user
                user_shifts = []
                for shift in all_shifts:
                    relationships = shift.get("relationships", {})
                    user_rel = relationships.get("user", {}).get("data") or {}
                    shift_user_id = str(user_rel.get("id", ""))

                    if shift_user_id == rootly_id_str:
                        attrs = shift.get("attributes", {})
                        schedule_rel = relationships.get("schedule", {}).get("data") or {}
                        schedule_id = str(schedule_rel.get("id", ""))
                        schedule_info = schedules_map.get(schedule_id, {})
                        schedule_name = schedule_info.get("attributes", {}).get("name", "Unknown")

                        starts_at = attrs.get("starts_at")
                        ends_at = attrs.get("ends_at")
                        hours = 0.0
                        if starts_at and ends_at:
                            try:
                                start_dt = datetime.fromisoformat(starts_at.replace("Z", "+00:00"))
                                end_dt = datetime.fromisoformat(ends_at.replace("Z", "+00:00"))
                                hours = (end_dt - start_dt).total_seconds() / 3600
                            except (ValueError, AttributeError):
                                pass

                        user_shifts.append(
                            {
                                "schedule_id": schedule_id,
                                "schedule_name": schedule_name,
                                "starts_at": starts_at,
                                "ends_at": ends_at,
                                "hours": round(hours, 1),
                            }
                        )

                if user_shifts:
                    total_hours = sum(s["hours"] for s in user_shifts)
                    at_risk_scheduled.append(
                        {
                            "user_name": user["user_name"],
                            "user_id": int(rootly_id),
                            "och_score": user["och_score"],
                            "risk_level": user["risk_level"],
                            "health_risk_score": user["health_risk_score"],
                            "total_hours": round(total_hours, 1),
                            "shifts": user_shifts,
                        }
                    )
                else:
                    at_risk_not_scheduled.append(
                        {
                            "user_name": user["user_name"],
                            "user_id": int(rootly_id) if rootly_id else None,
                            "och_score": user["och_score"],
                            "risk_level": user["risk_level"],
                        }
                    )

            # 6. Get recommended replacements (if requested)
            recommended_replacements = []
            if include_replacements and safe_users:
                safe_rootly_ids = [
                    str(u["rootly_user_id"]) for u in safe_users[:10] if u.get("rootly_user_id")
                ]

                if safe_rootly_ids:
                    # Calculate current hours for safe users
                    for user in safe_users[:5]:
                        rootly_id = user.get("rootly_user_id")
                        if not rootly_id:
                            continue

                        rootly_id_str = str(rootly_id)
                        user_hours = 0.0

                        for shift in all_shifts:
                            relationships = shift.get("relationships", {})
                            user_rel = relationships.get("user", {}).get("data") or {}
                            shift_user_id = str(user_rel.get("id", ""))

                            if shift_user_id == rootly_id_str:
                                attrs = shift.get("attributes", {})
                                starts_at = attrs.get("starts_at")
                                ends_at = attrs.get("ends_at")
                                if starts_at and ends_at:
                                    try:
                                        start_dt = datetime.fromisoformat(
                                            starts_at.replace("Z", "+00:00")
                                        )
                                        end_dt = datetime.fromisoformat(
                                            ends_at.replace("Z", "+00:00")
                                        )
                                        user_hours += (end_dt - start_dt).total_seconds() / 3600
                                    except (ValueError, AttributeError):
                                        pass

                        recommended_replacements.append(
                            {
                                "user_name": user["user_name"],
                                "user_id": int(rootly_id),
                                "och_score": user["och_score"],
                                "risk_level": user["risk_level"],
                                "current_hours_in_period": round(user_hours, 1),
                            }
                        )

            # 7. Build summary
            total_scheduled_hours = sum(u["total_hours"] for u in at_risk_scheduled)
            action_required = len(at_risk_scheduled) > 0

            if action_required:
                message = (
                    f"{len(at_risk_scheduled)} at-risk user(s) scheduled for "
                    f"{total_scheduled_hours} hours. Consider reassignment."
                )
            else:
                message = "No at-risk users are scheduled for the period."

            return {
                "period": {"start": start_date, "end": end_date},
                "och_analysis_id": och_analysis_id,
                "och_threshold": och_threshold,
                "at_risk_scheduled": at_risk_scheduled,
                "at_risk_not_scheduled": at_risk_not_scheduled,
                "recommended_replacements": recommended_replacements,
                "summary": {
                    "total_at_risk": len(at_risk_users),
                    "at_risk_scheduled": len(at_risk_scheduled),
                    "action_required": action_required,
                    "message": message,
                },
            }

        except PermissionError as e:
            return mcp_error.tool_error(str(e), "permission_error")
        except ConnectionError as e:
            return mcp_error.tool_error(str(e), "connection_error")
        except ValueError as e:
            return mcp_error.tool_error(str(e), "validation_error")
        except Exception as e:
            import traceback

            error_type, error_message = mcp_error.categorize_error(e)
            return mcp_error.tool_error(
                f"Failed to check health risk: {error_message}",
                error_type,
                details={
                    "params": {
                        "start_date": start_date,
                        "end_date": end_date,
                        "och_analysis_id": och_analysis_id,
                    },
                    "exception_type": type(e).__name__,
                    "traceback": traceback.format_exc(),
                },
            )
