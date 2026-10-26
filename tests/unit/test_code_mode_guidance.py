"""Code Mode guidance and error legibility, checked against the real server.

These drive the Code Mode server built from the bundled swagger, the real
Monty sandbox and the real tool schemas, so the description, the call errors
and the hints are checked against exactly what a model sees in production.
Argument validation fails before any HTTP request, so no Rootly API is needed.
"""

import ast
import re
from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock

import pytest
from fastmcp import Client, FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.tools.function_tool import FunctionTool

from rootly_mcp_server.code_mode import (
    EXECUTE_DESCRIPTION,
    _format_execute_exception,
    build_code_mode_transform,
    create_rootly_codemode_server,
    execute_code_for_call_shape,
)

SWAGGER_PATH = "src/rootly_mcp_server/data/swagger.json"


@pytest.fixture(scope="module")
def server() -> FastMCP:
    return create_rootly_codemode_server(swagger_path=SWAGGER_PATH, hosted=True)


@pytest.fixture
async def client(server: FastMCP) -> AsyncIterator[Client]:
    async with Client(server) as connected:
        yield connected


async def _execute(client: Client, arguments: dict[str, Any]) -> tuple[bool, str]:
    result = await client.call_tool("execute", arguments, raise_on_error=False)
    text = "\n".join(getattr(item, "text", "") for item in result.content)
    return result.is_error, text


async def _run(client: Client, code: str) -> str:
    is_error, text = await _execute(client, {"code": code})
    assert is_error, text
    return text


# ---------------------------------------------------------------------------
# A/B: the execute description names only real parameters
# ---------------------------------------------------------------------------

# Every (tool, parameter) pair the description names in prose. Kept explicit
# so a parameter without an underscore (teams, status, query) is checked too;
# the test below also asserts each one is actually mentioned.
_DESCRIBED_PARAMETERS: dict[str, set[str]] = {
    "list_incidents": {
        "teams",
        "team_ids",
        "service_ids",
        "severity",
        "status",
        "started_after",
        "started_before",
        "page_size",
        "page_number",
    },
    "collect_incidents": {"max_results"},
    "search_incidents": {"query", "page_size", "max_results"},
}
_CALL_EXAMPLE = re.compile(r"call_tool\('(\w+)', (\{[^{}]*\})\)")
_SNAKE_CASE_NAME = re.compile(r"\b[a-z][a-z0-9]*(?:_[a-z0-9]+)+\b")
# `mcp__rootly-codemode__tool_search` / `rootly:get_current_user` are named as
# client prefixes to avoid, not as tools to call.
_CLIENT_PREFIX_EXAMPLE = re.compile(r"(?:mcp__|rootly:)\S+")
_NON_PARAMETER_NAMES = {"call_tool"}


async def _catalog(server: FastMCP, description: str) -> dict[str, set[str]]:
    """Parameter names of every tool the description could be naming."""
    candidates = {
        *_SNAKE_CASE_NAME.findall(description),
        *_DESCRIBED_PARAMETERS,
        *(tool for tool, _ in _CALL_EXAMPLE.findall(description)),
    }
    catalog: dict[str, set[str]] = {}
    for name in sorted(candidates):
        tool = await server.get_tool(name)
        if tool is not None:
            catalog[name] = set((tool.parameters or {}).get("properties") or {})
    return catalog


def _description_problems(description: str, catalog: dict[str, set[str]]) -> list[str]:
    """Every parameter the description names that its tool does not take."""
    problems: list[str] = []

    for tool, params in _CALL_EXAMPLE.findall(description):
        for name in ast.literal_eval(params):
            if name not in catalog.get(tool, set()):
                problems.append(f"{tool}.{name} (call_tool example)")

    # Each clause that names a tool ("Use list_incidents for ... filters like
    # teams, team_ids, ...") may only name that tool's parameters.
    text = _CLIENT_PREFIX_EXAMPLE.sub("", description)
    for clause in re.split(r"; |\. ", text):
        names = _SNAKE_CASE_NAME.findall(clause)
        subject = next((name for name in names if name in catalog), None)
        if subject is None:
            continue
        for name in names:
            if name in catalog or name in _NON_PARAMETER_NAMES:
                continue
            if name not in catalog[subject]:
                problems.append(f"{subject}.{name}")

    for tool, params in _DESCRIBED_PARAMETERS.items():
        problems.extend(
            f"{tool}.{name} (listed)" for name in params if name not in catalog.get(tool, set())
        )
    return problems


