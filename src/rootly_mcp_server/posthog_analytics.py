"""PostHog MCP analytics for the hosted server.

Opt-in via POSTHOG_PROJECT_TOKEN, so self-hosted and local runs are unchanged.
"""

import functools
import logging
import os
from typing import Any

from fastmcp import FastMCP
from posthog import Posthog
from posthog.mcp import MCPAnalyticsOptions, UserIdentity, instrument

from .telemetry_scrubber import scrub_posthog_mcp_event
from .transport import get_hosted_authenticated_user


def build_posthog_client(logger: logging.Logger, *, agentcat_enabled: bool) -> Posthog | None:
    """Create the process-wide PostHog client, or None when analytics stays off.

    Off while AgentCat is active: posthog.mcp (<= 7.60.1) breaks tools/list on
    servers carrying AgentCat's middleware.
    """
    token = os.getenv("POSTHOG_PROJECT_TOKEN", "").strip()
    if not token:
        return None
    if agentcat_enabled:
        logger.info("PostHog MCP analytics off: AgentCat is active")
        return None
    try:
        return Posthog(token, host=os.getenv("POSTHOG_HOST", "https://us.i.posthog.com"))
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


def maybe_enable_posthog_mcp_analytics(
    server: FastMCP, posthog_client: Posthog | None, logger: logging.Logger
) -> None:
    """Instrument *server* with PostHog MCP analytics when a client is configured."""
    if posthog_client is None:
        return
    try:
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
