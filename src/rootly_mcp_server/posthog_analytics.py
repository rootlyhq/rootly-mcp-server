"""PostHog MCP analytics for the hosted server.

Opt-in via POSTHOG_PROJECT_TOKEN, so self-hosted and local runs send nothing.
"""

import functools
import logging
import os
import re
from contextvars import ContextVar
from typing import Any

from fastmcp import FastMCP
from fastmcp.server.middleware import Middleware as MCPMiddleware
from posthog import Posthog
from posthog.exception_utils import exceptions_from_error_tuple
from posthog.mcp import (
    MCPAnalyticsOptions,
    PostHogMcpStatelessSessionMiddleware,
    UserIdentity,
    instrument,
)
from starlette.middleware import Middleware as ASGIMiddleware

from . import __version__
from .exceptions import GUIDED_ERROR_TYPE_NAMES, is_guided_exception
from .telemetry_scrubber import redact_telemetry_text, scrub_posthog_mcp_event
from .transport import get_hosted_authenticated_user


def build_posthog_client(logger: logging.Logger) -> Posthog | None:
    """Create the process-wide PostHog client, or None when analytics stays off."""
    token = os.getenv("POSTHOG_PROJECT_TOKEN", "").strip()
    if not token:
        return None
    try:
        client = Posthog(token, host=os.getenv("POSTHOG_HOST", "https://us.i.posthog.com"))
        logger.info("PostHog MCP analytics on")
        return client
    except Exception as error:
        logger.warning(
            "PostHog MCP analytics could not be enabled; skipping (%s)",
            type(error).__name__,
        )
        return None


def identify(_request: Any, _extra: Any) -> UserIdentity | None:
    """Identify hosted users by Rootly user ID only."""
    user = get_hosted_authenticated_user()
    return UserIdentity(distinct_id=str(user["id"])) if user else None


class CallableMiddlewareAdapter(MCPMiddleware):
    """Presents a plain-callable middleware as a fastmcp Middleware.

    posthog.mcp (7.60.1 through at least 7.73.3) reads dispatch hooks off each
    middleware's class and raises for anything that is not a Middleware
    subclass, which broke tools/list on fastmcp 3 / mcp SDK 1.x. On fastmcp 4 /
    mcp SDK 2.x, instrument() takes its v2 handler-registry path, which skips
    that lookup, so this adapter is a guard rather than a fix there; it is kept
    because the lookup is still in posthog.mcp. FastMCP (both phases of its
    fastmcp 4 dispatch) only calls `await middleware(context, call_next)`, so
    forwarding __call__ keeps behavior identical. PostHog uses the hook lookup
    only to decide whether to inject `llm_model`, which capture_model=False
    turns off. Delete once posthog.mcp tolerates such middleware.
    """

    def __init__(self, inner: Any) -> None:
        self.inner = inner

    async def __call__(self, context: Any, call_next: Any) -> Any:
        return await self.inner(context, call_next)


def adapt_callable_middleware(server: FastMCP) -> None:
    """Wrap every non-Middleware entry of the chain in place, keeping its position."""
    server.middleware[:] = [
        m if isinstance(m, MCPMiddleware) else CallableMiddlewareAdapter(m)
        for m in server.middleware
    ]


_logger = logging.getLogger(__name__)

# The exception the current tool call raised, as our middleware saw it. FastMCP
# turns a tool's exception into an `isError` result before posthog.mcp records
# the call, so the SDK only gets the text: `$mcp_error_type` is always "Error"
# and `$exception_list` has no frames. The event is built in the same request
# (its capture task copies this context), so `before_send` can read it back.
_tool_exception: ContextVar[BaseException | None] = ContextVar(
    "rootly_posthog_tool_exception", default=None
)

# Rootly API answers about the caller's request, not server faults: a missing
# or invisible record, a feature off for the plan, a token without the scope, a
# conflict, the rate limit. 400 and 422 stay unexpected: those can mean the
# server built the request wrong.
_EXPECTED_UPSTREAM_STATUSES = frozenset({401, 403, 404, 409, 429})
_HTTP_STATUS = re.compile(r"\bHTTP error (\d{3})\b")
_FASTMCP_TOOL_WRAPPER = "Error calling tool"
_MAX_FRAMES = 50
_MAX_VALUE_LENGTH = 2048
# posthog-python can attach frame locals; they would carry tool arguments.
_FRAME_KEYS_DROPPED = ("code_variables", "vars")


