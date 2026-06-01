"""Incident tool registration for Rootly MCP server."""

from __future__ import annotations

import logging
import re
from collections.abc import Awaitable, Callable
from typing import Annotated, Any, Literal, cast

from pydantic import Field

from ..smart_utils import SolutionExtractor, TextSimilarityAnalyzer

logger = logging.getLogger(__name__)

JsonDict = dict[str, Any]
MakeAuthenticatedRequest = Callable[..., Awaitable[Any]]
StripHeavyNestedData = Callable[[JsonDict], JsonDict]
GenerateRecommendation = Callable[[JsonDict], str]

RETROSPECTIVE_PROGRESS_STATUSES = ("not_started", "active", "completed", "skipped")
INCIDENT_SEARCH_FIELDS = (
    "id,title,summary,status,created_at,updated_at,url,started_at,retrospective_progress_status"
)
INCIDENT_LIST_FIELDS = (
    "id,sequential_id,title,summary,status,severity,created_at,updated_at,url,"
    "started_at,resolved_at,retrospective_progress_status"
)
INCIDENT_REFERENCE_FIELDS = "id,sequential_id"
INCIDENT_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$",
    re.IGNORECASE,
)
INCIDENT_SEQUENTIAL_REF_RE = re.compile(r"^(?:#|INC-)?(\d+)$")


def _split_csv_values(value: str) -> list[str]:
    """Split comma-separated values into a normalized list."""
    return [part.strip() for part in value.split(",") if part.strip()]


def _normalize_optional_text(value: str | None) -> str | None:
    """Normalize optional text inputs by trimming whitespace and empty values."""
    if value is None:
        return None
    normalized = value.strip()
    return normalized or None


def _extract_incident_severity(severity_value: Any) -> str | None:
    """Normalize severity values from Rootly API responses into a compact string."""
    if severity_value is None:
        return None
    if isinstance(severity_value, str):
        return severity_value
    if isinstance(severity_value, dict):
        if severity_value.get("slug") or severity_value.get("name"):
            return cast(str | None, severity_value.get("slug") or severity_value.get("name"))
        severity_data = severity_value.get("data")
        if isinstance(severity_data, dict):
            attributes = severity_data.get("attributes", {})
            if isinstance(attributes, dict):
                return cast(str | None, attributes.get("slug") or attributes.get("name"))
    return None


def _summarize_incident_record(incident: dict[str, Any]) -> dict[str, Any]:
    """Return a compact incident summary suitable for list/query workflows."""
    attrs = incident.get("attributes", {})
    sequential_id = attrs.get("sequential_id")
    incident_number = f"INC-{sequential_id}" if sequential_id is not None else None

    return {
        "incident_id": incident.get("id"),
        "incident_number": incident_number,
        "title": attrs.get("title"),
        "summary": attrs.get("summary"),
        "status": attrs.get("status"),
        "severity": _extract_incident_severity(attrs.get("severity")),
        "started_at": attrs.get("started_at"),
        "resolved_at": attrs.get("resolved_at"),
        "created_at": attrs.get("created_at"),
        "updated_at": attrs.get("updated_at"),
        "retrospective_progress_status": attrs.get("retrospective_progress_status"),
        "url": attrs.get("url"),
    }


def _extract_sequential_id(incident: dict[str, Any]) -> int | None:
    """Extract a numeric sequential incident ID from a Rootly incident record."""
    attrs = incident.get("attributes", {})
    sequential_id = attrs.get("sequential_id")
    if sequential_id is None:
        return None
    try:
        return int(sequential_id)
    except (TypeError, ValueError):
        return None


def _normalize_incident_reference(reference: str) -> tuple[str, str | int]:
    """Classify and normalize an incident reference."""
    normalized = _normalize_optional_text(reference)
    if normalized is None:
        raise ValueError("Incident reference is required")
    if INCIDENT_UUID_RE.match(normalized):
        return ("uuid", normalized)
    sequential_match = INCIDENT_SEQUENTIAL_REF_RE.match(normalized)
    if sequential_match:
        return ("sequential", int(sequential_match.group(1)))
    return ("direct", normalized)


