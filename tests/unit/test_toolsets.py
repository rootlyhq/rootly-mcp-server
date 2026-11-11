"""Toolsets: every tool is placed, selection parses, and the filter holds."""

from __future__ import annotations

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError

from rootly_mcp_server import toolsets
from rootly_mcp_server.server import ToolSelectionMiddleware, create_rootly_mcp_server
from rootly_mcp_server.toolsets import (
    ALL,
    CORE,
    CORE_TOOLS,
    TOOLSETS,
    ToolSelection,
    parse_toolsets,
    selection_from_request,
)


@pytest.fixture
def token(monkeypatch):
    monkeypatch.setenv("ROOTLY_API_TOKEN", "rootly_test")
    monkeypatch.delenv("ROOTLY_MCP_TOOLSETS", raising=False)
    monkeypatch.delenv("ROOTLY_MCP_ENABLED_TOOLS", raising=False)


def _selection_middleware(server) -> ToolSelectionMiddleware:
    return next(m for m in server.middleware if isinstance(m, ToolSelectionMiddleware))


async def _names(server, selection: ToolSelection | None = None) -> set[str]:
    reset = toolsets.requested_selection.set(selection)
    try:
        async with Client(server) as client:
            return {tool.name for tool in await client.list_tools()}
    finally:
        toolsets.requested_selection.reset(reset)


@pytest.mark.parametrize("hosted", [True, False])
def test_every_tool_belongs_to_a_known_toolset(token, hosted):
    # A new OpenAPI tag or curated tool must be placed in TAG_TOOLSETS /
    # CURATED_TOOLSETS, or it would only ever appear under ?toolsets=all.
    server = create_rootly_mcp_server(hosted=hosted, enable_write_tools=True)
    placed = _selection_middleware(server).tool_toolsets

    unplaced = sorted(name for name, toolset in placed.items() if toolset is None)
    assert unplaced == []
    assert set(placed.values()) <= set(TOOLSETS)


async def test_hosted_default_is_everything_until_a_client_opts_in(token):
    # Opt-in: an existing hosted config with no ?toolsets= sees every tool.
    server = create_rootly_mcp_server(hosted=True, enable_write_tools=True)

    assert len(await _names(server)) > 200
    assert await _names(server, ToolSelection(frozenset({CORE}))) == set(CORE_TOOLS)


async def test_env_default_core_narrows_the_hosted_surface(token, monkeypatch):
    monkeypatch.setenv("ROOTLY_MCP_TOOLSETS", "core")
    server = create_rootly_mcp_server(hosted=True, enable_write_tools=True)

    assert await _names(server) == set(CORE_TOOLS)


async def test_self_hosted_default_is_everything(token):
    server = create_rootly_mcp_server(hosted=False, enable_write_tools=True)

    assert len(await _names(server)) > 200


async def test_requested_toolsets_add_their_tools(token):
    server = create_rootly_mcp_server(hosted=True, enable_write_tools=True)
    placed = _selection_middleware(server).tool_toolsets

    names = await _names(server, ToolSelection(frozenset({CORE, "workflows"})))

    workflows = {name for name, toolset in placed.items() if toolset == "workflows"}
    assert workflows <= names
    assert set(CORE_TOOLS) <= names
    assert "create_alert_route" not in names


# Tools that store instructions Rootly runs later. They live in `automations`
# so a connection can leave them out while keeping `ai` and `workflows`.
_AUTOMATION_TOOLS = {
    "list_ai_automations",
    "get_ai_automation",
    "create_ai_automation",
    "update_ai_automation",
    "run_ai_automation",
    "create_workflow_task",
    "update_workflow_task",
    "get_workflow_task_params_schema",
}


async def test_every_toolset_but_automations_leaves_them_all_out(token):
    server = create_rootly_mcp_server(hosted=True, enable_write_tools=True)
    everything_else = frozenset(set(TOOLSETS) - {"automations"})

    names = await _names(server, ToolSelection(everything_else))

    assert not _AUTOMATION_TOOLS & names
    # Their neighbours stay: investigations and skills, workflows and runs,
    # and listing or reading a workflow task.
    assert {
        "start_investigation",
        "list_ai_skills",
        "create_workflow_run",
        "list_workflow_tasks",
        "get_workflow_task",
    } <= names


