"""Curated tools for Rootly AI SRE investigations (``/v1/ai/investigations``)."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Annotated, Any, Protocol

from mcp.types import ToolAnnotations
from pydantic import Field

InvestigationId = Annotated[str, Field(description="Investigation id (UUID).")]


class MCPErrorLike(Protocol):
    @staticmethod
    def tool_error(
        error_message: str,
        error_type: str = "execution_error",
        details: dict[str, Any] | None = None,
    ) -> dict[str, Any]: ...

    @staticmethod
    def categorize_error(exception: Exception) -> tuple[str, str]: ...


def _flatten(resource: dict[str, Any]) -> dict[str, Any]:
    return {"id": resource.get("id"), **(resource.get("attributes") or {})}


def _api_error(exc: Exception) -> dict[str, Any] | None:
    response = getattr(exc, "response", None)
    try:
        errors = response.json().get("errors") if response is not None else None
    except ValueError:
        return None
    if (
        isinstance(errors, list)
        and errors
        and isinstance(errors[0], dict)
        and errors[0].get("title")
    ):
        return errors[0]
    return None


def register_investigation_tools(
    mcp: Any,
    make_authenticated_request: Callable[..., Awaitable[Any]],
    mcp_error: MCPErrorLike,
    enable_write_tools: bool = True,
) -> None:
    """Register investigation tools. Start and cancel need write tools enabled."""

    async def _request(method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        response = await make_authenticated_request(method, path, **kwargs)
        response.raise_for_status()
        body: dict[str, Any] = response.json()
        return body

    def _error(exc: Exception) -> dict[str, Any]:
        error_type, message = mcp_error.categorize_error(exc)
        api_error = _api_error(exc)
        if api_error:
            code = api_error.get("code")
            message = f"{api_error['title']} ({code})" if code else api_error["title"]
        status = getattr(getattr(exc, "response", None), "status_code", None)
        details = {"upstream_status": status} if isinstance(status, int) else None
        return mcp_error.tool_error(message, error_type, details)

    @mcp.tool(
        annotations=ToolAnnotations(
            read_only_hint=True, destructive_hint=False, open_world_hint=True
        ),
    )
    async def list_investigations(
        alert_id: Annotated[
            str | None, Field(description="Only investigations of this alert.")
        ] = None,
        incident_id: Annotated[
            str | None, Field(description="Only investigations of this incident.")
        ] = None,
        status: Annotated[
            str | None,
            Field(description="pending, running, completed, failed or cancelled."),
        ] = None,
        outcome: Annotated[
            str | None,
            Field(
                description="root_cause_identified, best_effort, inconclusive, blocked or could_not_investigate."
            ),
        ] = None,
        page_size: Annotated[int, Field(ge=1, le=50)] = 10,
        page_number: Annotated[int, Field(ge=1)] = 1,
    ) -> dict[str, Any]:
        """List AI SRE investigations you can see, newest first."""
        params: dict[str, Any] = {"page[size]": page_size, "page[number]": page_number}
        for key, value in (
            ("alert_id", alert_id),
            ("incident_id", incident_id),
            ("status", status),
            ("outcome", outcome),
        ):
            if value:
                params[f"filter[{key}]"] = value
        try:
            body = await _request("GET", "/v1/ai/investigations", params=params)
        except Exception as exc:
            return _error(exc)
        return {
            "investigations": [_flatten(item) for item in body.get("data", [])],
            "meta": body.get("meta", {}),
        }

    @mcp.tool(
        annotations=ToolAnnotations(
            read_only_hint=True, destructive_hint=False, open_world_hint=True
        ),
    )
    async def get_investigation(
        investigation_id: Annotated[
            str | None, Field(description="Investigation id. Omit to use alert_id/incident_id.")
        ] = None,
        alert_id: Annotated[
            str | None, Field(description="Use the latest investigation of this alert.")
        ] = None,
        incident_id: Annotated[
            str | None, Field(description="Use the latest investigation of this incident.")
        ] = None,
    ) -> dict[str, Any]:
        """Get an investigation's current status, outcome, summary and root cause.
        While it is pending or running, call this tool later to check its status.
        The API returns a snapshot; it does not expose a step-by-step progress stream."""
        try:
            if not investigation_id:
                if not (alert_id or incident_id):
                    return mcp_error.tool_error(
                        "Pass investigation_id, alert_id or incident_id.", "validation_error"
                    )
                latest: dict[str, Any] = await list_investigations(
                    alert_id=alert_id, incident_id=incident_id, page_size=1
                )
                if latest.get("error"):
                    return latest
                if not latest["investigations"]:
                    return mcp_error.tool_error("No investigation found.", "not_found")
                investigation_id = str(latest["investigations"][0]["id"])
            body = await _request("GET", f"/v1/ai/investigations/{investigation_id}")
            return {"investigation": _flatten(body["data"])}
        except Exception as exc:
            return _error(exc)

    if not enable_write_tools:
        return

    @mcp.tool(
        annotations=ToolAnnotations(
            read_only_hint=False,
            destructive_hint=False,
            idempotent_hint=False,
            open_world_hint=True,
        ),
    )
    async def start_investigation(
        alert_id: Annotated[str | None, Field(description="Alert to investigate.")] = None,
        incident_id: Annotated[str | None, Field(description="Incident to investigate.")] = None,
        rerun: Annotated[
            bool,
            Field(
                description=(
                    "Start a fresh run. Fails with rerun_unavailable while the latest run is "
                    "still going or rerun is off for the team."
                )
            ),
        ] = False,
    ) -> dict[str, Any]:
        """Start an AI SRE investigation on one alert or incident. Then check its
        status and result with get_investigation."""
        if bool(alert_id) == bool(incident_id):
            return mcp_error.tool_error(
                "Pass exactly one of alert_id or incident_id.", "validation_error"
            )
        attributes: dict[str, Any] = (
            {"alert_id": alert_id} if alert_id else {"incident_id": incident_id}
        )
        if rerun:
            attributes["rerun"] = True
        try:
            body = await _request(
                "POST",
                "/v1/ai/investigations",
                json={"data": {"type": "investigations", "attributes": attributes}},
            )
        except Exception as exc:
            return _error(exc)
        investigation = _flatten(body["data"])
        return {
            "investigation": investigation,
            "next_step": "Call get_investigation with "
            f"investigation_id={investigation['id']!r} to check its status and result.",
        }

    @mcp.tool(
        annotations=ToolAnnotations(
            read_only_hint=False,
            destructive_hint=True,
            idempotent_hint=True,
            open_world_hint=True,
        ),
    )
    async def cancel_investigation(investigation_id: InvestigationId) -> dict[str, Any]:
        """Cancel a running investigation."""
        try:
            body = await _request(
                "POST",
                f"/v1/ai/investigations/{investigation_id}/cancel",
                json={"data": {"type": "investigations", "attributes": {}}},
            )
        except Exception as exc:
            return _error(exc)
        return {"investigation": _flatten(body["data"])}
