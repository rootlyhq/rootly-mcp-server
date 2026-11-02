"""Guided error types survive the real FastMCP call path to where AgentCat captures.

AgentCat inserts its middleware at ``server.middleware[0]`` and records the
exception ``call_next`` raises: its type name, and the names along the
``__cause__``/``__context__`` chain. If FastMCP, a middleware or the Monty
sandbox replaced a guided exception with a plain one on the way out, guided
failures would open Sentry issues again. These tests run real calls through
the real servers and check what reaches that position.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastmcp import Client, FastMCP
from fastmcp.server.middleware import Middleware

from rootly_mcp_server.code_mode import create_rootly_codemode_server
from rootly_mcp_server.server import create_rootly_mcp_server
from rootly_mcp_server.telemetry_scrubber import is_guided_error, mark_expected_tool_errors

SWAGGER_PATH = "src/rootly_mcp_server/data/swagger.json"


class _CaptureLikeAgentCat(Middleware):
    """Records the exception raised past middleware[0], as AgentCat's adapter does."""

    def __init__(self) -> None:
        self.raised: list[BaseException] = []

    async def on_call_tool(self, context: Any, call_next: Any) -> Any:
        try:
            return await call_next(context)
        except Exception as exc:
            self.raised.append(exc)
            raise


def _payload(exc: BaseException) -> dict[str, Any]:
    """The type fields of agentcat's capture_exception for ``exc``.

    agentcat records ``type(exc).__name__`` and walks ``__cause__`` when
    ``__suppress_context__`` is set, ``__context__`` otherwise.
    """
    chain: list[dict[str, Any]] = []
    current: BaseException | None = exc
    while current is not None:
        nxt = current.__cause__ if current.__suppress_context__ else current.__context__
        if nxt is None:
            break
        chain.append({"message": str(nxt), "type": type(nxt).__name__})
        current = nxt
    return {"message": str(exc), "type": type(exc).__name__, "chained_errors": chain}


async def _raised_by(server: FastMCP, tool: str, arguments: dict[str, Any]) -> BaseException:
    capture = _CaptureLikeAgentCat()
    server.middleware.insert(0, capture)
    try:
        async with Client(server) as client:
            result = await client.call_tool(tool, arguments, raise_on_error=False)
    finally:
        server.middleware.remove(capture)
    assert result.is_error, result
    assert len(capture.raised) == 1, capture.raised
    return capture.raised[0]


def _published_as_error(exc: BaseException) -> bool:
    event = type("Event", (), {"is_error": True, "error": _payload(exc)})()
    return mark_expected_tool_errors(event).is_error


@pytest.fixture
def server(monkeypatch) -> FastMCP:
    monkeypatch.setenv("ROOTLY_API_TOKEN", "test-token")
    return create_rootly_mcp_server(hosted=False)


@pytest.fixture
def codemode_server() -> FastMCP:
    return create_rootly_codemode_server(swagger_path=SWAGGER_PATH, hosted=True)


async def test_unknown_argument_reaches_capture_as_guided(server):
    exc = await _raised_by(server, "list_incidents", {"filter_title_cont": "db"})
    assert is_guided_error(_payload(exc)), _payload(exc)
    assert not _published_as_error(exc)


async def test_placeholder_argument_reaches_capture_as_guided(server):
    exc = await _raised_by(server, "get_incident", {"incident_id": "null"})
    assert is_guided_error(_payload(exc)), _payload(exc)
    assert not _published_as_error(exc)


async def test_sandbox_module_error_reaches_capture_as_guided(codemode_server):
    exc = await _raised_by(codemode_server, "execute", {"code": "import zoneinfo\nreturn 1"})
    assert "Code Mode runs in a sandbox" in str(exc)
    assert is_guided_error(_payload(exc)), _payload(exc)
    assert not _published_as_error(exc)


async def test_unexplained_script_error_still_reaches_capture_as_error(codemode_server):
    exc = await _raised_by(codemode_server, "execute", {"code": "return 1 / 0"})
    assert not is_guided_error(_payload(exc)), _payload(exc)
    assert _published_as_error(exc)