async def _resolve_incident_reference_to_uuid(
    incident_reference: str,
    make_authenticated_request: MakeAuthenticatedRequest,
) -> str:
    """Resolve supported incident references to the Rootly incident UUID."""
    reference_kind, normalized_reference = _normalize_incident_reference(incident_reference)
    if reference_kind in {"uuid", "direct"}:
        return cast(str, normalized_reference)

    target_sequential_id = cast(int, normalized_reference)
    page_cache: dict[int, tuple[list[dict[str, Any]], dict[str, Any]]] = {}

    async def _fetch_page(page_number: int) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        cached = page_cache.get(page_number)
        if cached is not None:
            return cached

        response = await make_authenticated_request(
            "GET",
            "/v1/incidents",
            params={
                "page[size]": 100,
                "page[number]": page_number,
                "fields[incidents]": INCIDENT_REFERENCE_FIELDS,
                "include": "",
                "sort": "-created_at",
            },
        )
        response.raise_for_status()
        response_data = response.json()
        incidents = cast(list[dict[str, Any]], response_data.get("data", []))
        meta = cast(dict[str, Any], response_data.get("meta", {}))
        page_cache[page_number] = (incidents, meta)
        return incidents, meta

    incidents, meta = await _fetch_page(1)
    total_pages = int(meta.get("total_pages") or 1)

    left = 1
    right = total_pages

    while left <= right:
        page_number = (left + right) // 2
        if page_number == 1:
            page_incidents, _ = incidents, meta
        else:
            page_incidents, _ = await _fetch_page(page_number)

        sequential_ids = [
            sequential_id
            for sequential_id in (_extract_sequential_id(incident) for incident in page_incidents)
            if sequential_id is not None
        ]

        if not sequential_ids:
            break

        page_max = max(sequential_ids)
        page_min = min(sequential_ids)

        if target_sequential_id > page_max:
            right = page_number - 1
            continue
        if target_sequential_id < page_min:
            left = page_number + 1
            continue

        for incident in page_incidents:
            if _extract_sequential_id(incident) == target_sequential_id:
                incident_uuid = incident.get("id")
                if isinstance(incident_uuid, str) and incident_uuid:
                    return incident_uuid
                break

        raise LookupError(f"Incident reference not found: INC-{target_sequential_id}")

    raise LookupError(f"Incident reference not found: INC-{target_sequential_id}")