async def test_the_automations_toolset_brings_them_back(token):
    server = create_rootly_mcp_server(hosted=True, enable_write_tools=True)

    names = await _names(server, ToolSelection(frozenset({"automations"})))

    assert _AUTOMATION_TOOLS <= names


async def test_readonly_drops_every_write(token):
    server = create_rootly_mcp_server(hosted=True, enable_write_tools=True)

    names = await _names(server, ToolSelection(frozenset({ALL}), readonly=True))

    assert "list_incidents" in names
    assert not {"create_incident", "update_incident", "create_alert_route"} & names


async def test_readonly_alone_narrows_the_default(token):
    server = create_rootly_mcp_server(hosted=True, enable_write_tools=True)

    everything = await _names(server)
    names = await _names(server, ToolSelection(frozenset(), readonly=True))

    assert names and names < everything
    assert "list_workflows" in names
    assert not {"create_incident", "create_workflow", "update_alert_route"} & names


async def test_calling_a_tool_outside_the_selection_names_the_fix(token):
    server = create_rootly_mcp_server(hosted=True, enable_write_tools=True)
    reset = toolsets.requested_selection.set(ToolSelection(frozenset({CORE})))
    try:
        async with Client(server) as client:
            with pytest.raises(ToolError) as excinfo:
                await client.call_tool("list_workflows", {})
    finally:
        toolsets.requested_selection.reset(reset)

    message = str(excinfo.value)
    assert "'workflows' toolset" in message
    assert "?toolsets=core,workflows" in message


async def test_explicit_allowlist_ignores_toolsets_but_honours_readonly(token):
    # The slim profile and ROOTLY_MCP_ENABLED_TOOLS fix the tool list, so
    # toolsets do not narrow it; ?readonly=true must still drop its writes.
    from rootly_mcp_server.server_defaults import DEFAULT_HOSTED_ENABLED_TOOLS

    server = create_rootly_mcp_server(
        hosted=True, enable_write_tools=True, enabled_tools=set(DEFAULT_HOSTED_ENABLED_TOOLS)
    )
    everything = await _names(server)
    readonly = await _names(server, ToolSelection(frozenset(), readonly=True))
    other_toolset = await _names(server, ToolSelection(frozenset({"alerts"})))

    assert "create_workflow_task" in everything
    assert other_toolset == everything
    assert readonly and readonly < everything
    assert not {"create_workflow_task", "create_incident", "update_incident"} & readonly

    reset = toolsets.requested_selection.set(ToolSelection(frozenset(), readonly=True))
    try:
        async with Client(server) as client:
            with pytest.raises(ToolError) as excinfo:
                await client.call_tool("create_incident", {"title": "x"})
    finally:
        toolsets.requested_selection.reset(reset)
    assert "Remove readonly=true" in str(excinfo.value)
    assert "?toolsets=" not in str(excinfo.value)


def test_code_mode_lists_its_meta_tools_unfiltered(token):
    from rootly_mcp_server.code_mode import create_rootly_codemode_server

    server = create_rootly_codemode_server(hosted=True)

    [middleware] = [m for m in server.middleware if isinstance(m, ToolSelectionMiddleware)]
    assert middleware.filter_list is False
    assert middleware.toolsets_apply is False


def test_instructions_only_when_the_default_hides_tools(token, monkeypatch):
    # Default `all` hides nothing, so no instructions (no cost to anyone).
    assert create_rootly_mcp_server(hosted=True).instructions is None
    assert create_rootly_mcp_server(hosted=False).instructions is None

    monkeypatch.setenv("ROOTLY_MCP_TOOLSETS", "core")
    instructions = create_rootly_mcp_server(hosted=True).instructions
    assert instructions and "?toolsets=" in instructions
    assert "only `core` is loaded" in instructions