async def test_execute_description_names_only_real_tool_parameters(server: FastMCP):
    description = build_code_mode_transform().execute_description
    assert description == EXECUTE_DESCRIPTION

    catalog = await _catalog(server, description)
    assert {"list_incidents", "collect_incidents", "search_incidents"} <= set(catalog)
    assert _CALL_EXAMPLE.search(description), "the description should carry an example"
    assert _description_problems(description, catalog) == []

    words = set(re.findall(r"\w+", description))
    for tool, params in _DESCRIBED_PARAMETERS.items():
        assert tool in words
        assert params <= words, f"{tool}: listed but not described: {params - words}"


async def test_description_check_catches_invented_parameters(server: FastMCP):
    # The pre-fix wording, which sent ~1,270 list_incidents calls to Sentry.
    previous = (
        "Use list_incidents for structured incident queries with filters like teams, "
        "team_ids, start_time, end_time, severity, or status; use search_incidents for "
        "lightweight free-text lookups. Example: await call_tool('list_incidents', "
        "{'max_results': 1})."
    )
    catalog = await _catalog(server, previous + " " + EXECUTE_DESCRIPTION)

    problems = _description_problems(previous, catalog)

    assert "list_incidents.start_time" in problems
    assert "list_incidents.end_time" in problems
    assert "list_incidents.max_results (call_tool example)" in problems


# ---------------------------------------------------------------------------
# C: legible call_tool errors
# ---------------------------------------------------------------------------


async def test_unexpected_argument_names_the_real_parameter(client: Client):
    # start_time/end_time are rewritten to started_after/started_before before
    # validation (ToolArgumentAliasMiddleware); since/until are only suggested.
    text = await _run(
        client,
        "return await call_tool('list_incidents', {'since': '2026-01-01', 'until': '2026-02-01'})",
    )

    assert "call_tool('list_incidents') failed:" in text
    assert "'since' (did you mean 'started_after'?)" in text
    assert "'until' (did you mean 'started_before'?)" in text
    assert "Valid parameters for list_incidents (* = required): query, teams," in text
    assert "started_after, started_before" in text
    # Listed once, and pydantic's own noise stays out of the model's context.
    assert text.count("Valid parameters for") == 1
    assert "errors.pydantic.dev" not in text


async def test_did_you_mean_follows_each_tools_own_schema(client: Client):
    text = await _run(
        client,
        "return await call_tool('get_schedule_shifts', {'id': 's1', 'since': 'x'})",
    )

    assert "'since' (did you mean 'start_date'?)" in text
    assert "Valid parameters for get_schedule_shifts (* = required): id*, start_date" in text


async def test_did_you_mean_never_swaps_in_a_different_filter(client: Client):
    text = await _run(
        client,
        "return await call_tool('list_incidents', {'updated_after': 'x', 'include': 'y'})",
    )

    assert "'updated_after' (there is no updated-time filter)" in text
    assert "'include' (related records cannot be side-loaded" in text
    assert "did you mean" not in text
    assert "Valid parameters for list_incidents" in text


async def test_literal_error_keeps_the_allowed_values(client: Client):
    text = await _run(client, "return await call_tool('list_incidents', {'sort': 'newest'})")

    assert "sort: Input should be 'created_at', '-created_at', 'updated_at'" in text
    assert "'resolved_at' or '-resolved_at'" in text
    assert "Valid parameters for list_incidents" in text


async def test_missing_required_argument_marks_required_parameters(client: Client):
    text = await _run(client, "return await call_tool('get_incident', {})")

    assert "call_tool('get_incident') failed: incident_id: Missing required argument" in text
    assert "Valid parameters for get_incident (* = required): incident_id*" in text


async def test_call_error_speaks_in_the_name_the_caller_used(client: Client):
    text = await _run(client, "return await call_tool('rootly:getIncident', {})")

    assert "call_tool('rootly:getIncident') failed:" in text
    assert "Valid parameters for rootly:getIncident" in text


@pytest.mark.asyncio
async def test_non_validation_tool_error_is_prefixed_without_a_parameter_list():
    transform = build_code_mode_transform()
    execute_tool = cast(FunctionTool, transform._get_execute_tool())  # noqa: SLF001
    backend = SimpleNamespace(
        name="get_incident",
        version=None,
        meta=None,
        parameters={"properties": {"incident_id": {}}, "required": ["incident_id"]},
    )
    fake_fastmcp = SimpleNamespace(
        list_tools=AsyncMock(return_value=[backend]),
        call_tool=AsyncMock(side_effect=ToolError("upstream returned 503")),
    )

    with pytest.raises(Exception) as excinfo:
        await execute_tool.fn(
            "return await call_tool('get_incident', {'incident_id': '1'})",
            ctx=SimpleNamespace(fastmcp=fake_fastmcp),
        )

    message = str(excinfo.value)
    assert "call_tool('get_incident') failed: upstream returned 503" in message
    assert "Valid parameters" not in message


