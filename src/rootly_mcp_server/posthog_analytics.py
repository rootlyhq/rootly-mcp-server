"""PostHog MCP analytics for the hosted server.

Opt-in via POSTHOG_PROJECT_TOKEN, so self-hosted and local runs are unchanged.
It runs alongside AgentCat when both are configured.
"""

import functools
import logging
import os
from typing import Any

from fastmcp import FastMCP
from fastmcp.server.middleware import Middleware as MCPMiddleware
from posthog import Posthog
from posthog.mcp import (
    MCPAnalyticsOptions,
    PostHogMcpStatelessSessionMiddleware,
    UserIdentity,
    instrument,
)
from starlette.middleware import Middleware as ASGIMiddleware

from .telemetry_scrubber import scrub_posthog_mcp_event
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
    """Presents a plain-callable middleware, such as AgentCat's, as a fastmcp Middleware.

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


def maybe_enable_posthog_mcp_analytics(
    server: FastMCP, posthog_client: Posthog | None, logger: logging.Logger
) -> None:
    """Instrument *server* with PostHog MCP analytics when a client is configured."""
    if posthog_client is None:
        return
    try:
        adapt_callable_middleware(server)
        options = MCPAnalyticsOptions(
            # Match the AgentCat configuration: no injected `context`,
            # `conversation_id`, `llm_model` or `get_more_tools`, so tool
            # schemas are unchanged and agents are never asked about themselves.
            context=False,
            enable_conversation_id=False,
            capture_model=False,
            report_missing=False,
            identify=identify,
            before_send=scrub_posthog_mcp_event,
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
    only, never an auth or tenancy signal. AgentCat's session resolver also keys
    on `Mcp-Session-Id` (always prefixed by the authenticated caller), so with
    this middleware on, AgentCat sessions become per-connection instead of
    hourly buckets for clients that replay the header.
    """
    if posthog_client is None:
        return []
    return [ASGIMiddleware(PostHogMcpStatelessSessionMiddleware)]