def test_instructions_name_the_configured_default(token, monkeypatch):
    monkeypatch.setenv("ROOTLY_MCP_TOOLSETS", "alerts,oncall")

    instructions = create_rootly_mcp_server(hosted=True).instructions

    assert instructions and "only `alerts`, `oncall` are loaded" in instructions


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("core,alerts", {"core", "alerts"}),
        (" Core , STATUS-PAGES ", {"core", "status_pages"}),
        ("all", {"all"}),
        ("alerts,nope", {"alerts"}),
        ("nope", None),
        ("", None),
        (None, None),
    ],
)
def test_parse_toolsets(raw, expected):
    parsed = parse_toolsets(raw)
    assert parsed == (frozenset(expected) if expected is not None else None)


@pytest.mark.parametrize(
    ("query", "headers", "expected"),
    [
        ({}, {}, None),
        ({"toolsets": "alerts"}, {}, ToolSelection(frozenset({"alerts"}))),
        ({}, {"x-mcp-toolsets": "oncall"}, ToolSelection(frozenset({"oncall"}))),
        (
            {"toolsets": "alerts"},
            {"x-mcp-toolsets": "oncall"},
            ToolSelection(frozenset({"alerts"})),
        ),
        ({"readonly": "true"}, {}, ToolSelection(frozenset(), readonly=True)),
        ({}, {"x-mcp-readonly": "1"}, ToolSelection(frozenset(), readonly=True)),
        # The pre-toolsets profile parameter keeps meaning everything.
        ({"tool_profile": "full"}, {}, ToolSelection(frozenset({ALL}))),
        ({}, {"x-rootly-tool-profile": "all"}, ToolSelection(frozenset({ALL}))),
    ],
)
def test_selection_from_request(query, headers, expected):
    assert selection_from_request(query, headers) == expected


def test_env_sets_the_default(monkeypatch):
    monkeypatch.setenv("ROOTLY_MCP_TOOLSETS", "alerts,oncall")
    assert toolsets.default_toolsets(hosted=True) == {"alerts", "oncall"}
    monkeypatch.setenv("ROOTLY_MCP_TOOLSETS", "bogus")
    assert toolsets.default_toolsets(hosted=True) == {ALL}
    assert toolsets.default_toolsets(hosted=False) == {ALL}


async def test_toolsets_query_param_reaches_the_filter_over_http(token):
    # End to end through the hosted app: AuthCaptureMiddleware reads the URL,
    # ToolSelectionMiddleware filters tools/list.
    import json as _json
    from unittest.mock import patch

    import httpx2 as httpx
    from starlette.middleware import Middleware

    from rootly_mcp_server.__main__ import build_dual_http_app
    from rootly_mcp_server.transport import AuthCaptureMiddleware

    async def valid(self, auth_header):
        return {"id": "1", "email": "u@example.test"}

    server = create_rootly_mcp_server(hosted=True, enable_write_tools=True)
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
    headers = {
        "Authorization": "Bearer rootly_test",
        "Accept": "application/json, text/event-stream",
        "Content-Type": "application/json",
        "MCP-Protocol-Version": "2025-06-18",
    }

    async def listed(url: str) -> set[str]:
        with patch.object(AuthCaptureMiddleware, "_validate_token_upstream", valid):
            app = build_dual_http_app(server, middleware=[Middleware(AuthCaptureMiddleware)])
            async with app.router.lifespan_context(app):
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app), base_url="http://test"
                ) as client:
                    response = await client.post(url, json=body, headers=headers)
        text = response.text
        payload = _json.loads(
            next(line[5:] for line in text.splitlines() if line.startswith("data:"))
            if text.lstrip().startswith(("event:", "data:"))
            else text
        )
        return {tool["name"] for tool in payload["result"]["tools"]}

    assert len(await listed("/mcp")) > 200
    assert await listed("/mcp?toolsets=core") == set(CORE_TOOLS)
    alerts = await listed("/mcp?toolsets=alerts")
    assert "create_alert_route" in alerts and "create_incident" not in alerts
    assert len(await listed("/mcp?tool_profile=full")) > 200