# ---------------------------------------------------------------------------
# D: execute called like call_tool
# ---------------------------------------------------------------------------


async def test_execute_schema_still_takes_only_code(server: FastMCP):
    tools = {tool.name: tool for tool in await server.list_tools()}
    schema = tools["execute"].parameters
    assert schema["required"] == ["code"]
    assert set(schema["properties"]) == {"code"}


@pytest.mark.parametrize(
    "arguments",
    [
        {"tool": "get_server_version", "arguments": {}},
        {"tool_name": "get_server_version", "params": {}},
        {"tool": "get_server_version", "params": "{}"},
        {"name": "mcp__rootly__get_server_version", "args": None},
        {"tool": "getServerVersion"},
    ],
    ids=["tool+arguments", "tool_name+params", "json_string", "prefixed_name", "no_arguments"],
)
async def test_call_shaped_execute_runs_through_call_tool(
    client: Client, arguments: dict[str, Any]
):
    is_error, text = await _execute(client, arguments)

    assert not is_error, text
    assert '"package":"rootly-mcp-server"' in text.replace(" ", "")


async def test_call_shaped_execute_gets_the_same_legible_errors(client: Client):
    is_error, text = await _execute(
        client, {"tool": "list_incidents", "arguments": {"since": "2026-01-01"}}
    )

    assert is_error
    assert "'since' (did you mean 'started_after'?)" in text


async def test_call_shaped_execute_cannot_reach_unknown_tools(client: Client):
    is_error, text = await _execute(client, {"tool": "drop_everything", "arguments": {}})

    assert is_error
    assert "Unknown tool: drop_everything" in text


@pytest.mark.parametrize(
    ("arguments", "expected"),
    [
        ({"tags": ["incidents"]}, "execute takes one argument, `code`"),
        ({"tool": "get_incident", "params": "not json"}, "could not read the arguments"),
        ({"tool": "get_incident", "params": [1]}, "could not read the arguments"),
        (
            {"tool": "get_incident", "arguments": {}, "incident_id": "1"},
            "got both 'arguments' and other arguments",
        ),
    ],
)
async def test_untranslatable_execute_requests_explain_the_code_shape(
    client: Client, arguments: dict[str, Any], expected: str
):
    is_error, text = await _execute(client, arguments)

    assert is_error
    assert expected in text
    assert "code" in text


async def test_flat_call_shape_uses_the_remaining_keys_as_params():
    assert execute_code_for_call_shape({"tool": "list_incidents", "page_size": 5}) == (
        "return await call_tool('list_incidents', {'page_size': 5})"
    )


@pytest.mark.asyncio
async def test_call_shape_values_stay_data_in_the_sandbox():
    # A hostile string must arrive as the same string, never run as code.
    hostile = "'); raise Exception('pwned') #\n\"\"\"\\"
    params = {"query": hostile, "nested": {"list": [1, 2.5, True, None]}}
    code = execute_code_for_call_shape({"tool": "search_incidents", "arguments": params})
    seen: list[tuple[str, Any]] = []

    async def call_tool(name: str, received: dict[str, Any]) -> str:
        seen.append((name, received))
        return "ok"

    provider = build_code_mode_transform().sandbox_provider
    assert await provider.run(code, external_functions={"call_tool": call_tool}) == "ok"
    assert seen == [("search_incidents", params)]


# ---------------------------------------------------------------------------
# E: hints for unsupported Python and result-shape guesses (real Monty)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        ("return datetime.now()", "Add `import datetime` first"),
        ("return dir()", "Introspection builtins"),
        ("return globals()", "Use tool_search / get_schema"),
        ("items = [1, 2]\nreturn next(items)", "`next(iter(items), None)`"),
        ("return sorted([{'a': 1}, {'a': 0}])", "key=lambda item"),
        ("r = {'data': []}\nreturn r['result']", "no 'result' wrapper"),
        ("r = 'text'\nreturn r.get('data')", "no 'result' wrapper"),
        ("r = 'text'\nreturn r['data']", "no 'result' wrapper"),
        ("r = {'data': []}\nreturn r.result", "no 'result' wrapper"),
        ("r = None\nreturn r['data']", "no 'result' wrapper"),
        # Syntax errors name their line and show it (ROOTLY-MCP-2GJ).
        ("a = 1\nb = 2 c = 3\nreturn a", "SyntaxError on line 2"),
        ("a = 1\nb = 2 c = 3\nreturn a", "    b = 2 c = 3"),
        ("const x = 1;\nreturn x", "not JavaScript or JSON"),
        ("a = 1\\nreturn a", "literal backslash-n"),
        ("return {'ok': true}", "write True, not the JSON/JavaScript literal true"),
        ("return [null]", "write None"),
        ("import requests\nreturn 1", "no third-party packages, network or filesystem"),
    ],
)
async def test_sandbox_errors_carry_an_actionable_hint(client: Client, code: str, expected: str):
    text = await _run(client, code)

    assert expected in text