class ToolExceptionCaptureMiddleware(MCPMiddleware):
    """Remembers the exception a tool call raised, for `enrich_posthog_mcp_event`.

    Registered first, so it wraps every other middleware and sees what they
    raise too (guided errors included). It never changes the outcome.
    """

    async def on_call_tool(self, context: Any, call_next: Any) -> Any:
        _tool_exception.set(None)
        try:
            return await call_next(context)
        except Exception as exc:
            _tool_exception.set(exc)
            raise


def _chain(exc: BaseException) -> list[BaseException]:
    seen: set[int] = set()
    chain: list[BaseException] = []
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        chain.append(current)
        current = current.__cause__ or current.__context__
    return chain


def _primary_exception(exc: BaseException) -> BaseException:
    """Step past fastmcp's "Error calling tool 'x': ..." wrapper to its cause."""
    if (
        type(exc).__module__.startswith("fastmcp.")
        and str(exc).startswith(_FASTMCP_TOOL_WRAPPER)
        and exc.__cause__ is not None
    ):
        return exc.__cause__
    return exc


def _http_status(exc: BaseException) -> int | None:
    for link in _chain(exc):
        status = getattr(getattr(link, "response", None), "status_code", None)
        if isinstance(status, int):
            return status
    match = _HTTP_STATUS.search(str(exc))
    return int(match.group(1)) if match else None


def _exception_list(exc: BaseException) -> list[dict[str, Any]]:
    exceptions = exceptions_from_error_tuple((type(exc), exc, exc.__traceback__))
    for entry in exceptions:
        if isinstance(entry.get("value"), str):
            # Scrub before cutting: a cut can drop the closing quote a quoted
            # credential pattern needs, and the rest of the secret would pass.
            entry["value"] = redact_telemetry_text(entry["value"])[:_MAX_VALUE_LENGTH]
        stacktrace = entry.get("stacktrace")
        if not isinstance(stacktrace, dict):
            continue
        frames = stacktrace.get("frames")
        if isinstance(frames, list):
            stacktrace["frames"] = [
                {key: value for key, value in frame.items() if key not in _FRAME_KEYS_DROPPED}
                for frame in frames[-_MAX_FRAMES:]
                if isinstance(frame, dict)
            ]
    return exceptions


def _event_messages(properties: dict[str, Any]) -> list[str]:
    """The error texts an event already carries: its message and exception values."""
    messages = [properties.get("$mcp_error_message")]
    exception_list = properties.get("$exception_list")
    if isinstance(exception_list, list):
        messages.extend(entry.get("value") for entry in exception_list if isinstance(entry, dict))
    return [message for message in messages if isinstance(message, str)]


def _event_types(properties: dict[str, Any]) -> set[str]:
    types = {properties.get("$mcp_error_type")}
    exception_list = properties.get("$exception_list")
    if isinstance(exception_list, list):
        types.update(entry.get("type") for entry in exception_list if isinstance(entry, dict))
    return {name for name in types if isinstance(name, str)}


def _status_from_messages(messages: list[str]) -> int | None:
    # The first status in a message is the response's own; a later one is
    # text quoted inside the response body.
    for message in messages:
        match = _HTTP_STATUS.search(message)
        if match:
            return int(match.group(1))
    return None


