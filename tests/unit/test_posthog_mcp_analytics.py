"""Tests for PostHog MCP analytics (posthog_analytics) and its wiring in main().

The end-to-end tests drive a real FastMCP server through the real posthog.mcp
pipeline with only `capture` intercepted, so a change in the SDK's event shape
that would bypass `scrub_posthog_mcp_event` fails here instead of leaking.
"""

import asyncio
import logging
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock, patch

import pytest
from fastmcp import Client, FastMCP
from posthog import Posthog

from rootly_mcp_server.__main__ import main
from rootly_mcp_server.posthog_analytics import (
    build_posthog_client,
    maybe_enable_posthog_mcp_analytics,
)
from rootly_mcp_server.server import (
    ArgumentNormalizationMiddleware,
    CamelCaseAliasMiddleware,
    InjectedToolAnnotationMiddleware,
    LegacyContextArgumentMiddleware,
    ToolUsageLoggingMiddleware,
)
from rootly_mcp_server.telemetry_scrubber import scrub_posthog_mcp_event

# Fake credentials, split so secret scanners do not flag the fixtures.
# The bearer value is one the SDK's own sanitizer lets through, so only our
# hook removes it.
FAKE_BEARER = "abcdefghijklm" + "nopqrstuvwxyz123456"
FAKE_STRIPE = "sk_" + "live_abcdefghijklmnopqrstuvwxyz"


def test_build_posthog_client_is_noop_without_token():
    with patch.dict("os.environ", {}, clear=True):
        assert build_posthog_client(Mock(), agentcat_enabled=False) is None


def test_build_posthog_client_uses_env_host():
    environ = {"POSTHOG_PROJECT_TOKEN": "phc_test", "POSTHOG_HOST": "https://eu.i.posthog.com"}
    with patch.dict("os.environ", environ, clear=True):
        client = build_posthog_client(Mock(), agentcat_enabled=False)
    assert isinstance(client, Posthog)
    assert client.host == "https://eu.i.posthog.com"


def test_build_posthog_client_stays_off_while_agentcat_is_active():
    logger = Mock()
    with patch.dict("os.environ", {"POSTHOG_PROJECT_TOKEN": "phc_test"}, clear=True):
        assert build_posthog_client(logger, agentcat_enabled=True) is None
    logger.info.assert_called_once_with("PostHog MCP analytics off: AgentCat is active")


def test_maybe_enable_posthog_mcp_analytics_is_noop_without_client():
    with patch("rootly_mcp_server.posthog_analytics.instrument") as mock_instrument:
        maybe_enable_posthog_mcp_analytics(FastMCP("test"), None, Mock())
    mock_instrument.assert_not_called()


def test_maybe_enable_posthog_mcp_analytics_disables_schema_injection():
    with patch("rootly_mcp_server.posthog_analytics.instrument") as mock_instrument:
        maybe_enable_posthog_mcp_analytics(FastMCP("test"), Mock(), Mock())

    options = mock_instrument.call_args.args[2]
    assert options.context is False
    assert options.enable_conversation_id is False
    assert options.capture_model is False
    assert options.report_missing is False
    assert options.before_send is scrub_posthog_mcp_event


def test_maybe_enable_posthog_mcp_analytics_swallows_instrument_errors():
    logger = Mock()
    with patch("rootly_mcp_server.posthog_analytics.instrument", side_effect=RuntimeError("boom")):
        maybe_enable_posthog_mcp_analytics(FastMCP("test"), Mock(), logger)
    logger.warning.assert_called_once()


def stdio_args() -> SimpleNamespace:
    return SimpleNamespace(
        swagger_path=None,
        log_level="ERROR",
        name="Rootly",
        transport="stdio",
        debug=False,
        base_url=None,
        allowed_paths=None,
        hosted=False,
        enable_code_mode=False,
        enable_write_tools=None,
        enabled_tools=None,
        list_tools=False,
        code_mode_path=None,
        host=False,
    )


@pytest.mark.parametrize("agentcat_enabled", [True, False])
def test_main_builds_posthog_only_without_agentcat(agentcat_enabled):
    server = SimpleNamespace(run=Mock())

    with patch.dict("os.environ", {"ROOTLY_API_TOKEN": "x" * 40}, clear=True):
        with patch("rootly_mcp_server.__main__.parse_args", return_value=stdio_args()):
            with patch("rootly_mcp_server.__main__.setup_logging"):
                with patch(
                    "rootly_mcp_server.__main__.create_rootly_mcp_server",
                    return_value=server,
                ):
                    with patch(
                        "rootly_mcp_server.__main__.maybe_enable_mcpcat_tracking",
                        return_value=agentcat_enabled,
                    ):
                        with patch(
                            "rootly_mcp_server.__main__.build_posthog_client",
                            return_value=None,
                        ) as build:
                            main()

    build.assert_called_once()
    assert build.call_args.kwargs == {"agentcat_enabled": agentcat_enabled}