@pytest.mark.parametrize(
    "code",
    [
        "a = {'x': 1}\nb = {'y': 2}\nreturn a | b",
        "import datetime\nreturn datetime.datetime.now(datetime.timezone.utc).isoformat()",
        "import datetime\nreturn datetime.date.today().isoformat()",
        "import json\nreturn json.loads('{\"a\": 1}')",
    ],
)
async def test_monty_supports_what_the_guidance_recommends(client: Client, code: str):
    # Monty 1.1+ (fastmcp 4.1) added dict `|`, a clock and json; the execute
    # description and hints point at them, so they must work.
    is_error, text = await _execute(client, {"code": code})

    assert not is_error, text


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        ("return open('/etc/passwd').read()", "PermissionError"),
        ("import os\nreturn os.listdir('/')", "PermissionError"),
        ("import os\nreturn os.path.exists('/etc/passwd')", "PermissionError"),
        ("from pathlib import Path\nreturn Path('/etc/hosts').read_text()", "PermissionError"),
        ("import os\nreturn os.getenv('ROOTLY_API_TOKEN')", "not supported"),
        ("import os\nreturn dict(os.environ)", "not supported"),
        ("import subprocess\nreturn 1", "No module named 'subprocess'"),
    ],
)
async def test_sandbox_has_no_host_filesystem_or_environment(
    client: Client, code: str, expected: str
):
    # Monty 1.2 added os.path, pathlib scans and open() options; with no OS
    # access granted they must all still be denied.
    is_error, text = await _execute(client, {"code": code})

    assert is_error
    assert expected in text


@pytest.mark.parametrize(
    "message",
    [
        # Shapes observed in Sentry from earlier Monty releases.
        "TypeError: '<' not supported between instances of 'list' and 'list'",
        "KeyError: value",
        "TypeError: list indices must be integers or slices, not str",
    ],
)
def test_format_execute_exception_recognizes_sentry_shapes(message: str):
    hint = _format_execute_exception(Exception(message))

    assert hint is not None
    assert hint.startswith(message)
    assert len(hint) > len(message)


def test_format_execute_exception_leaves_call_tool_failures_alone():
    message = (
        "Exception: call_tool('list_incidents') failed: start_time: Unexpected keyword "
        "argument (did you mean 'started_after'?)"
    )
    assert _format_execute_exception(Exception(message)) is None


# ---------------------------------------------------------------------------
# F: get_schema called with a single name
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "arguments",
    [
        {"tool_name": "get_incident"},
        {"name": "get_incident"},
        {"tool": "get_incident"},
        {"tools": "get_incident"},
        {"tool_name": ["get_incident"]},
    ],
    ids=["tool_name", "name", "tool", "tools-string", "tool_name-list"],
)
async def test_get_schema_accepts_a_single_tool_name(client: Client, arguments: dict[str, Any]):
    result = await client.call_tool("get_schema", arguments, raise_on_error=False)
    text = "\n".join(getattr(item, "text", "") for item in result.content)

    assert not result.is_error, text
    assert "get_incident" in text
    assert "incident_id" in text


async def test_get_schema_with_tools_ignores_the_alias_keys(client: Client):
    # An explicit `tools` list wins; the stray key still fails validation.
    result = await client.call_tool(
        "get_schema",
        {"tools": ["get_incident"], "tool_name": "list_incidents"},
        raise_on_error=False,
    )

    assert result.is_error


async def test_get_schema_alias_is_code_mode_only(server: FastMCP):
    from rootly_mcp_server.code_mode import GetSchemaArgumentShapeMiddleware
    from rootly_mcp_server.server import create_rootly_mcp_server

    classic = create_rootly_mcp_server(swagger_path=SWAGGER_PATH, hosted=True)

    assert any(isinstance(m, GetSchemaArgumentShapeMiddleware) for m in server.middleware)
    assert not any(isinstance(m, GetSchemaArgumentShapeMiddleware) for m in classic.middleware)
