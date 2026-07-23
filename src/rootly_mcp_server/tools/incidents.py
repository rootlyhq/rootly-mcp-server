"""Incident tool registration for Rootly MCP server."""

from __future__ import annotations

import logging
import re
from collections.abc import Awaitable, Callable
from typing import Annotated, Any, Literal, cast

from mcp.types import ToolAnnotations
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
INCIDENT_SEQUENTIAL_REF_RE = re.compile(r"^(?:#|INC-)?(\d+)$", re.IGNORECASE)


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
            attributes = severity_data.get("attributes") or {}
            if isinstance(attributes, dict):
                return cast(str | None, attributes.get("slug") or attributes.get("name"))
    return None


def _summarize_incident_record(incident: dict[str, Any]) -> dict[str, Any]:
    """Return a compact incident summary suitable for list/query workflows."""
    # `or {}` (not a default) so a present-but-null "attributes" doesn't crash.
    attrs = incident.get("attributes") or {}
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
    attrs = incident.get("attributes") or {}
    sequential_id = attrs.get("sequential_id")
    if sequential_id is None:
        return None
    try:
        return int(sequential_id)
    except (TypeError, ValueError):
        return None


def _augment_pagination_error(result: JsonDict, page_number: int) -> JsonDict:
    """Append deep-pagination guidance to a client error from a paged list call.

    The Rootly API rejects deep offset pagination (a large ``page[number]``) with
    a 400. When a paged list request fails with a client error on a page beyond
    the first, point the caller at filters or ``collect_incidents`` instead of
    leaving a bare 400. Advisory only — does not change the error type.
    """
    if (
        page_number > 1
        and result.get("error")
        and result.get("error_type") == "client_error"
        and isinstance(result.get("message"), str)
    ):
        result["message"] += (
            " If you were paging deep, note the API rejects large page numbers; "
            "narrow the query with filters (team, service, severity, date range) "
            "or use collect_incidents instead of a high page_number."
        )
    return result


# Generic incident vocabulary that doesn't help narrow a topical search.
_SEARCH_STOPTERMS = frozenset(
    {
        "down",
        "up",
        "error",
        "errors",
        "issue",
        "issues",
        "failing",
        "fail",
        "failed",
        "all",
        "calls",
        "call",
        "hard",
        "slow",
        "broken",
        "outage",
        "incident",
        "incidents",
        "alert",
        "alerts",
        "high",
        "low",
        "not",
        "working",
        "unavailable",
        "degraded",
        "problem",
        "problems",
        "major",
        "minor",
        "critical",
        "prod",
        "production",
        "staging",
    }
)


def _extract_incident_search_terms(
    target_incident: dict[str, Any], max_terms: int = 3
) -> list[str]:
    """Pick the most distinctive tokens from a target incident's text.

    Used to retrieve topically-relevant historical candidates via
    ``filter[search]`` (which reaches incidents of any age) instead of only
    scanning the most-recent page. Longer tokens are treated as more specific.
    """
    attributes = target_incident.get("attributes", {}) or {}
    text = " ".join(
        str(attributes.get(field) or "") for field in ("title", "summary", "description")
    ).lower()
    ranked: list[str] = []
    seen: set[str] = set()
    for token in sorted(re.findall(r"[a-z0-9][a-z0-9._-]{2,}", text), key=len, reverse=True):
        if token in _SEARCH_STOPTERMS or token in seen:
            continue
        seen.add(token)
        ranked.append(token)
    return ranked[:max_terms]


