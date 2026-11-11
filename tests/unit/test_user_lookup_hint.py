"""A get_user 404 says where real user ids come from.

get_user was the most-retried 404 in MCP telemetry: agents looped on an id that
is not a user the token can see, and the upstream body says only "Not found or
unauthorized".
"""

from __future__ import annotations

import json
from typing import Any

import httpx2
import pytest
from fastmcp import Client

from rootly_mcp_server import transport
from rootly_mcp_server.server import _provider_tool_inventory, create_rootly_mcp_server

pytestmark = pytest.mark.unit

NOT_FOUND = {"errors": [{"title": "Not found or unauthorized", "status": "404"}]}


def _annotate(method: str, path: str, status: int = 404) -> dict[str, Any]:
    url = "https://api.rootly.com" + path
    response = httpx2.Response(
        status, content=json.dumps(NOT_FOUND).encode(), request=httpx2.Request(method, url)
    )
    annotated = transport.AuthenticatedHTTPXClient._maybe_annotate_user_lookup_404(
        method, url, response
    )
    return annotated.json()


@pytest.mark.parametrize(
    "path", ["/v1/users/123", "/v1/users/a95520fa-f45d-401b-a6d2-9098af345ca1"]
)
def test_user_lookup_404_gets_the_hint(path):
    body = _annotate("GET", path)

    assert "get_current_user" in body["_lookup_hint"]
    assert "list_users" in body["_lookup_hint"]
    assert body["errors"] == NOT_FOUND["errors"]


@pytest.mark.parametrize(
    ("method", "path", "status"),
    [
        ("GET", "/v1/users/me", 404),
        ("GET", "/v1/users", 404),
        ("GET", "/v1/users/123/email_addresses", 404),
        ("GET", "/v1/teams/123", 404),
        ("PUT", "/v1/users/123", 404),
        ("GET", "/v1/users/123", 500),
    ],
)
def test_other_responses_are_left_alone(method, path, status):
    assert "_lookup_hint" not in _annotate(method, path, status)


async def test_get_user_tool_returns_the_hint(monkeypatch):
    monkeypatch.setenv("ROOTLY_API_TOKEN", "test-token")
    server = create_rootly_mcp_server(
        swagger_path="src/rootly_mcp_server/data/swagger.json", hosted=False
    )
    _, provider, _ = _provider_tool_inventory(server)

    async def fake_send(request, **kwargs):
        return httpx2.Response(404, request=request, json=NOT_FOUND)

    next(iter(provider._tools.values()))._client.client.send = fake_send
    async with Client(server) as client:
        result = await client.call_tool("get_user", {"id": "123"}, raise_on_error=False)

    assert result.is_error
    text = " ".join(getattr(block, "text", "") for block in result.content)
    assert "HTTP error 404" in text
    assert "use get_current_user" in text
