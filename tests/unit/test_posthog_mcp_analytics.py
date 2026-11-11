"""Tests for PostHog MCP analytics (posthog_analytics) and its wiring in main().

The end-to-end tests drive a real FastMCP server through the real posthog.mcp
pipeline with only `capture` intercepted, so a change in the SDK's event shape
that would bypass `scrub_posthog_mcp_event` fails here instead of leaking.
"""

import asyncio
import logging
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock, patch

import httpx2
import pytest
from fastmcp import Client, Context, FastMCP
from fastmcp.exceptions import ToolError
from posthog import Posthog
from posthog.mcp import PostHogMCP
from posthog.mcp.session import derive_session_id_from_mcp_session
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route

from rootly_mcp_server.__main__ import main
from rootly_mcp_server.exceptions import GuidedToolError
from rootly_mcp_server.posthog_analytics import (
    CallableMiddlewareAdapter,
    build_posthog_client,
    identify,
    maybe_enable_posthog_mcp_analytics,
    new_posthog_client,
    posthog_session_middleware,
    prepare_posthog_mcp_event,
)
from rootly_mcp_server.server import (
    ArgumentNormalizationMiddleware,
    CamelCaseAliasMiddleware,
    LegacyContextArgumentMiddleware,
    ToolUsageLoggingMiddleware,
)

# Fake credentials, split so secret scanners do not flag the fixtures.
# The bearer value is one the SDK's own sanitizer lets through, so only our
# hook removes it.
FAKE_BEARER = "abcdefghijklm" + "nopqrstuvwxyz123456"
FAKE_STRIPE = "sk_" + "live_abcdefghijklmnopqrstuvwxyz"


def test_build_posthog_client_is_noop_without_token():
    with patch.dict("os.environ", {}, clear=True):
        assert build_posthog_client(Mock()) is None


def test_build_posthog_client_uses_env_host():
    environ = {"POSTHOG_PROJECT_TOKEN": "phc_test", "POSTHOG_HOST": "https://eu.i.posthog.com"}
    with patch.dict("os.environ", environ, clear=True):
        client = build_posthog_client(Mock())
    assert isinstance(client, PostHogMCP)
    assert client.host == "https://eu.i.posthog.com"


def test_build_posthog_client_logs_that_analytics_is_on():
    logger = Mock()
    with patch.dict("os.environ", {"POSTHOG_PROJECT_TOKEN": "phc_test"}, clear=True):
        assert isinstance(build_posthog_client(logger), PostHogMCP)
    logger.info.assert_called_once_with("PostHog MCP analytics on")


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
    assert options.before_send is prepare_posthog_mcp_event


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


@pytest.mark.parametrize(
    ("posthog_token", "expect_posthog"),
    [("", False), ("phc_test", True)],
    ids=["no-token", "token"],
)
def test_the_posthog_token_alone_turns_analytics_on(posthog_token, expect_posthog):
    server = SimpleNamespace(run=Mock())
    environ = {"ROOTLY_API_TOKEN": "x" * 40, "POSTHOG_PROJECT_TOKEN": posthog_token}

    with patch.dict("os.environ", environ, clear=True):
        with patch("rootly_mcp_server.__main__.parse_args", return_value=stdio_args()):
            with patch("rootly_mcp_server.__main__.setup_logging"):
                with patch(
                    "rootly_mcp_server.__main__.create_rootly_mcp_server",
                    return_value=server,
                ):
                    with patch("rootly_mcp_server.posthog_analytics.PostHogMCP"):
                        with patch(
                            "rootly_mcp_server.__main__.maybe_enable_posthog_mcp_analytics"
                        ) as enable:
                            main()

    posthog_client = enable.call_args.args[1]
    assert (posthog_client is not None) is expect_posthog


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

    assert list(tools[0].input_schema["properties"]) == ["note"]


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
        ToolUsageLoggingMiddleware(),
    )

    tools, events = await list_tools_and_capture(server)

    assert tools == ["ping"]
    assert "$mcp_tools_list" in events


class DuckTypedMiddleware:
    """A plain callable middleware, not a fastmcp Middleware subclass."""

    def __init__(self) -> None:
        self.seen: list[str] = []

    async def __call__(self, context, call_next):
        self.seen.append(context.method)
        return await call_next(context)


async def test_callable_middleware_is_adapted_and_keeps_running():
    duck = DuckTypedMiddleware()
    server = server_with(duck)
    position = next(i for i, m in enumerate(server.middleware) if m is duck)

    tools, events = await list_tools_and_capture(server)

    # ToolExceptionCaptureMiddleware goes first; everything else keeps its order.
    adapted = server.middleware[position + 1]
    assert isinstance(adapted, CallableMiddlewareAdapter)
    assert adapted.inner is duck
    assert tools == ["ping"]
    assert "tools/list" in duck.seen
    assert "$mcp_tools_list" in events


def test_session_middleware_only_with_a_client():
    assert posthog_session_middleware(None) == []
    assert len(posthog_session_middleware(Mock())) == 1


async def test_session_middleware_mints_and_keeps_the_session_header():
    async def mcp(_request):
        return JSONResponse({"jsonrpc": "2.0", "id": 1, "result": {}})

    app = Starlette(
        routes=[Route("/mcp", mcp, methods=["POST"])],
        middleware=posthog_session_middleware(Mock()),
    )
    initialize = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "claude-code", "version": "2.0.0"},
        },
    }
    async with httpx2.AsyncClient(
        transport=httpx2.ASGITransport(app=app), base_url="http://test"
    ) as client:
        minted = (await client.post("/mcp", json=initialize)).headers.get("mcp-session-id")
        replayed = await client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
            headers={"Mcp-Session-Id": minted or ""},
        )

    assert minted, "initialize did not mint a session id"
    assert replayed.headers.get("mcp-session-id") in (None, minted)