def test_main_shutdown_failure_does_not_mask_exit():
    server = SimpleNamespace(run=Mock())
    posthog_client = Mock()
    posthog_client.shutdown.side_effect = RuntimeError("flush failed")

    with patch.dict("os.environ", {"ROOTLY_API_TOKEN": "x" * 40}, clear=True):
        with patch("rootly_mcp_server.__main__.parse_args", return_value=stdio_args()):
            with patch("rootly_mcp_server.__main__.setup_logging"):
                with patch(
                    "rootly_mcp_server.__main__.create_rootly_mcp_server",
                    return_value=server,
                ):
                    with patch(
                        "rootly_mcp_server.__main__.build_posthog_client",
                        return_value=posthog_client,
                    ):
                        with patch("rootly_mcp_server.__main__.maybe_enable_posthog_mcp_analytics"):
                            main()

    server.run.assert_called_once()
    posthog_client.shutdown.assert_called_once()


async def _capture_session(calls: list[tuple[str, dict[str, Any]]]) -> list[dict[str, Any]]:
    server = FastMCP("test")

    @server.tool
    def echo(db_conn_password: str, note: str) -> str:
        return f"note={note} Bearer {FAKE_BEARER}"

    @server.tool
    def boom(q: str) -> str:
        raise ValueError(f"upstream rejected Bearer {FAKE_BEARER}")

    client = Posthog("phc_test", send=False)
    captured: list[dict[str, Any]] = []
    with patch.object(client, "capture", side_effect=lambda event, **kw: captured.append(kw)):
        maybe_enable_posthog_mcp_analytics(server, client, logging.getLogger(__name__))
        async with Client(server) as mcp_client:
            for name, arguments in calls:
                await mcp_client.call_tool(name, arguments, raise_on_error=False)
        # Capture is fire-and-forget; let the pending tasks finish.
        await asyncio.sleep(0.2)
    return captured


@pytest.mark.parametrize(
    "calls",
    [
        [("echo", {"db_conn_password": "hunter2", "note": "hi"})],
        [("boom", {"q": "x"})],
        [(f"token={FAKE_STRIPE}", {})],
    ],
    ids=["arguments-and-response", "error-message", "unknown-tool-name"],
)
async def test_no_secret_reaches_posthog(calls):
    captured = await _capture_session(calls)

    assert captured, "expected events to be captured"
    for value in ("hunter2", FAKE_BEARER, FAKE_STRIPE):
        assert value not in repr(captured)


async def test_tool_schemas_are_unchanged():
    server = FastMCP("test")

    @server.tool
    def echo(note: str) -> str:
        return note

    maybe_enable_posthog_mcp_analytics(server, Posthog("phc_test", send=False), Mock())
    async with Client(server) as mcp_client:
        tools = await mcp_client.list_tools()

    assert list(tools[0].inputSchema["properties"]) == ["note"]


@pytest.mark.parametrize("with_middleware", [True, False], ids=["tolerated", "rejected"])
async def test_legacy_context_argument_text_never_reaches_posthog(with_middleware):
    from rootly_mcp_server.server import LegacyContextArgumentMiddleware

    server = FastMCP("test")
    if with_middleware:
        server.add_middleware(LegacyContextArgumentMiddleware())

    @server.tool
    def list_incidents(page_size: int = 10) -> str:
        return f"page_size={page_size}"

    intent = "Checking incidents for Acme before paging"
    client = Posthog("phc_test", send=False)
    captured: list[dict[str, Any]] = []
    with patch.object(client, "capture", side_effect=lambda event, **kw: captured.append(kw)):
        maybe_enable_posthog_mcp_analytics(server, client, logging.getLogger(__name__))
        async with Client(server) as mcp_client:
            result = await mcp_client.call_tool(
                "list_incidents", {"page_size": 5, "context": intent}, raise_on_error=False
            )
        await asyncio.sleep(0.2)

    # With the middleware the call succeeds; without it the error message would
    # carry the intent, and the scrubber must still remove it.
    assert result.is_error is (not with_middleware)
    assert captured, "expected events to be captured"
    assert "Acme" not in repr(captured)


def server_with(*middleware) -> FastMCP:
    server = FastMCP("test")
    for item in middleware:
        server.add_middleware(item)

    @server.tool
    def ping() -> str:
        return "pong"

    return server


async def list_tools_and_capture(server: FastMCP) -> tuple[list[str], list[str]]:
    """List tools through the real PostHog pipeline; return tool and event names."""
    client = Posthog("phc_test", send=False)
    events: list[str] = []
    with patch.object(client, "capture", side_effect=lambda event, **_kw: events.append(event)):
        maybe_enable_posthog_mcp_analytics(server, client, logging.getLogger(__name__))
        async with Client(server) as mcp_client:
            tools = [tool.name for tool in await mcp_client.list_tools()]
        await asyncio.sleep(0.2)
    return tools, events


async def test_our_middleware_keeps_posthog_on():
    server = server_with(
        CamelCaseAliasMiddleware({"listIncidents": "list_incidents"}),
        ArgumentNormalizationMiddleware(),
        LegacyContextArgumentMiddleware(),
        InjectedToolAnnotationMiddleware(),
        ToolUsageLoggingMiddleware(),
    )

    tools, events = await list_tools_and_capture(server)

    assert tools == ["ping"]
    assert "$mcp_tools_list" in events