async def test_code_mode_readonly_blocks_writes_inside_execute(token):
    # /mcp-codemode?readonly=true: the five meta-tools stay listed and
    # `execute` runs, but a script that calls a write tool is refused.
    from rootly_mcp_server.code_mode import create_rootly_codemode_server

    server = create_rootly_codemode_server(hosted=True, enable_write_tools=True)
    reset = toolsets.requested_selection.set(ToolSelection(frozenset(), readonly=True))
    try:
        async with Client(server) as client:
            listed = {tool.name for tool in await client.list_tools()}
            result = await client.call_tool(
                "execute",
                {"code": "return await call_tool('create_incident', {'title': 'x'})"},
                raise_on_error=False,
            )
    finally:
        toolsets.requested_selection.reset(reset)

    assert "execute" in listed and "tool_search" in listed
    assert result.is_error
    assert "read-only" in str(result.content)


async def test_code_mode_without_readonly_reaches_every_tool(token):
    from rootly_mcp_server.code_mode import create_rootly_codemode_server

    server = create_rootly_codemode_server(hosted=True, enable_write_tools=True)
    async with Client(server) as client:
        result = await client.call_tool(
            "execute",
            {"code": "return await call_tool('list_endpoints', {})"},
            raise_on_error=False,
        )

    # list_endpoints sits in `config`, outside core: Code Mode is not narrowed.
    assert not result.is_error, result.content


@pytest.mark.parametrize(
    ("query", "headers", "expected"),
    [
        ({"tool_profile": "slim"}, {}, ("query", "slim")),
        ({}, {"x-rootly-tool-profile": "full"}, ("header", "full")),
        ({"toolsets": "alerts"}, {}, None),
    ],
)
def test_tool_profile_use_is_detected(query, headers, expected):
    assert toolsets.tool_profile_in_request(query, headers) == expected


async def test_tool_profile_request_logs_a_deprecation_event(token, monkeypatch):
    import json as _json
    from unittest.mock import patch

    from rootly_mcp_server.transport import AuthCaptureMiddleware

    async def valid(self, auth_header):
        return {"id": "1", "email": "u@example.test"}

    async def app(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"{}"})

    logged: list[str] = []
    monkeypatch.setattr(
        toolsets, "_deprecation_logger", type("L", (), {"warning": staticmethod(logged.append)})()
    )
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/mcp",
        "query_string": b"",
        "headers": [
            (b"authorization", b"Bearer rootly_test"),
            (b"x-rootly-tool-profile", b"slim"),
            (b"user-agent", b"legacy-client/1"),
        ],
    }

    async def receive():
        return {"type": "http.request"}

    async def send(_message):
        return None

    with patch.object(AuthCaptureMiddleware, "_validate_token_upstream", valid):
        await AuthCaptureMiddleware(app)(scope, receive, send)

    [line] = logged
    event = _json.loads(line)
    assert event["event"] == "tool_profile_deprecated"
    assert event["source"] == "header" and event["tool_profile"] == "slim"
    assert event["user_agent"] == "legacy-client/1"
    assert event["token_fingerprint"] and "rootly_test" not in line


@pytest.mark.parametrize(
    ("value", "readonly"),
    [
        ("true", True),
        ("1", True),
        ("YES", True),
        # Fail closed: anything that is not an explicit "off" is read-only.
        ("enabled", True),
        ("readonly", True),
        ("false", False),
        ("0", False),
        (" Off ", False),
        ("n", False),
        ("", False),
    ],
)
def test_readonly_values_fail_closed(value, readonly):
    selection = selection_from_request({"readonly": value}, {})
    assert (selection is not None and selection.readonly) is readonly
    header = selection_from_request({}, {"x-mcp-readonly": value})
    assert (header is not None and header.readonly) is readonly


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE"])
async def test_transport_refuses_writes_on_a_read_only_connection(method):
    # Defence in depth: even a tool whose read-only annotation were wrong
    # could not write on a read-only connection.
    from rootly_mcp_server.exceptions import RootlyValidationError
    from rootly_mcp_server.transport import AuthenticatedHTTPXClient

    reset = toolsets.requested_selection.set(ToolSelection(frozenset(), readonly=True))
    try:
        with pytest.raises(RootlyValidationError, match="read-only"):
            AuthenticatedHTTPXClient._refuse_writes_when_readonly(method)
        AuthenticatedHTTPXClient._refuse_writes_when_readonly("GET")
    finally:
        toolsets.requested_selection.reset(reset)
    AuthenticatedHTTPXClient._refuse_writes_when_readonly(method)