HOSTED_USER = {
    "id": "4242",
    "email": "jane@example.com",
    "name": "Jane Doe",
    "organization_id": "2837",
}


def hour_session_ids(user_id: str, *moments: datetime) -> set[str]:
    return {
        derive_session_id_from_mcp_session(f"user:{user_id}:hour:{moment:%Y%m%d%H}")
        for moment in moments
    }


async def capture_final_events(
    server: FastMCP, calls: list[tuple[str, dict[str, Any]]], user: dict[str, str] | None
) -> list[dict[str, Any]]:
    """Call tools through the real pipeline; return every event as it would be sent."""
    client = new_posthog_client("phc_test", send=False)
    hook = client.before_send
    captured: list[dict[str, Any]] = []

    def record(message: dict[str, Any]) -> dict[str, Any] | None:
        kept = hook(message)
        if kept:
            captured.append(kept)
        return kept

    client.before_send = record
    with (
        patch(
            "rootly_mcp_server.posthog_analytics.get_hosted_authenticated_user", return_value=user
        ),
        patch(
            "rootly_mcp_server.posthog_sessions.get_hosted_authenticated_user", return_value=user
        ),
    ):
        maybe_enable_posthog_mcp_analytics(server, client, logging.getLogger(__name__))
        async with Client(server) as mcp_client:
            for name, arguments in calls:
                await mcp_client.call_tool(name, arguments, raise_on_error=False)
        await asyncio.sleep(0.2)
        client.flush()
    return captured


def tool_calls(events: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {
        event["properties"]["$mcp_tool_name"]: event["properties"]
        for event in events
        if event["event"] == "$mcp_tool_call"
    }


def test_identify_sends_email_name_and_organization_group():
    with patch(
        "rootly_mcp_server.posthog_analytics.get_hosted_authenticated_user",
        return_value=HOSTED_USER,
    ):
        identity = identify(None, None)

    assert identity is not None
    assert identity.distinct_id == "4242"
    assert identity.properties == {"email": "jane@example.com", "name": "Jane Doe"}
    assert identity.groups == {"organization": "2837"}


def test_identify_leaves_out_what_the_user_lacks():
    with patch(
        "rootly_mcp_server.posthog_analytics.get_hosted_authenticated_user",
        return_value={"id": "4242"},
    ):
        identity = identify(None, None)

    assert identity is not None
    assert identity.properties is None
    assert identity.groups is None


async def test_every_event_uses_the_callers_session():
    before = datetime.now(UTC)
    events = await capture_final_events(server_with(), [("ping", {}), ("ping", {})], HOSTED_USER)
    after = datetime.now(UTC)

    session_ids = {event["properties"]["$session_id"] for event in events}
    assert len(session_ids) == 1
    assert session_ids <= hour_session_ids("4242", before, after)


def nesting_server() -> FastMCP:
    server = FastMCP("Rootly Code Mode")

    @server.tool
    async def execute(code: str, ctx: Context) -> str:
        result = await ctx.fastmcp.call_tool("lookup", {"q": code, "api_token": "hunter2"})
        try:
            await ctx.fastmcp.call_tool("lookup_guided", {})
        except ToolError:
            pass
        return str(result.content[0].text)

    @server.tool
    def lookup(q: str, api_token: str) -> str:
        return f"found {q} Bearer {FAKE_BEARER}"

    @server.tool
    def lookup_guided() -> str:
        raise GuidedToolError("pass q")

    return server


async def test_calls_nested_in_execute_are_captured():
    before = datetime.now(UTC)
    events = await capture_final_events(
        nesting_server(), [("execute", {"code": "incidents"})], HOSTED_USER
    )
    after = datetime.now(UTC)

    calls = tool_calls(events)
    assert set(calls) == {"execute", "lookup", "lookup_guided"}
    nested = calls["lookup"]
    assert nested["rootly_parent_tool"] == "execute"
    assert nested["$mcp_server_name"] == "Rootly Code Mode"
    assert nested["$mcp_is_error"] is False
    assert nested["$groups"] == {"organization": "2837"}
    assert {nested["$session_id"], calls["execute"]["$session_id"]} <= hour_session_ids(
        "4242", before, after
    )
    assert nested["$session_id"] == calls["execute"]["$session_id"]
    guided = calls["lookup_guided"]
    assert guided["$mcp_is_error"] is True
    assert guided["mcp_error_expected"] is True
    assert guided["mcp_error_expected_reason"] == "guided"
    assert guided["$mcp_error_type"] == "GuidedToolError"
    assert "rootly_parent_tool" not in calls["execute"]
    nested_distinct_ids = {
        event["distinct_id"] for event in events if "rootly_parent_tool" in event["properties"]
    }
    assert nested_distinct_ids == {"4242"}
    assert not [event for event in events if event["event"] == "$exception"]
    for secret in ("hunter2", FAKE_BEARER):
        assert secret not in repr(events)


async def test_nested_calls_are_skipped_without_a_hosted_user():
    events = await capture_final_events(
        nesting_server(), [("execute", {"code": "incidents"})], None
    )

    assert set(tool_calls(events)) == {"execute"}
