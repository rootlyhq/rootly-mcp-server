"""`submit_feedback`: agents report what went wrong with the server, to PostHog.

MCP analytics show which tools ran, not what the agent was trying to do. This
tool lets the agent say so: what it attempted, what it expected, and a fix it
would propose. Each report is a `mcp_feedback_submitted` event in the same
PostHog session as the caller's tool calls, so a report can be read next to
the calls that led to it.

Hosted only: the tool is registered when PostHog analytics is on (it has
nowhere to send otherwise). Feedback text is caller-controlled: it is
credential-scrubbed and length-capped here, and must be treated as untrusted
data by anything that reads it downstream (never as instructions).
"""

from __future__ import annotations

import logging
import threading
import time
from collections import defaultdict, deque
from typing import Annotated, Any, Literal

import mcp.types as mt
from fastmcp import FastMCP
from posthog.client import Client
from pydantic import Field

from . import __version__
from .posthog_analytics import identity_groups, identity_properties, server_build_from_env
from .posthog_sessions import current_session_id
from .telemetry_scrubber import redact_telemetry_text
from .transport import get_hosted_authenticated_user

logger = logging.getLogger(__name__)

FEEDBACK_TOOL_NAME = "submit_feedback"
FEEDBACK_EVENT = "mcp_feedback_submitted"

FeedbackCategory = Literal["bug", "missing_capability", "confusing_output", "docs", "other"]

SUMMARY_MAX_LENGTH = 200
FIELD_MAX_LENGTH = 2000
TOOL_NAME_MAX_LENGTH = 100
# Per user, per process. Enough for real reports, too few to flood the channel.
RATE_LIMIT_COUNT = 5
RATE_LIMIT_WINDOW_SECONDS = 3600.0

_TRUNCATION_MARK = "…[truncated]"

FEEDBACK_TOOL_DESCRIPTION = """Report a problem with this Rootly MCP server to the Rootly team.

Call it only when a Rootly tool failed in a way you could not work around,
returned something unhelpful or misleading, or a capability you needed was
missing. Do not call it for successful calls, for errors you fixed by
changing your arguments, or for questions about the user's own Rootly data.

Be specific: name the tool, say what you were trying to do for the user, what
you expected and what happened, and propose a fix if you have one. Do not
include secrets, and include only as much of the user's data as the report
needs. Nothing is changed in the user's Rootly account."""


class FeedbackRateLimiter:
    """Allows `limit` reports per caller in any rolling `window` seconds."""

    def __init__(
        self,
        limit: int = RATE_LIMIT_COUNT,
        window: float = RATE_LIMIT_WINDOW_SECONDS,
        clock: Any = time.monotonic,
    ) -> None:
        self.limit = limit
        self.window = window
        self.clock = clock
        self._sent: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def allow(self, key: str) -> bool:
        now = self.clock()
        with self._lock:
            sent = self._sent[key]
            while sent and now - sent[0] >= self.window:
                sent.popleft()
            if len(sent) >= self.limit:
                return False
            sent.append(now)
            return True


def clean_text(value: str | None, max_length: int) -> str | None:
    """Scrub credentials, trim, and cap one caller-supplied field."""
    if value is None:
        return None
    text = redact_telemetry_text(str(value)).strip()
    if not text:
        return None
    if len(text) > max_length:
        text = text[: max_length - len(_TRUNCATION_MARK)] + _TRUNCATION_MARK
    return text


def feedback_properties(
    *,
    summary: str,
    category: str,
    tool_name: str | None,
    attempted: str | None,
    expected: str | None,
    proposed_fix: str | None,
) -> dict[str, Any]:
    """The event properties for one report, scrubbed and capped."""
    properties: dict[str, Any] = {
        "feedback_summary": clean_text(summary, SUMMARY_MAX_LENGTH) or "",
        "feedback_category": category,
        "feedback_tool_name": clean_text(tool_name, TOOL_NAME_MAX_LENGTH),
        "feedback_attempted": clean_text(attempted, FIELD_MAX_LENGTH),
        "feedback_expected": clean_text(expected, FIELD_MAX_LENGTH),
        "feedback_proposed_fix": clean_text(proposed_fix, FIELD_MAX_LENGTH),
        "$mcp_server_version": __version__,
    }
    build = server_build_from_env()
    if build:
        properties["$mcp_server_build"] = build
    return {key: value for key, value in properties.items() if value is not None}


def register_feedback_tool(
    server: FastMCP,
    posthog_client: Client | None,
    *,
    rate_limiter: FeedbackRateLimiter | None = None,
) -> None:
    """Add `submit_feedback` to *server* when PostHog analytics is on."""
    if posthog_client is None:
        return
    limiter = rate_limiter or FeedbackRateLimiter()

    @server.tool(
        name=FEEDBACK_TOOL_NAME,
        description=FEEDBACK_TOOL_DESCRIPTION,
        annotations=mt.ToolAnnotations(
            title="Report a problem with the Rootly MCP server",
            # Nothing in the user's Rootly account changes, so it stays
            # available to ?readonly=true connections.
            read_only_hint=True,
            destructive_hint=False,
            idempotent_hint=False,
            open_world_hint=True,
        ),
    )
    def submit_feedback(
        summary: Annotated[
            str, Field(description="One line: what went wrong. Up to 200 characters.")
        ],
        category: Annotated[
            FeedbackCategory,
            Field(
                description=(
                    "bug (a tool errored or returned wrong data), missing_capability "
                    "(no tool does what was needed), confusing_output (a result or "
                    "error was unclear or misleading), docs (a tool description or "
                    "parameter was wrong or unclear), or other."
                )
            ),
        ],
        tool_name: Annotated[
            str | None, Field(description="The Rootly tool the report is about, if any.")
        ] = None,
        attempted: Annotated[
            str | None,
            Field(description="What you were trying to do for the user, and the call you made."),
        ] = None,
        expected: Annotated[
            str | None, Field(description="What you expected, and what happened instead.")
        ] = None,
        proposed_fix: Annotated[
            str | None,
            Field(description="The change to the server that would have helped, if you know."),
        ] = None,
    ) -> dict[str, Any]:
        user = get_hosted_authenticated_user()
        if not user or not user.get("id"):
            return {
                "status": "not_recorded",
                "message": "Feedback is collected only for signed-in hosted users.",
            }
        if not limiter.allow(str(user["id"])):
            return {
                "status": "not_recorded",
                "message": (
                    "Feedback limit reached for now; the earlier reports were received. "
                    "Continue the user's task without reporting further."
                ),
            }
        properties = feedback_properties(
            summary=summary,
            category=category,
            tool_name=tool_name,
            attempted=attempted,
            expected=expected,
            proposed_fix=proposed_fix,
        )
        person = identity_properties(user)
        if person:
            properties["$set"] = person
        session_id = current_session_id()
        if session_id:
            properties["$session_id"] = session_id
        try:
            posthog_client.capture(
                FEEDBACK_EVENT,
                distinct_id=str(user["id"]),
                properties=properties,
                groups=identity_groups(user),
            )
        except Exception as error:  # noqa: BLE001 - feedback never breaks the session
            logger.warning("Feedback not recorded (%s)", type(error).__name__)
            return {"status": "not_recorded", "message": "Feedback could not be recorded."}
        return {
            "status": "recorded",
            "message": "Thanks, the Rootly team will review it. Continue the user's task.",
        }