async def _fetch_similarity_candidates(
    make_authenticated_request: MakeAuthenticatedRequest,
    strip_heavy_nested_data: Callable[[JsonDict], JsonDict],
    target_incident: dict[str, Any],
    *,
    status_filter: str = "",
    fields: str | None = None,
    max_candidates: int = 400,
) -> list[dict[str, Any]]:
    """Build the candidate pool for incident similarity analysis.

    Combines the most-recent page (so results never regress below the previous
    behavior) with targeted ``filter[search]`` queries on the target's most
    distinctive terms. The search queries surface relevant incidents of *any*
    age rather than only those in the last ~100 by recency. Both the baseline
    and each search are best-effort: a failure (e.g. a deployment that ignores
    ``filter[search]``) degrades gracefully to whatever else was collected.
    """
    base_params: dict[str, Any] = {"include": "", "page[size]": 100, "page[number]": 1}
    if fields:
        base_params["fields[incidents]"] = fields
    if status_filter:
        base_params["filter[status]"] = status_filter

    candidates: dict[str, dict[str, Any]] = {}

    async def _collect(params: dict[str, Any]) -> None:
        response = await make_authenticated_request("GET", "/v1/incidents", params=params)
        response.raise_for_status()
        for incident in strip_heavy_nested_data(response.json()).get("data", []):
            incident_id = str(incident.get("id") or "")
            if incident_id:
                candidates.setdefault(incident_id, incident)

    # 1. Recent baseline — preserves the previous behavior as a floor.
    try:
        await _collect(dict(base_params))
    except Exception:  # noqa: BLE001 - baseline is best-effort
        logger.debug("Similarity candidate baseline fetch failed", exc_info=True)

    # 2. Topical search across all history for the target's distinctive terms.
    for term in _extract_incident_search_terms(target_incident):
        if len(candidates) >= max_candidates:
            break
        try:
            await _collect({**base_params, "filter[search]": term})
        except Exception:  # noqa: BLE001 - search is optional/best-effort
            logger.debug("Similarity candidate search failed for %r", term, exc_info=True)

    return list(candidates.values())[:max_candidates]


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
    # A "direct" reference is interpolated into the request path
    # (/v1/incidents/{ref}), so reject anything that could redirect the target
    # (path traversal, extra segments, query/whitespace injection).
    if (
        "/" in normalized
        or "\\" in normalized
        or ".." in normalized
        or any(char.isspace() for char in normalized)
    ):
        raise ValueError(f"Invalid incident reference: {reference!r}")
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

    # Resolve the human-readable incident number (the 123 in INC-123) directly
    # via the API's filter[sequential_id] rather than walking the incident list.
    # Deep pagination (a high page[number]) is rejected by the API with a 400,
    # so the previous page-scanning approach broke for accounts with enough
    # incidents to push the match onto a deep page.
    response = await make_authenticated_request(
        "GET",
        "/v1/incidents",
        params={
            "filter[sequential_id]": target_sequential_id,
            "page[size]": 1,
            "fields[incidents]": INCIDENT_REFERENCE_FIELDS,
        },
    )
    response.raise_for_status()
    response_data = response.json()
    incidents = cast(list[dict[str, Any]], response_data.get("data", []))

    for incident in incidents:
        # Guard against the filter being ignored/unsupported: only accept an
        # exact sequential_id match so we never resolve to the wrong incident.
        if _extract_sequential_id(incident) == target_sequential_id:
            incident_uuid = incident.get("id")
            if isinstance(incident_uuid, str) and incident_uuid:
                return incident_uuid
            break

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

    def _reference_tool_error(action: str, exc: Exception) -> JsonDict:
        """Map an incident-reference operation failure to a consistent tool error.

        Every tool that resolves an incident reference shares this taxonomy:
        a bad/blank reference is a ``validation_error``, an unresolved
        sequential number is ``not_found``, and anything else is categorized by
        ``mcp_error``. Centralized here so the tools can't drift apart.
        """
        if isinstance(exc, ValueError):
            return cast(JsonDict, mcp_error.tool_error(f"{action}: {exc}", "validation_error"))
        if isinstance(exc, LookupError):
            return cast(JsonDict, mcp_error.tool_error(f"{action}: {exc}", "not_found"))
        error_type, error_message = mcp_error.categorize_error(exc)
        return cast(JsonDict, mcp_error.tool_error(f"{action}: {error_message}", error_type))

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

    @mcp.tool(
        annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=True),
    )
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
            return _augment_pagination_error(
                cast(JsonDict, mcp_error.tool_error(error_message, error_type)),
                page_number,
            )

    @mcp.tool(
        annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=True),
    )
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

    @mcp.tool(
        annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=True),
    )
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
                return _augment_pagination_error(
                    cast(JsonDict, mcp_error.tool_error(error_message, error_type)),
                    page_number,
                )

        # Multi-page mode (page_number = 0)
        all_incidents: list[dict[str, Any]] = []
        current_page = 1
        effective_page_size = page_size  # Use requested page size (already limited to max 20)
        max_pages = 10  # Safety limit to prevent infinite loops
        page_error: str | None = None  # Set if a page fetch fails mid-scan

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
                    # For other errors, stop paging but flag the result as partial
                    # so callers don't mistake a truncated set for a complete one.
                    _, page_error = mcp_error.categorize_error(e)
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
                        # True when paging stopped early due to a page error; the
                        # result set is incomplete.
                        "partial": page_error is not None,
                        **({"error": page_error} if page_error else {}),
                    },
                }
            )
        except Exception as e:
            error_type, error_message = mcp_error.categorize_error(e)
            return cast(JsonDict, mcp_error.tool_error(error_message, error_type))

    @mcp.tool(
        name="get_incident",
        annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=True),
    )
    async def get_incident(
        incident_id: Annotated[
            str,
            Field(
                description=(
                    "Incident reference to retrieve. "
                    "Accepts: UUID, bare sequential number "
                    "(4460), #4460, or INC-4460."
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
        except Exception as e:
            return _reference_tool_error("Failed to retrieve incident", e)

    @mcp.tool(
        name="list_incident_roles",
        annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=True),
    )
    async def list_incident_roles(
        incident_id: Annotated[
            str,
            Field(
                description=(
                    "Incident reference whose role assignments to list. "
                    "Accepts: UUID, bare sequential number (4460), "
                    "#4460, or INC-4460."
                )
            ),
        ],
    ) -> JsonDict:
        """List role assignments for an incident (who is Commander, Scribe, etc.).

        Returns one entry per defined role with role metadata and the assigned
        user (or `user_id: null` for roles that are defined on the incident but
        not yet assigned). Use this to answer questions like "who is the
        incident commander?", "is there a postmortem owner?", or "list all
        roles for INC-4460".

        Implementation note: this wraps `GET /v1/incidents/{id}?include=roles`
        and flattens the JSON:API `included` array of `incident_role_assignments`
        into a flat table so callers don't need to walk the relationships graph.
        """
        try:
            resolved_incident_id = await _resolve_incident_reference_to_uuid(
                incident_id, make_authenticated_request
            )
            response = await make_authenticated_request(
                "GET",
                f"/v1/incidents/{resolved_incident_id}",
                params={"include": "roles"},
            )
            response.raise_for_status()

            payload = response.json()
            included = payload.get("included") if isinstance(payload, dict) else None
            assignments: list[dict[str, Any]] = []
            if isinstance(included, list):
                for item in included:
                    if (
                        not isinstance(item, dict)
                        or item.get("type") != "incident_role_assignments"
                    ):
                        continue
                    attrs = item.get("attributes") or {}
                    role_envelope = attrs.get("incident_role") or {}
                    role_data = (
                        role_envelope.get("data") if isinstance(role_envelope, dict) else None
                    ) or {}
                    role_attrs = role_data.get("attributes") or {}
                    user_envelope = attrs.get("user") or {}
                    user_data = (
                        user_envelope.get("data") if isinstance(user_envelope, dict) else None
                    )
                    user_attrs = (
                        user_data.get("attributes") or {} if isinstance(user_data, dict) else {}
                    )
                    role_name = role_attrs.get("name")
                    if isinstance(role_name, str):
                        role_name = role_name.strip() or None
                    assignments.append(
                        {
                            "assignment_id": item.get("id"),
                            "role_id": role_data.get("id"),
                            "role_name": role_name,
                            "role_slug": role_attrs.get("slug"),
                            "role_summary": role_attrs.get("summary"),
                            "user_id": (
                                user_data.get("id") if isinstance(user_data, dict) else None
                            ),
                            "user_email": user_attrs.get("email"),
                            "user_name": user_attrs.get("full_name") or user_attrs.get("name"),
                            "assigned_at": attrs.get("created_at"),
                            "updated_at": attrs.get("updated_at"),
                        }
                    )

            return cast(
                JsonDict,
                {
                    "data": assignments,
                    "meta": {
                        "incident_id": resolved_incident_id,
                        "total_count": len(assignments),
                        "assigned_count": sum(1 for a in assignments if a.get("user_id")),
                        "unassigned_count": sum(1 for a in assignments if not a.get("user_id")),
                    },
                },
            )
        except Exception as e:
            return _reference_tool_error("Failed to list incident roles", e)

    if enable_write_tools:

        @mcp.tool(
            name="create_incident",
            annotations=ToolAnnotations(
                readOnlyHint=False,
                destructiveHint=False,
                idempotentHint=False,
                openWorldHint=True,
            ),
        )
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

        @mcp.tool(
            name="update_incident",
            annotations=ToolAnnotations(
                readOnlyHint=False,
                destructiveHint=False,
                idempotentHint=True,
                openWorldHint=True,
            ),
        )
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

            # Normalize like create_incident so a whitespace-only summary isn't
            # sent to the API verbatim.
            summary = _normalize_optional_text(summary)
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
                return _reference_tool_error("Failed to update incident", e)

    @mcp.tool(
        annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=True),
    )
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

            # Build the candidate pool: recent incidents plus topical search
            # matches (so relevant incidents older than the most-recent page are
            # still considered, not just the last ~100 by recency).
            historical_incidents = await _fetch_similarity_candidates(
                make_authenticated_request,
                strip_heavy_nested_data,
                target_incident,
                status_filter=status_filter,
                fields="id,title,summary,status,created_at,url",
            )

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
            return _reference_tool_error("Failed to find related incidents", e)

    @mcp.tool(
        annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=True),
    )
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

            # Mine solutions from recent incidents plus topical search matches,
            # so proven resolutions from older (but similar) incidents are not
            # missed just because they fell outside the most-recent page.
            historical_incidents = await _fetch_similarity_candidates(
                make_authenticated_request,
                strip_heavy_nested_data,
                target_incident,
                status_filter=status_filter,
            )

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
            return _reference_tool_error("Failed to suggest solutions", e)