def enrich_posthog_mcp_event(event: Any) -> Any:
    """Put the real exception back on a failed tool call's events.

    On `$mcp_tool_call` and its `$exception` sibling: the exception class as
    `$mcp_error_type`, the upstream HTTP status as `$mcp_error_status`, and
    `mcp_error_expected` (with `mcp_error_expected_reason`) for failures that
    are the server answering a mistake (a guided error) or the API answering
    the caller (401/403/404/409/429). The `$exception` sibling gets the real
    `$exception_list`, frames and cause chain included. Every event gets this
    server's version in place of the framework's.

    The `$exception` sibling of an expected failure is dropped (None): it would
    open an Error Tracking issue for something that is not a bug. The `$mcp_tool_call` event stays, so failure tables
    and the error rate still count it.

    Without the exception our middleware saw (posthog.mcp may record the call
    itself one day), the type and status come from what the event carries.
    """
    properties = event.get("properties") if isinstance(event, dict) else None
    if not isinstance(properties, dict):
        return event
    properties["$mcp_server_version"] = __version__

    name = event.get("event")
    if name not in ("$mcp_tool_call", "$exception"):
        return event
    if name == "$mcp_tool_call" and not properties.get("$mcp_is_error"):
        return event

    exc = _tool_exception.get()
    if exc is not None:
        properties["$mcp_error_type"] = type(_primary_exception(exc)).__name__
        status = _http_status(exc)
        guided = is_guided_exception(exc)
    else:
        status = _status_from_messages(_event_messages(properties))
        guided = bool(_event_types(properties) & GUIDED_ERROR_TYPE_NAMES)
    if status is not None:
        properties["$mcp_error_status"] = str(status)
    if guided:
        properties["mcp_error_expected"] = True
        properties["mcp_error_expected_reason"] = "guided"
    elif status in _EXPECTED_UPSTREAM_STATUSES:
        properties["mcp_error_expected"] = True
        properties["mcp_error_expected_reason"] = "upstream_answer"
    else:
        properties["mcp_error_expected"] = False

    if name == "$exception":
        if properties["mcp_error_expected"]:
            return None
        if exc is not None:
            try:
                properties["$exception_list"] = _exception_list(exc)
            except Exception as error:  # noqa: BLE001 - keep the SDK's text-only list
                _logger.debug(
                    "PostHog MCP analytics: kept the text-only exception list (%s)",
                    type(error).__name__,
                )
    return event


def prepare_posthog_mcp_event(event: Any) -> Any:
    """PostHog `before_send`: restore error detail, drop expected exceptions, scrub."""
    enriched = enrich_posthog_mcp_event(event)
    return None if enriched is None else scrub_posthog_mcp_event(enriched)


def server_build_from_env() -> str | None:
    """The deployed commit, baked into the image as ROOTLY_MCP_BUILD."""
    return os.getenv("ROOTLY_MCP_BUILD", "").strip() or None


def maybe_enable_posthog_mcp_analytics(
    server: FastMCP, posthog_client: Posthog | None, logger: logging.Logger
) -> None:
    """Instrument *server* with PostHog MCP analytics when a client is configured."""
    if posthog_client is None:
        return
    try:
        adapt_callable_middleware(server)
        server.middleware.insert(0, ToolExceptionCaptureMiddleware())
        options = MCPAnalyticsOptions(
            # No injected `context`, `conversation_id`, `llm_model` or
            # `get_more_tools`, so tool schemas are unchanged and agents are
            # never asked about themselves.
            context=False,
            enable_conversation_id=False,
            capture_model=False,
            report_missing=False,
            identify=identify,
            before_send=prepare_posthog_mcp_event,
            server_build=server_build_from_env(),
            logger=functools.partial(logger.debug, "PostHog MCP analytics: %s"),
        )
        instrument(server, posthog_client, options)
    except Exception as error:
        logger.warning(
            "PostHog MCP analytics could not be enabled; skipping (%s)",
            type(error).__name__,
        )


def posthog_session_middleware(posthog_client: Posthog | None) -> list[ASGIMiddleware]:
    """ASGI middleware that ties a client's stateless requests into one PostHog session.

    It mints a self-encoded token into the `Mcp-Session-Id` header at
    initialize, which clients replay on every request. instrument() attaches it
    only to FastMCP's own app factories; the dual and profiled servers build
    their Starlette apps directly, so main() adds it after hosted auth. The
    stateless SDK ignores the header (and 2026-07-28 requests never initialize,
    so they get no token), so requests are otherwise unchanged.

    The token is unsigned and client-replayed: it is an analytics grouping key
    only, never an auth or tenancy signal.
    """
    if posthog_client is None:
        return []
    return [ASGIMiddleware(PostHogMcpStatelessSessionMiddleware)]
