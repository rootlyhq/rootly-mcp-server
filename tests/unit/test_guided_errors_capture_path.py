"""Guided error types survive the real FastMCP call path to where telemetry captures.

PostHog's ToolExceptionCaptureMiddleware sits at ``server.middleware[0]`` and
records the exception ``call_next`` raises; ``is_guided_exception`` then walks
its ``__cause__``/``__context__`` chain. If FastMCP, a middleware or the Monty
sandbox replaced a guided exception with a plain one on the way out, guided
failures would open Error Tracking issues. These tests run real calls through
the real servers and check what reaches that position.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastmcp import Client, FastMCP
from fastmcp.server.middleware import Middleware

from rootly_mcp_server.code_mode import create_rootly_codemode_server
from rootly_mcp_server.exceptions import is_guided_exception
from rootly_mcp_server.server import create_rootly_mcp_server

SWAGGER_PATH = "src/rootly_mcp_server/data/swagger.json"


class _CaptureAtTheTop(Middleware):
    """Records the exception raised past middleware[0], as the telemetry capture does."""

    def __init__(self) -> None:
        self.raised: list[BaseException] = []

    async def on_call_tool(self, context: Any, call_next: Any) -> Any:
        try:
            return await call_next(context)
        except Exception as exc:
            self.raised.append(exc)
            raise


async def _raised_by(server: FastMCP, tool: str, arguments: dict[str, Any]) -> BaseException:
    capture = _CaptureAtTheTop()
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
    return not is_guided_exception(exc)


@pytest.fixture
def server(monkeypatch) -> FastMCP:
    monkeypatch.setenv("ROOTLY_API_TOKEN", "test-token")
    return create_rootly_mcp_server(hosted=False)


@pytest.fixture
def codemode_server() -> FastMCP:
    return create_rootly_codemode_server(swagger_path=SWAGGER_PATH, hosted=True)


async def test_unknown_argument_reaches_capture_as_guided(server):
    exc = await _raised_by(server, "list_incidents", {"filter_title_cont": "db"})
    assert is_guided_exception(exc), exc
    assert not _published_as_error(exc)


async def test_placeholder_argument_reaches_capture_as_guided(server):
    exc = await _raised_by(server, "get_incident", {"incident_id": "null"})
    assert is_guided_exception(exc), exc
    assert not _published_as_error(exc)


async def test_sandbox_module_error_reaches_capture_as_guided(codemode_server):
    exc = await _raised_by(codemode_server, "execute", {"code": "import zoneinfo\nreturn 1"})
    assert "Code Mode runs in a sandbox" in str(exc)
    assert is_guided_exception(exc), exc
    assert not _published_as_error(exc)


async def test_unexplained_script_error_still_reaches_capture_as_error(codemode_server):
    exc = await _raised_by(codemode_server, "execute", {"code": "return 1 / 0"})
    assert not is_guided_exception(exc), exc
    assert _published_as_error(exc)


# Monty re-raises a host function's exception as MontyRuntimeError, dropping its
# type, so execute() re-tags a script that dies of a rejection it explained.


async def _execute_events(server: FastMCP, code: str) -> list[BaseException]:
    capture = _CaptureAtTheTop()
    server.middleware.insert(0, capture)
    try:
        async with Client(server) as client:
            result = await client.call_tool("execute", {"code": code}, raise_on_error=False)
    finally:
        server.middleware.remove(capture)
    assert result.is_error, result
    return capture.raised


async def test_bad_bare_tool_call_reaches_capture_as_guided(codemode_server):
    (exc,) = await _execute_events(codemode_server, "return await list_incidents(1)")
    assert "takes one params dict or keyword arguments" in str(exc)
    assert is_guided_exception(exc), exc
    assert not _published_as_error(exc)


async def test_guided_call_tool_failure_marks_inner_and_outer_events(codemode_server):
    # call_tool re-enters the middleware chain, so the capture records the
    # inner call and the execute() call as separate events. Neither is a real failure.
    inner, outer = await _execute_events(
        codemode_server, "return await call_tool('list_incidents', {'bogus_arg': 1})"
    )
    assert "does not accept 'bogus_arg'" in str(outer)
    assert not _published_as_error(inner)
    assert not _published_as_error(outer)


async def test_real_failure_after_a_caught_rejection_stays_an_error(codemode_server):
    code = "try:\n    await list_incidents(1)\nexcept Exception:\n    pass\nreturn 1 / 0"
    (exc,) = await _execute_events(codemode_server, code)
    assert "division by zero" in str(exc)
    assert _published_as_error(exc)


async def test_unguided_call_tool_failure_stays_an_error(codemode_server):
    # A rejection the server did not raise as guided (here pydantic's type
    # validation) must not be re-tagged by execute().
    raised = await _execute_events(
        codemode_server, "return await call_tool('list_incidents', {'page_size': 'many'})"
    )
    assert raised
    assert not any(is_guided_exception(exc) for exc in raised), raised
    assert all(_published_as_error(exc) for exc in raised), raised
