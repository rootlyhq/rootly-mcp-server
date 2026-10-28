"""Curated tools for Rootly AI SRE investigations (``/v1/investigations``)."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from typing import Annotated, Any, Protocol

from mcp.types import ToolAnnotations
from pydantic import Field

PROGRESS_POLL_INTERVAL_SECONDS = 3.0
MAX_WAIT_SECONDS = 15

InvestigationId = Annotated[str, Field(description="Investigation id (UUID).")]
WaitSeconds = Annotated[
    int,
    Field(
        ge=0,
        le=MAX_WAIT_SECONDS,
        description="How long to wait for new progress before returning (0-15s).",
    ),
]


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


def _progress_instruction(done: bool, next_cursor: str | None) -> str:
    if done:
        return "The investigation has finished. Call get_investigation for the verdict."
    return (
        "The investigation is still running. Call get_investigation_progress again with "
        f"after_cursor={next_cursor!r} until done is true."
    )


def register_investigation_tools(
    mcp: Any,
    make_authenticated_request: Callable[..., Awaitable[Any]],
    mcp_error: MCPErrorLike,
    enable_write_tools: bool = True,
    poll_interval: float = PROGRESS_POLL_INTERVAL_SECONDS,
    sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
) -> None:
    """Register investigation tools. Start, ask and cancel need write tools enabled."""

    async def _request(method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        response = await make_authenticated_request(method, path, **kwargs)
        response.raise_for_status()
        body: dict[str, Any] = response.json()
        return body

    def _error(exc: Exception) -> dict[str, Any]:
        error_type, message = mcp_error.categorize_error(exc)
        api_error = _api_error(exc)
        if api_error:
            message = f"{api_error.get('title')} ({api_error.get('code')})"
        return mcp_error.tool_error(message, error_type)

    async def _fetch_progress(investigation_id: str, after_cursor: str | None) -> dict[str, Any]:
        params = {"after": after_cursor} if after_cursor else None
        body = await _request(
            "GET", f"/v1/investigations/{investigation_id}/progress", params=params
        )
        attributes: dict[str, Any] = body["data"]["attributes"]
        return attributes

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
            body = await _request("GET", "/v1/investigations", params=params)
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
        include_messages: Annotated[
            bool, Field(description="Also return the follow-up conversation.")
        ] = False,
    ) -> dict[str, Any]:
        """Get an investigation's verdict: summary, root cause, confidence, hypotheses,
        evidence, causal chain and open_questions (what Rootly couldn't verify; answer
        them with ask_investigation)."""
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
            body = await _request("GET", f"/v1/investigations/{investigation_id}")
            result = {"investigation": _flatten(body["data"])}
            if include_messages:
                result["conversation"] = await _conversation(investigation_id)
            return result
        except Exception as exc:
            return _error(exc)

    async def _conversation(investigation_id: str) -> dict[str, Any]:
        body = await _request("GET", f"/v1/investigations/{investigation_id}/messages")
        return {
            "messages": [_flatten(item) for item in body.get("data", [])],
            "meta": body.get("meta", {}),
        }

    @mcp.tool(
        annotations=ToolAnnotations(
            read_only_hint=True, destructive_hint=False, open_world_hint=True
        ),
    )
    async def get_investigation_progress(
        investigation_id: InvestigationId,
        after_cursor: Annotated[
            str | None, Field(description="next_cursor from the previous call.")
        ] = None,
        wait_seconds: WaitSeconds = MAX_WAIT_SECONDS,
    ) -> dict[str, Any]:
        """Follow a running investigation. Returns new steps and tool calls since
        after_cursor, waiting up to wait_seconds for them. Keep calling with
        after_cursor=next_cursor until done is true."""
        deadline = time.monotonic() + wait_seconds
        entries: list[dict[str, Any]] = []
        cursor = after_cursor
        try:
            while True:
                page = await _fetch_progress(investigation_id, cursor)
                entries.extend(page.get("entries", []))
                cursor = page.get("next_cursor") or cursor
                if page.get("has_more"):
                    continue
                if entries or page.get("done") or time.monotonic() + poll_interval > deadline:
                    break
                await sleep(poll_interval)
        except Exception as exc:
            return _error(exc)
        done = bool(page.get("done"))
        return {
            "investigation_id": investigation_id,
            "status": page.get("status"),
            "outcome": page.get("outcome"),
            "done": done,
            "phases": page.get("phases", []),
            "open_questions": page.get("open_questions", []),
            "entries": entries,
            "next_cursor": cursor,
            "next_step": _progress_instruction(done, cursor),
        }

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
            bool, Field(description="Start a fresh run even if one already exists.")
        ] = False,
    ) -> dict[str, Any]:
        """Start an AI SRE investigation on one alert or incident. Then follow it with
        get_investigation_progress."""
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
                "/v1/investigations",
                json={"data": {"type": "investigations", "attributes": attributes}},
            )
        except Exception as exc:
            return _error(exc)
        investigation = _flatten(body["data"])
        return {
            "investigation": investigation,
            "next_step": "Call get_investigation_progress with "
            f"investigation_id={investigation['id']!r} to follow it.",
        }

    @mcp.tool(
        annotations=ToolAnnotations(
            read_only_hint=False,
            destructive_hint=False,
            idempotent_hint=False,
            open_world_hint=True,
        ),
    )
    async def ask_investigation(
        investigation_id: InvestigationId,
        message: Annotated[
            str,
            Field(
                min_length=1,
                max_length=20_000,
                description="Follow-up question, or what you found locally (e.g. kubectl output) for an open question.",
            ),
        ],
    ) -> dict[str, Any]:
        """Send a follow-up to a completed investigation, e.g. answers to its
        open_questions. Rootly answers asynchronously: read it with
        get_investigation(include_messages=true) until conversation.meta.answering is false."""
        try:
            body = await _request(
                "POST",
                f"/v1/investigations/{investigation_id}/messages",
                json={
                    "data": {"type": "investigation_messages", "attributes": {"message": message}}
                },
            )
        except Exception as exc:
            return _error(exc)
        return {
            "messages": [_flatten(item) for item in body.get("data", [])],
            "meta": body.get("meta", {}),
            "next_step": "Rootly is answering. Call get_investigation with "
            f"investigation_id={investigation_id!r} and include_messages=true in a few seconds.",
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
                f"/v1/investigations/{investigation_id}/cancel",
                json={"data": {"type": "investigations", "attributes": {}}},
            )
        except Exception as exc:
            return _error(exc)
        return {"investigation": _flatten(body["data"])}