def register_incident_tools(
    mcp: Any,
    make_authenticated_request: MakeAuthenticatedRequest,
    strip_heavy_nested_data: StripHeavyNestedData,
    mcp_error: Any,
    generate_recommendation: GenerateRecommendation,
    enable_write_tools: bool = True,
) -> None:
    """Register incident search and recommendation tools on the MCP server."""

    # Initialize smart analysis tools
    similarity_analyzer = TextSimilarityAnalyzer()
    solution_extractor = SolutionExtractor()

    async def _resolve_team_names_to_ids(teams: str) -> tuple[str, dict[str, str]]:
        """Resolve comma-separated team names/slugs to Rootly team IDs."""
        requested_teams = _split_csv_values(teams)
        if not requested_teams:
            return "", {}

        resolved_team_ids: list[str] = []
        resolved_team_lookup: dict[str, str] = {}
        unresolved_teams: list[str] = []

        for team in requested_teams:
            matched_id = None

            for filter_key, expected_value in (
                ("filter[slug]", team),
                ("filter[name]", team),
            ):
                response = await make_authenticated_request(
                    "GET",
                    "/v1/teams",
                    params={
                        "page[size]": 100,
                        "page[number]": 1,
                        filter_key: team,
                    },
                )
                response.raise_for_status()

                for candidate in response.json().get("data", []):
                    attrs = candidate.get("attributes", {})
                    candidate_value = (
                        attrs.get("slug") if filter_key == "filter[slug]" else attrs.get("name")
                    )
                    if (
                        isinstance(candidate_value, str)
                        and candidate_value.lower() == expected_value.lower()
                    ):
                        matched_id = str(candidate.get("id"))
                        break

                if matched_id:
                    break

            if matched_id:
                resolved_team_ids.append(matched_id)
                resolved_team_lookup[team] = matched_id
            else:
                unresolved_teams.append(team)

        if unresolved_teams:
            raise ValueError(
                "Could not resolve team names/slugs to team IDs: " + ", ".join(unresolved_teams)
            )

        return ",".join(dict.fromkeys(resolved_team_ids)), resolved_team_lookup

    async def _prepare_incident_query_context(
        *,
        query: str,
        teams: str,
        team_ids: str,
        service_ids: str,
        severity: str,
        status: str,
        started_after: str,
        started_before: str,
        custom_field_selected_option_ids: str,
        sort: Literal["created_at", "-created_at", "updated_at", "-updated_at"],
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """Build shared incident query params and filter metadata for list/collect tools."""
        resolved_team_lookup: dict[str, str] = {}
        resolved_team_ids = team_ids

        if teams:
            resolved_teams_value, resolved_team_lookup = await _resolve_team_names_to_ids(teams)
            if resolved_team_ids and resolved_teams_value:
                combined_team_ids = _split_csv_values(resolved_team_ids) + _split_csv_values(
                    resolved_teams_value
                )
                resolved_team_ids = ",".join(dict.fromkeys(combined_team_ids))
            elif resolved_teams_value:
                resolved_team_ids = resolved_teams_value

        params: dict[str, Any] = {
            "fields[incidents]": INCIDENT_LIST_FIELDS,
            "include": "",
            "sort": sort,
        }

        if query:
            params["filter[search]"] = query
        if resolved_team_ids:
            params["filter[team_ids]"] = resolved_team_ids
        if service_ids:
            params["filter[service_ids]"] = service_ids
        if severity:
            params["filter[severity]"] = severity
        if status:
            params["filter[status]"] = status
        if started_after:
            params["filter[started_at][gte]"] = started_after
        if started_before:
            params["filter[started_at][lte]"] = started_before
        if custom_field_selected_option_ids:
            params["filter[custom_field_selected_option_ids]"] = custom_field_selected_option_ids

        filters = {
            "query": query,
            "teams": teams,
            "team_ids": team_ids,
            "resolved_team_ids": resolved_team_ids,
            "resolved_team_lookup": resolved_team_lookup,
            "service_ids": service_ids,
            "severity": severity,
            "status": status,
            "started_after": started_after,
            "started_before": started_before,
            "custom_field_selected_option_ids": custom_field_selected_option_ids,
            "sort": sort,
        }

        return params, filters

    @mcp.tool()
    async def list_incidents(
        query: Annotated[
            str,
            Field(description="Optional free-text search across incident titles and summaries"),
        ] = "",
        teams: Annotated[
            str,
            Field(
                description="Comma-separated team names or slugs to filter incidents (e.g., 'Infrastructure,platform-team')"
            ),
        ] = "",
        team_ids: Annotated[
            str,
            Field(
                description="Comma-separated Rootly team IDs to filter incidents (e.g., '123,456')"
            ),
        ] = "",
        service_ids: Annotated[
            str,
            Field(
                description="Comma-separated Rootly service IDs to filter incidents (e.g., 'svc-1,svc-2')"
            ),
        ] = "",
        severity: Annotated[
            str,
            Field(description="Optional severity filter (e.g., critical, high, medium, low)"),
        ] = "",
        status: Annotated[
            str,
            Field(
                description="Optional incident status filter (e.g., started, investigating, resolved)"
            ),
        ] = "",
        started_after: Annotated[
            str,
            Field(description="Filter incidents that started at or after this ISO 8601 timestamp"),
        ] = "",
        started_before: Annotated[
            str,
            Field(description="Filter incidents that started at or before this ISO 8601 timestamp"),
        ] = "",
        custom_field_selected_option_ids: Annotated[
            str,
            Field(
                description="Comma-separated custom field option IDs for structured incident filtering"
            ),
        ] = "",
        sort: Annotated[
            Literal["created_at", "-created_at", "updated_at", "-updated_at"],
            Field(
                description="Sort order for incidents. Supported values: created_at, -created_at, updated_at, -updated_at"
            ),
        ] = "-created_at",
        page_size: Annotated[
            int,
            Field(description="Number of incidents per page (max: 100)", ge=1, le=100),
        ] = 25,
        page_number: Annotated[
            int,
            Field(description="Page number to retrieve (1-indexed)", ge=1),
        ] = 1,
    ) -> JsonDict:
        """
        🚨 List incidents with structured filters - ESSENTIAL for incident response.

        WHEN TO USE:
        • During incident response to check for related ongoing incidents
        • For shift handoffs to review recent incidents by team/service
        • For post-incident analysis to find patterns by severity/date range
        • For audit workflows requiring specific filtering criteria

        Use this when you need date-range, team, service, severity, or status filters.
        For simple text searches, prefer search_incidents instead.
        """
        try:
            params, filters = await _prepare_incident_query_context(
                query=query,
                teams=teams,
                team_ids=team_ids,
                service_ids=service_ids,
                severity=severity,
                status=status,
                started_after=started_after,
                started_before=started_before,
                custom_field_selected_option_ids=custom_field_selected_option_ids,
                sort=sort,
            )
        except ValueError as e:
            return cast(JsonDict, mcp_error.tool_error(str(e), "validation_error"))
        except Exception as e:
            error_type, error_message = mcp_error.categorize_error(e)
            return cast(JsonDict, mcp_error.tool_error(error_message, error_type))

        params["page[size]"] = page_size
        params["page[number]"] = page_number

        try:
            response = await make_authenticated_request("GET", "/v1/incidents", params=params)
            response.raise_for_status()

            response_data = strip_heavy_nested_data(response.json())
            incidents = response_data.get("data", [])
            meta = response_data.get("meta", {})

            return {
                "incidents": [_summarize_incident_record(incident) for incident in incidents],
                "returned_incidents": len(incidents),
                "pagination": {
                    "page_size": page_size,
                    "page_number": page_number,
                    "current_page": meta.get("current_page", page_number),
                    "next_page": meta.get("next_page"),
                    "prev_page": meta.get("prev_page"),
                    "total_pages": meta.get("total_pages"),
                    "total_count": meta.get("total_count"),
                    "has_more": meta.get("next_page") is not None,
                },
                "filters": filters,
            }
        except Exception as e:
            error_type, error_message = mcp_error.categorize_error(e)
            return cast(JsonDict, mcp_error.tool_error(error_message, error_type))

    @mcp.tool()
    async def collect_incidents(
        query: Annotated[
            str,
            Field(description="Optional free-text search across incident titles and summaries"),
        ] = "",
        teams: Annotated[
            str,
            Field(
                description="Comma-separated team names or slugs to filter incidents (e.g., 'Infrastructure,platform-team')"
            ),
        ] = "",
        team_ids: Annotated[
            str,
            Field(
                description="Comma-separated Rootly team IDs to filter incidents (e.g., '123,456')"
            ),
        ] = "",
        service_ids: Annotated[
            str,
            Field(
                description="Comma-separated Rootly service IDs to filter incidents (e.g., 'svc-1,svc-2')"
            ),
        ] = "",
        severity: Annotated[
            str,
            Field(description="Optional severity filter (e.g., critical, high, medium, low)"),
        ] = "",
        status: Annotated[
            str,
            Field(
                description="Optional incident status filter (e.g., started, investigating, resolved)"
            ),
        ] = "",
        started_after: Annotated[
            str,
            Field(description="Filter incidents that started at or after this ISO 8601 timestamp"),
        ] = "",
        started_before: Annotated[
            str,
            Field(description="Filter incidents that started at or before this ISO 8601 timestamp"),
        ] = "",
        custom_field_selected_option_ids: Annotated[
            str,
            Field(
                description="Comma-separated custom field option IDs for structured incident filtering"
            ),
        ] = "",
        sort: Annotated[
            Literal["created_at", "-created_at", "updated_at", "-updated_at"],
            Field(
                description="Sort order for incidents. Supported values: created_at, -created_at, updated_at, -updated_at"
            ),
        ] = "-created_at",
        max_results: Annotated[
            int,
            Field(
                description="Maximum number of compact incident summaries to collect across pages (max: 100)",
                ge=1,
                le=100,
            ),
        ] = 50,
        batch_size: Annotated[
            int,
            Field(
                description="Number of incidents to request per upstream page while collecting (min: 10, max: 100)",
                ge=10,
                le=100,
            ),
        ] = 25,
    ) -> JsonDict:
        """
        Collect a bounded working set of incidents across multiple pages for audits and analysis.

        Use this instead of list_incidents when you want a compact batch of incidents in one
        tool call, while keeping payload size under control.
        """
        try:
            params, filters = await _prepare_incident_query_context(
                query=query,
                teams=teams,
                team_ids=team_ids,
                service_ids=service_ids,
                severity=severity,
                status=status,
                started_after=started_after,
                started_before=started_before,
                custom_field_selected_option_ids=custom_field_selected_option_ids,
                sort=sort,
            )
        except ValueError as e:
            return cast(JsonDict, mcp_error.tool_error(str(e), "validation_error"))
        except Exception as e:
            error_type, error_message = mcp_error.categorize_error(e)
            return cast(JsonDict, mcp_error.tool_error(error_message, error_type))

        collected_incidents: list[dict[str, Any]] = []
        page_number = 1
        pages_fetched = 0
        total_matching_count: int | None = None
        results_truncated = False

        try:
            while len(collected_incidents) < max_results:
                page_params = dict(params)
                page_params["page[size]"] = batch_size
                page_params["page[number]"] = page_number

                response = await make_authenticated_request(
                    "GET", "/v1/incidents", params=page_params
                )
                response.raise_for_status()

                response_data = strip_heavy_nested_data(response.json())
                page_incidents = response_data.get("data", [])
                meta = response_data.get("meta", {})
                pages_fetched += 1

                if total_matching_count is None:
                    total_matching_count = meta.get("total_count")

                if not page_incidents:
                    break

                remaining = max_results - len(collected_incidents)
                if len(page_incidents) > remaining:
                    results_truncated = True
                collected_incidents.extend(page_incidents[:remaining])

                next_page = meta.get("next_page")
                if next_page is None:
                    break
                if len(collected_incidents) >= max_results:
                    results_truncated = True
                    break
                page_number = next_page

            if total_matching_count is not None and total_matching_count > len(collected_incidents):
                results_truncated = True

            return {
                "incidents": [
                    _summarize_incident_record(incident) for incident in collected_incidents
                ],
                "returned_incidents": len(collected_incidents),
                "collection": {
                    "max_results": max_results,
                    "batch_size": batch_size,
                    "pages_fetched": pages_fetched,
                    "total_matching_count": total_matching_count,
                    "results_truncated": results_truncated,
                },
                "filters": filters,
            }
        except Exception as e:
            error_type, error_message = mcp_error.categorize_error(e)
            return cast(JsonDict, mcp_error.tool_error(error_message, error_type))

    @mcp.tool()
    async def search_incidents(
        query: Annotated[
            str, Field(description="Search query to filter incidents by title/summary")
        ] = "",
        page_size: Annotated[
            int, Field(description="Number of results per page (max: 20)", ge=1, le=20)
        ] = 10,
        page_number: Annotated[
            int, Field(description="Page number to retrieve (use 0 for all pages)", ge=0)
        ] = 1,
        max_results: Annotated[
            int,
            Field(
                description=(
                    "Maximum total results when fetching all pages "
                    "(ignored if page_number > 0). Max: 10. For larger result sets, use "
                    "page_number > 0 and paginate explicitly, or use collect_incidents."
                ),
                ge=1,
                le=10,
            ),
        ] = 5,
    ) -> JsonDict:
        """
        Search incidents with flexible pagination control.

        Use page_number=0 to fetch all matching results across multiple pages up to max_results.
        Use page_number>0 to fetch a specific page.

        Argument caps: page_size <= 20, max_results <= 10.
        """
        # Single page mode
        if page_number > 0:
            params = {
                "page[size]": page_size,  # Use requested page size (already limited to max 20)
                "page[number]": page_number,
                "include": "",
                "fields[incidents]": INCIDENT_SEARCH_FIELDS,
            }
            if query:
                params["filter[search]"] = query

            try:
                response = await make_authenticated_request("GET", "/v1/incidents", params=params)
                response.raise_for_status()
                return strip_heavy_nested_data(response.json())
            except Exception as e:
                error_type, error_message = mcp_error.categorize_error(e)
                return cast(JsonDict, mcp_error.tool_error(error_message, error_type))

        # Multi-page mode (page_number = 0)
        all_incidents: list[dict[str, Any]] = []
        current_page = 1
        effective_page_size = page_size  # Use requested page size (already limited to max 20)
        max_pages = 10  # Safety limit to prevent infinite loops

        try:
            while len(all_incidents) < max_results and current_page <= max_pages:
                params = {
                    "page[size]": effective_page_size,
                    "page[number]": current_page,
                    "include": "",
                    "fields[incidents]": INCIDENT_SEARCH_FIELDS,
                }
                if query:
                    params["filter[search]"] = query

                try:
                    response = await make_authenticated_request(
                        "GET", "/v1/incidents", params=params
                    )
                    response.raise_for_status()
                    response_data = response.json()

                    if "data" in response_data:
                        incidents = response_data["data"]
                        if not incidents:
                            # No more incidents available
                            break

                        # Check if we got fewer incidents than requested (last page)
                        if len(incidents) < effective_page_size:
                            all_incidents.extend(incidents)
                            break

                        all_incidents.extend(incidents)

                        # Check metadata if available
                        meta = response_data.get("meta", {})
                        current_page_meta = meta.get("current_page", current_page)
                        total_pages = meta.get("total_pages")

                        # If we have reliable metadata, use it
                        if total_pages and current_page_meta >= total_pages:
                            break

                        current_page += 1
                    else:
                        break

                except Exception as e:
                    # Re-raise authentication or critical errors for immediate handling
                    if (
                        "401" in str(e)
                        or "Unauthorized" in str(e)
                        or "authentication" in str(e).lower()
                    ):
                        error_type, error_message = mcp_error.categorize_error(e)
                        return cast(JsonDict, mcp_error.tool_error(error_message, error_type))
                    # For other errors, break loop and return partial results
                    break

            # Limit to max_results
            if len(all_incidents) > max_results:
                all_incidents = all_incidents[:max_results]

            return strip_heavy_nested_data(
                {
                    "data": all_incidents,
                    "meta": {
                        "total_fetched": len(all_incidents),
                        "max_results": max_results,
                        "query": query,
                        "pages_fetched": current_page - 1,
                        "page_size": effective_page_size,
                    },
                }
            )
        except Exception as e:
            error_type, error_message = mcp_error.categorize_error(e)
            return cast(JsonDict, mcp_error.tool_error(error_message, error_type))

    @mcp.tool(name="get_incident")
    async def get_incident(
        incident_id: Annotated[
            str,
            Field(
                description=(
                    "Incident reference to retrieve. "
                    "ARGUMENT NAME IS `incident_id` (not `id`). "
                    "Accepts: UUID (`7e83d9f4-6bc1-...`), bare sequential number "
                    "(`4460`), `#4460`, or `INC-4460`."
                )
            ),
        ],
    ) -> JsonDict:
        """Retrieve a single incident with PIR-related fields for direct verification."""
        try:
            resolved_incident_id = await _resolve_incident_reference_to_uuid(
                incident_id, make_authenticated_request
            )
            response = await make_authenticated_request(
                "GET", f"/v1/incidents/{resolved_incident_id}"
            )
            response.raise_for_status()

            response_data = response.json()
            if isinstance(response_data.get("data"), dict):
                stripped = strip_heavy_nested_data({"data": [response_data["data"]]})
                response_data["data"] = stripped["data"][0]
            return cast(JsonDict, response_data)
        except ValueError as e:
            return cast(
                JsonDict,
                mcp_error.tool_error(
                    f"Failed to retrieve incident: {e}",
                    "validation_error",
                ),
            )
        except LookupError as e:
            return cast(
                JsonDict,
                mcp_error.tool_error(
                    f"Failed to retrieve incident: {e}",
                    "not_found",
                ),
            )
        except Exception as e:
            error_type, error_message = mcp_error.categorize_error(e)
            return cast(
                JsonDict,
                mcp_error.tool_error(
                    f"Failed to retrieve incident: {error_message}",
                    error_type,
                ),
            )

    if enable_write_tools:

        @mcp.tool(name="create_incident")
        async def create_incident(
            title: Annotated[
                str | None,
                Field(description="Incident title. If omitted, Rootly may autogenerate one."),
            ] = None,
            summary: Annotated[
                str | None,
                Field(description="Incident summary or short description."),
            ] = None,
            severity_id: Annotated[
                str | None,
                Field(description="Optional severity ID to attach to the incident."),
            ] = None,
            service_ids: Annotated[
                str | None,
                Field(description="Comma-separated service IDs to attach to the incident."),
            ] = None,
            team_ids: Annotated[
                str | None,
                Field(description="Comma-separated team IDs to attach to the incident."),
            ] = None,
            environment_ids: Annotated[
                str | None,
                Field(description="Comma-separated environment IDs to attach to the incident."),
            ] = None,
            incident_type_ids: Annotated[
                str | None,
                Field(description="Comma-separated incident type IDs to attach to the incident."),
            ] = None,
        ) -> JsonDict:
            """Create an incident with a scoped set of fields for agent-driven workflows."""
            normalized_title = _normalize_optional_text(title)
            normalized_summary = _normalize_optional_text(summary)

            if normalized_title is None and normalized_summary is None:
                return cast(
                    JsonDict,
                    mcp_error.tool_error(
                        "Must provide at least one of title or summary",
                        "validation_error",
                    ),
                )

            attributes: dict[str, Any] = {}

            if normalized_title is not None:
                attributes["title"] = normalized_title
            if normalized_summary is not None:
                attributes["summary"] = normalized_summary

            normalized_severity_id = _normalize_optional_text(severity_id)
            if normalized_severity_id is not None:
                attributes["severity_id"] = normalized_severity_id

            csv_attribute_map = (
                ("service_ids", service_ids),
                ("group_ids", team_ids),
                ("environment_ids", environment_ids),
                ("incident_type_ids", incident_type_ids),
            )
            for attribute_name, raw_value in csv_attribute_map:
                if raw_value is None:
                    continue
                values = _split_csv_values(raw_value)
                if values:
                    attributes[attribute_name] = values

            payload = {
                "data": {
                    "type": "incidents",
                    "attributes": attributes,
                }
            }

            try:
                response = await make_authenticated_request("POST", "/v1/incidents", json=payload)
                response.raise_for_status()

                response_data = response.json()
                if isinstance(response_data.get("data"), dict):
                    stripped = strip_heavy_nested_data({"data": [response_data["data"]]})
                    response_data["data"] = stripped["data"][0]
                return cast(JsonDict, response_data)
            except Exception as e:
                error_type, error_message = mcp_error.categorize_error(e)
                return cast(
                    JsonDict,
                    mcp_error.tool_error(
                        f"Failed to create incident: {error_message}",
                        error_type,
                    ),
                )

        @mcp.tool(name="update_incident")
        async def update_incident(
            incident_id: Annotated[
                str,
                Field(
                    description="Incident reference to update: UUID, bare number like 4460, #4460, or INC-4460"
                ),
            ],
            retrospective_progress_status: Annotated[
                str | None,
                Field(
                    description="Retrospective/PIR status: one of not_started, active, completed, skipped"
                ),
            ] = None,
            summary: Annotated[
                str | None,
                Field(description="Updated incident summary"),
            ] = None,
        ) -> JsonDict:
            """Update scoped incident fields for PIR lifecycle automation."""
            attributes: dict[str, Any] = {}

            if retrospective_progress_status is not None:
                if retrospective_progress_status not in RETROSPECTIVE_PROGRESS_STATUSES:
                    allowed = ", ".join(RETROSPECTIVE_PROGRESS_STATUSES)
                    return cast(
                        JsonDict,
                        mcp_error.tool_error(
                            f"retrospective_progress_status must be one of: {allowed}",
                            "validation_error",
                        ),
                    )
                attributes["retrospective_progress_status"] = retrospective_progress_status

            if summary is not None:
                attributes["summary"] = summary

            if not attributes:
                return cast(
                    JsonDict,
                    mcp_error.tool_error(
                        "Must provide at least one of retrospective_progress_status or summary",
                        "validation_error",
                    ),
                )

            payload = {
                "data": {
                    "type": "incidents",
                    "attributes": attributes,
                }
            }

            try:
                resolved_incident_id = await _resolve_incident_reference_to_uuid(
                    incident_id, make_authenticated_request
                )
                response = await make_authenticated_request(
                    "PUT", f"/v1/incidents/{resolved_incident_id}", json=payload
                )
                response.raise_for_status()

                response_data = response.json()
                if isinstance(response_data.get("data"), dict):
                    stripped = strip_heavy_nested_data({"data": [response_data["data"]]})
                    response_data["data"] = stripped["data"][0]
                return cast(JsonDict, response_data)
            except Exception as e:
                error_type, error_message = mcp_error.categorize_error(e)
                return cast(
                    JsonDict,
                    mcp_error.tool_error(
                        f"Failed to update incident: {error_message}",
                        error_type,
                    ),
                )

    @mcp.tool()
    async def find_related_incidents(
        incident_id: str = "",
        incident_description: str = "",
        similarity_threshold: Annotated[
            float, Field(description="Minimum similarity score (0.0-1.0)", ge=0.0, le=1.0)
        ] = 0.15,
        max_results: Annotated[
            int, Field(description="Maximum number of related incidents to return", ge=1, le=20)
        ] = 5,
        status_filter: Annotated[
            str,
            Field(
                description="Filter incidents by status (empty for all, 'resolved', 'investigating', etc.)"
            ),
        ] = "",
    ) -> JsonDict:
        """
        🔍 Find historically similar incidents using ML similarity analysis - CRITICAL for incident response.

        WHEN TO USE:
        • EARLY in incident response to learn from past similar issues
        • When you're unsure about root cause or resolution approach
        • Before escalating to find if this is a known pattern
        • For post-incident analysis to identify recurring issues

        Provide either incident_id OR incident_description (e.g., 'website is down', 'database timeout errors').
        Use status_filter to limit to specific incident statuses or leave empty for all incidents.
        """
        try:
            target_incident: dict[str, Any] = {}
            resolved_incident_id = ""

            if incident_id:
                # Get the target incident details by ID
                resolved_incident_id = await _resolve_incident_reference_to_uuid(
                    incident_id, make_authenticated_request
                )
                target_response = await make_authenticated_request(
                    "GET", f"/v1/incidents/{resolved_incident_id}"
                )
                target_response.raise_for_status()
                target_incident_data = strip_heavy_nested_data(
                    {"data": [target_response.json().get("data", {})]}
                )
                target_incident = target_incident_data.get("data", [{}])[0]

                if not target_incident:
                    return cast(JsonDict, mcp_error.tool_error("Incident not found", "not_found"))

            elif incident_description:
                # Create synthetic incident for analysis from descriptive text
                target_incident = {
                    "id": "synthetic",
                    "attributes": {
                        "title": incident_description,
                        "summary": incident_description,
                        "description": incident_description,
                    },
                }
            else:
                return cast(
                    JsonDict,
                    mcp_error.tool_error(
                        "Must provide either incident_id or incident_description",
                        "validation_error",
                    ),
                )

            # Get historical incidents for comparison
            params = {
                "page[size]": 100,  # Get more incidents for better matching
                "page[number]": 1,
                "include": "",
                "fields[incidents]": "id,title,summary,status,created_at,url",
            }

            # Only add status filter if specified
            if status_filter:
                params["filter[status]"] = status_filter

            historical_response = await make_authenticated_request(
                "GET", "/v1/incidents", params=params
            )
            historical_response.raise_for_status()
            historical_data = strip_heavy_nested_data(historical_response.json())
            historical_incidents = historical_data.get("data", [])

            # Filter out the target incident itself if it exists
            if incident_id:
                historical_incidents = [
                    inc
                    for inc in historical_incidents
                    if str(inc.get("id")) != str(resolved_incident_id)
                ]

            if not historical_incidents:
                return {
                    "related_incidents": [],
                    "message": "No historical incidents found for comparison",
                    "target_incident": {
                        "id": incident_id or "synthetic",
                        "resolved_incident_id": resolved_incident_id or None,
                        "title": target_incident.get("attributes", {}).get(
                            "title", incident_description
                        ),
                    },
                }

            # Calculate similarities
            similar_incidents = similarity_analyzer.calculate_similarity(
                historical_incidents, target_incident
            )

            # Filter by threshold and limit results
            filtered_incidents = [
                inc for inc in similar_incidents if inc.similarity_score >= similarity_threshold
            ][:max_results]

            # Format response
            related_incidents = []
            for incident in filtered_incidents:
                related_incidents.append(
                    {
                        "incident_id": incident.incident_id,
                        "title": incident.title,
                        "similarity_score": round(incident.similarity_score, 3),
                        "matched_services": incident.matched_services,
                        "matched_keywords": incident.matched_keywords,
                        "resolution_summary": incident.resolution_summary,
                        "resolution_time_hours": incident.resolution_time_hours,
                    }
                )

            return {
                "target_incident": {
                    "id": incident_id or "synthetic",
                    "resolved_incident_id": resolved_incident_id or None,
                    "title": target_incident.get("attributes", {}).get(
                        "title", incident_description
                    ),
                },
                "related_incidents": related_incidents,
                "total_found": len(filtered_incidents),
                "similarity_threshold": similarity_threshold,
                "analysis_summary": f"Found {len(filtered_incidents)} similar incidents out of {len(historical_incidents)} historical incidents",
            }

        except Exception as e:
            error_type, error_message = mcp_error.categorize_error(e)
            return cast(
                JsonDict,
                mcp_error.tool_error(
                    f"Failed to find related incidents: {error_message}", error_type
                ),
            )

    @mcp.tool()
    async def suggest_solutions(
        incident_id: str = "",
        incident_title: str = "",
        incident_description: str = "",
        max_solutions: Annotated[
            int, Field(description="Maximum number of solution suggestions", ge=1, le=10)
        ] = 3,
        status_filter: Annotated[
            str,
            Field(
                description="Filter incidents by status (default 'resolved', empty for all, 'investigating', etc.)"
            ),
        ] = "resolved",
    ) -> JsonDict:
        """
        💡 Get actionable solution recommendations from similar resolved incidents - KEY for incident response.

        WHEN TO USE:
        • AFTER find_related_incidents to get specific resolution steps
        • When team is stuck on how to resolve the current incident
        • To speed up incident resolution with proven solutions
        • For training new responders on resolution patterns

        Provide either incident_id OR title/description. Defaults to resolved incidents for solution mining.
        """
        try:
            target_incident: dict[str, Any] = {}
            resolved_incident_id = ""

            if incident_id:
                # Get incident details by ID
                resolved_incident_id = await _resolve_incident_reference_to_uuid(
                    incident_id, make_authenticated_request
                )
                response = await make_authenticated_request(
                    "GET", f"/v1/incidents/{resolved_incident_id}"
                )
                response.raise_for_status()
                incident_data = strip_heavy_nested_data({"data": [response.json().get("data", {})]})
                target_incident = incident_data.get("data", [{}])[0]

                if not target_incident:
                    return cast(JsonDict, mcp_error.tool_error("Incident not found", "not_found"))

            elif incident_title or incident_description:
                # Create synthetic incident for analysis
                target_incident = {
                    "id": "synthetic",
                    "attributes": {
                        "title": incident_title,
                        "summary": incident_description,
                        "description": incident_description,
                    },
                }
            else:
                return cast(
                    JsonDict,
                    mcp_error.tool_error(
                        "Must provide either incident_id or incident_title/description",
                        "validation_error",
                    ),
                )

            # Get incidents for solution mining
            params = {
                "page[size]": 150,  # Get more incidents for better solution matching
                "page[number]": 1,
                "include": "",
            }

            # Only add status filter if specified
            if status_filter:
                params["filter[status]"] = status_filter

            historical_response = await make_authenticated_request(
                "GET", "/v1/incidents", params=params
            )
            historical_response.raise_for_status()
            historical_data = strip_heavy_nested_data(historical_response.json())
            historical_incidents = historical_data.get("data", [])

            # Filter out target incident if it exists
            if incident_id:
                historical_incidents = [
                    inc
                    for inc in historical_incidents
                    if str(inc.get("id")) != str(resolved_incident_id)
                ]

            if not historical_incidents:
                status_msg = f" with status '{status_filter}'" if status_filter else ""
                return {
                    "solutions": [],
                    "message": f"No historical incidents found{status_msg} for solution mining",
                }

            # Find similar incidents
            similar_incidents = similarity_analyzer.calculate_similarity(
                historical_incidents, target_incident
            )

            # Filter to reasonably similar incidents (lower threshold for solution suggestions)
            relevant_incidents = [inc for inc in similar_incidents if inc.similarity_score >= 0.2][
                : max_solutions * 2
            ]

            if not relevant_incidents:
                return {
                    "solutions": [],
                    "message": "No sufficiently similar incidents found for solution suggestions",
                    "suggestion": "This appears to be a unique incident. Consider escalating or consulting documentation.",
                }

            # Extract solutions
            solution_data = solution_extractor.extract_solutions(relevant_incidents)

            # Format response
            return {
                "target_incident": {
                    "id": incident_id or "synthetic",
                    "resolved_incident_id": resolved_incident_id or None,
                    "title": target_incident.get("attributes", {}).get("title", incident_title),
                    "description": target_incident.get("attributes", {}).get(
                        "summary", incident_description
                    ),
                },
                "solutions": solution_data["solutions"][:max_solutions],
                "insights": {
                    "common_patterns": solution_data["common_patterns"],
                    "average_resolution_time_hours": solution_data["average_resolution_time"],
                    "total_similar_incidents": solution_data["total_similar_incidents"],
                },
                "recommendation": generate_recommendation(solution_data),
            }

        except Exception as e:
            error_type, error_message = mcp_error.categorize_error(e)
            return cast(
                JsonDict,
                mcp_error.tool_error(
                    f"Failed to suggest solutions: {error_message}",
                    error_type,
                ),
            )
