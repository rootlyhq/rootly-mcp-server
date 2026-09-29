"""MCP protocol smoke tests against a running server (the built container in CI).

Speaks raw JSON-RPC over streamable HTTP, the same requests an MCP client and
the Datadog synthetic send. A JSON-RPC error inside an HTTP 200 fails here:
that is how the 2026-09-29 outage looked, when PostHog instrumentation broke
tools/list only while AgentCat was also enabled. CI therefore runs the
container with both telemetry layers configured, as production does.

Unlike test_essential.py these never skip on an unreachable server: once
MCP_SERVER_URL is set, a server that is down or erroring fails the run.

Environment:
    MCP_SERVER_URL          Base URL of the server; the module is skipped when unset
    ROOTLY_API_TOKEN        Bearer token; required by hosted mode
    MCP_PROTOCOL_UPSTREAM   "0" skips the call that needs a real Rootly API token
"""

import itertools
import json
import os
from typing import Any

import httpx
import pytest

pytestmark = [
    pytest.mark.remote,
    pytest.mark.integration,
    pytest.mark.skipif(
        not os.getenv("MCP_SERVER_URL"), reason="MCP_SERVER_URL not set; no server to test"
    ),
]

_ids = itertools.count(1)


def _endpoint() -> str:
    return os.environ["MCP_SERVER_URL"].rstrip("/") + "/mcp"


def _headers() -> dict[str, str]:
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    token = os.getenv("ROOTLY_API_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _decode(response: httpx.Response) -> dict[str, Any]:
    """Return the JSON-RPC message from a JSON or single-event SSE response."""
    if response.headers.get("content-type", "").startswith("text/event-stream"):
        for line in response.text.splitlines():
            if line.startswith("data:"):
                return json.loads(line.removeprefix("data:").strip())
        raise AssertionError(f"SSE response carried no data line: {response.text[:500]}")
    return response.json()


def rpc(method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    """Send one JSON-RPC request and return its `result`, failing on any error."""
    request_id = next(_ids)
    payload: dict[str, Any] = {"jsonrpc": "2.0", "id": request_id, "method": method}
    if params is not None:
        payload["params"] = params
    response = httpx.post(_endpoint(), json=payload, headers=_headers(), timeout=60.0)
    assert response.status_code == 200, (
        f"{method}: HTTP {response.status_code} {response.text[:500]}"
    )
    message = _decode(response)
    assert message.get("id") == request_id, f"{method}: mismatched id in {message}"
    assert "error" not in message, f"{method}: JSON-RPC error {message['error']}"
    return message["result"]


def test_initialize():
    result = rpc(
        "initialize",
        {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "rootly-mcp-protocol-test", "version": "1.0"},
        },
    )
    assert result["protocolVersion"]
    assert "tools" in result["capabilities"]


def test_tools_list_advertises_core_tools():
    names = {tool["name"] for tool in rpc("tools/list")["tools"]}
    assert len(names) >= 20, f"expected at least 20 tools, got {len(names)}"
    for expected in ("get_current_user", "search_incidents", "list_endpoints"):
        assert expected in names, f"{expected} missing from tools/list"


def test_tools_call_local_tool():
    """A tool that never reaches the Rootly API, so it exercises only the server."""
    result = rpc("tools/call", {"name": "list_endpoints", "arguments": {}})
    assert result["isError"] is False, result


@pytest.mark.skipif(
    os.getenv("MCP_PROTOCOL_UPSTREAM", "1") == "0",
    reason="MCP_PROTOCOL_UPSTREAM=0: no real Rootly API token",
)
def test_tools_call_get_current_user():
    result = rpc("tools/call", {"name": "get_current_user", "arguments": {}})
    assert result["isError"] is False, result
