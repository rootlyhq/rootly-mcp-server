"""Alert tool registration for Rootly MCP server."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Annotated, Any, Protocol

from pydantic import Field

JsonDict = dict[str, Any]
MakeAuthenticatedRequest = Callable[..., Awaitable[Any]]


class MCPErrorLike(Protocol):
    """Protocol for MCP error helpers used by alert tools."""

    @staticmethod
    def tool_error(
        error_message: str,
        error_type: str = "execution_error",
        details: dict[str, Any] | None = None,
    ) -> JsonDict: ...

    @staticmethod
    def categorize_error(exception: Exception) -> tuple[str, str]: ...


def register_alert_tools(
    mcp: Any,
    make_authenticated_request: MakeAuthenticatedRequest,
    mcp_error: MCPErrorLike,
) -> None:
    """Register alert tools on the MCP server."""

    @mcp.tool()
    async def get_alert_by_short_id(
        short_id: Annotated[
            str,
            Field(
                description="The alert short_id (e.g., 'PhIQtP') or full alert URL (e.g., 'https://rootly.com/account/alerts/PhIQtP')"
            ),
        ],
    ) -> JsonDict:
        """Get alert details by short_id or alert URL. Use this when a user pastes an alert URL or short_id from a pager notification and wants to investigate the alert."""
        try:
            alert_short_id = short_id.strip()
            if "/" in alert_short_id:
                alert_short_id = alert_short_id.rstrip("/").split("/")[-1]

            if not alert_short_id:
                return mcp_error.tool_error("short_id is required", "validation_error")

            # The Rootly API silently ignores filter[short_id], so we use
            # filter[search] (which works) and verify the match client-side
            # to guard against fuzzy hits on summary/description.
            response = await make_authenticated_request(
                "GET",
                "/v1/alerts",
                params={
                    "filter[search]": alert_short_id,
                    "page[size]": 10,
                    "fields[alerts]": "id,summary,status,started_at,ended_at,short_id,source,description,noise,alert_urgency_id,url,created_at",
                },
            )
            response.raise_for_status()
            for alert in response.json().get("data", []):
                attrs = alert.get("attributes", {})
                if attrs.get("short_id") == alert_short_id:
                    return {
                        "id": alert.get("id"),
                        "short_id": attrs.get("short_id"),
                        "summary": attrs.get("summary"),
                        "status": attrs.get("status"),
                        "source": attrs.get("source"),
                        "description": attrs.get("description"),
                        "started_at": attrs.get("started_at"),
                        "ended_at": attrs.get("ended_at"),
                        "noise": attrs.get("noise"),
                        "url": attrs.get("url"),
                        "created_at": attrs.get("created_at"),
                    }

            return mcp_error.tool_error(
                f"Alert with short_id '{alert_short_id}' not found",
                "not_found",
            )

        except Exception as e:
            error_type, error_message = mcp_error.categorize_error(e)
            return mcp_error.tool_error(
                f"Failed to get alert by short_id: {error_message}", error_type
            )
